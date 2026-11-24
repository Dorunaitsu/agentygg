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
import re
import secrets
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


# An agent id is a plain name, never a path (security audit YGG-35): it comes from pointers,
# arguments and the gateway, and is joined into file names below.
_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def valid_id(agent_id: object) -> bool:
    return isinstance(agent_id, str) and _AGENT_ID.fullmatch(agent_id) is not None


def _checked(agent_id: str) -> str:
    if not valid_id(agent_id):
        raise LookupError(f"not an agent id: {agent_id!r}")
    return agent_id


def private_dir(d: Path) -> Path:
    """Make `d` (under the client's folder) and that folder readable by this user only (0700),
    whatever the umask (security audit YGG-37): tokens, message text and state live there.
    POSIX only: on Windows the user profile's ACL protects them."""
    d.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root = home()
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)
            rel = d.resolve().relative_to(root.resolve())
            cur = root
            for part in rel.parts:
                cur = cur / part
                os.chmod(cur, 0o700)
    return d


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
    d = agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows
        with _windows_lock(d / f".{agent_id}.lock"):
            yield
        return
    with open(d / f".{agent_id}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def _windows_lock(path: Path, wait_s: float = 60.0):
    """An exclusive lock across processes on Windows (msvcrt locks one byte of the file). With
    the Claude Code plugin an agent has several processes that may refresh its sign-in (relay,
    MCP server, listener); a refresh token works once, so they must take turns (2026-11-15)."""
    import msvcrt

    with open(path, "a+b") as f:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        try:
            yield
        finally:
            f.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


# The sign-in travels as a whole: a refresh token only works with the client id it was issued
# to, at the API that issued it (Claudeo, 2026-11-20: a relogin's new token sent with the old
# client id by a process started before it).
_SIGN_IN = ("api", "client_id", "access_token", "refresh_token", "expires_at")


def _adopt_disk(identity: Identity) -> None:
    """Take the sign-in saved on disk when it is not the one in memory: another process
    refreshed it, or `ygg connect --relogin` replaced it (and revoked the old one)."""
    with contextlib.suppress(LookupError, ValueError, TypeError, OSError):
        disk = load(identity.agent_id)
        if disk.refresh_token and disk.refresh_token != identity.refresh_token:
            for field in _SIGN_IN:
                setattr(identity, field, getattr(disk, field))


def bearer(identity: Identity, now=time.time, http=None) -> str:
    """The credential to send: a live access token (refreshed first when about to expire), or
    the agent key of an identity from before 0.7.0. Raises login.LoginNeeded when the sign-in
    is gone."""
    from agentygg import login

    if identity.key and not identity.refresh_token:
        return identity.key
    _adopt_disk(identity)  # a relogin or another process's refresh, since this one loaded it
    if not identity.refresh_token:
        raise login.LoginNeeded(f"@{identity.agent_id} is not signed in: run `ygg connect`")
    if identity.expires_at - now() > REFRESH_MARGIN_S:
        return identity.access_token
    with _refresh_lock, _agent_lock(identity.agent_id):
        # Another process may have refreshed it meanwhile: take what is on disk first.
        _adopt_disk(identity)
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


def _to_disk(path: Path) -> None:
    """Push a file (or a folder's entries) out of the system's cache. Best effort: a file system
    or a platform that cannot (a folder on Windows) changes nothing."""
    full = None
    with contextlib.suppress(ImportError):
        import fcntl

        full = getattr(fcntl, "F_FULLFSYNC", None)  # macOS: fsync alone stops at the drive's cache
    with contextlib.suppress(OSError):
        # Windows flushes only what is open for writing (and no folder at all).
        fd = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
        try:
            os.fsync(fd)
            if full is not None:
                fcntl.fcntl(fd, full)
        finally:
            os.close(fd)


def save(identity: Identity) -> Path:
    """Written through to the disk: a refresh token works once, so a power cut that brings the
    old one back ends the sign-in (Claudeo, 2026-11-24)."""
    d = agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(home(), 0o700)
    os.chmod(d, 0o700)
    path = d / f"{_checked(identity.agent_id)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(identity.__dict__, indent=2))
    os.chmod(tmp, 0o600)
    _to_disk(tmp)
    tmp.replace(path)
    _to_disk(d)
    return path


def load(agent_id: str) -> Identity:
    path = agents_dir() / f"{_checked(agent_id)}.json"
    if not path.exists():
        raise LookupError(f"no agent @{agent_id} on this machine (connect it with: ygg connect)")
    data = json.loads(path.read_text())
    known = Identity.__dataclass_fields__
    return Identity(**{k: v for k, v in data.items() if k in known})


def all_ids() -> list[str]:
    d = agents_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def remove(agent_id: str) -> bool:
    if not valid_id(agent_id):
        return False
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


# -- local pokes: a job on this machine wakes its agent (`ygg relay poke`) ------------------ #

POKES_MAX = 20
_POKE_SEQ = 0


def pokes_dir(agent_id: str) -> Path:
    return state_dir() / "pokes" / _checked(agent_id)


def write_poke(agent_id: str, text: str, topic: str = "", conversation: str = "") -> Path:
    """Leave a note for this agent's relay (or its live session): a job it started is done.
    Refused past POKES_MAX notes waiting, so a runaway script cannot pile them up."""
    d = private_dir(pokes_dir(agent_id))
    if len(list(d.glob("*.json"))) >= POKES_MAX:
        raise RuntimeError(f"{POKES_MAX} notes already wait for @{agent_id}; not adding more")
    body = {"text": text[:4000], "topic": topic, "conversation": conversation, "ts": time.time()}
    # Unique even within one clock tick (Windows' clock is coarse: two notes written in a row got
    # the same name and the second replaced the first, CI 2026-11-17), and in order.
    global _POKE_SEQ
    _POKE_SEQ += 1
    path = d / f"{time.time_ns():020d}-{_POKE_SEQ:06d}-{os.getpid()}-{secrets.token_hex(3)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body))
    tmp.replace(path)
    return path


def take_pokes(agent_id: str) -> list[dict]:
    """The notes waiting for this agent, oldest first; taking them removes them."""
    out = []
    for f in sorted(pokes_dir(agent_id).glob("*.json")):
        try:
            data = json.loads(f.read_text())
            f.unlink()
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("text"):
            out.append(data)
    return out


def has_pokes(agent_id: str) -> bool:
    d = pokes_dir(agent_id)
    return d.is_dir() and any(d.glob("*.json"))


def listen_path(agent_id: str) -> Path:
    return state_dir() / f"{_checked(agent_id)}.listen"


def read_listen(agent_id: str) -> dict | None:
    try:
        data = json.loads(listen_path(agent_id).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def touch_listen(agent_id: str, nonce: str) -> None:
    """Mark this agent as listened to by a live session (the relay steps aside meanwhile)."""
    p = listen_path(agent_id)
    private_dir(p.parent)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")  # per process: two listeners never race
    tmp.write_text(json.dumps({"nonce": nonce, "pid": os.getpid(), "ts": time.time()}))
    tmp.replace(p)


def listener_active(agent_id: str, now: float | None = None) -> bool:
    data = read_listen(agent_id)
    ts = data.get("ts") if data else None
    if not isinstance(ts, (int, float)):
        return False
    return ((time.time() if now is None else now) - ts) < LISTEN_TTL_S


def restart_path(agent_id: str) -> Path:
    return state_dir() / f"{_checked(agent_id)}.restart"


def request_restart(agent_id: str) -> None:
    """Ask this agent's relay to restart once its current turn is over (see Relay._worker)."""
    p = restart_path(agent_id)
    private_dir(p.parent)
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
            if valid_id(agent_id):  # a path or garbage is no pointer (YGG-35)
                return agent_id, d
    return None


def write_pointer(directory: Path, agent_id: str) -> Path:
    f = directory / POINTER
    f.write_text(agent_id + "\n")
    legacy = directory / LEGACY_POINTER
    if legacy.is_file():  # a 0.4.0 pointer: migrated (never the ~/.ygg store folder)
        legacy.unlink()
    record_binding(directory, agent_id)
    return f


# -- folders this machine connected (YGG-SEC-2026-11-13-01) -------------------- #
#
# A `.agentygg` file says which agent a folder belongs to, but anyone can ship one: a cloned
# repository, or a parent folder. The Claude Code plugin acts as that agent in every session it
# finds one in, so it only honours a pointer this machine wrote itself (`ygg connect`, `ygg
# setup`, `ygg agent use`), recorded here with the folder's real path.


def bindings_path() -> Path:
    return state_dir() / "bindings.json"


def read_bindings() -> dict[str, str]:
    try:
        data = json.loads(bindings_path().read_text())
    except (OSError, ValueError):
        return {}
    return (
        {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(data, dict)
        else {}
    )


def record_binding(directory: Path, agent_id: str) -> None:
    data = read_bindings()
    data[str(directory.resolve())] = agent_id
    p = bindings_path()
    private_dir(p.parent)
    tmp = p.with_name(f"bindings.{os.getpid()}.tmp")  # one per process: two writers never race
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


def trusted_pointer(start: Path | None = None) -> tuple[str, Path] | None:
    """The folder's pointer, only if this machine wrote it (see record_binding): a `.agentygg`
    that came with a repository or sits in a shared parent folder is ignored."""
    found = pointer_for(start)
    if not found:
        return None
    agent_id, directory = found
    return found if read_bindings().get(str(directory.resolve())) == agent_id else None


def resolve(agent_id: str | None = None, start: Path | None = None) -> Identity:
    """Which identity to act as: an explicit id, then `YGG_AGENT_ID`, then the directory's
    `.agentygg` pointer, then the only agent on this machine."""
    agent_id = agent_id or os.environ.get("YGG_AGENT_ID", "").strip() or None
    untrusted = None
    if not agent_id:
        # Only a pointer this machine wrote (YGG-11): one that came with a cloned repository
        # or sits in a shared parent folder never picks which agent speaks.
        found = trusted_pointer(start)
        if found:
            agent_id = found[0]
        else:
            untrusted = pointer_for(start)
    if not agent_id:
        ids = all_ids()
        if len(ids) == 1:
            agent_id = ids[0]
        elif not ids:
            raise LookupError("no agent on this machine yet: run `ygg connect`")
        else:
            names = ", ".join(f"@{i}" for i in ids)
            why = (
                f"its .agentygg ({untrusted[1]}) was not written by this machine, so it is ignored"
                if untrusted
                else "this folder belongs to no agent"
            )
            raise LookupError(
                f"{why}, and this machine has several agents ({names}). "
                "Connect this folder with `ygg connect` (sign in, then pick or create the "
                f"agent), bind it to one of them with `ygg agent use <id>` (for example `ygg "
                f"agent use {ids[0]}`, without the @), or pass --agent <id>"
            )
    return load(agent_id)
