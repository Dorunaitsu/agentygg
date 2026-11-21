"""Who may reach this agent: Hermes' own allowlist for the platform, applied by the plugin to what
reaches the agent besides the message that wakes it.

Hermes checks the sender of each message before it opens a turn (AGENTYGG_ALLOWED_USERS /
AGENTYGG_ALLOW_ALL_USERS, declared at registration, and the global GATEWAY_* ones). Two other
paths carried a refused sender's words anyway (Betty, 2026-11-11): the context gathered into an
admitted message (a system.ygg notice of the refused sender's topic), and the ygg tool reading a
conversation. Both now drop what a refused sender wrote. Exact ids, never @handles.
"""

from __future__ import annotations

from gateway.platforms._shared import get_scoped_secret

_TRUTHY = {"1", "true", "yes", "on"}


def _env(name: str) -> str:
    return (get_scoped_secret(name, "") or "").strip()


def allowed(sender: str, me: str = "") -> bool:
    """Whether Hermes would let `sender` (an Agent'Ygg id) reach this agent; its own words always."""
    if not sender:
        return False
    if me and sender == me:
        return True
    if any(
        _env(n).lower() in _TRUTHY for n in ("AGENTYGG_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS")
    ):
        return True
    listed = ",".join(_env(n) for n in ("AGENTYGG_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS"))
    return sender in {x.strip() for x in listed.split(",") if x.strip()}


def visible(entry: dict, me: str = "") -> dict | None:
    """A context entry as the agent may see it, or None. A message from a refused sender is
    dropped; a system notice quoting other messages (topic_history) is rebuilt from its
    structured history with the allowed senders' lines only, and dropped when none is left."""
    env = entry.get("envelope") or {}
    if not allowed(str(env.get("from") or ""), me):
        return None
    history = ((env.get("payload") or {}).get("data") or {}).get("history")
    if not isinstance(history, list):
        return entry
    kept = [h for h in history if isinstance(h, dict) and allowed(str(h.get("from") or ""), me)]
    if not kept:
        return None
    if len(kept) == len(history):
        return entry
    # A head of our own, never the notice's text: only ids, so nothing quoted survives in it.
    head = (
        f"Earlier messages of topic {env.get('topic_id')} in conversation "
        f"{env.get('conversation_id')}, from senders you allow only:"
    )
    lines = [f"{h.get('from')}: {h.get('text')}" for h in kept]
    payload = {
        **(env.get("payload") or {}),
        "text": "\n".join([head, *lines]),
        "data": {"history": kept},
    }
    return {**entry, "envelope": {**env, "payload": payload}}
