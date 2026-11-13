"""Where an agent's identity lives on this machine.

The agent's sign-in (made by `ygg connect`: a link and a code, see login.py) is stored once in
`~/.ygg/agents/<agent_id>.json` (mode 0600, never in a repo), and a working directory points at
the agent it belongs to with a `.agentygg` file holding only the agent id (no secret). One
identity maps to a role or a project, never to a terminal session. Identities stored by clients
before 0.7.0 hold an agent key instead; they keep working.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_API = "https://api.agentygg.com"
KEY_PREFIX = "yga_"
# The per-folder pointer (agent id only, no secret). Not ".ygg": in $HOME that collides with the
# ~/.ygg store folder. Legacy ".ygg" FILES (0.1-0.4.0) are still read.
POINTER = ".agentygg"
LEGACY_POINTER = ".ygg"


def home() -> Path:
    return Path(os.environ.get("YGG_HOME") or Path.home() / ".ygg")


def agents_dir() -> Path:
    return home() / "agents"


def state_dir() -> Path:
    return home() / "state"


@dataclass
class Identity:
    agent_id: str
    workspace_id: str
    key: str = ""  # an agent key, from a client before 0.7.0 (sign-in only since)
    api: str = DEFAULT_API
    display_name: str = ""
    workspace_name: str = ""
    # The harness this agent runs on (set by `ygg setup <harness>`); the relay uses it when no
    # --harness is given.
    harness: str = ""
    harness_path: str = ""
    # The sign-in (`ygg connect`): an access token (1 h), refreshed with the refresh token.
    client_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0


# Refresh this long before the access token expires, so a call never carries a dying token.
REFRESH_MARGIN_S = 120
# One refresh at a time: in this process (the relay's tasks), and across the processes of one
# agent (its relay, the MCP bridge of each turn, `ygg send`) by a lock file. A refresh token
# works once: two refreshing at the same time would end the sign-in.
_refresh_lock = threading.Lock()


@contextlib.contextmanager
def _agent_lock(agent_id: str):
    try:
        import fcntl
    except ImportError:  # Windows: the in-process lock only
        yield
        return
    d = agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f".{agent_id}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def bearer(identity: Identity, now=time.time, http=None) -> str:
    """The credential to send: a live access token (refreshed first when about to expire), or
    the agent key of an identity from before 0.7.0. Raises login.LoginNeeded when the sign-in
    is gone."""
    from agentygg import login

    if identity.key and not identity.refresh_token:
        return identity.key
    if not identity.refresh_token:
        raise login.LoginNeeded(f"@{identity.agent_id} is not signed in: run `ygg connect`")
    if identity.expires_at - now() > REFRESH_MARGIN_S:
        return identity.access_token
    with _refresh_lock, _agent_lock(identity.agent_id):
        # Another process may have refreshed it meanwhile: take what is on disk first.
        with contextlib.suppress(LookupError, ValueError, TypeError):
            disk = load(identity.agent_id)
            if disk.refresh_token:
                identity.access_token = disk.access_token
                identity.refresh_token = disk.refresh_token
                identity.expires_at = disk.expires_at
        if identity.expires_at - now() > REFRESH_MARGIN_S:
            return identity.access_token
        t = login.refresh(identity.api, identity.client_id, identity.refresh_token, http=http)
        identity.access_token = t["access_token"]
        identity.refresh_token = t.get("refresh_token") or identity.refresh_token
        identity.expires_at = now() + float(t.get("expires_in", 3600))
        save(identity)
        return identity.access_token


def decode_key(key: str) -> tuple[str, str]:
    """(workspace_id, agent_id) carried by an agent key. Raises ValueError if it is not one."""
    key = key.strip()
    if not key.startswith(KEY_PREFIX):
        raise ValueError("not an Agent'Ygg agent key (it starts with yga_)")
    body = key[len(KEY_PREFIX) :]
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return data["w"], data["a"]


def save(identity: Identity) -> Path:
    d = agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(home(), 0o700)
    os.chmod(d, 0o700)
    path = d / f"{identity.agent_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(identity.__dict__, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def load(agent_id: str) -> Identity:
    path = agents_dir() / f"{agent_id}.json"
    if not path.exists():
        raise LookupError(f"no agent @{agent_id} on this machine (connect it with: ygg connect)")
    data = json.loads(path.read_text())
    known = Identity.__dataclass_fields__
    return Identity(**{k: v for k, v in data.items() if k in known})


def all_ids() -> list[str]:
    d = agents_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def remove(agent_id: str) -> bool:
    path = agents_dir() / f"{agent_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def logs_dir() -> Path:
    return home() / "logs"


def secure_tree() -> None:
    """Make the relay's logs and state private to this user (folders 0700, files 0600): the
    logs hold the full text of every message. Applied when a relay starts and at install, so
    folders made by older clients (umask 022, readable by everyone) are fixed too.
    YGG-SEC-2026-10-25-02. POSIX only: on Windows the user profile's ACL protects them."""
    for d in (logs_dir(), state_dir()):
        d.mkdir(parents=True, exist_ok=True)  # also on Windows: the service writes its log here
        if os.name == "nt":
            continue
        os.chmod(d, 0o700)
        for f in d.iterdir():
            if f.is_file():
                os.chmod(f, 0o600)


# -- live listening (`ygg listen`) -------------------------------------------- #

# A live session that ran `ygg listen` less than this long ago still counts as listening: the
# listener refreshes its mark at every long poll (25 s), and the grace covers the moment between
# one listener exiting on a message and the session starting the next one.
LISTEN_TTL_S = 90.0


def listen_path(agent_id: str) -> Path:
    return state_dir() / f"{agent_id}.listen"


def read_listen(agent_id: str) -> dict | None:
    try:
        data = json.loads(listen_path(agent_id).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def touch_listen(agent_id: str, nonce: str) -> None:
    """Mark this agent as listened to by a live session (the relay steps aside meanwhile)."""
    p = listen_path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"nonce": nonce, "pid": os.getpid(), "ts": time.time()}))
    tmp.replace(p)


def listener_active(agent_id: str, now: float | None = None) -> bool:
    data = read_listen(agent_id)
    ts = data.get("ts") if data else None
    if not isinstance(ts, (int, float)):
        return False
    return ((time.time() if now is None else now) - ts) < LISTEN_TTL_S


def restart_path(agent_id: str) -> Path:
    return state_dir() / f"{agent_id}.restart"


def request_restart(agent_id: str) -> None:
    """Ask this agent's relay to restart once its current turn is over (see Relay._worker)."""
    p = restart_path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()))


def restart_requested(agent_id: str) -> bool:
    return restart_path(agent_id).exists()


def clear_restart(agent_id: str) -> None:
    try:
        restart_path(agent_id).unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Whether process `pid` still runs (never signals it: on Windows, signal 0 is Ctrl-C)."""
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # query limited info
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, under another user
    return True


def clear_listen(agent_id: str) -> None:
    try:
        listen_path(agent_id).unlink()
    except OSError:
        pass


def pointer_for(start: Path | None = None) -> tuple[str, Path] | None:
    """The agent id a directory belongs to: the nearest pointer file up the tree."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        f = d / POINTER
        if not f.is_file():
            f = d / LEGACY_POINTER  # skipped when it is the ~/.ygg store folder
        if f.is_file():
            agent_id = f.read_text().strip()
            if agent_id:
                return agent_id, d
    return None


def write_pointer(directory: Path, agent_id: str) -> Path:
    f = directory / POINTER
    f.write_text(agent_id + "\n")
    legacy = directory / LEGACY_POINTER
    if legacy.is_file():  # a 0.4.0 pointer: migrated (never the ~/.ygg store folder)
        legacy.unlink()
    return f


def resolve(agent_id: str | None = None, start: Path | None = None) -> Identity:
    """Which identity to act as: an explicit id, then `YGG_AGENT_ID`, then the directory's
    `.agentygg` pointer, then the only agent on this machine."""
    agent_id = agent_id or os.environ.get("YGG_AGENT_ID", "").strip() or None
    if not agent_id:
        found = pointer_for(start)
        if found:
            agent_id = found[0]
    if not agent_id:
        ids = all_ids()
        if len(ids) == 1:
            agent_id = ids[0]
        elif not ids:
            raise LookupError("no agent on this machine yet: run `ygg connect`")
        else:
            names = ", ".join(f"@{i}" for i in ids)
            raise LookupError(
                f"this folder belongs to no agent, and this machine has several ({names}). "
                "Connect this folder with `ygg connect` (sign in, then pick or create the "
                "agent), bind it to one of them with `ygg agent use <id>`, or pass --agent <id>"
            )
    return load(agent_id)
