"""Hermes connects a platform on one event loop and runs turns (their sends, their tool calls)
on others (PoC with Betty, 2026-11-06). Every request now goes through Hermes'
credentialed_provider_request, from a worker thread: it must work from any loop, and Hermes must
still see the plugin as the caller (it tells by the calling frame). Exercised against a real
keep-alive HTTP server."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from conftest import HERMES_CALLS, HERMES_HTTP

from agentygg_hermes.gateway_client import AgentYggClient


class _Me(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server's name
        _Me.seen.append(self.headers.get("Authorization", ""))
        body = json.dumps({"agent_id": "betty", "handle": "@betty"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def test_one_client_serves_calls_from_several_event_loops_through_hermes():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Me)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = httpx.Client()  # Hermes' own client, with the token it attaches

    def network(method, url, body, headers):
        r = http.request(method, url, json=body, headers={**headers, "Authorization": "Bearer t"})
        return r.status_code, dict(r.headers), r.content

    HERMES_HTTP["handler"] = network
    try:
        client = AgentYggClient(f"http://127.0.0.1:{server.server_port}")
        for _ in range(3):  # three loops, as Hermes' turns
            assert asyncio.run(client.me())["handle"] == "@betty"
        assert _Me.seen == ["Bearer t"] * 3
        assert {c["provider"] for c in HERMES_CALLS} == {"agentygg"}
        assert all(c["headers"]["X-Ygg-Instance"] == client.nonce for c in HERMES_CALLS)
    finally:
        server.shutdown()
        http.close()
