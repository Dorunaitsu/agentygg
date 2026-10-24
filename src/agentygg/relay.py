"""The relay (`ygg agent run`): what wakes an agent whose model only runs when asked to.

It holds the agent's wake stream open (one live instance, the lease) and, for each wake the
backend grants, runs one turn of the agent's harness (Claude Code, Codex, Hermes Agent,
OpenClaw, or any command) in the agent's working directory, then waits again. Wakes are handled
one at a time, in order. Harnesses that can resume a session resume the same one from wake to
wake, so the agent remembers its earlier exchanges. The turn gets the `agentygg` MCP server,
acting under this relay's lease (see `harnesses.py`).

The relay never decides anything: the backend already judged every wake it receives, and
whatever the agent sends back is judged again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentygg import harnesses, store
from agentygg.api import Gateway, GatewayError
from agentygg.harnesses import TurnResult
from agentygg.render import wake_prompt

__all__ = ["Relay", "TurnResult", "log"]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def instance_path(agent_id: str) -> Path:
    """Where a running relay publishes its instance nonce, so the MCP server or `ygg send`
    started by ANY harness acts under the relay's lease (some harnesses do not pass their
    environment on to MCP servers)."""
    return store.state_dir() / f"{agent_id}.instance"


@dataclass
class Relay:
    identity: store.Identity
    workdir: Path
    harness: str = "claude"
    extra_args: list[str] = field(default_factory=list)
    turn_timeout_s: float = 1800.0
    harness_path: str | None = None
    # `--harness command`: the shell command to run per wake. A list runs as-is (tests).
    command: str | list[str] | None = None
    # How often to check whether a live session started listening (`ygg listen`).
    listen_check_s: float = 1.0

    def __post_init__(self) -> None:
        if isinstance(self.command, list):
            self.h: harnesses.Harness = harnesses.Command(self.command)
        else:
            kind = "command" if self.command and self.harness == "claude" else self.harness
            self.h = harnesses.build(kind, self.harness_path, self.command)
        self.gw = Gateway(self.identity)
        self.me: dict = {}
        self.context: list[dict] = []
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.turns = 0

    # -- session memory (one harness session per agent + directory) --------- #

    def _state_path(self) -> Path:
        return store.state_dir() / f"{self.identity.agent_id}.json"

    def _load_session(self) -> str | None:
        p = self._state_path()
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        same = (
            data.get("workdir") == str(self.workdir)
            and data.get("harness", "claude") == self.h.kind
        )
        return data.get("session_id") if same else None

    def _save_session(self, session_id: str) -> None:
        p = self._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"session_id": session_id, "workdir": str(self.workdir), "harness": self.h.kind}
            )
        )

    def _publish_instance(self) -> None:
        p = instance_path(self.identity.agent_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"nonce": self.gw.nonce, "pid": os.getpid()}))

    def _clear_instance(self) -> None:
        with contextlib.suppress(OSError):
            instance_path(self.identity.agent_id).unlink()

    # -- one turn ----------------------------------------------------------- #

    def _env(self, prompt: str) -> dict[str, str]:
        env = dict(os.environ)
        env["YGG_INSTANCE"] = self.gw.nonce  # the MCP server acts under OUR lease
        env["YGG_AGENT_ID"] = self.identity.agent_id
        env["YGG_PROMPT"] = prompt
        env["YGG_RELAY"] = "1"  # a relay turn, not a live session (`ygg listen` refuses)
        env.pop("YGG_AGENT_KEY", None)
        return env

    async def _exec(self, turn: harnesses.Turn, prompt: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *turn.cmd,
            cwd=str(self.workdir),
            env={**self._env(prompt), **turn.env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(turn.stdin), self.turn_timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return 124, "", f"turn timed out after {self.turn_timeout_s:.0f}s"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def _attempt(self, prompt: str, session: str | None) -> TurnResult:
        mcp = harnesses.mcp_spec(self.identity.agent_id, self.gw.nonce)
        turn = self.h.prepare(prompt, mcp, session, self.extra_args, str(self.workdir))
        try:
            code, out, err = await self._exec(turn, prompt)
        finally:
            for path in turn.cleanup:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        return self.h.parse(code, out, err)

    async def run_turn(self, prompt: str) -> TurnResult:
        started = time.monotonic()
        session = self._load_session() if self.h.resumable else None
        result = await self._attempt(prompt, session)
        if not result.ok and session:
            # The saved session may be gone (cleared, other machine): start a fresh one.
            result = await self._attempt(prompt, None)
        if result.session_id and self.h.resumable:
            self._save_session(result.session_id)
        result.seconds = time.monotonic() - started
        return result

    async def handle(self, wake: dict) -> None:
        env = wake.get("envelope") or {}
        who = (wake.get("from") or {}).get("label") or env.get("from")
        text = ((env.get("payload") or {}).get("text") or "").replace("\n", " ")
        log(f'wake: {who} ({env.get("type")}) "{text[:80]}"')
        prompt = wake_prompt(self.me, wake, self.context)
        self.context = []
        result = await self.run_turn(prompt)
        self.turns += 1
        state = "done" if result.ok else "FAILED"
        log(
            f"  turn {state} in {result.seconds:.0f}s"
            + (f": {result.summary}" if result.summary else "")
        )

    # -- the loop ----------------------------------------------------------- #

    async def _consume(self) -> None:
        async with contextlib.aclosing(self.gw.stream()) as frames:
            async for frame in frames:
                kind = frame.get("kind")
                if kind == "hello":
                    self._backoff = 2.0
                    log(f"connected as @{self.identity.agent_id}, waiting for messages")
                elif kind in ("wake", "context"):
                    await self.queue.put(frame)

    def _listened(self) -> bool:
        return store.listener_active(self.identity.agent_id)

    async def _stream_once(self) -> bool:
        """One connection of the stream. Returns True if it ended because a live session
        started listening (the relay then steps aside), False if the server closed it."""
        consume = asyncio.create_task(self._consume())
        try:
            while not consume.done():
                await asyncio.wait({consume}, timeout=self.listen_check_s)
                if not consume.done() and self._listened():
                    return True
            consume.result()  # raises what ended it (GatewayError, connection lost...)
            return False
        finally:
            if not consume.done():
                consume.cancel()  # the server puts back anything it was handing over
                with contextlib.suppress(asyncio.CancelledError):
                    await consume

    async def _reader(self) -> None:
        """Keep the stream open (reconnecting) and queue what arrives. While a live session
        listens for this agent (`ygg listen`), step aside so the two never race for a wake;
        come back once it stops listening."""
        self._backoff = 2.0
        while True:
            if self._listened():
                log("paused: a live session is listening for this agent (ygg listen)")
                while self._listened():
                    await asyncio.sleep(self.listen_check_s)
                log("resumed: the live session stopped listening")
                continue
            try:
                if await self._stream_once():
                    continue
                log("stream closed by the server, reconnecting")
            except GatewayError as e:
                if e.status == 409:
                    log("another instance of this agent is connected; retrying in 30s")
                    await asyncio.sleep(30)
                    continue
                if e.status in (401, 403):
                    log(f"stopped: {e.detail}")
                    raise
                log(f"gateway error ({e.detail}); retrying in {self._backoff:.0f}s")
            except (OSError, TimeoutError) as e:
                log(f"connection lost ({e.__class__.__name__}); retrying in {self._backoff:.0f}s")
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 60.0)

    async def _worker(self) -> None:
        while True:
            frame = await self.queue.get()
            self.context.extend(frame.get("mailbox") or [])
            if frame.get("kind") == "wake":
                try:
                    await self.handle(frame)
                except Exception as e:  # noqa: BLE001 - one failed turn never stops the relay
                    log(f"  turn crashed: {e!r}")

    async def run(self) -> None:
        log(f"harness: {self.h.describe()}")  # fails now (not on the first wake) if missing
        self.me = await self.gw.me()
        self._publish_instance()
        log(
            f'agent @{self.me["agent_id"]} in "{self.me.get("workspace_name") or self.me["workspace_id"]}", '
            f"working in {self.workdir}"
        )
        reader = asyncio.create_task(self._reader())
        worker = asyncio.create_task(self._worker())
        try:
            done, _ = await asyncio.wait({reader, worker}, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()
        finally:
            reader.cancel()
            worker.cancel()
            self._clear_instance()
            await self.gw.release()
            await self.gw.close()
