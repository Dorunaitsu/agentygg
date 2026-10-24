"""`ygg listen` and its hand-off with the relay, without a server: a live session sees its
pings (system notices never end the wait), and a relay for the same agent steps aside while
that session listens, so the two never race for a wake."""

import asyncio
import types

import pytest

from agentygg import listen as lst
from agentygg import store
from agentygg.api import Gateway, GatewayError
from agentygg.relay import Relay


def _frame(sender, kind, text):
    return {
        "from": {"id": sender, "kind": kind, "label": sender},
        "envelope": {
            "id": f"m-{text}",
            "from": sender,
            "type": "request",
            "payload": {"text": text},
        },
    }


class FakeGw:
    def __init__(self, replies):
        self.identity = types.SimpleNamespace(agent_id="bot")
        self.nonce = "n1"
        self.replies = list(replies)
        self.waits = []

    async def inbox(self, wait=0.0):
        self.waits.append(wait)
        r = self.replies.pop(0) if self.replies else {"wakes": [], "mailbox": []}
        if isinstance(r, Exception):
            raise r
        if r == "idle":
            await asyncio.sleep(wait)
            return {"wakes": [], "mailbox": []}
        return r


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("YGG_INSTANCE", raising=False)
    return tmp_path


def test_system_notices_wait_for_the_next_real_message(home):
    poll = _frame("system.ygg", "system", "closure poll")
    ping = _frame("human.1", "human", "PING")
    gw = FakeGw([{"wakes": [poll], "mailbox": []}, {"wakes": [ping], "mailbox": []}])
    out = []
    assert asyncio.run(lst.listen(gw, out=out.append)) == lst.GOT_MESSAGE
    assert len(gw.waits) == 2  # the poll alone did not end the wait
    assert "closure poll" in out[0] and "PING" in out[1]  # both shown, in order
    assert store.listener_active("bot")
    assert store.read_listen("bot")["nonce"] == "n1"


def test_timeout_with_nothing(home):
    gw = FakeGw(["idle"])
    out = []
    assert asyncio.run(lst.listen(gw, timeout=0.05, out=out.append)) == lst.NOTHING
    assert out == ["Nothing new."]


def test_gateway_restart_is_retried_and_refusal_stops(home):
    ping = _frame("agent.x", "agent", "hello")
    gw = FakeGw(
        [GatewayError(502, "Bad Gateway"), OSError("down"), {"wakes": [], "mailbox": [ping]}]
    )
    out = []
    assert asyncio.run(lst.listen(gw, out=out.append, retry_cap=0)) == lst.GOT_MESSAGE
    assert "hello" in out[0]
    gw = FakeGw([GatewayError(409, "identity_busy")])
    out = []
    assert asyncio.run(lst.listen(gw, out=out.append)) == lst.STOPPED
    assert "identity_busy" in out[-1]


def test_listening_mark_expires(home):
    store.touch_listen("bot", "n1")
    ts = store.read_listen("bot")["ts"]
    assert store.listener_active("bot", now=ts + 10)
    assert not store.listener_active("bot", now=ts + store.LISTEN_TTL_S + 1)
    store.clear_listen("bot")
    assert not store.listener_active("bot")


def test_relaunching_listen_keeps_its_instance(home):
    store.touch_listen("bot", "abc123")
    assert lst.listen_nonce("bot") == "abc123"  # no collision with our own lease


def test_a_title_opens_a_new_topic(home):
    gw = Gateway(store.Identity("bot", "lab", "yga_x"))
    sent = {}

    async def fake_call(method, path, **kw):
        sent.update(kw["json"])
        return {}

    gw._call = fake_call
    asyncio.run(gw.send(type="request", to="betty", text="hi", title="Pings"))
    assert sent["topic_id"] == "new"
    asyncio.run(gw.send(type="request", to="betty", text="hi", title="Pings", topic_id="t1"))
    assert sent["topic_id"] == "t1"
    asyncio.run(gw.close())


def test_relay_steps_aside_while_a_session_listens(home):
    relay = Relay(
        store.Identity("bot", "lab", "yga_x"), home, command=["true"], listen_check_s=0.01
    )
    closed = []

    async def stream():
        try:
            yield {"kind": "hello"}
            await asyncio.sleep(3600)
        finally:
            closed.append(True)

    relay.gw.stream = stream

    async def go():
        task = asyncio.create_task(relay._stream_once())
        await asyncio.sleep(0.05)
        assert not task.done()  # nobody listening: the relay holds the stream
        store.touch_listen("bot", "n1")
        return await asyncio.wait_for(task, 1)

    assert asyncio.run(go()) is True
    assert closed  # the connection was closed, so the server hands wakes to the listener
    asyncio.run(relay.gw.close())


def test_listen_refuses_inside_a_relay_turn(home, monkeypatch):
    from agentygg import cli

    monkeypatch.setenv("YGG_RELAY", "1")
    with pytest.raises(SystemExit):
        cli.cmd_listen(types.SimpleNamespace(agent="bot", all=False, timeout=0.0))
