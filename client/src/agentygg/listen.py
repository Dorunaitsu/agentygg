"""`ygg listen`: live pings inside a session a human is having with the agent.

The relay answers pings when nobody is there, each in a turn of its own. When a human is
talking with the agent in an interactive session, that session should see its pings itself,
with the whole conversation in mind. It runs `ygg listen` as a background command: the command
waits on the gateway (server-side long poll, nothing is spent while it waits) and exits as soon
as a human or an agent writes, printing what arrived. The harness then wakes the session on that
exit; the session answers and starts `ygg listen` again.

While a session listens, a relay for the same agent on this machine steps aside (see
`store.listener_active`), so the two never race for the same wake. System notices (closure
polls, nudges...) do not end the wait: they are kept and printed with the next real message.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from collections.abc import Callable

import httpx

from agentygg import store
from agentygg.api import Gateway, GatewayError, _relay_nonce
from agentygg.render import render_message

POLL_S = 25.0  # the gateway's inbox wait cap

# Exit codes.
GOT_MESSAGE = 0
NOTHING = 1  # --timeout reached with nothing
STOPPED = 2  # the gateway refused us (revoked, busy elsewhere...)


def is_system(frame: dict) -> bool:
    who = frame.get("from") or {}
    env = frame.get("envelope") or {}
    return who.get("kind") == "system" or env.get("from") == "system.ygg"


def listen_nonce(agent_id: str) -> str:
    """Which instance to listen as: the session's own (`YGG_INSTANCE`), a running relay's, or
    the previous listener's, so relaunching `ygg listen` never collides with our own lease."""
    prev = (store.read_listen(agent_id) or {}).get("nonce")
    return (
        os.environ.get("YGG_INSTANCE")
        or _relay_nonce(agent_id)
        or (prev if isinstance(prev, str) else None)
        or secrets.token_hex(8)
    )


async def listen(
    gw: Gateway,
    *,
    include_system: bool = False,
    timeout: float = 0.0,
    out: Callable[[str], None] = print,
    retry_cap: float = 30.0,
    busy_grace: float = 75.0,
    alive: Callable[[], bool] | None = None,
) -> int:
    agent_id = gw.identity.agent_id
    held: list[dict] = []
    deadline = time.monotonic() + timeout if timeout > 0 else None
    backoff = 2.0
    busy_since: float | None = None
    while True:
        if alive is not None and not alive():
            # Whoever started us is gone (an OpenCode closed without stopping its plugin): stop
            # taking messages nobody would read, and hand the pings back to the relay at once.
            store.clear_listen(agent_id)
            return STOPPED
        store.touch_listen(agent_id, gw.nonce)
        wait = POLL_S
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            wait = min(wait, left)
        try:
            got = await gw.inbox(wait=wait)
            backoff = 2.0
            busy_since = None
        except GatewayError as e:
            if e.status == 409:
                # Another instance holds the lease. Often our own session a moment ago (a send
                # under another nonce): it lapses within the lease TTL, so wait that long first.
                now = time.monotonic()
                busy_since = busy_since if busy_since is not None else now
                if now - busy_since < busy_grace:
                    await asyncio.sleep(min(5.0, retry_cap) if retry_cap else 0)
                    continue
            if e.status in (401, 403, 409):
                for f in held:
                    out(render_message(f))
                out(f"ygg listen stopped: {e.detail}")
                return STOPPED
            await asyncio.sleep(min(backoff, retry_cap))  # gateway restarting: try again
            backoff *= 2
            continue
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(min(backoff, retry_cap))  # network down: try again
            backoff *= 2
            continue
        frames = (got.get("wakes") or []) + (got.get("mailbox") or [])
        held.extend(frames)
        if any(include_system or not is_system(f) for f in frames):
            for f in held:
                out(render_message(f))
            return GOT_MESSAGE
    for f in held:
        out(render_message(f))
    if not held:
        out("Nothing new.")
    return GOT_MESSAGE if held else NOTHING


# -- `ygg listen --follow`: the Claude Code plugin's monitor -------------------- #

# What leads each delivery in a session the plugin listens for: the monitor's output reaches the
# model as a notification, so it says where the messages come from and how to answer.
FOLLOW_LEAD = (
    "Agent'Ygg delivered this while you work (the agentygg plugin listens for you). Answer it "
    "with the agentygg tools, in its topic, if it calls for an answer:"
)
STOP_PREFIX = "ygg listen stopped: "
# Claude Code cuts a monitor notification at about 500 characters per line and 3,000 in all
# (measured 2026-11-13: "...(truncated)"). A delivery is shaped to fit: long lines are wrapped,
# and a message too long for its share is cut here, saying where to read it whole.
MONITOR_LINE = 300
MONITOR_TOTAL = 2400
MONITOR_MIN_BODY = 300
_MSG_ID = re.compile(r'message_id="([^"]+)"')
_CONV_ID = re.compile(r'conversation_id="([^"]+)"')


def _wrap(line: str, width: int = MONITOR_LINE) -> list[str]:
    """A line split at spaces into pieces of at most `width` characters (a word longer than that
    is split too)."""
    out: list[str] = []
    while len(line) > width:
        cut = line.rfind(" ", 0, width + 1)
        cut = cut if cut > width // 2 else width
        out.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    out.append(line)
    return out


def fit_message(rendered: str, budget: int) -> str:
    """One rendered message shaped for a monitor notification: its frame kept whole, its body
    wrapped and, past `budget` characters, cut with a line saying how to read the rest."""
    lines = rendered.split("\n")
    if len(lines) < 2 or not lines[-1].startswith("</agentygg-message"):
        return "\n".join(p for ln in lines for p in _wrap(ln))
    head, body, tail = lines[0], lines[1:-1], lines[-1]
    kept: list[str] = []
    used = 0
    cut = False
    for piece in (p for ln in body for p in _wrap(ln)):
        if used + len(piece) > budget:
            room = budget - used
            if room > 40:
                kept.append(_wrap(piece, room)[0])
            cut = True
            break
        kept.append(piece)
        used += len(piece) + 1
    if cut:
        mid = _MSG_ID.search(head)
        conv = _CONV_ID.search(head)
        where = f" (message_id {mid.group(1)})" if mid else ""
        how = f"ygg_conversation on {conv.group(1)}" if conv else "ygg_conversation"
        kept.append(f"[cut here to fit the notification: read the whole message{where} with {how}]")
    return "\n".join([head, *kept, tail])


def fit_delivery(messages: list[str]) -> str:
    """A delivery that fits a monitor notification: the lead, then each message with an equal
    share of what is left (each one keeps at least a short preview)."""
    overhead = sum(len(m.split("\n")[0]) + 25 for m in messages)
    share = max(MONITOR_MIN_BODY, (MONITOR_TOTAL - len(FOLLOW_LEAD) - overhead) // len(messages))
    return FOLLOW_LEAD + "\n\n" + "\n\n".join(fit_message(m, share) for m in messages)


FOLLOW_RETRY_S = 60.0
# A refusal reported to the session only once it lasts: a one-off 401 while the server restarts
# must not wake the model for nothing (2026-11-13).
FOLLOW_REPORT_AFTER = 3


async def follow(
    gw: Gateway,
    *,
    out: Callable[[str], None] = print,
    retry_s: float = FOLLOW_RETRY_S,
    alive: Callable[[], bool] | None = None,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> int:
    """Listen for the whole session: each message is printed as it arrives (one block per
    delivery, its system notices with it), and listening goes on. Nothing is printed while
    waiting, and a refusal (revoked, signed out, another instance busy) is retried every
    `retry_s`, told to the session once if it lasts, so the monitor never wakes the model for a
    hiccup. Returns only when `alive` says the session is gone."""
    refused = 0
    reported: str | None = None
    while True:
        if alive is not None and not alive():
            store.clear_listen(gw.identity.agent_id)
            return STOPPED
        lines: list[str] = []
        code = await listen(gw, out=lines.append, alive=alive)
        stop = code == STOPPED and bool(lines) and lines[-1].startswith(STOP_PREFIX)
        messages = lines[:-1] if stop else lines
        if messages:
            out(fit_delivery(messages))
        if code != STOPPED:
            refused, reported = 0, None
            continue
        if alive is not None and not alive():
            store.clear_listen(gw.identity.agent_id)
            return STOPPED
        refused += 1
        detail = lines[-1][len(STOP_PREFIX) :] if stop else "refused"
        if refused >= FOLLOW_REPORT_AFTER and detail != reported:
            out(
                f"Agent'Ygg: messages are not reaching this session for now ({detail}). "
                "The plugin keeps trying every minute; nothing to do unless it lasts."
            )
            reported = detail
        await sleep(retry_s)
