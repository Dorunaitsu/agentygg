"""Relays as background services: started at login, restarted if they crash, one per agent.

* macOS: a LaunchAgent per agent (`~/Library/LaunchAgents/com.agentygg.relay.<id>.plist`), run
  through `/bin/zsh -lc` so the login PATH and the user's Full Disk Access grant for zsh apply
  to projects under Desktop or Documents.
* Linux: a systemd user unit (`~/.config/systemd/user/agentygg-relay-<id>.service`).
* Windows: a Task Scheduler task at logon ("AgentYgg Relay <id>") that starts a hidden launcher
  (`~/.ygg/relays/<id>.vbs` -> `<id>.cmd`); the .cmd restarts the relay if it crashes.

Logs go to `~/.ygg/logs/<id>.log`. A relay whose key is revoked exits cleanly (code 0) and is
not restarted; any other exit is retried.
"""

from __future__ import annotations

import json
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
TASK_PREFIX = "AgentYgg Relay "


def logs_dir() -> Path:
    return store.logs_dir()


def log_path(agent_id: str) -> Path:
    return logs_dir() / f"{agent_id}.log"


def relays_dir() -> Path:
    return store.home() / "relays"


def _ygg_executable() -> str:
    found = shutil.which("ygg")
    if found:
        return str(Path(found).absolute())  # not resolve(): keep the stable launcher path
    raise RuntimeError("the `ygg` command is not on PATH (install with uv tool install)")


def _harness_path(harness: str = "claude", path: str | None = None) -> str | None:
    """The harness executable, resolved NOW from the user's interactive PATH, so the service
    does not depend on the thin PATH a service manager gives. Kept as the launcher path (e.g.
    ~/.local/bin/claude), never a symlink target: those are versioned binaries that disappear
    when the harness updates itself."""
    from agentygg import harnesses

    if harness == "command":
        return None
    h = harnesses.build(harness, path)
    try:
        return str(Path(h.exe()).absolute())
    except FileNotFoundError:
        return path


def relay_argv(
    agent_id: str,
    workdir: Path,
    harness_args: list[str],
    harness: str = "claude",
    harness_path: str | None = None,
    cmd: str | None = None,
) -> list[str]:
    """The relay command a service runs, for one agent in its directory."""
    argv = [_ygg_executable(), "agent", "run", "--agent", agent_id, "--dir", str(workdir)]
    argv += ["--harness", harness]
    exe = _harness_path(harness, harness_path)
    if exe:
        argv += ["--harness-path", exe]
    if cmd:
        argv += ["--cmd", cmd]
    if harness_args:
        argv += ["--", *harness_args]
    return argv


def run_command(agent_id: str, workdir: Path, harness_args: list[str], **kw) -> str:
    """The POSIX shell form of `relay_argv` (for launchd / systemd)."""
    return "exec " + " ".join(
        shlex.quote(p) for p in relay_argv(agent_id, workdir, harness_args, **kw)
    )


@dataclass
class RelayService:
    agent_id: str
    workdir: str = ""
    running: bool = False
    pid: int | None = None
    path: str = ""
    harness: str = ""


def _meta_path(agent_id: str) -> Path:
    return relays_dir() / f"{agent_id}.json"


def _write_meta(agent_id: str, workdir: Path, harness_args: list[str], harness: str) -> None:
    p = _meta_path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "workdir": str(workdir),
                "args": harness_args,
                "harness": harness,
            }
        )
    )


def _read_meta(agent_id: str) -> dict:
    p = _meta_path(agent_id)
    return json.loads(p.read_text()) if p.exists() else {}


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), check=False, capture_output=True, text=True)


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
        # The log it writes holds every message: create it readable by this user only.
        "Umask": 0o077,
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _unload(agent_id: str) -> None:
    """Stop and unload a loaded relay job, waiting (up to ~10 s) until launchd let it go."""
    target = f"{_domain()}/{LABEL_PREFIX}{agent_id}"
    if _run("launchctl", "print", target).returncode != 0:
        return
    _run("launchctl", "bootout", target)
    for _ in range(20):
        if _run("launchctl", "print", target).returncode != 0:
            return
        time.sleep(0.5)


def _launchd_state(agent_id: str) -> tuple[bool, int | None]:
    r = _run("launchctl", "print", f"{_domain()}/{LABEL_PREFIX}{agent_id}")
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
        # The log it writes holds every message: create it readable by this user only.
        "UMask=0077\n"
        f"StandardOutput=append:{log_path(agent_id)}\nStandardError=append:{log_path(agent_id)}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run("systemctl", "--user", *args)


# -- Windows (Task Scheduler) ------------------------------------------------- #


def _task_name(agent_id: str) -> str:
    return f"{TASK_PREFIX}{agent_id}"


def windows_launchers(agent_id: str, workdir: Path, argv: list[str]) -> tuple[str, str]:
    """(.cmd, .vbs) contents. The .cmd runs the relay, appends to its log and restarts it
    after a crash (not after a clean exit, e.g. a revoked key); the .vbs starts the .cmd with
    no console window, which is what the logon task runs."""
    line = subprocess.list2cmdline(argv)
    log = log_path(agent_id)
    home = f'set "YGG_HOME={os.environ["YGG_HOME"]}"\r\n' if os.environ.get("YGG_HOME") else ""
    cmd = (
        "@echo off\r\n"
        'set "YGG_SERVICE=1"\r\nset "PYTHONUNBUFFERED=1"\r\n'
        f"{home}"
        f'cd /d "{workdir}"\r\n'
        ":loop\r\n"
        f'{line} >> "{log}" 2>&1\r\n'
        "if %ERRORLEVEL% EQU 0 exit /b 0\r\n"
        "timeout /t 30 /nobreak >nul\r\n"
        "goto loop\r\n"
    )
    cmd_path = relays_dir() / f"{agent_id}.cmd"
    # In a VBScript string "" is one quote: this runs  cmd /c "<path to .cmd>"  hidden (0).
    vbs = f'CreateObject("WScript.Shell").Run "cmd /c ""{cmd_path}""", 0, False\r\n'
    return cmd, vbs


def _windows_install(agent_id: str, workdir: Path, argv: list[str]) -> Path:
    d = relays_dir()
    d.mkdir(parents=True, exist_ok=True)
    cmd, vbs = windows_launchers(agent_id, workdir, argv)
    (d / f"{agent_id}.cmd").write_text(cmd)
    vbs_path = d / f"{agent_id}.vbs"
    vbs_path.write_text(vbs)
    name = _task_name(agent_id)
    _windows_stop(agent_id)
    r = _run(
        "schtasks", "/Create", "/TN", name, "/TR", f'wscript.exe "{vbs_path}"',
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    )  # fmt: skip
    if r.returncode != 0:
        raise RuntimeError(
            f"Task Scheduler refused the relay: {r.stderr.strip() or r.stdout.strip()}"
        )
    _run("schtasks", "/Run", "/TN", name)
    return vbs_path


def _windows_stop(agent_id: str) -> None:
    _run("schtasks", "/End", "/TN", _task_name(agent_id))
    # The task only owns the launcher; stop the relay process itself too.
    try:
        pid = json.loads((store.state_dir() / f"{agent_id}.instance").read_text()).get("pid")
    except (OSError, ValueError):
        pid = None
    if isinstance(pid, int) and pid > 0:
        _run("taskkill", "/PID", str(pid), "/T", "/F")


def _windows_running(agent_id: str) -> bool:
    r = _run("schtasks", "/Query", "/TN", _task_name(agent_id), "/FO", "LIST")
    return r.returncode == 0 and "Running" in r.stdout


# -- public API --------------------------------------------------------------- #


def system() -> str:
    s = platform.system()
    if s == "Darwin":
        return "launchd"
    if s == "Windows":
        return "schtasks"
    if s == "Linux" and shutil.which("systemctl"):
        return "systemd"
    raise RuntimeError(
        f"background relays are supported on macOS, Windows and Linux (systemd), not {s}; "
        "run `ygg agent run` instead"
    )


def install(
    agent_id: str,
    workdir: Path,
    harness_args: list[str],
    harness: str = "claude",
    harness_path: str | None = None,
    cmd: str | None = None,
) -> Path:
    store.load(agent_id)  # fail early if the agent is not on this machine
    store.secure_tree()  # logs hold every message: private to this user (0700 / 0600)
    kw = {"harness": harness, "harness_path": harness_path, "cmd": cmd}
    kind = system()
    _write_meta(agent_id, workdir, harness_args, harness)
    if kind == "schtasks":
        return _windows_install(
            agent_id, workdir, relay_argv(agent_id, workdir, harness_args, **kw)
        )
    command = run_command(agent_id, workdir, harness_args, **kw)
    if kind == "launchd":
        path = _plist_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _unload(agent_id)  # replace a previous one
        with path.open("wb") as f:
            plistlib.dump(launchd_plist(agent_id, workdir, command), f)
        for _attempt in range(5):
            r = _run("launchctl", "bootstrap", _domain(), str(path))
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
    existed = _meta_path(agent_id).exists()
    if kind == "launchd":
        path = _plist_path(agent_id)
        _unload(agent_id)
    elif kind == "systemd":
        path = _unit_path(agent_id)
        _systemctl("disable", "--now", path.name)
    else:
        _windows_stop(agent_id)
        r = _run("schtasks", "/Delete", "/TN", _task_name(agent_id), "/F")
        existed = existed or r.returncode == 0
        for ext in ("cmd", "vbs"):
            (relays_dir() / f"{agent_id}.{ext}").unlink(missing_ok=True)
        path = _meta_path(agent_id)
    _meta_path(agent_id).unlink(missing_ok=True)
    if path.exists():
        path.unlink()
        if kind == "systemd":
            _systemctl("daemon-reload")
        return True
    return existed


def listed() -> list[RelayService]:
    out: list[RelayService] = []
    kind = system()
    if kind == "launchd":
        folder = Path.home() / "Library" / "LaunchAgents"
        for p in sorted(folder.glob(f"{LABEL_PREFIX}*.plist")):
            aid = p.stem[len(LABEL_PREFIX) :]
            meta = _read_meta(aid)
            workdir = meta.get("workdir") or plistlib.loads(p.read_bytes()).get(
                "WorkingDirectory", ""
            )
            running, pid = _launchd_state(aid)
            out.append(
                RelayService(aid, workdir, running, pid, str(p), meta.get("harness", "claude"))
            )
        return out
    if kind == "systemd":
        for p in sorted(_unit_path("x").parent.glob(f"{UNIT_PREFIX}*.service")):
            aid = p.stem[len(UNIT_PREFIX) :]
            meta = _read_meta(aid)
            active = _systemctl("is-active", p.name).stdout.strip() == "active"
            out.append(
                RelayService(
                    aid, meta.get("workdir", ""), active, None, str(p), meta.get("harness", "")
                )
            )
        return out
    for p in sorted(relays_dir().glob("*.json")) if relays_dir().exists() else []:
        meta = json.loads(p.read_text())
        aid = meta.get("agent_id") or p.stem
        out.append(
            RelayService(
                aid,
                meta.get("workdir", ""),
                _windows_running(aid),
                None,
                str(p),
                meta.get("harness", ""),
            )
        )
    return out


def restart(agent_id: str) -> None:
    kind = system()
    if kind == "launchd":
        _run("launchctl", "kickstart", "-k", f"{_domain()}/{LABEL_PREFIX}{agent_id}")
    elif kind == "systemd":
        _systemctl("restart", f"{UNIT_PREFIX}{agent_id}.service")
    else:
        _windows_stop(agent_id)
        _run("schtasks", "/Run", "/TN", _task_name(agent_id))
