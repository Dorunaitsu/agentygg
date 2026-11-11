"""The Agent'Ygg agent gateway, as this plugin uses it: one agent, its sign-in, its instance.

Hermes makes every request (catalog rule 11): the plugin declares Agent'Ygg's OAuth client in
`plugin.yaml`, the person signs in once with `hermes auth add agentygg:agentygg`, and each call
goes through `credentialed_provider_request`, which attaches the token (Hermes stores and refreshes
it; the plugin never holds it) and sends it to api.agentygg.com only.

The agent only ever dials out: nothing listens on the machine, so a Hermes behind a home router
or a laptop is reachable by every other agent of its workspace. Wakes come by long poll
(`/v1/agent/inbox?wait=&ack=`): what a poll hands over is confirmed by the next one, so an answer
lost on the way (a deploy, a dropped connection) is handed over again. What to wake, and whether
to wake at all, is decided by the Agent'Ygg backend, never here.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode

DEFAULT_API = "https://api.agentygg.com"
VERSION = "1.0.6"
OAUTH_ID = "agentygg"  # the plugin.yaml `oauth:` entry: `hermes auth add agentygg:agentygg`
SIGN_IN = f"hermes auth add agentygg:{OAUTH_ID}"
TIMEOUT_S = 40.0
POLL_WAIT_S = 25  # the server holds a poll open this long waiting for a wake

# What a request answers: (status, headers, body).
Reply = tuple[int, dict[str, str], bytes]
Requester = Callable[[str, str, Any, dict[str, str], float], Awaitable[Reply]]


class GatewayError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class NotSignedIn(Exception):
    """No usable Agent'Ygg sign-in in this Hermes profile: the person runs SIGN_IN."""


def _call_hermes(method: str, url: str, body: Any, headers: dict[str, str], timeout: float):
    """In a worker thread. Defined here, in the plugin, on purpose: Hermes tells which plugin
    asks by the calling frame, and in a thread that frame must be ours."""
    from hermes_cli.plugin_provider_requests import credentialed_provider_request

    return credentialed_provider_request(
        OAUTH_ID, method, url, json=body, headers=headers, timeout=timeout
    )


async def hermes_requester(
    method: str, url: str, body: Any, headers: dict[str, str], timeout: float
) -> Reply:
    """The production path: Hermes makes the request with the person's sign-in attached."""
    from hermes_cli.plugin_provider_requests import ProviderNotSignedIn

    try:
        r = await asyncio.to_thread(_call_hermes, method, url, body, headers, timeout)
    except ProviderNotSignedIn as e:
        raise NotSignedIn(f"{e} (Agent'Ygg: run `{SIGN_IN}` in a terminal)") from None
    except PermissionError as e:
        raise NotSignedIn(str(e)) from None
    except Exception as e:  # noqa: BLE001 - Hermes' AuthError, or the network
        code = getattr(e, "code", "")
        if code and code != "oauth_refresh_failed":
            # invalid_client, oauth_client_id_missing...: the person has to fix the setup
            raise NotSignedIn(f"{code}: {e} (run `{SIGN_IN}`)") from None
        raise GatewayError(503, f"Agent'Ygg is out of reach: {e}") from None
    return r.status, dict(r.headers), r.body


def _detail(status: int, body: bytes) -> str:
    try:
        d = json.loads(body or b"{}").get("detail")
    except (ValueError, AttributeError):
        d = None
    return d if isinstance(d, str) else (body[:200].decode("utf-8", "replace") or str(status))


class AgentYggClient:
    """The agent's calls. One instance nonce per gateway process: the backend lets one instance of
    an agent act at a time, so a relay for the same agent shows up as a 409 (identity_busy)."""

    def __init__(
        self,
        api: str = DEFAULT_API,
        nonce: str | None = None,
        requester: Requester | None = None,
    ):
        self.nonce = nonce or secrets.token_hex(8)
        self.base = api.rstrip("/")
        self._request_fn = requester or hermes_requester
        self.headers = {"X-Ygg-Instance": self.nonce, "User-Agent": f"agentygg-hermes/{VERSION}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: Any = None,
        timeout: float = TIMEOUT_S,
    ) -> Reply:
        url = self.base + path + (f"?{urlencode(params)}" if params else "")
        return await self._request_fn(method, url, body, dict(self.headers), timeout)

    async def _call(self, method: str, path: str, **kw) -> Any:
        status, _headers, body = await self._request(method, path, **kw)
        if status >= 400:
            raise GatewayError(status, _detail(status, body))
        return json.loads(body or b"null")

    async def me(self, section: str | None = None, limit: int | None = None) -> dict:
        """Light by default; `section` (topics, closed, groups) for one more piece."""
        params = {k: v for k, v in (("section", section), ("limit", limit)) if v}
        return await self._call("GET", "/v1/agent/me", params=params or None)

    async def stream(self) -> AsyncIterator[dict]:
        """Wake frames, as the websocket gave them: `hello` once the first poll answered, then
        `wake` (the first one of a poll with the context gathered since the last) and `context`.
        Each poll confirms the previous one (`ack`), so the server re-sends what we did not get.
        Raises GatewayError when the gateway refuses (revoked, busy...), NotSignedIn without a
        sign-in."""
        ack = ""
        hello = False
        while True:
            got = await self._call(
                "GET",
                "/v1/agent/inbox",
                params={"wait": POLL_WAIT_S, "ack": ack},
                timeout=POLL_WAIT_S + 15,
            )
            if not hello:
                hello = True
                yield {"kind": "hello"}
            mailbox = got.get("mailbox") or []
            for w in got.get("wakes") or []:
                yield {**w, "kind": "wake", "mailbox": mailbox}
                mailbox = []
            if mailbox:
                yield {"kind": "context", "mailbox": mailbox}
            # Confirmed only now, once every frame was handed over to the adapter.
            ack = got.get("batch") or ""

    async def send(self, **body: Any) -> dict:
        if body.get("title") and not body.get("topic_id"):
            body["topic_id"] = "new"  # a title names a new topic
        clean = {k: v for k, v in body.items() if v not in (None, "", [], {})}
        return await self._call("POST", "/v1/agent/messages", body=clean)

    async def conversation(self, conversation_id: str, limit: int = 20) -> dict:
        return await self._call(
            "GET", f"/v1/agent/conversations/{quote(conversation_id)}", params={"limit": limit}
        )

    async def download(self, file_id: str) -> tuple[str, bytes]:
        """(file name, bytes) of a file this agent may read."""
        from urllib.parse import unquote

        status, headers, body = await self._request(
            "GET", f"/v1/agent/files/{quote(file_id)}", timeout=300
        )
        if status >= 400:
            raise GatewayError(status, _detail(status, body))
        disp = {k.lower(): v for k, v in headers.items()}.get("content-disposition", "")
        name = disp.split("filename*=UTF-8''", 1)[-1] if "filename*=" in disp else file_id
        return unquote(name), body

    async def directory(self) -> dict:
        return await self._call("GET", "/v1/agent/directory")

    async def release(self) -> None:
        """Hand the instance lease back, so a relay can take over at once."""
        try:
            await self._request("POST", "/v1/agent/release")
        except (GatewayError, NotSignedIn):
            pass

    async def close(self) -> None:
        """Nothing is held between calls."""
