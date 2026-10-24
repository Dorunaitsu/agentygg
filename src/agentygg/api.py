"""The agent gateway, client side: HTTP calls and the wake stream."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agentygg import __version__
from agentygg.store import Identity


class GatewayError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
        return d if isinstance(d, str) else json.dumps(d)
    except (ValueError, AttributeError):  # not a JSON object: show the raw body
        return r.text[:300]


def _relay_nonce(agent_id: str) -> str | None:
    """The instance nonce a running relay published for this agent (see relay.instance_path),
    so tools started by a harness that does not pass its environment on still act under the
    relay's lease instead of being refused as a second instance."""
    from agentygg import store

    p = store.state_dir() / f"{agent_id}.instance"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    pid = data.get("pid")
    # On Windows os.kill(pid, 0) would SEND Ctrl+C (signal 0 is CTRL_C_EVENT there), so the
    # liveness check is POSIX-only; a stale file only means the next call is refused as busy.
    if isinstance(pid, int) and pid > 0 and os.name != "nt":
        try:
            os.kill(pid, 0)  # still running?
        except ProcessLookupError:
            return None
        except (PermissionError, OSError):
            pass
    nonce = data.get("nonce")
    return nonce if isinstance(nonce, str) else None


def _listen_nonce(agent_id: str) -> str | None:
    """The instance nonce of a live session listening for this agent (`ygg listen`), so the
    `ygg send` / MCP calls of that same session act under its lease."""
    from agentygg import store

    if not store.listener_active(agent_id):
        return None
    nonce = (store.read_listen(agent_id) or {}).get("nonce")
    return nonce if isinstance(nonce, str) else None


class Gateway:
    """One live instance of an agent. `nonce` identifies this instance to the backend's lease;
    processes that act as the same instance (the relay and the MCP server it launches) share
    it through `YGG_INSTANCE`."""

    def __init__(self, identity: Identity, nonce: str | None = None):
        self.identity = identity
        self.nonce = (
            nonce
            or os.environ.get("YGG_INSTANCE")
            or _relay_nonce(identity.agent_id)
            or _listen_nonce(identity.agent_id)
        )
        self.nonce = self.nonce or secrets.token_hex(8)
        self.base = identity.api.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base, headers=self.headers, timeout=60)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.identity.key}",
            "X-Ygg-Instance": self.nonce,
            "User-Agent": f"agentygg/{__version__}",
        }

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw) -> Any:
        r = await self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise GatewayError(r.status_code, _detail(r))
        return r.json()

    async def me(self) -> dict:
        return await self._call("GET", "/v1/agent/me")

    async def inbox(self, wait: float = 0.0) -> dict:
        return await self._call("GET", "/v1/agent/inbox", params={"wait": wait})

    async def send(self, **envelope: Any) -> dict:
        if envelope.get("title") and not envelope.get("topic_id"):
            envelope["topic_id"] = "new"  # a title names a new topic
        body = {k: v for k, v in envelope.items() if v not in (None, "", [], {})}
        return await self._call("POST", "/v1/agent/messages", json=body)

    async def conversation(self, conversation_id: str, limit: int = 50) -> dict:
        return await self._call(
            "GET", f"/v1/agent/conversations/{conversation_id}", params={"limit": limit}
        )

    async def directory(self) -> dict:
        return await self._call("GET", "/v1/agent/directory")

    async def tasks(self) -> dict:
        return await self._call("GET", "/v1/agent/tasks")

    async def release(self) -> None:
        # Best effort on the way out: the lease expires by itself anyway.
        with contextlib.suppress(GatewayError, httpx.HTTPError):
            await self._call("POST", "/v1/agent/release")

    async def stream(self) -> AsyncIterator[dict]:
        """The wake stream: yields `hello`, `wake`, `context` and `ping` frames until the
        server closes it. Raises GatewayError on a refused connection (bad key, busy...)."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        url = self.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        try:
            async with connect(
                f"{url}/v1/agent/stream",
                additional_headers=self.headers,
                open_timeout=30,
                ping_interval=20,
                max_size=8 * 1024 * 1024,
            ) as ws:
                async for raw in ws:
                    yield json.loads(raw)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else 1006
            if code >= 4000:
                raise GatewayError(code - 4000, _close_reason(code - 4000)) from e
        except InvalidStatus as e:
            raise GatewayError(e.response.status_code, "the gateway refused the connection") from e


def _close_reason(status: int) -> str:
    return {
        401: "this agent key is not valid (revoked, or the agent was deleted)",
        403: "this agent is revoked, or its workspace is suspended",
        409: "identity_busy: another instance of this agent is already connected",
        400: "bad request",
    }.get(status, "closed by the server")
