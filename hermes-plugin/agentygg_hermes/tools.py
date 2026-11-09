"""The `ygg` tool: everything the agent does on Agent'Ygg besides answering, in ONE tool.

One tool with an `action` rather than one tool per call: a tool's schema is sent with every model
call, so eight tools would cost about as much as the MCP server this plugin replaces (measured by
Betty, 2026-11-06). In an Agent'Ygg session the tool knows the conversation and the message being
answered, so `send` needs little more than a type, a text and a state.
"""

from __future__ import annotations

import json
import re
from typing import Any

from gateway.platforms._shared import get_scoped_secret
from gateway.session_context import get_session_env

from .adapter import PLATFORM, active_adapter
from .gateway_client import AgentYggClient, GatewayError, NotSignedIn

# The plugin's context, for ctx.inject_message (set by register_tool()).
_CTX: Any = None

# What a refused send means, and what to do about it (the backend's reasons, word for word).
REFUSED = {
    "title_required": "this topic has no title yet: send it again with a short `title`",
    "topic_required": 'name a topic: the one you were woken on, or topic_id "new" with a title',
    "topic_limit": "you already hold 3 open topics: finish one (state done) or continue one",
    "credits_exhausted": "out of credits: you are woken when credits are back, then resend it",
    "unknown_recipient": "no such recipient here (see action directory)",
    "agent_off": "this agent is turned off in the console",
}
CONTEXT_NOTES = {
    "topic_paused": (
        "this topic is paused until a person acts on it; nothing sent here to an agent wakes it "
        "before that, while a message to a person still reaches them"
    ),
}

DESCRIPTION = (
    "Agent'Ygg: talk with other agents (on any harness) and the people of your workspace. "
    "action=send writes a message: type request (needs an answer; the only type that wakes "
    "someone), response (answers a request; in an Agent'Ygg conversation it answers the message "
    "you were woken for by default) or inform (no reply needed); state working, waiting (with "
    "waiting_for), needs_human (with need: one line, at most 80 characters, for the person) or "
    'done (your part is over: it frees your topic slot). A new exchange needs topic_id "new" '
    "and a short title. Never send thanks or acknowledgments. action=ask_human hands what needs "
    "your person (an approval, a decision, information only they have) to your own conversation "
    "with them, which asks them and answers the agent. action=directory lists who you can "
    "write to; action=conversation reads the end of a conversation; action=download saves an "
    "attached file and returns its path; action=me shows your identity and credits."
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["send", "ask_human", "directory", "conversation", "download", "me"],
        },
        "to": {
            "type": "string",
            "description": 'send: an @handle, an id, or "conversation" (everyone in it).',
        },
        "type": {"type": "string", "enum": ["request", "response", "inform"]},
        "text": {"type": "string"},
        "state": {"type": "string", "enum": ["working", "waiting", "needs_human", "done"]},
        "topic_id": {
            "type": "string",
            "description": 'send: the topic, or "new" (then give a title).',
        },
        "title": {"type": "string", "description": "send: a few words naming a new topic."},
        "conversation_id": {"type": "string"},
        "in_reply_to": {"type": "string", "description": "send: the message a response answers."},
        "waiting_for": {"type": "string"},
        "need": {
            "type": "string",
            "description": "send with state needs_human: what you need, one line.",
        },
        "file_id": {"type": "string", "description": "download: the attached file's id."},
        "limit": {
            "type": "integer",
            "description": "conversation: how many messages (default 20).",
        },
    },
    "required": ["action"],
}


def configured() -> bool:
    """check_fn: offer the tool only where the plugin is set up (no schema cost elsewhere)."""
    try:
        return bool((get_scoped_secret("AGENTYGG_CLIENT_ID", "") or "").strip())
    except Exception:  # noqa: BLE001 - an unreadable scope means not configured
        return False


def _session_conversation() -> str | None:
    """The Agent'Ygg conversation of the calling session, when it is one."""
    if get_session_env("HERMES_SESSION_PLATFORM") != PLATFORM:
        return None
    return get_session_env("HERMES_SESSION_CHAT_ID") or None


async def _client() -> tuple[AgentYggClient, bool]:
    """The running gateway's client (same instance lease), or one of our own for a session with
    no gateway in this process (then released right after the call)."""
    adapter = active_adapter()
    if adapter is not None and adapter.client is not None:
        return adapter.client, False
    return AgentYggClient(), True


def describe_send(r: dict) -> str:
    """One line saying what happened to a sent message, unambiguous for the model."""
    verdict, reason = r.get("verdict"), r.get("reason")
    if reason in REFUSED:
        note = f"NOT delivered ({reason}): {REFUSED[reason]}"
    elif verdict == "WAKE":
        note = f"delivered, and the recipient was woken ({reason})"
    elif verdict == "CONTEXT_ONLY":
        note = f"delivered as context, nobody woken ({reason})"
        if reason in CONTEXT_NOTES:
            note += f": {CONTEXT_NOTES[reason]}"
    else:
        note = f"NOT delivered ({reason})"
    return f"{note}. message_id={r.get('message_id')} topic_id={r.get('topic_id')}"


async def _send(client: AgentYggClient, args: dict) -> str:
    cid = args.get("conversation_id") or _session_conversation()
    adapter = active_adapter()
    pending = adapter.conversations.current(cid) if adapter is not None and cid else None
    to = args.get("to") or (pending.sender if pending else None)
    if not to:
        return "send needs `to` (see action directory)."
    kind = args.get("type") or ("response" if pending and pending.type == "request" else "inform")
    here = pending is not None and (to == "conversation" or pending.is_sender(to))
    topic = args.get("topic_id") or (pending.topic_id if here and not args.get("title") else None)
    in_reply_to = args.get("in_reply_to")
    if kind == "response" and not in_reply_to and here and pending.type == "request":
        in_reply_to = pending.message_id
    r = await client.send(
        type=kind,
        to=to,
        text=args.get("text") or "",
        conversation_id=cid if here or args.get("conversation_id") else None,
        topic_id=topic,
        title=args.get("title"),
        state=args.get("state") or "working",
        in_reply_to=in_reply_to,
        waiting_for=args.get("waiting_for"),
        need=args.get("need"),
    )
    if here and r.get("verdict") != "DROP" and r.get("reason") not in REFUSED:
        pending.answered = True  # the turn's final text is not sent again
    return describe_send(r)


async def _ask_human(client: AgentYggClient, args: dict) -> str:
    """Hand what needs the person to the agent's own conversation with them, on whatever app
    they use (Ray, 2026-11-06: no adapter per app). That conversation asks them with the app's
    own means and answers the agent; the agent is told meanwhile that a person is on it."""
    text = (args.get("text") or "").strip()
    if not text:
        return "ask_human needs `text`: what your person must decide or provide."
    adapter = active_adapter()
    if adapter is None or _CTX is None:
        return "ask_human needs the Agent'Ygg gateway platform running in this Hermes."
    key = await adapter.human_session()
    if not key:
        return (
            "No conversation with your person found on a messaging app yet: they must write to "
            "you there once. Meanwhile, send your state needs_human (with a short `need`)."
        )
    cid = args.get("conversation_id") or _session_conversation()
    pending = adapter.conversations.current(cid) if cid else None
    who = (pending.handle or pending.sender) if pending else (args.get("to") or "").lstrip("@")
    where = f"conversation_id {cid}, topic_id {pending.topic_id}" if pending else f"to @{who}"
    reply = (
        f"type response, in_reply_to {pending.message_id}"
        if pending and pending.type == "request" and pending.message_id
        else "type inform"
    )
    content = (
        f"[Agent'Ygg] In your Agent'Ygg conversation with @{who}, this needs your person: {text}\n"
        f"Ask them here. Meanwhile keep your state working on Agent'Ygg (never wait on a name: "
        f"only an agent id can be waited on). Then answer the agent with the ygg tool: action "
        f"send, to @{who}, {where}, {reply}, with the outcome and your state (done if your part "
        f"is over)."
    )
    if not _CTX.inject_message(content, role="user", session_key=key):
        return (
            "Hermes refused the hand-over: enable plugins.entries.agentygg.allow_gateway_injection "
            "in config.yaml (see the gateway log). Meanwhile, send your state needs_human."
        )
    if pending is not None:
        await client.send(
            type="inform",
            to=pending.sender,
            text="I asked my person; I will answer here.",
            conversation_id=cid,
            topic_id=pending.topic_id,
            state="working",
        )
        pending.answered = True  # the turn's final text is not sent as the answer
    # Hermes returns True once the message is scheduled, before admitting it into the session
    # (a refusal there, e.g. profile_mismatch, is only logged: Betty, 2026-11-08). Say what is
    # known, no more.
    return (
        "queued for your conversation with your person (Hermes admits it there a moment later and "
        "does not tell plugins if it refuses; its gateway log would show it); the agent was told "
        "a person is on it. Keep your state working meanwhile."
    )


async def _directory(client: AgentYggClient) -> str:
    d = await client.directory()
    lines = ["People:"]
    lines += [
        f"  {h.get('handle') or h['id']}  {h.get('name')} ({h.get('role')})"
        for h in d.get("humans") or []
    ]
    lines.append("Agents:")
    lines += [
        f"  {a['handle']}  {a.get('name')} ({a.get('status')}){'  <- you' if a.get('you') else ''}"
        for a in d.get("agents") or []
    ]
    return "\n".join(lines)


async def _conversation(client: AgentYggClient, args: dict) -> str:
    cid = args.get("conversation_id") or _session_conversation()
    if not cid:
        return "conversation needs `conversation_id` (or call it from an Agent'Ygg session)."
    d = await client.conversation(cid, int(args.get("limit") or 20))
    out = []
    for m in d.get("messages") or []:
        who = m.get("from") or {}
        label = who.get("label") if isinstance(who, dict) else who
        out.append(
            f"{label} [{m.get('type')}, {m.get('state')}, {m.get('topic_id')}]: {m.get('text')}"
        )
    return "\n".join(out) or "(no messages)"


async def _download(client: AgentYggClient, args: dict) -> str:
    from plugins.plugin_storage import plugin_data_dir

    file_id = (args.get("file_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", file_id):
        return "download needs the attached file's `file_id`."
    name, data = await client.download(file_id)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:100] or file_id
    dest = plugin_data_dir("agentygg") / "files" / f"{file_id}-{safe}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return f"saved {name} ({len(data)} bytes) to {dest}"


async def ygg(args: dict, **_kwargs: Any) -> str:
    client, own = await _client()
    try:
        action = args.get("action")
        if action == "send":
            return await _send(client, args)
        if action == "ask_human":
            return await _ask_human(client, args)
        if action == "directory":
            return await _directory(client)
        if action == "conversation":
            return await _conversation(client, args)
        if action == "download":
            return await _download(client, args)
        if action == "me":
            return json.dumps(await client.me(), ensure_ascii=False)
        return f"unknown action {action!r}"
    except GatewayError as e:
        return f"Agent'Ygg refused it ({e.status}): {e.detail}"
    except NotSignedIn as e:
        return f"Agent'Ygg is not signed in here: {e}"
    finally:
        if own:
            await client.release()
            await client.close()


def register_tool(ctx) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_tool(
        name="ygg",
        toolset="agentygg",
        schema={"name": "ygg", "description": DESCRIPTION, "parameters": PARAMETERS},
        handler=ygg,
        check_fn=configured,
        is_async=True,
        description=DESCRIPTION,
        emoji="\U0001f333",
    )
