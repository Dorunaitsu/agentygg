"""The relay (`ygg agent run`): what wakes an agent whose model only runs when asked to.

It holds the agent's wake stream open (one live instance, the lease) and, for each wake the
backend grants, runs one turn of the agent's harness in the agent's working directory, then
waits again. Wakes are handled one at a time, in order. For Claude Code a turn is
`claude -p <prompt> --resume <session>`: the same session is resumed from one wake to the
next, so the agent remembers its earlier exchanges. The harness gets the `agentygg` MCP
server (sharing this instance) to read, answer and ask.

The relay never decides anything: the backend already judged every wake it receives, and
whatever the agent sends back is judged again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentygg import store
from agentygg.api import Gateway, GatewayError
from agentygg.render import skill_text, wake_prompt


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


@dataclass
class TurnResult:
    ok: bool
    session_id: str | None = None
    summary: str = ""
    seconds: float = 0.0


@dataclass
class Relay:
    identity: store.Identity
    workdir: Path
    harness: str = "claude"
    extra_args: list[str] = field(default_factory=list)
    turn_timeout_s: float = 1800.0
    # Testing / other harnesses: a full command to run instead of the Claude Code one; the
    # prompt is passed on stdin and in YGG_PROMPT.
    command: list[str] | None = None

    def __post_init__(self) -> None:
        self.gw = Gateway(self.identity)
        self.me: dict = {}
        self.context: list[dict] = []
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.turns = 0

    # -- session memory (one Claude session per agent + directory) ---------- #

    def _state_path(self) -> Path:
        return store.state_dir() / f"{self.identity.agent_id}.json"

    def _load_session(self) -> str | None:
        p = self._state_path()
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        return data.get("session_id") if data.get("workdir") == str(self.workdir) else None

    def _save_session(self, session_id: str) -> None:
        p = self._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"session_id": session_id, "workdir": str(self.workdir)}))

    # -- one turn ----------------------------------------------------------- #

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["YGG_INSTANCE"] = self.gw.nonce  # the MCP server acts under OUR lease
        env["YGG_AGENT_ID"] = self.identity.agent_id
        env.pop("YGG_AGENT_KEY", None)
        return env

    def _mcp_config(self) -> str:
        cfg = {
            "mcpServers": {
                "agentygg": {
                    "command": sys.executable,
                    "args": ["-m", "agentygg.cli", "mcp"],
                    "env": {"YGG_AGENT_ID": self.identity.agent_id, "YGG_INSTANCE": self.gw.nonce},
                }
            }
        }
        fd, path = tempfile.mkstemp(prefix="agentygg-mcp-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        return path

    def _harness_exe(self) -> str:
        """The harness executable. A background service has a thin PATH (launchd does not read
        ~/.zshrc), so look in the usual install places too."""
        if os.path.isabs(self.harness) and os.access(self.harness, os.X_OK):
            return self.harness
        found = shutil.which(self.harness)
        if found:
            return found
        home = Path.home()
        for cand in (
            home / ".local" / "bin" / self.harness,
            home / ".claude" / "local" / self.harness,
            Path("/opt/homebrew/bin") / self.harness,
            Path("/usr/local/bin") / self.harness,
        ):
            if os.access(cand, os.X_OK):
                return str(cand)
        raise FileNotFoundError(
            f"cannot find `{self.harness}` (Claude Code); reinstall the relay from a terminal "
            "where it works, or pass --harness /full/path/to/claude"
        )

    def _claude_cmd(self, prompt: str, mcp_path: str, session: str | None) -> list[str]:
        exe = self._harness_exe()
        cmd = [
            exe,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--mcp-config",
            mcp_path,
            "--allowedTools",
            "mcp__agentygg",
            "--append-system-prompt",
            "You are connected to Agent'Ygg as an agent. Protocol:\n\n" + skill_text(),
        ]
        if session:
            cmd += ["--resume", session]
        return cmd + self.extra_args

    async def _exec(self, cmd: list[str], prompt: str) -> tuple[int, str, str]:
        env = self._env()
        env["YGG_PROMPT"] = prompt
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.workdir),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode() if self.command else None), self.turn_timeout_s
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return 124, "", f"turn timed out after {self.turn_timeout_s:.0f}s"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def run_turn(self, prompt: str) -> TurnResult:
        started = time.monotonic()
        if self.command:
            code, out, err = await self._exec(self.command, prompt)
            return TurnResult(
                code == 0, None, (out or err).strip()[:200], time.monotonic() - started
            )
        mcp_path = self._mcp_config()
        try:
            session = self._load_session()
            code, out, err = await self._exec(self._claude_cmd(prompt, mcp_path, session), prompt)
            if code != 0 and session:
                # The saved session may be gone (cleared, other machine): start a fresh one.
                code, out, err = await self._exec(self._claude_cmd(prompt, mcp_path, None), prompt)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(mcp_path)
        seconds = time.monotonic() - started
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return TurnResult(False, None, (err or out).strip()[-300:], seconds)
        sid = data.get("session_id")
        if sid:
            self._save_session(sid)
        ok = code == 0 and not data.get("is_error")
        return TurnResult(ok, sid, str(data.get("result") or "").strip()[:200], seconds)

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

    async def _reader(self) -> None:
        """Keep the stream open (reconnecting) and queue what arrives."""
        backoff = 2.0
        while True:
            try:
                async for frame in self.gw.stream():
                    kind = frame.get("kind")
                    if kind == "hello":
                        backoff = 2.0
                        log(f"connected as @{self.identity.agent_id}, waiting for messages")
                    elif kind in ("wake", "context"):
                        await self.queue.put(frame)
                log("stream closed by the server, reconnecting")
            except GatewayError as e:
                if e.status == 409:
                    log("another instance of this agent is connected; retrying in 30s")
                    await asyncio.sleep(30)
                    continue
                if e.status in (401, 403):
                    log(f"stopped: {e.detail}")
                    raise
                log(f"gateway error ({e.detail}); retrying in {backoff:.0f}s")
            except (OSError, TimeoutError) as e:
                log(f"connection lost ({e.__class__.__name__}); retrying in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

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
        if not self.command:
            log(f"harness: {self._harness_exe()}")  # fail now, not on the first wake
        self.me = await self.gw.me()
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
            await self.gw.release()
            await self.gw.close()
