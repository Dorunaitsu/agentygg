"""Where an agent's identity lives on this machine.

Like an SSH key: the agent key is stored once in `~/.ygg/agents/<agent_id>.json` (mode 0600,
never in a repo), and a working directory points at the agent it belongs to with a `.ygg`
file holding only the agent id (no secret). One identity maps to a role or a project, never
to a terminal session.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_API = "https://api.agentygg.com"
KEY_PREFIX = "yga_"
POINTER = ".ygg"


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
    return Identity(**json.loads(path.read_text()))


def all_ids() -> list[str]:
    d = agents_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def remove(agent_id: str) -> bool:
    path = agents_dir() / f"{agent_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def pointer_for(start: Path | None = None) -> tuple[str, Path] | None:
    """The agent id a directory belongs to: the nearest `.ygg` file up the tree."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        f = d / POINTER
        if f.is_file():
            agent_id = f.read_text().strip()
            if agent_id:
                return agent_id, d
    return None


def write_pointer(directory: Path, agent_id: str) -> Path:
    f = directory / POINTER
    f.write_text(agent_id + "\n")
    return f


def resolve(agent_id: str | None = None, start: Path | None = None) -> Identity:
    """Which identity to act as: an explicit id, then `YGG_AGENT_KEY` / `YGG_AGENT_ID`, then
    the directory's `.ygg` pointer, then the only agent on this machine."""
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
