"""Which harness is this? So `ygg connect` needs no harness argument.

When an agent runs `ygg connect` from its own shell tool, its harness is one of our ancestor
processes: walk up the process tree and match each command line. A few harnesses also mark the
environment of the commands they run (`CLAUDECODE=1` in Claude Code). When a human runs it from a
plain terminal there is no harness above us: fall back to the harnesses installed on the machine.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

# Matched against the EXECUTABLE of each ancestor (its program, or the script an interpreter
# runs), never its arguments: a shell running `echo "opencode run"` is not OpenCode. Nearest
# ancestor first.
_HARNESS_NAMES = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "openclaw": "openclaw",
    "hermes": "hermes",
    "hermes-agent": "hermes",
}
_INTERPRETERS = ("node", "bun", "deno", "python", "python3", "pythonw", "uv", "npx", "tsx")
# Install layouts whose launcher has a generic name (e.g. Claude Code's versioned binary).
_PATH_HINTS = [
    (re.compile(r"[\\/]claude[\\/]versions[\\/]"), "claude"),
    (re.compile(r"[\\/]anthropic\.claude-code-[^\\/]*[\\/]"), "claude"),
    (re.compile(r"[\\/]node_modules[\\/]@openai[\\/]codex[\\/]"), "codex"),
    (re.compile(r"[\\/]node_modules[\\/]opencode-ai[\\/]"), "opencode"),
    (re.compile(r"[\\/]node_modules[\\/]openclaw[\\/]"), "openclaw"),
]


def _base(token: str) -> str:
    name = re.split(r"[\\/]", token.strip("\"'"))[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".mjs", ".cjs", ".js", ".ts", ".py"):
        name = name.removesuffix(ext)
    return name


def classify(cmdline: str) -> str | None:
    """The harness a process IS, from its command line, or None."""
    tokens = cmdline.split()
    if not tokens:
        return None
    exe = tokens[0]
    candidates = [exe]
    if _base(exe).rstrip("0123456789.") in _INTERPRETERS:
        # The script it runs: first argument that is not an option.
        script = next((t for t in tokens[1:] if not t.startswith("-")), "")
        candidates.append(script)
    for tok in candidates:
        kind = _HARNESS_NAMES.get(_base(tok))
        if kind:
            return kind
        low = tok.lower()
        for pattern, hinted in _PATH_HINTS:
            if pattern.search(low):
                return hinted
    return None


_ENV_MARKERS = [("CLAUDECODE", "claude"), ("OPENCODE", "opencode"), ("CODEX_THREAD_ID", "codex")]


def _process_table() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, command line), for every process we can see."""
    table: dict[int, tuple[int, str]] = {}
    if os.name == "nt":
        ps = (
            "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine"
            " | ConvertTo-Json -Compress"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            rows = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            return table
        for row in rows if isinstance(rows, list) else [rows]:
            table[int(row["ProcessId"])] = (
                int(row["ParentProcessId"] or 0),
                row.get("CommandLine") or "",
            )
        return table
    r = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="], check=False, capture_output=True, text=True
    )
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def from_ancestors(
    table: dict[int, tuple[int, str]] | None = None, start: int | None = None
) -> str | None:
    """The harness found among our ancestor processes, nearest first, or None."""
    table = _process_table() if table is None else table
    pid = os.getppid() if start is None else start
    seen = set()
    for _ in range(40):
        if pid in seen or pid not in table:
            return None
        seen.add(pid)
        ppid, cmdline = table[pid]
        kind = classify(cmdline)
        if kind:
            return kind
        if ppid in (0, pid):
            return None
        pid = ppid
    return None


def from_environment(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    for var, kind in _ENV_MARKERS:
        if env.get(var):
            return kind
    return None


def installed() -> list[str]:
    """Harnesses found on this machine (PATH and their usual install places)."""
    from agentygg import harnesses

    out = []
    for kind in ("claude", "codex", "hermes", "openclaw", "opencode"):
        h = harnesses.build(kind)
        try:
            h.exe()
            out.append(kind)
        except FileNotFoundError:
            continue
    return out


def detect() -> tuple[str | None, str]:
    """(harness, how it was found). Harness None means: ask the user."""
    kind = from_environment()
    if kind:
        return kind, "from the environment"
    kind = from_ancestors()
    if kind:
        return kind, "running inside it"
    found = installed()
    if len(found) == 1:
        return found[0], "the only harness installed here"
    return None, (
        "several harnesses installed: " + ", ".join(found)
    ) if found else "no harness found"


def which_ygg() -> str:
    found = shutil.which("ygg")
    return found or "ygg"
