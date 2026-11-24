"""A sign-in survives a power cut, a deploy and a dead token (Claudeo's report, 2026-11-24: a PC
reset seconds after waking lost the token pair it had just been given and used; the old one came
back from disk and was refused for good)."""

import argparse
import asyncio
import os

import httpx
import pytest

from agentygg import cli, store
from agentygg.api import GatewayError
from agentygg.relay import Relay


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_HOME", str(tmp_path / ".ygg"))


def _ident(**kw):
    kw = {"client_id": "c", "access_token": "a", "refresh_token": "r", "expires_at": 9e12, **kw}
    return store.Identity("bot", "lab", **kw)


def test_a_saved_sign_in_is_on_the_disk_before_it_takes_its_name(monkeypatch):
    done = []
    real = os.replace
    monkeypatch.setattr(store, "_to_disk", lambda path: done.append(("disk", path.suffix)))
    monkeypatch.setattr(
        store.Path, "replace", lambda self, to: (done.append(("replace", "")), real(self, to))[1]
    )
    store.save(_ident())
    assert done == [("disk", ".tmp"), ("replace", ""), ("disk", "")]  # the file, then its folder


def test_reaching_the_disk_never_fails_a_save(monkeypatch, tmp_path):
    def broken(_fd):
        raise OSError("not supported here")

    monkeypatch.setattr(store.os, "fsync", broken)
    store.save(_ident())
    assert store.load("bot").refresh_token == "r"
    store._to_disk(tmp_path / "missing")  # nothing there: no error either


def test_a_502_on_the_refresh_does_not_kill_the_relay(tmp_path, monkeypatch):
    relay = Relay(_ident(), tmp_path, command=["true"])
    tries = []

    async def stream_once():
        tries.append(1)
        if len(tries) < 3:
            raise httpx.HTTPError("Agent'Ygg is out of reach (502)")
        raise asyncio.CancelledError

    async def no_sleep(_):
        return None

    monkeypatch.setattr(relay, "_stream_once", stream_once)
    monkeypatch.setattr("agentygg.relay.asyncio.sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(relay._reader())
    assert len(tries) == 3


class _Gw:
    answer: object = None

    def __init__(self, ident):
        pass

    async def me(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"agent_id": "bot"}

    async def close(self):
        pass


@pytest.mark.parametrize(
    ("answer", "ended"),
    [
        (None, False),
        (GatewayError(401, "the Agent'Ygg sign-in ended (invalid_grant)"), True),
        (GatewayError(409, "identity_busy"), False),
        (GatewayError(503, "restarting"), False),
        (httpx.ConnectError("offline"), False),
    ],
)
def test_connect_signs_in_again_only_when_the_sign_in_is_dead(
    tmp_path, monkeypatch, capsys, answer, ended
):
    monkeypatch.chdir(tmp_path)
    store.save(_ident())
    store.write_pointer(tmp_path, "bot")
    monkeypatch.setattr(_Gw, "answer", answer)
    monkeypatch.setattr("agentygg.api.Gateway", _Gw)
    signed = []
    monkeypatch.setattr(cli, "_sign_in", lambda *a, **k: signed.append(1) or _ident())
    monkeypatch.setattr(cli, "cmd_setup", lambda a: None)
    monkeypatch.setattr(cli, "cmd_relay_install", lambda a: None)
    cli.cmd_connect(
        argparse.Namespace(
            harness="codex",
            plugin=False,
            relogin=False,
            api="https://x",
            no_relay=False,
            no_browser=True,
            harness_path=None,
            cmd=None,
            harness_args=[],
        )
    )
    assert bool(signed) is ended
    assert ("signing in again" in capsys.readouterr().out) is ended
