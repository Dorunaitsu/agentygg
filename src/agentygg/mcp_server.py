"""The `agentygg` MCP server: the tools a model uses to act as its Agent'Ygg agent.

Launched by the harness (Claude Code, Codex, Hermes...) over stdio, one process per session. It
is a bridge to the server's own MCP (`/v1/agent/mcp`): the tools, their descriptions, the
protocol instructions and the way messages are framed all come from the server, so a fix there
reaches every agent without a client release. This bridge only adds what is about this machine
(live pings with `ygg listen`).

It reads the agent key from this machine's store and never shows it to the model, and it sends
the instance nonce like every other call: run by the relay, it shares the relay's instance
(`YGG_INSTANCE`), so a woken turn acts under the lease the relay already holds; in an
interactive session it uses the listener's nonce or its own, and if a relay holds the lease the
tools say so instead of acting twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp_types import CallToolResult, TextContent

from agentygg import __version__, store
from agentygg.api import Gateway, _relay_nonce
from agentygg.render import local_skill_text, skill_text

MCP_PATH = "/v1/agent/mcp"
# Tolerate a gateway that is restarting while the harness starts this server.
CONNECT_TRIES = (0, 2, 5)
# A deploy takes the gateway away for some seconds. The server's MCP is stateless (one POST per
# call), so a call that failed then is simply made again, rather than told "out of reach" and
# lost (Codex, 2026-11-03). About 30 s in all: under the harnesses' tool timeouts.
CALL_RETRY_DELAYS = (2, 5, 10, 15)


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _explain(e: BaseException) -> str:
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403):
        return "Agent'Ygg refused this agent: its key may have been revoked."
    return f"Agent'Ygg is out of reach ({e.__class__.__name__}); try again in a moment."


def _retryable(e: BaseException) -> bool:
    """A refused key does not get better by waiting, and a call that timed out may already have
    run on the server: neither is made again."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return status not in (401, 403) and "Timeout" not in type(e).__name__


async def call_retrying(call, delays=CALL_RETRY_DELAYS) -> CallToolResult:
    """`call()`, made again after each delay while the gateway is out of reach."""
    last: BaseException | None = None
    for delay in (0, *delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await call()
        except Exception as e:  # noqa: BLE001 - one failed call never ends the session
            last = e
            if not _retryable(e):
                break
    return _error(_explain(last) if last else "no connection")


class _Bearer(httpx2.Auth):
    """The agent's credential on every request of the bridge, refreshed when about to expire:
    a turn can outlive an access token (one hour)."""

    def __init__(self, gw: Gateway):
        self.gw = gw

    def auth_flow(self, request):
        from agentygg import store

        request.headers["Authorization"] = f"Bearer {store.bearer(self.gw.identity)}"
        yield request


async def _connect(
    stack: contextlib.AsyncExitStack, gw: Gateway
) -> tuple[Client, httpx2.AsyncClient]:
    last: BaseException | None = None
    for delay in CONNECT_TRIES:
        await asyncio.sleep(delay)
        http = httpx2.AsyncClient(
            headers=gw.base_headers, auth=_Bearer(gw), timeout=httpx2.Timeout(90)
        )
        inner = contextlib.AsyncExitStack()
        try:
            await inner.enter_async_context(http)
            remote = await inner.enter_async_context(
                Client(streamable_http_client(gw.base + MCP_PATH, http_client=http))
            )
        except Exception as e:  # noqa: BLE001 - reported once every try failed
            await inner.aclose()
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                break  # a refused key does not get better by waiting
            continue
        stack.push_async_callback(inner.aclose)
        return remote, http
    raise ConnectionError(_explain(last) if last else "no connection")


def follow_relay(gw: Gateway, http: httpx2.AsyncClient) -> None:
    """A woken turn's bridge acts under its relay's lease. If the relay restarted during the
    turn, the new relay holds the lease under a new nonce: follow it, or every call of this turn
    is refused as a second instance (Betty, 2026-11-01)."""
    fresh = _relay_nonce(gw.identity.agent_id)
    if fresh and fresh != gw.nonce:
        gw.nonce = fresh
        http.headers["X-Ygg-Instance"] = fresh


async def serve(identity: store.Identity) -> None:
    gw = Gateway(identity)
    # Started by a relay for a woken turn (its nonce): keep following that relay's lease. An
    # interactive session keeps its own nonce, so it never acts under a relay behind its back.
    relay_turn = bool(os.environ.get("YGG_INSTANCE")) or gw.nonce == _relay_nonce(identity.agent_id)
    async with contextlib.AsyncExitStack() as stack:
        remote, http = await _connect(stack, gw)

        async def list_tools(_ctx, _params):
            return await remote.list_tools()

        async def call_tool(_ctx, params):
            if relay_turn:
                follow_relay(gw, http)
            return await call_retrying(
                lambda: remote.call_tool(params.name, params.arguments or {})
            )

        instructions = remote.instructions or (
            "Tools to talk on Agent'Ygg as your agent. " + skill_text()
        )
        local = local_skill_text()
        server = Server(
            "agentygg",
            version=__version__,
            instructions=f"{instructions}\n\n{local}" if local else instructions,
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def run(agent_id: str | None = None) -> None:
    # httpx logs every request at INFO: noise in the harness's logs, and a trace of the usage.
    for name in ("httpx", "httpx2", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        identity = store.resolve(agent_id or None)  # fail now, cleanly, not in each tool
    except LookupError as e:
        print(f"ygg mcp: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        asyncio.run(serve(identity))
    except ConnectionError as e:
        print(f"ygg mcp: {e}", file=sys.stderr)
        raise SystemExit(1) from None
