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

import httpx

from agentygg import harnesses, store
from agentygg.api import Gateway, GatewayError
from agentygg.harnesses import TurnResult
from agentygg.render import poke_prompt, wake_prompt

__all__ = ["Relay", "TurnResult", "log"]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def instance_path(agent_id: str) -> Path:
    """Where a running relay publishes its instance nonce, so the MCP server or `ygg send`
    started by ANY harness acts under the relay's lease (some harnesses do not pass their
    environment on to MCP servers)."""
    return store.state_dir() / f"{agent_id}.instance"


class RestartRequested(Exception):
    """The relay ends so that its service starts it again (on the code now installed)."""


# The exit code for it: not 0, which every service manager reads as "stopped on purpose".
RESTART_EXIT = 75


# How many refusals (401/403) in a row stop the relay: with the backoff (2 s doubling to 60 s)
# that is several minutes of a refused sign-in, not a hiccup.
REFUSALS_BEFORE_STOP = 6
# During a turn: how often the relay tells the gateway it is still here (the lease lives 60 s).
PRESENCE_EVERY_S = 20.0
# How often an idle relay looks for local notes (`ygg relay poke`).
POKE_CHECK_S = 2.0


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
        # Set while no turn runs and nothing waits for one: only then does the relay hold the
        # stream. During a turn the stream is closed, so the next messages stay on the server:
        # a live session that opens meanwhile gets them, and a restart loses none (Claudeo,
        # 2026-11-17: a 29-minute turn held every message sent meanwhile in this process).
        self.idle = asyncio.Event()
        self.idle.set()
        self.presence_every_s = PRESENCE_EVERY_S
        self.poke_check_s = POKE_CHECK_S
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
        # A relay started from an editor's terminal inherits its entry point (claude-vscode):
        # the harness must say what this turn is itself.
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
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

    async def _keep_present(self) -> None:
        """During a turn the stream is closed (messages wait on the server): tell the gateway
        we are still here, so the agent shows online, not offline. An older server without
        /presence answers 404: then stop trying."""
        while True:
            try:
                await self.gw.presence()
            except GatewayError as e:
                if e.status == 404:
                    return
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(self.presence_every_s)

    async def _turn(self, prompt: str) -> TurnResult:
        present = asyncio.create_task(self._keep_present())
        try:
            return await self.run_turn(prompt)
        finally:
            present.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await present

    async def _poker(self) -> None:
        """Local notes (`ygg relay poke`): when no turn runs and no live session listens (that
        one delivers them itself), take them and start a turn."""
        while True:
            await asyncio.sleep(self.poke_check_s)
            if not self.idle.is_set() or self._listened():
                continue
            if not store.has_pokes(self.identity.agent_id):
                continue
            pokes = store.take_pokes(self.identity.agent_id)
            if pokes:
                self.idle.clear()
                await self.queue.put({"kind": "poke", "pokes": pokes})

    async def handle_poke(self, pokes: list[dict]) -> None:
        log(f'local note ({len(pokes)}): "{pokes[0].get("text", "")[:80]}"')
        result = await self._turn(poke_prompt(self.me, pokes))
        self.turns += 1
        log(
            f"  turn {'done' if result.ok else 'FAILED'} in {result.seconds:.0f}s"
            + (f": {result.summary}" if result.summary else "")
        )

    async def handle(self, wake: dict) -> None:
        env = wake.get("envelope") or {}
        who = (wake.get("from") or {}).get("label") or env.get("from")
        text = ((env.get("payload") or {}).get("text") or "").replace("\n", " ")
        log(f'wake: {who} ({env.get("type")}) "{text[:80]}"')
        prompt = wake_prompt(self.me, wake, self.context)
        self.context = []
        result = await self._turn(prompt)
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
                    self._refused = 0
                    log(f"connected as @{self.identity.agent_id}, waiting for messages")
                elif kind == "wake":
                    self.idle.clear()
                    await self.queue.put(frame)
                    return  # one wake at a time: the rest waits on the server
                elif kind == "context":
                    await self.queue.put(frame)

    def _listened(self) -> bool:
        return store.listener_active(self.identity.agent_id)

    async def _stream_once(self) -> bool | None:
        """One connection of the stream. Returns True if it ended because a live session
        started listening (the relay then steps aside), None if it took a wake (the stream is
        closed until that turn is over), False if the server closed it."""
        consume = asyncio.create_task(self._consume())
        try:
            while not consume.done():
                await asyncio.wait({consume}, timeout=self.listen_check_s)
                if not consume.done() and self._listened():
                    return True
                if not consume.done() and not self.idle.is_set():
                    return None  # a local note started a turn: let the server keep the rest
            consume.result()  # raises what ended it (GatewayError, connection lost...)
            return None if not self.idle.is_set() else False
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
        self._refused = 0
        while True:
            if self._listened():
                log("paused: a live session is listening for this agent (ygg listen)")
                while self._listened():
                    await asyncio.sleep(self.listen_check_s)
                log("resumed: the live session stopped listening")
                continue
            if not self.idle.is_set():
                await self.idle.wait()  # a turn is running: its end opens the stream again
                continue
            try:
                ended = await self._stream_once()
                if ended is True:
                    continue
                if ended is None:
                    await self.idle.wait()  # the turn, then back to the stream
                    continue
                log("stream closed by the server, reconnecting")
            except GatewayError as e:
                if e.status == 409:
                    log("another instance of this agent is connected; retrying in 30s")
                    await asyncio.sleep(30)
                    continue
                if e.status in (401, 403):
                    # One refusal is not a verdict: after a sleep or a lost connection the
                    # gateway refused test-agent's relay once and it stopped for good, though
                    # the sign-in was fine (2026-11-14). Sign in again and retry; stop only
                    # when the refusals last.
                    self._refused += 1
                    if self._refused >= REFUSALS_BEFORE_STOP:
                        log(f"stopped: {e.detail} ({self._refused} times in a row)")
                        raise
                    self.identity.expires_at = 0.0  # a fresh access token on the next try
                    log(f"refused ({e.detail}); signing in again, retrying in {self._backoff:.0f}s")
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, 60.0)
                    continue
                log(f"gateway error ({e.detail}); retrying in {self._backoff:.0f}s")
            except (OSError, TimeoutError) as e:
                log(f"connection lost ({e.__class__.__name__}); retrying in {self._backoff:.0f}s")
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 60.0)

    async def _worker(self) -> None:
        while True:
            frame = await self.queue.get()
            self.context.extend(frame.get("mailbox") or [])
            if frame.get("kind") in ("wake", "poke"):
                try:
                    if frame.get("kind") == "poke":
                        await self.handle_poke(frame["pokes"])
                    else:
                        await self.handle(frame)
                except Exception as e:  # noqa: BLE001 - one failed turn never stops the relay
                    log(f"  turn crashed: {e!r}")
            if self.queue.empty():
                self.idle.set()
            # A restart asked during a turn (`ygg relay restart` run by the agent itself) waits
            # for that turn, and for what is already queued: restarting at once would end the
            # turn that asked for it before it could answer (Codex, 2026-11-03).
            if self.queue.empty() and store.restart_requested(self.identity.agent_id):
                store.clear_restart(self.identity.agent_id)
                log("restarting, as asked during the last turn")
                raise RestartRequested

    async def _first_contact(self) -> dict:
        """Who we are, from the gateway. A gateway that is restarting (5xx) or out of reach is
        waited for, instead of crashing the relay into its service's restart loop; a refused
        key (401/403) still stops it at once."""
        backoff = 2.0
        while True:
            try:
                return await self.gw.me()
            except GatewayError as e:
                if e.status < 500:
                    raise
                log(f"gateway unavailable ({e.detail}); retrying in {backoff:.0f}s")
            except (httpx.HTTPError, OSError) as e:
                log(f"gateway out of reach ({e.__class__.__name__}); retrying in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def run(self) -> None:
        store.secure_tree()  # logs and state are private (they hold every message)
        log(f"harness: {self.h.describe()}")  # fails now (not on the first wake) if missing
        self.me = await self._first_contact()
        self._publish_instance()
        log(
            f'agent @{self.me["agent_id"]} in "{self.me.get("workspace_name") or self.me["workspace_id"]}", '
            f"working in {self.workdir}"
        )
        reader = asyncio.create_task(self._reader())
        worker = asyncio.create_task(self._worker())
        poker = asyncio.create_task(self._poker())
        try:
            done, _ = await asyncio.wait(
                {reader, worker, poker}, return_when=asyncio.FIRST_EXCEPTION
            )
            for t in done:
                t.result()
        finally:
            reader.cancel()
            worker.cancel()
            poker.cancel()
            self._clear_instance()
            await self.gw.release()
            await self.gw.close()
