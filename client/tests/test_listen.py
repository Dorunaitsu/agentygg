"""`ygg listen` and its hand-off with the relay, without a server: a live session sees its
pings (system notices never end the wait), and a relay for the same agent steps aside while
that session listens, so the two never race for a wake."""

import contextlib
import asyncio
import os
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

    async def inbox(self, wait=0.0, ack=None):
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
    assert asyncio.run(lst.listen(gw, out=out.append, busy_grace=0)) == lst.STOPPED
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


def test_listen_stops_when_the_process_it_serves_is_gone(home):
    """A plugin passes its harness's pid: once that process is gone, nobody would read what we
    take, so we stop at once and hand the pings back to the relay."""
    gw = FakeGw([{"wakes": [], "mailbox": []}])
    out = []
    assert asyncio.run(lst.listen(gw, out=out.append, alive=lambda: False)) == lst.STOPPED
    assert not store.listener_active(gw.identity.agent_id)


def test_pid_alive_tells_a_live_process_from_a_gone_one():
    import subprocess
    import sys

    assert store.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not store.pid_alive(p.pid)


def test_a_refused_send_never_reads_as_delivered():
    from agentygg.api import describe_send

    refused = describe_send({"verdict": "CONTEXT_ONLY", "reason": "title_required"})
    assert refused.startswith("NOT delivered (title_required)") and "title" in refused
    ctx = describe_send({"verdict": "CONTEXT_ONLY", "reason": "context_inform"})
    assert ctx.startswith("delivered as context")
    assert describe_send({"verdict": "WAKE", "reason": "ok_wake"}).startswith("delivered, and")
    paused = describe_send({"verdict": "CONTEXT_ONLY", "reason": "topic_paused"})
    assert paused.startswith("delivered as context, nobody woken (topic_paused): this topic is")
    assert "a person acts on it" in paused


def test_a_lease_held_a_moment_ago_by_our_own_send_is_waited_out(home):
    ping = _frame("human.1", "human", "PING")
    gw = FakeGw([GatewayError(409, "identity_busy"), {"wakes": [ping], "mailbox": []}])
    out = []
    assert asyncio.run(lst.listen(gw, out=out.append, retry_cap=0)) == lst.GOT_MESSAGE
    assert "PING" in out[0]


def test_a_send_long_after_the_listener_keeps_its_instance(home):
    from agentygg.api import _listen_nonce

    store.touch_listen("bot", "abc123")
    data = store.read_listen("bot")
    data["ts"] -= 3600  # the session answered an hour after its listener ended
    store.listen_path("bot").write_text(__import__("json").dumps(data))
    assert _listen_nonce("bot") == "abc123"


def test_an_untitled_topic_asks_the_first_agent_for_a_title():
    from agentygg.render import wake_prompt

    wake = {
        "from": {"id": "human.1", "kind": "human", "label": "@ray"},
        "envelope": {
            "id": "m1",
            "type": "request",
            "from": "human.1",
            "conversation_id": "c1",
            "topic_id": "t1",
            "payload": {"text": "ping"},
            "meta": {"topic_untitled": True},
        },
    }
    prompt = wake_prompt({"agent_id": "bot"}, wake, [])
    assert 'topic_title="(none yet)"' in prompt and "give it a short `title`" in prompt


def test_a_relay_starting_during_a_gateway_restart_waits_for_it(home, monkeypatch):
    relay = Relay(store.Identity("bot", "lab", "yga_x"), home, command=["true"])
    calls = []

    async def me():
        calls.append(1)
        if len(calls) < 3:
            raise GatewayError(502, "Bad Gateway")
        return {"agent_id": "bot"}

    async def no_sleep(_):
        return None

    relay.gw.me = me
    monkeypatch.setattr("agentygg.relay.asyncio.sleep", no_sleep)
    assert asyncio.run(relay._first_contact()) == {"agent_id": "bot"} and len(calls) == 3

    async def refused():
        raise GatewayError(401, "unauthorized")

    relay.gw.me = refused
    with pytest.raises(GatewayError):
        asyncio.run(relay._first_contact())
    asyncio.run(relay.gw.close())


def test_a_relay_turn_says_it_was_started_by_the_relay_and_off_reads_clearly():
    from agentygg.api import describe_send
    from agentygg.render import wake_prompt

    wake = {"from": {"id": "h", "kind": "human"}, "envelope": {"id": "m", "type": "request"}}
    assert "started by your Agent'Ygg relay" in wake_prompt({"agent_id": "bot"}, wake, [])
    off = describe_send({"verdict": "DROP", "reason": "agent_off"})
    assert off.startswith("NOT delivered (agent_off)") and "turned off" in off


def test_a_restart_asked_during_a_turn_waits_for_the_turn(home, monkeypatch):
    """Codex, 2026-11-03: asked to update, it ran `ygg relay restart` inside its relay turn,
    which killed that turn before it could answer. Inside a turn the restart is only asked for;
    the relay restarts once the turn, and anything already queued, is done."""
    from agentygg import cli, relay as rl

    monkeypatch.setenv("YGG_RELAY", "1")
    called = []
    monkeypatch.setattr("agentygg.service.restart", lambda agent_id: called.append(agent_id))
    monkeypatch.setattr(cli, "_relay_target", lambda args: "bot")
    cli.cmd_relay_restart(types.SimpleNamespace())
    assert called == [] and store.restart_requested("bot")

    relay = Relay(store.Identity("bot", "lab", "yga_x"), home, command=["true"])
    turns = []

    async def handle(frame):
        turns.append(frame["n"])

    relay.handle = handle

    async def go():
        for n in (1, 2):
            await relay.queue.put({"kind": "wake", "n": n})
        await relay._worker()

    with pytest.raises(rl.RestartRequested):
        asyncio.run(go())
    assert turns == [1, 2]  # both queued wakes were handled first
    assert not store.restart_requested("bot")


def test_relay_restart_outside_a_turn_restarts_at_once(home, monkeypatch):
    from agentygg import cli

    monkeypatch.delenv("YGG_RELAY", raising=False)
    called = []
    monkeypatch.setattr("agentygg.service.restart", lambda agent_id: called.append(agent_id))
    monkeypatch.setattr(cli, "_relay_target", lambda args: "bot")
    cli.cmd_relay_restart(types.SimpleNamespace())
    assert called == ["bot"] and not store.restart_requested("bot")


def test_a_refused_reconnection_is_retried_and_only_a_lasting_one_stops_the_relay(
    home, monkeypatch
):
    """2026-11-14: after a lost connection the gateway refused test-agent's relay once and the
    relay stopped for good, though its sign-in was fine. Now it signs in again and retries; it
    stops only after REFUSALS_BEFORE_STOP refusals in a row."""
    from agentygg import relay as rl

    async def no_sleep(_):
        return None

    monkeypatch.setattr("agentygg.relay.asyncio.sleep", no_sleep)
    relay = Relay(store.Identity("bot", "lab", "yga_x"), home, command=["true"])
    relay._listened = lambda: False
    tries = []

    async def flaky():
        tries.append(1)
        if len(tries) <= 2:
            raise GatewayError(401, "the gateway refused the connection")
        raise RuntimeError("connected again")  # the hiccup passed: the relay kept going

    relay._stream_once = flaky
    with pytest.raises(RuntimeError):
        asyncio.run(relay._reader())
    assert len(tries) == 3

    tries.clear()

    async def refused():
        tries.append(1)
        raise GatewayError(403, "revoked")

    relay._stream_once = refused
    with pytest.raises(GatewayError):
        asyncio.run(relay._reader())
    assert len(tries) == rl.REFUSALS_BEFORE_STOP
    asyncio.run(relay.gw.close())


def test_the_relay_takes_one_wake_and_leaves_the_rest_on_the_server(home):
    """Claudeo, 2026-11-17: during a 29-minute turn the relay kept reading its stream, taking
    every message sent meanwhile into its own memory; the live session opened then never saw
    them. Now it closes the stream on a wake and opens it again only once the turn is over."""
    relay = Relay(
        store.Identity("bot", "lab", "yga_x"), home, command=["true"], listen_check_s=0.01
    )
    relay._listened = lambda: False
    opened, closed = [], []

    async def stream():
        opened.append(1)
        try:
            yield {"kind": "hello"}
            yield {"kind": "wake", "envelope": {"message_id": "m1"}}
            yield {"kind": "wake", "envelope": {"message_id": "m2"}}  # must stay on the server
            await asyncio.sleep(3600)
        finally:
            closed.append(1)

    relay.gw.stream = stream

    async def go():
        assert await relay._stream_once() is None  # took a wake, closed the stream
        assert closed == [1] and relay.queue.qsize() == 1 and not relay.idle.is_set()
        reader = asyncio.create_task(relay._reader())
        await asyncio.sleep(0.05)
        assert len(opened) == 1  # no new connection while the turn is pending
        relay.queue.get_nowait()
        relay.idle.set()  # the turn is over
        await asyncio.sleep(0.05)
        assert len(opened) == 2  # back on the stream
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader

    asyncio.run(go())
    asyncio.run(relay.gw.close())


def test_local_notes_are_kept_in_order_taken_once_and_capped(home):
    store.write_poke("bot", "first", topic="t1")
    store.write_poke("bot", "second")
    assert store.has_pokes("bot")
    got = store.take_pokes("bot")
    assert [g["text"] for g in got] == ["first", "second"] and got[0]["topic"] == "t1"
    assert store.take_pokes("bot") == [] and not store.has_pokes("bot")
    for i in range(store.POKES_MAX):
        store.write_poke("bot", f"n{i}")
    with pytest.raises(RuntimeError):
        store.write_poke("bot", "one too many")


def test_an_idle_relay_turns_a_local_note_into_a_turn(home):
    """Claudeo, 2026-11-17: a long render runs in the background; when it ends, its script runs
    `ygg relay poke` and the agent is woken here, without a message."""
    relay = Relay(store.Identity("bot", "lab", "yga_x"), home, command=["true"])
    relay.poke_check_s = 0.01
    relay._listened = lambda: False
    store.write_poke("bot", "render 42 done: out/loop.webp", topic="t9")

    async def go():
        task = asyncio.create_task(relay._poker())
        frame = await asyncio.wait_for(relay.queue.get(), 1)
        task.cancel()
        return frame

    frame = asyncio.run(go())
    assert frame["kind"] == "poke" and frame["pokes"][0]["text"].startswith("render 42")
    assert not relay.idle.is_set()  # a turn is due: the stream closes meanwhile
    from agentygg.render import poke_prompt

    prompt = poke_prompt({"agent_id": "bot"}, frame["pokes"])
    assert "not a message from anyone" in prompt and 'topic_id="t9"' in prompt
    asyncio.run(relay.gw.close())


def test_a_busy_or_listened_relay_leaves_the_notes_alone(home):
    relay = Relay(store.Identity("bot", "lab", "yga_x"), home, command=["true"])
    relay.poke_check_s = 0.01
    store.write_poke("bot", "done")

    async def go(listened: bool, idle: bool):
        relay._listened = lambda: listened
        relay.idle.set() if idle else relay.idle.clear()
        task = asyncio.create_task(relay._poker())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go(listened=True, idle=True))  # the live session delivers them
    asyncio.run(go(listened=False, idle=False))  # a turn runs: after it
    assert relay.queue.empty() and store.has_pokes("bot")
    asyncio.run(relay.gw.close())


def test_a_live_session_gets_the_local_notes(home):
    gw = types.SimpleNamespace(identity=store.Identity("bot", "lab", "yga_x"), nonce="n1")
    store.write_poke("bot", "render done")
    seen = []

    async def fake_listen(gw_, out, alive=None, **_kw):
        return lst.STOPPED

    async def go():
        orig = lst.listen
        lst.listen = fake_listen
        try:
            calls = iter([True, False])
            await lst.follow(gw, out=seen.append, alive=lambda: next(calls, False), sleep=lambda s: asyncio.sleep(0))
        finally:
            lst.listen = orig

    asyncio.run(go())
    assert any("render done" in s and "[local]" in s for s in seen)


def test_the_relay_shows_present_during_a_turn_and_stops_on_an_old_server(home):
    relay = Relay(store.Identity("bot", "lab", "yga_x"), home, command=["true"])
    relay.presence_every_s = 0.01
    calls = []

    async def present():
        calls.append(1)
        if len(calls) >= 3:
            raise GatewayError(404, "not found")
        return {"ok": True}

    relay.gw.presence = present
    asyncio.run(asyncio.wait_for(relay._keep_present(), 1))
    assert len(calls) == 3  # kept going, then stopped on the server without /presence
    asyncio.run(relay.gw.close())


def test_a_listener_that_cannot_reach_the_gateway_stops_claiming_the_agent(home, monkeypatch):
    """Claudeo, 2026-11-17: a listener whose client was replaced under it failed every call for
    an hour while still marking "a session listens": the relay stayed paused, nothing was
    delivered. Now it stops claiming the agent after CLAIM_FOR_S, and gives up after GIVE_UP_S
    (clearing the mark so the relay answers at once)."""
    import httpx as _httpx

    clock = [1000.0]
    monkeypatch.setattr(lst.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(lst, "CLAIM_FOR_S", 60.0)
    monkeypatch.setattr(lst, "GIVE_UP_S", 120.0)
    lst._LAST_OK.pop("bot", None)

    class Down:
        identity = store.Identity("bot", "lab", "yga_x")
        nonce = "n1"

        async def inbox(self, wait=0.0, ack=None):
            clock[0] += 30.0  # each failed try takes time
            raise _httpx.ConnectError("certificate file gone")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(lst.asyncio, "sleep", no_sleep)
    claims = []
    orig = store.touch_listen
    monkeypatch.setattr(store, "touch_listen", lambda a, n: (claims.append(clock[0]), orig(a, n)))
    lines = []
    code = asyncio.run(lst.listen(Down(), out=lines.append, retry_cap=0))
    assert code == lst.STOPPED
    assert lines[-1].startswith("ygg listen stopped: cannot reach Agent'Ygg")
    assert all(t - 1000.0 < 60.0 for t in claims)  # no claim once it had failed for a minute
    assert not store.listener_active("bot")  # cleared: the relay takes over now
    # No relay installed for this agent here: say so, and that messages wait (not that a relay
    # answers): seen live on 2026-11-19, a Mac asleep with no relay for its agent.
    assert "messages wait" in lines[-1] and "ygg relay install" in lines[-1]
    assert "the relay answers" not in lines[-1]

    relays = store.home() / "relays"
    relays.mkdir(parents=True, exist_ok=True)
    (relays / "bot.json").write_text("{}")
    lst._LAST_OK.pop("bot", None)
    lines = []
    asyncio.run(lst.listen(Down(), out=lines.append, retry_cap=0))
    assert lines[-1].endswith("(the relay answers meanwhile)")


def test_a_listener_that_reaches_the_gateway_keeps_claiming(home, monkeypatch):
    lst._LAST_OK.pop("bot", None)
    ping = {"kind": "wake", "from": {"kind": "human"}, "envelope": {"from": "human.ray"}}

    class Up:
        identity = store.Identity("bot", "lab", "yga_x")
        nonce = "n1"

        async def inbox(self, wait=0.0, ack=None):
            return {"wakes": [ping], "mailbox": []}

    out = []
    assert asyncio.run(lst.listen(Up(), out=out.append)) == lst.GOT_MESSAGE
    assert store.listener_active("bot")


# -- what a poll hands over is confirmed once delivered (2026-11-24) -------------------------- #


class AckGw(FakeGw):
    def __init__(self, replies, confirm=()):
        super().__init__(replies)
        self.acks, self.confirmed, self.confirm_replies = [], [], list(confirm)

    async def inbox(self, wait=0.0, ack=None):
        self.acks.append(ack)
        return await super().inbox(wait)

    async def confirm(self, batch):
        self.confirmed.append(batch)
        r = self.confirm_replies.pop(0) if self.confirm_replies else {"confirmed": True}
        if isinstance(r, Exception):
            raise r
        return r


def _batch(text="hi", batch="b1"):
    env = {"id": "m1", "type": "request", "from": "a", "payload": {"text": text}}
    return {"wakes": [{"kind": "wake", "envelope": env}], "mailbox": [], "batch": batch}


def _listen(gw, monkeypatch, tmp_path):
    monkeypatch.setenv("YGG_HOME", str(tmp_path / ".ygg"))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(lst.asyncio, "sleep", no_sleep)
    out = []
    return asyncio.run(lst.listen(gw, out=out.append)), out


def test_a_batch_is_confirmed_once_it_is_printed(monkeypatch, tmp_path):
    gw = AckGw([_batch()])
    code, out = _listen(gw, monkeypatch, tmp_path)
    assert code == lst.GOT_MESSAGE and len(out) == 1
    assert gw.acks == [""] and gw.confirmed == ["b1"]


def test_a_batch_that_could_not_be_printed_is_not_confirmed(monkeypatch, tmp_path):
    monkeypatch.setenv("YGG_HOME", str(tmp_path / ".ygg"))
    gw = AckGw([_batch()])

    def broken(_):
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        asyncio.run(lst.listen(gw, out=broken))
    assert gw.confirmed == []


def _follow(gw, monkeypatch, tmp_path, alive_for, **kw):
    """Follow until `alive` has been asked `alive_for` times."""
    monkeypatch.setenv("YGG_HOME", str(tmp_path / ".ygg"))
    asked, out = [0], []

    def alive():
        asked[0] += 1
        return asked[0] <= alive_for

    async def no_sleep(_):
        return None

    monkeypatch.setattr(lst.asyncio, "sleep", no_sleep)
    asyncio.run(lst.follow(gw, out=out.append, alive=alive, sleep=no_sleep, **kw))
    return out


def test_a_session_gets_its_delivery_before_it_is_confirmed(monkeypatch, tmp_path):
    gw = AckGw([_batch()])
    seen = []
    gw_confirm = gw.confirm

    async def confirm(batch):
        seen.append(len(out_ref))
        return await gw_confirm(batch)

    gw.confirm, out_ref = confirm, []
    monkeypatch.setenv("YGG_HOME", str(tmp_path / ".ygg"))
    left = [6]

    def alive():
        left[0] -= 1
        return left[0] > 0

    async def no_sleep(_):
        return None

    asyncio.run(lst.follow(gw, out=out_ref.append, alive=alive, sleep=no_sleep))
    assert gw.confirmed == ["b1"] and seen == [1]


def test_a_session_that_ends_right_after_a_delivery_does_not_confirm_it(monkeypatch, tmp_path):
    """Codex takes a message for a session that is closing (its own queue): nobody reads it, so
    the relay must get it (seen live, 2026-11-24)."""
    gw = AckGw([_batch()])
    out = _follow(gw, monkeypatch, tmp_path, alive_for=2, settle_s=3.0)
    assert len(out) == 1 and gw.confirmed == []


def test_a_confirmation_that_fails_is_tried_again_and_never_costs_the_message(
    monkeypatch, tmp_path
):
    import httpx as _httpx

    gw = AckGw([_batch()], confirm=[_httpx.ConnectError("down")] * 9)
    code, out = _listen(gw, monkeypatch, tmp_path)
    assert code == lst.GOT_MESSAGE and len(out) == 1 and 2 <= len(gw.confirmed) <= 4


def test_a_server_without_confirmation_is_asked_once(monkeypatch, tmp_path):
    gw = AckGw([_batch()], confirm=[lst.GatewayError(404, "Not Found")] * 9)
    code, out = _listen(gw, monkeypatch, tmp_path)
    assert code == lst.GOT_MESSAGE and len(out) == 1 and len(gw.confirmed) == 1


def test_a_server_that_gives_no_batch_is_not_asked(monkeypatch, tmp_path):
    reply = _batch()
    del reply["batch"]
    gw = AckGw([reply])
    code, _ = _listen(gw, monkeypatch, tmp_path)
    assert code == lst.GOT_MESSAGE and gw.confirmed == []
