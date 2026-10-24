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
) -> int:
    agent_id = gw.identity.agent_id
    held: list[dict] = []
    deadline = time.monotonic() + timeout if timeout > 0 else None
    backoff = 2.0
    while True:
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
        except GatewayError as e:
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
