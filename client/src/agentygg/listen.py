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
# A listener that has not reached the gateway for this long stops claiming the agent (the relay
# takes over within its 90 s), and after GIVE_UP_S it stops and says so. Claudeo, 2026-11-17: a
# listener whose client was replaced under it (`uv tool install --force` mid-session) failed
# every call for an hour while still marking "a session listens", so the relay stayed paused
# and nothing was delivered at all.
CLAIM_FOR_S = 60.0
GIVE_UP_S = 300.0  # longer than a deploy of the gateway (2026-11-17: 2 min was not)
UNREACHED = "cannot reach Agent'Ygg"
# When this process last reached the gateway, per agent: kept across `follow`'s calls.
_LAST_OK: dict[str, float] = {}

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


def _meanwhile(agent_id: str) -> str:
    """What happens to this agent's messages once the live session gives up: a relay installed
    here answers them; without one they wait on the server for the next session."""
    if (store.home() / "relays" / f"{agent_id}.json").exists():
        return "(the relay answers meanwhile)"
    return (
        "(no relay for this agent on this machine: messages wait until a session listens again; "
        "`ygg relay install` here answers them while you are away)"
    )


async def _confirm(gw: Gateway, batch: str) -> None:
    """Tell the server the batch is delivered, so it neither keeps it nor gives it to the relay. A
    few quick tries; failing costs at most a second delivery, never the message."""
    for pause in (0.0, 0.5, 2.0):
        await asyncio.sleep(pause)
        try:
            await gw.confirm(batch)
            return
        except GatewayError:
            return  # an older server (404), or it refuses us: the next poll says why
        except (httpx.HTTPError, OSError):
            continue


async def listen(
    gw: Gateway,
    *,
    include_system: bool = False,
    timeout: float = 0.0,
    out: Callable[[str], None] = print,
    retry_cap: float = 30.0,
    busy_grace: float = 75.0,
    alive: Callable[[], bool] | None = None,
    ack: str = "",
    taken: list[str] | None = None,
) -> int:
    """`ack`: the batch to confirm with the first poll. `taken`: the caller delivers and confirms
    itself (follow); the batch to confirm goes there instead of being confirmed here."""
    agent_id = gw.identity.agent_id
    held: list[dict] = []
    batch = ack  # the last one taken: the next poll confirms it
    deadline = time.monotonic() + timeout if timeout > 0 else None
    backoff = 2.0
    busy_since: float | None = None
    _LAST_OK.setdefault(agent_id, time.monotonic())
    while True:
        if alive is not None and not alive():
            # Whoever started us is gone (an OpenCode closed without stopping its plugin): stop
            # taking messages nobody would read, and hand the pings back to the relay at once.
            store.clear_listen(agent_id)
            return STOPPED
        failing_for = time.monotonic() - _LAST_OK[agent_id]
        if failing_for < CLAIM_FOR_S:
            store.touch_listen(agent_id, gw.nonce)
        elif failing_for >= GIVE_UP_S:
            store.clear_listen(agent_id)  # the relay answers again at once
            for f in held:
                out(render_message(f))
            out(f"ygg listen stopped: {UNREACHED} {_meanwhile(agent_id)}")
            return STOPPED
        wait = POLL_S
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            wait = min(wait, left)
        try:
            got = await gw.inbox(wait=wait, ack=batch)
            batch = got.get("batch") or ""
            backoff = 2.0
            busy_since = None
            _LAST_OK[agent_id] = time.monotonic()
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
            # Delivered first, confirmed after: a delivery that fails is handed over again.
            if taken is not None:
                taken.append(batch)
            elif batch:
                await _confirm(gw, batch)
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
    "Agent'Ygg delivered this while you work (the agentygg plugin listens for you). A request "
    "waits for your answer: ygg_send type response, in its topic, in_reply_to its message_id. An "
    "inform or a response needs no answer unless you have something to add, and a notice from "
    "Agent'Ygg itself (from system.ygg) never does:"
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
    shape: Callable[[list[str]], str] = fit_delivery,
    settle_s: float = 0.0,
) -> int:
    """Listen for the whole session: each message is printed as it arrives (one block per
    delivery, its system notices with it), and listening goes on. Nothing is printed while
    waiting, and a refusal (revoked, signed out, another instance busy) is retried every
    `retry_s`, told to the session once if it lasts, so the monitor never wakes the model for a
    hiccup. A delivery is confirmed to the server once the session outlived it by `settle_s`: one
    made to a session that is closing is the relay's to answer. `shape` makes one delivery of the messages (a monitor's cut to fit, by default).
    Returns only when `alive` says the session is gone."""
    refused = 0
    reported: str | None = None
    batch = ""
    while True:
        if alive is not None and not alive():
            store.clear_listen(gw.identity.agent_id)
            return STOPPED
        # Notes a job on this machine left (`ygg relay poke`): this session gets them, the
        # relay steps aside while it listens.
        notes = store.take_pokes(gw.identity.agent_id)
        if notes:
            from agentygg.render import render_poke

            out(shape([render_poke(n) for n in notes]))
        lines: list[str] = []
        taken: list[str] = []
        code = await listen(gw, out=lines.append, alive=alive, ack=batch, taken=taken)
        batch = taken[-1] if taken else ""
        stop = code == STOPPED and bool(lines) and lines[-1].startswith(STOP_PREFIX)
        messages = lines[:-1] if stop else lines
        if messages:
            out(shape(messages))
        if batch:
            await sleep(settle_s)
            if alive is not None and not alive():
                store.clear_listen(gw.identity.agent_id)
                return STOPPED
            await _confirm(gw, batch)
        if code != STOPPED:
            refused, reported = 0, None
            continue
        if alive is not None and not alive():
            store.clear_listen(gw.identity.agent_id)
            return STOPPED
        refused += 1
        detail = lines[-1][len(STOP_PREFIX) :] if stop else "refused"
        # Unreachable for minutes already: say it now, not after more rounds.
        lasting = refused >= FOLLOW_REPORT_AFTER or detail.startswith(UNREACHED)
        if lasting and detail != reported:
            out(
                f"Agent'Ygg: messages are not reaching this session for now ({detail}). "
                "The plugin keeps trying every minute; nothing to do unless it lasts."
            )
            reported = detail
        await sleep(retry_s)
