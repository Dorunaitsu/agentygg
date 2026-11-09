"""`hermes agentygg login | status | logout` (login.py) and the poll (gateway_client.stream): the
sign-in is Hermes' own (catalog rule 11), the plugin never holds a token, and a batch is
confirmed by the next poll."""

from __future__ import annotations

import asyncio
import json
import types

import pytest
from conftest import HERMES_CALLS, HERMES_HTTP

from agentygg_hermes import gateway_client as gc
from agentygg_hermes import login


def _hermes_ok(cmd, **kw):
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _fake_hermes(tmp_path):
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/bin/sh\n")
    hermes.chmod(0o755)
    return hermes


@pytest.mark.parametrize(
    ("answer", "flag", "turned_on", "says"),
    [
        (True, False, True, "bring you its requests"),
        (False, False, False, "decline"),
        (True, True, False, "decline"),  # --no-questions: not even asked
    ],
)
def test_login_asks_first_then_runs_hermes_own_sign_in(
    tmp_path, monkeypatch, capsys, answer, flag, turned_on, says
):
    hermes = _fake_hermes(tmp_path)
    monkeypatch.setattr(login.sys, "argv", [str(hermes), "-p", "work", "agentygg", "login"])
    ran, asked = [], []

    def run(cmd, **kw):
        ran.append((cmd, kw))
        return _hermes_ok(cmd)

    args = types.SimpleNamespace(agentygg_cmd="login", no_questions=flag)
    code = login.run_cli(args, ask=lambda q: asked.append(q) or answer, run=run)
    out = capsys.readouterr().out
    assert code == 0 and says in out and "hermes gateway restart" in out
    sign_in, sign_in_kw = ran[0]
    # Hermes' own sign-in for the plugin's oauth: entry, on the same profile, given the terminal
    assert sign_in == [str(hermes), "-p", "work", "auth", "add", "agentygg:agentygg"]
    assert "capture_output" not in sign_in_kw
    config = [c for c, _ in ran if "config" in c]
    assert bool(config) is turned_on
    if turned_on:
        assert config[0][-4:] == [
            "config",
            "set",
            "plugins.entries.agentygg.allow_gateway_injection",
            "true",
        ]
    assert ("Recommended" in asked[0]) if not flag else asked == []


def test_a_failed_sign_in_says_what_to_run(tmp_path, monkeypatch, capsys):
    hermes = _fake_hermes(tmp_path)
    monkeypatch.setattr(login.sys, "argv", [str(hermes), "agentygg", "login"])
    ran = []
    code = login.run_cli(
        types.SimpleNamespace(agentygg_cmd="login", no_questions=False),
        ask=lambda q: True,
        run=lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=1, stderr=""),
    )
    out = capsys.readouterr().out
    assert code == 1 and "hermes auth add agentygg:agentygg" in out
    assert len(ran) == 1  # the sign-in ran once, nothing turned on after it failed


def test_questions_fall_back_to_hermes_on_the_path(monkeypatch, tmp_path):
    hermes = _fake_hermes(tmp_path)
    monkeypatch.setattr(login.shutil, "which", lambda name: str(hermes))
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        return types.SimpleNamespace(returncode=0 if cmd[0] == str(hermes) else 1, stderr="no")

    ok, _ = login.allow_questions(["/opt/hermes/main.py", "-p", "work", "agentygg", "login"], run)
    assert ok and ran[-1][:3] == [str(hermes), "-p", "work"]


def test_status_asks_agentygg_through_hermes(capsys):
    def network(method, url, body, headers):
        assert url == "https://api.agentygg.com/v1/agent/me"
        return 200, {}, json.dumps({"handle": "@betty", "workspace_name": "Lab"}).encode()

    HERMES_HTTP["handler"] = network
    assert login.run_cli(types.SimpleNamespace(agentygg_cmd="status")) == 0
    assert 'Connected as @betty in "Lab"' in capsys.readouterr().out
    HERMES_HTTP["handler"] = None  # not signed in
    assert login.run_cli(types.SimpleNamespace(agentygg_cmd="status")) == 1
    assert "hermes auth add agentygg:agentygg" in capsys.readouterr().out


def test_the_plugin_keeps_no_credential_and_names_no_token_url():
    """Rule 11: no token stored, refreshed or written by the plugin; Hermes warns when plugin
    code names the token URL."""
    import pathlib

    src = pathlib.Path(gc.__file__).parent
    code = "\n".join(p.read_text() for p in src.glob("*.py"))
    assert "/token" not in code and "refresh_token" not in code and "credentials.json" not in code
    assert '"Authorization"' not in code


def test_the_poll_confirms_each_batch_with_the_next_one():
    replies = [
        {
            "wakes": [{"kind": "wake", "envelope": {"id": "m1"}}],
            "mailbox": [{"x": 1}],
            "batch": "b1",
        },
        {"wakes": [], "mailbox": [{"x": 2}], "batch": "b2"},
        {"wakes": [], "mailbox": [], "batch": ""},
    ]
    seen = []

    async def requester(method, url, body, headers, timeout):
        seen.append(url)
        if not replies:
            raise asyncio.CancelledError
        return 200, {}, json.dumps(replies.pop(0)).encode()

    async def take():
        frames = []
        with pytest.raises(asyncio.CancelledError):
            async for f in gc.AgentYggClient("https://api.test", requester=requester).stream():
                frames.append(f)
        return frames

    frames = asyncio.run(take())
    assert [f["kind"] for f in frames] == ["hello", "wake", "context"]
    assert frames[1]["mailbox"] == [{"x": 1}] and frames[2]["mailbox"] == [{"x": 2}]
    acks = [u.split("ack=")[1] for u in seen]
    assert acks == ["", "b1", "b2", ""]  # each poll confirms the one before


def test_hermes_refusals_become_clear_errors():
    with pytest.raises(gc.NotSignedIn, match="hermes auth add agentygg:agentygg"):
        asyncio.run(gc.AgentYggClient().me())
    assert HERMES_CALLS and HERMES_CALLS[0]["provider"] == "agentygg"

    def offline(method, url, body, headers):
        raise OSError("connection refused")

    HERMES_HTTP["handler"] = offline
    with pytest.raises(gc.GatewayError) as e:
        asyncio.run(gc.AgentYggClient().me())
    assert e.value.status == 503  # out of reach: the adapter tries again
