"""Relays as background services: started at login, restarted if they crash, one per agent.

macOS: a LaunchAgent per agent (`~/Library/LaunchAgents/com.agentygg.relay.<id>.plist`), run
through `/bin/zsh -lc` so the login PATH (uv tools, Claude Code) and the user's Full Disk
Access grant for zsh apply to projects under Desktop or Documents. Linux: a systemd user unit
(`~/.config/systemd/user/agentygg-relay-<id>.service`). Logs go to `~/.ygg/logs/<id>.log`.

A relay whose key is revoked exits cleanly (code 0) and is not restarted; any other exit is
retried by the service manager.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agentygg import store

LABEL_PREFIX = "com.agentygg.relay."
UNIT_PREFIX = "agentygg-relay-"


def logs_dir() -> Path:
    return store.home() / "logs"


def log_path(agent_id: str) -> Path:
    return logs_dir() / f"{agent_id}.log"


def _ygg_executable() -> str:
    found = shutil.which("ygg")
    if found:
        return str(Path(found).absolute())  # not resolve(): keep the stable launcher path
    raise RuntimeError("the `ygg` command is not on PATH (install with uv tool install)")


def _harness_path(harness: str = "claude") -> str:
    """Claude Code's absolute path, resolved NOW (from the user's interactive PATH), so the
    service does not depend on what launchd/systemd put on PATH."""
    found = shutil.which(harness)
    # Keep the launcher path (e.g. ~/.local/bin/claude), never the symlink target: that is a
    # versioned binary that disappears when Claude Code updates itself.
    return str(Path(found).absolute()) if found else harness


def run_command(agent_id: str, workdir: Path, harness_args: list[str]) -> str:
    """The shell command a service runs: the relay for one agent, in its directory."""
    parts = [_ygg_executable(), "agent", "run", "--agent", agent_id, "--dir", str(workdir)]
    parts += ["--harness", _harness_path()]
    if harness_args:
        parts += ["--", *harness_args]
    return "exec " + " ".join(shlex.quote(p) for p in parts)


@dataclass
class RelayService:
    agent_id: str
    workdir: str = ""
    running: bool = False
    pid: int | None = None
    path: str = ""


# -- macOS (launchd) ---------------------------------------------------------- #


def _plist_path(agent_id: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}{agent_id}.plist"


def launchd_plist(agent_id: str, workdir: Path, command: str) -> dict:
    env = {"YGG_SERVICE": "1", "PYTHONUNBUFFERED": "1"}
    if os.environ.get("YGG_HOME"):
        env["YGG_HOME"] = os.environ["YGG_HOME"]
    return {
        "Label": f"{LABEL_PREFIX}{agent_id}",
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "WorkingDirectory": str(workdir),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        # Restart after a crash or a lost connection, not after a clean stop (revoked key).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": str(log_path(agent_id)),
        "StandardErrorPath": str(log_path(agent_id)),
        "ProcessType": "Background",
    }


def _meta_path(agent_id: str) -> Path:
    return store.home() / "relays" / f"{agent_id}.json"


def _write_meta(agent_id: str, workdir: Path, harness_args: list[str]) -> None:
    import json

    p = _meta_path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"agent_id": agent_id, "workdir": str(workdir), "args": harness_args}))


def _read_meta(agent_id: str) -> dict:
    import json

    p = _meta_path(agent_id)
    return json.loads(p.read_text()) if p.exists() else {}


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], check=False, capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _unload(agent_id: str) -> None:
    """Stop and unload a loaded relay job, waiting (up to ~10 s) until launchd let it go."""
    target = f"{_domain()}/{LABEL_PREFIX}{agent_id}"
    if _launchctl("print", target).returncode != 0:
        return
    _launchctl("bootout", target)
    for _ in range(20):
        if _launchctl("print", target).returncode != 0:
            return
        time.sleep(0.5)


# -- Linux (systemd --user) --------------------------------------------------- #


def _unit_path(agent_id: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / f"{UNIT_PREFIX}{agent_id}.service"


def systemd_unit(agent_id: str, workdir: Path, command: str) -> str:
    home_env = (
        f"Environment=YGG_HOME={os.environ['YGG_HOME']}\n" if os.environ.get("YGG_HOME") else ""
    )
    return (
        "[Unit]\n"
        f"Description=Agent'Ygg relay for @{agent_id}\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={workdir}\n"
        "Environment=YGG_SERVICE=1\nEnvironment=PYTHONUNBUFFERED=1\n"
        f"{home_env}"
        f"ExecStart=/bin/sh -lc {shlex.quote(command)}\n"
        "Restart=on-failure\nRestartSec=30\n"
        f"StandardOutput=append:{log_path(agent_id)}\nStandardError=append:{log_path(agent_id)}\n"
        f"# agentygg: agent_id={agent_id} workdir={workdir}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], check=False, capture_output=True, text=True
    )


# -- public API --------------------------------------------------------------- #


def system() -> str:
    s = platform.system()
    if s == "Darwin":
        return "launchd"
    if s == "Linux" and shutil.which("systemctl"):
        return "systemd"
    raise RuntimeError(
        f"background relays are supported on macOS and Linux (systemd), not {s}; "
        "run `ygg agent run` in a terminal instead"
    )


def install(agent_id: str, workdir: Path, harness_args: list[str]) -> Path:
    store.load(agent_id)  # fail early if the agent is not on this machine
    logs_dir().mkdir(parents=True, exist_ok=True)
    command = run_command(agent_id, workdir, harness_args)
    kind = system()
    _write_meta(agent_id, workdir, harness_args)
    if kind == "launchd":
        path = _plist_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _unload(agent_id)  # replace a previous one
        with path.open("wb") as f:
            plistlib.dump(launchd_plist(agent_id, workdir, command), f)
        for _attempt in range(5):
            r = _launchctl("bootstrap", _domain(), str(path))
            if r.returncode == 0:
                return path
            time.sleep(1)  # launchd may still be tearing the previous instance down
        raise RuntimeError(f"launchctl refused the relay: {r.stderr.strip() or r.stdout.strip()}")
    path = _unit_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(systemd_unit(agent_id, workdir, command))
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", path.name)
    if r.returncode != 0:
        raise RuntimeError(f"systemd refused the relay: {r.stderr.strip()}")
    return path


def remove(agent_id: str) -> bool:
    kind = system()
    if kind == "launchd":
        path = _plist_path(agent_id)
        _unload(agent_id)
    else:
        path = _unit_path(agent_id)
        _systemctl("disable", "--now", path.name)
    _meta_path(agent_id).unlink(missing_ok=True)
    if path.exists():
        path.unlink()
        if kind == "systemd":
            _systemctl("daemon-reload")
        return True
    return False


def _launchd_state(agent_id: str) -> tuple[bool, int | None]:
    r = _launchctl("print", f"{_domain()}/{LABEL_PREFIX}{agent_id}")
    if r.returncode != 0:
        return False, None
    pid = None
    state = None
    # Only the job's own (first) `state` / `pid`: nested sections repeat `state = active`.
    for line in r.stdout.splitlines():
        s = line.strip()
        if pid is None and s.startswith("pid = "):
            pid = int(s.split("=", 1)[1])
        if state is None and s.startswith("state = "):
            state = s.split("=", 1)[1].strip()
    return state == "running", pid


def listed() -> list[RelayService]:
    out: list[RelayService] = []
    kind = system()
    if kind == "launchd":
        folder = Path.home() / "Library" / "LaunchAgents"
        for p in sorted(folder.glob(f"{LABEL_PREFIX}*.plist")):
            aid = p.stem[len(LABEL_PREFIX) :]
            workdir = _read_meta(aid).get("workdir") or plistlib.loads(p.read_bytes()).get(
                "WorkingDirectory", ""
            )
            running, pid = _launchd_state(aid)
            out.append(RelayService(aid, workdir, running, pid, str(p)))
        return out
    folder = _unit_path("x").parent
    for p in sorted(folder.glob(f"{UNIT_PREFIX}*.service")):
        aid = p.stem[len(UNIT_PREFIX) :]
        workdir = ""
        for line in p.read_text().splitlines():
            if line.startswith("WorkingDirectory="):
                workdir = line.split("=", 1)[1]
        active = _systemctl("is-active", p.name).stdout.strip() == "active"
        out.append(RelayService(aid, workdir, active, None, str(p)))
    return out


def restart(agent_id: str) -> None:
    if system() == "launchd":
        _launchctl("kickstart", "-k", f"{_domain()}/{LABEL_PREFIX}{agent_id}")
    else:
        _systemctl("restart", f"{UNIT_PREFIX}{agent_id}.service")
