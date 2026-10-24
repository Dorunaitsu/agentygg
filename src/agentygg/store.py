"""Where an agent's identity lives on this machine.

Like an SSH key: the agent key is stored once in `~/.ygg/agents/<agent_id>.json` (mode 0600,
never in a repo), and a working directory points at the agent it belongs to with a `.agentygg`
file holding only the agent id (no secret). One identity maps to a role or a project, never
to a terminal session.
"""

from __future__ import annotations

import base64
import json
import os
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
    key: str
    api: str = DEFAULT_API
    display_name: str = ""
    workspace_name: str = ""
    # The harness this agent runs on (set by `ygg setup <harness>`); the relay uses it when no
    # --harness is given.
    harness: str = ""
    harness_path: str = ""


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
        raise LookupError(f"no agent @{agent_id} on this machine (add it with: ygg agent add)")
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
    """Which identity to act as: an explicit id, then `YGG_AGENT_KEY` / `YGG_AGENT_ID`, then
    the directory's `.agentygg` pointer, then the only agent on this machine."""
    env_key = os.environ.get("YGG_AGENT_KEY", "").strip()
    if not agent_id and env_key:
        ws, aid = decode_key(env_key)
        return Identity(aid, ws, env_key, os.environ.get("YGG_API", DEFAULT_API))
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
            raise LookupError("no agent on this machine yet: run `ygg agent add <key>`")
        else:
            raise LookupError(
                "several agents on this machine: pick one with --agent, or run "
                "`ygg agent use <id>` in this directory"
            )
    return load(agent_id)
