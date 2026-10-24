"""Harnesses: how the relay runs ONE turn of whatever agent the user brings.

Agent'Ygg is model-agnostic: the relay only needs a way to run a turn non-interactively in the
agent's folder, with the `agentygg` MCP server available, and (ideally) to resume the same
session next time. Three harnesses:

* `claude`  Claude Code: `claude -p <prompt> --resume <session> --mcp-config ...`
* `codex`   OpenAI Codex CLI (also shipped inside the ChatGPT desktop app):
            `codex exec --json ...` then `codex exec resume <thread> ...`, MCP given with `-c`.
* `hermes`  Nous Research Hermes Agent: `hermes chat --oneshot --query-file - --format
            stream-json --resume <session>`; MCP from its config (`ygg setup hermes`).
* `openclaw` OpenClaw: `openclaw agent exec --message-file - --json --cwd <dir>`, MCP injected
            for the run with `--config-override`.
* `command` anything else (a local model, another harness, a script...): a shell command run
            per wake, with the prompt on stdin and in `$YGG_PROMPT`. It answers through the
            `agentygg` MCP server (configured in that harness) or `ygg send`.

Every turn runs with `YGG_INSTANCE` set, so the MCP server or `ygg send` acts under the
relay's own lease.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agentygg.render import skill_text

KINDS = ("claude", "codex", "hermes", "openclaw", "command")
PROTOCOL_PREAMBLE = "You are connected to Agent'Ygg as an agent. Protocol:\n\n"


@dataclass
class McpSpec:
    """How to start the `agentygg` MCP server for one turn."""

    command: str
    args: list[str]
    env: dict[str, str]


@dataclass
class Turn:
    cmd: list[str]
    stdin: bytes | None = None
    cleanup: list[str] = field(default_factory=list)  # temp files to delete afterwards


@dataclass
class TurnResult:
    ok: bool
    session_id: str | None = None
    summary: str = ""
    seconds: float = 0.0


def find_executable(name: str, candidates: list[Path]) -> str:
    """`name` from PATH, else the first existing candidate. A background service has a thin
    PATH (launchd does not read ~/.zshrc), hence the usual install places."""
    if os.path.isabs(name) and os.path.exists(name):
        return name
    found = shutil.which(name)
    if found:
        return found
    for cand in candidates:
        if cand.exists():
            return str(cand)
    raise FileNotFoundError(name)


def _home_bins(name: str) -> list[Path]:
    home = Path.home()
    out = [
        home / ".local" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    ]
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        out = [
            home / ".local" / "bin" / f"{name}.exe",
            appdata / "npm" / f"{name}.cmd",
            *out,
        ]
    return out


class Harness:
    kind = "?"
    resumable = False

    def prepare(
        self, prompt: str, mcp: McpSpec, session: str | None, extra: list[str], workdir: str
    ) -> Turn:
        raise NotImplementedError

    def parse(self, code: int, out: str, err: str) -> TurnResult:
        raise NotImplementedError

    def describe(self) -> str:
        return self.kind


@dataclass
class ClaudeCode(Harness):
    path: str | None = None
    kind = "claude"
    resumable = True

    def exe(self) -> str:
        try:
            return find_executable(
                self.path or "claude",
                [*_home_bins("claude"), Path.home() / ".claude" / "local" / "claude"],
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                "cannot find Claude Code (`claude`); install it, or pass --harness-path"
            ) from None

    def describe(self) -> str:
        return f"Claude Code ({self.exe()})"

    def prepare(self, prompt, mcp, session, extra, workdir):
        cfg = {
            "mcpServers": {"agentygg": {"command": mcp.command, "args": mcp.args, "env": mcp.env}}
        }
        fd, path = tempfile.mkstemp(prefix="agentygg-mcp-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        cmd = [
            self.exe(),
            "-p",
            prompt,
            "--output-format",
            "json",
            "--mcp-config",
            path,
            "--allowedTools",
            "mcp__agentygg",
            "--append-system-prompt",
            PROTOCOL_PREAMBLE + skill_text(),
        ]
        if session:
            cmd += ["--resume", session]
        return Turn(cmd + extra, None, [path])

    def parse(self, code, out, err):
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return TurnResult(False, None, (err or out).strip()[-300:])
        ok = code == 0 and not data.get("is_error")
        return TurnResult(ok, data.get("session_id"), str(data.get("result") or "").strip()[:200])


def _toml(value) -> str:
    """A TOML value for `codex -c key=value` (JSON strings and arrays are valid TOML)."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={json.dumps(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


@dataclass
class Codex(Harness):
    path: str | None = None
    kind = "codex"
    resumable = True

    def exe(self) -> str:
        try:
            return find_executable(
                self.path or "codex",
                [
                    *_home_bins("codex"),
                    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
                    Path("/Applications/Codex.app/Contents/Resources/codex"),
                ],
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                "cannot find the Codex CLI (`codex`); install it (npm i -g @openai/codex) or "
                "pass --harness-path"
            ) from None

    def describe(self) -> str:
        return f"Codex ({self.exe()})"

    def prepare(self, prompt, mcp, session, extra, workdir):
        conf = [
            "-c",
            f"mcp_servers.agentygg.command={_toml(mcp.command)}",
            "-c",
            f"mcp_servers.agentygg.args={_toml(mcp.args)}",
            "-c",
            f"mcp_servers.agentygg.env={_toml(mcp.env)}",
        ]
        if session:
            cmd = [self.exe(), "exec", "resume", session, "--json", "--skip-git-repo-check", *conf]
        else:
            # Codex has no "append system prompt": a new session gets the protocol up front,
            # a resumed one already has it.
            prompt = PROTOCOL_PREAMBLE + skill_text() + "\n\n---\n\n" + prompt
            cmd = [self.exe(), "exec", "--json", "--skip-git-repo-check", *conf]
        return Turn([*cmd, *extra, prompt])

    def parse(self, code, out, err):
        thread, last, failed, completed = None, "", "", False
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                thread = ev.get("thread_id")
            elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                last = (ev["item"].get("text") or "").strip()
            elif t == "turn.failed":
                failed = ((ev.get("error") or {}).get("message") or "turn failed").strip()
            elif t == "error" and not failed:
                failed = (ev.get("message") or "").strip()
            elif t == "turn.completed":
                completed = True
        ok = code == 0 and completed and not failed
        return TurnResult(ok, thread, (last if ok else failed or err.strip())[:200])


@dataclass
class Hermes(Harness):
    path: str | None = None
    kind = "hermes"
    resumable = True

    def exe(self) -> str:
        try:
            local = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes" / "bin"
            return find_executable(
                self.path or "hermes", [*_home_bins("hermes"), local / "hermes.exe"]
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                "cannot find Hermes Agent (`hermes`); install it "
                "(https://hermes-agent.nousresearch.com) or pass --harness-path"
            ) from None

    def describe(self) -> str:
        return f"Hermes Agent ({self.exe()})"

    def prepare(self, prompt, mcp, session, extra, workdir):
        # The `agentygg` MCP server comes from Hermes' own config (`ygg setup hermes`); it
        # finds this agent's identity and lease from the folder and ~/.ygg/state.
        if not session:
            prompt = PROTOCOL_PREAMBLE + skill_text() + "\n\n---\n\n" + prompt
        cmd = [self.exe(), "chat", "--oneshot", "--query-file", "-", "--format", "stream-json"]
        cmd += ["--in", workdir]
        if session:
            cmd += ["--resume", session]
        return Turn([*cmd, *extra], prompt.encode())

    def parse(self, code, out, err):
        result: dict = {}
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result = ev
        if not result:
            return TurnResult(False, None, (err or out).strip()[-300:])
        ok = code == 0 and not result.get("error") and result.get("exit_code", 0) == 0
        text = str(result.get("text") if ok else result.get("error") or "turn failed")
        return TurnResult(ok, result.get("session_id"), text.strip()[:200])


@dataclass
class OpenClaw(Harness):
    path: str | None = None
    kind = "openclaw"
    resumable = False  # exec runs on isolated state; the conversation tool gives continuity

    def exe(self) -> str:
        try:
            return find_executable(self.path or "openclaw", _home_bins("openclaw"))
        except FileNotFoundError:
            raise FileNotFoundError(
                "cannot find OpenClaw (`openclaw`); install it (https://openclaw.ai) or pass "
                "--harness-path"
            ) from None

    def describe(self) -> str:
        return f"OpenClaw ({self.exe()})"

    def prepare(self, prompt, mcp, session, extra, workdir):
        override = {
            "mcp": {
                "servers": {"agentygg": {"command": mcp.command, "args": mcp.args, "env": mcp.env}}
            }
        }
        prompt = PROTOCOL_PREAMBLE + skill_text() + "\n\n---\n\n" + prompt
        cmd = [
            self.exe(),
            "agent",
            "exec",
            "--message-file",
            "-",
            "--json",
            "--cwd",
            workdir,
            "--config-override",
            json.dumps(override),
        ]
        return Turn([*cmd, *extra], prompt.encode())

    def parse(self, code, out, err):
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return TurnResult(False, None, (err or out).strip()[-300:])
        ok = code == 0 and bool(data.get("ok"))
        text = data.get("final") if ok else (data.get("error") or {}).get("message", "turn failed")
        return TurnResult(ok, data.get("sessionId"), str(text or "").strip()[:200])


@dataclass
class Command(Harness):
    """Any harness: a shell command per wake, prompt on stdin and in $YGG_PROMPT."""

    command: str | list[str] = ""
    kind = "command"
    resumable = False

    def describe(self) -> str:
        return (
            f"command: {self.command if isinstance(self.command, str) else ' '.join(self.command)}"
        )

    def prepare(self, prompt, mcp, session, extra, workdir):
        if isinstance(self.command, list):
            cmd = [*self.command, *extra]
        elif os.name == "nt":
            cmd = ["cmd", "/c", self.command, *extra]
        else:
            cmd = ["/bin/sh", "-c", self.command, *extra]
        return Turn(cmd, prompt.encode())

    def parse(self, code, out, err):
        lines = [ln for ln in (out or err).strip().splitlines() if ln.strip()]
        return TurnResult(code == 0, None, (lines[-1] if lines else "")[:200])


def build(kind: str = "claude", path: str | None = None, command: str | None = None) -> Harness:
    if kind == "claude":
        return ClaudeCode(path)
    if kind == "codex":
        return Codex(path)
    if kind == "hermes":
        return Hermes(path)
    if kind == "openclaw":
        return OpenClaw(path)
    if kind == "command":
        if not command:
            raise ValueError("--harness command needs --cmd '<the command to run per wake>'")
        return Command(command)
    # Back-compat: `--harness /path/to/claude` from 0.2.0 service definitions.
    if os.path.sep in kind or kind.endswith(("claude", "claude.exe")):
        return ClaudeCode(kind)
    raise ValueError(f"unknown harness {kind!r}: choose one of {', '.join(KINDS)}")


def mcp_spec(agent_id: str, nonce: str) -> McpSpec:
    """The MCP server, started with THIS interpreter so it is always the installed client."""
    return McpSpec(
        sys.executable,
        ["-m", "agentygg.cli", "mcp"],
        {"YGG_AGENT_ID": agent_id, "YGG_INSTANCE": nonce},
    )
