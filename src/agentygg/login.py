"""Signing an agent in: a link and a code (device login, RFC 8628), the only way to connect one.

`ygg connect` (or `ygg login`) shows a link and a short code; the person opens it on any device
(this machine, their phone), signs in to Agent'Ygg, checks the code and picks the workspace and
the agent, or creates one. Meanwhile the command polls; the tokens it gets are kept with the
agent's identity in `~/.ygg/agents/<id>.json` (0600): an access token (one hour) and a refresh
token (30 days, replaced at each use). No key is shown, copied or pasted anywhere. Ray,
2026-11-08: "every install and connection goes through the sign-in".

The tokens open the agent gateway as that agent only, and die with it (revoked, or deleted).
"""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Callable

import httpx


def device_name(label: str = "") -> str:
    """How the device is named on the sign-in page: "Claude Code on ray". The machine name
    tells the person which device asks (their laptop, the Mac mini); the ".local" macOS adds,
    or a domain, says nothing more (Ray, 2026-11-08)."""
    host = socket.gethostname().split(".")[0] or "this machine"
    return f"{label or 'An agent'} on {host}"


def qr_lines(url: str) -> list[str]:
    """The link as a QR code for the terminal (two modules per character), to open it on a phone
    by scanning it (Ray, 2026-11-08). Light modules are drawn, so it reads on the usual dark
    terminal. [] without the qrcode package (the link alone still works)."""
    try:
        import qrcode
    except ImportError:
        return []
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()
    if len(m) % 2:
        m.append([False] * len(m[0]))
    glyph = {
        (False, False): "\u2588",
        (False, True): "\u2580",
        (True, False): "\u2584",
        (True, True): " ",
    }
    return [
        "".join(glyph[(m[y][x], m[y + 1][x])] for x in range(len(m[0])))
        for y in range(0, len(m), 2)
    ]


def _say(line: str) -> None:
    """Print at once: an agent that runs the login in the background must see the link
    while the command waits (stdout to a pipe is buffered otherwise; found 2026-11-08)."""
    print(line, flush=True)


DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT_S = 20.0


class LoginNeeded(Exception):
    """No usable sign-in for this agent: run `ygg connect` (or `ygg login`) again."""


def _error(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error") or r.status_code)
    except ValueError:
        return str(r.status_code)


def device_login(
    api: str,
    client_name: str = "",
    out: Callable[[str], None] = None,  # default: print, flushed
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> dict:
    """The device login, start to finish. Returns {client_id, access_token, refresh_token,
    expires_at, me} (me = the agent's /v1/agent/me). Raises LoginNeeded if the person refuses
    or the code expires."""
    out = out or _say
    api = api.rstrip("/")
    own = http is None
    http = http or httpx.Client(timeout=TIMEOUT_S)
    try:
        name = client_name or device_name()
        r = http.post(f"{api}/v1/oauth/device", data={"client_name": name})
        if r.status_code != 200:
            raise LoginNeeded(f"Agent'Ygg could not start the sign-in ({r.status_code})")
        d = r.json()
        out("")
        out("To connect this agent to Agent'Ygg, open this link on any device:")
        out(f"    {d['verification_uri_complete']}")
        out(f"and check that it shows the code  {d['user_code']}")
        if sys.stdout.isatty():  # someone is at the terminal: let them scan it with their phone
            out("")
            for line in qr_lines(d["verification_uri_complete"]):
                out("    " + line)
        out("")
        out(f"Waiting for you (the code works for {int(d['expires_in']) // 60} minutes)...")
        interval = float(d.get("interval", 5))
        deadline = now() + float(d["expires_in"])
        while now() < deadline:
            sleep(interval)
            r = http.post(
                f"{api}/v1/oauth/device/token",
                data={
                    "grant_type": DEVICE_GRANT,
                    "device_code": d["device_code"],
                    "client_id": d["client_id"],
                },
            )
            if r.status_code == 200:
                t = r.json()
                me = http.get(
                    f"{api}/v1/agent/me",
                    headers={"Authorization": f"Bearer {t['access_token']}"},
                )
                if me.status_code != 200:
                    raise LoginNeeded(
                        f"signed in, but the agent is not reachable ({me.status_code})"
                    )
                return {
                    "client_id": d["client_id"],
                    "access_token": t["access_token"],
                    "refresh_token": t["refresh_token"],
                    "expires_at": now() + float(t.get("expires_in", 3600)),
                    "me": me.json(),
                }
            err = _error(r)
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                raise LoginNeeded(
                    "refused in the console" if err == "access_denied" else f"sign-in ended ({err})"
                )
        raise LoginNeeded("the code expired before it was approved; run the command again")
    finally:
        if own:
            http.close()


def refresh(api: str, client_id: str, refresh_token: str, http: httpx.Client | None = None) -> dict:
    """A new token pair. Raises LoginNeeded when the sign-in is gone (revoked agent, expired),
    httpx.HTTPError when Agent'Ygg is out of reach (try again later)."""
    own = http is None
    http = http or httpx.Client(timeout=TIMEOUT_S)
    try:
        r = http.post(
            f"{api.rstrip('/')}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
        )
    finally:
        if own:
            http.close()
    if r.status_code >= 500:
        raise httpx.HTTPError(f"Agent'Ygg is out of reach ({r.status_code})")
    if r.status_code != 200:
        raise LoginNeeded(f"the Agent'Ygg sign-in ended ({_error(r)}): run `ygg connect` again")
    return r.json()


def revoke(api: str, client_id: str, refresh_token: str, http: httpx.Client | None = None) -> None:
    """Best effort: end the sign-in on Agent'Ygg."""
    own = http is None
    http = http or httpx.Client(timeout=TIMEOUT_S)
    try:
        http.post(
            f"{api.rstrip('/')}/revoke", data={"token": refresh_token, "client_id": client_id}
        )
    except httpx.HTTPError:
        pass
    finally:
        if own:
            http.close()
