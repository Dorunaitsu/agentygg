"""The plugin's own behaviour, against stand-ins for Hermes (conftest) and for the gateway."""

from __future__ import annotations

import asyncio
import base64
import json
import types

import agentygg_hermes as agentygg
import pytest
from agentygg_hermes import adapter as ad
from agentygg_hermes import tools
from agentygg_hermes.frames import render_wake

from conftest import SECRETS, SESSION, MessageEvent, ProcessingOutcome


def _key(agent="betty", ws="lab"):
    return "yga_" + base64.urlsafe_b64encode(
        json.dumps({"w": ws, "a": agent, "s": "x"}).encode()
    ).decode().rstrip("=")


def _wake(
    text="review PR 12?", sender="claude-code", type_="request", mid="msg_1", cid="conv_1", **meta
):
    return {
        "kind": "wake",
        "from": {"id": sender, "kind": "agent", "label": "Claude Code (@claude-code)"},
        "envelope": {
            "id": mid,
            "from": sender,
            "to": "betty",
            "type": type_,
            "conversation_id": cid,
            "topic_id": "top_1",
            "state": "working",
            "payload": {"text": text},
            "meta": {"title": "PR 12", **meta},
        },
        "mailbox": [],
    }


class FakeClient:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, **body):
        self.sent.append(body)
        return {
            "verdict": "WAKE",
            "reason": "ok_response_delivery",
            "message_id": "msg_out",
            "topic_id": body.get("topic_id"),
        }


def _adapter() -> ad.AgentYggAdapter:
    a = ad.AgentYggAdapter(config=None)
    a.client = FakeClient()
    ad._active = a
    return a


def test_a_message_says_who_sent_it_as_what_and_where():
    text = render_wake({**_wake(), "mailbox": [_wake("earlier", mid="msg_0")]})
    assert text.startswith(
        '[Agent\'Ygg] Claude Code (@claude-code) (agent) sent a request in topic "PR 12" (top_1):'
    )
    assert "review PR 12?" in text and "Earlier in this conversation" in text and "earlier" in text


async def test_an_inbound_message_opens_a_turn_in_its_conversation_and_is_never_a_command():
    a = _adapter()
    await a._deliver(_wake("/stop everything"))
    (event,) = a.handled
    assert event.source.chat_id == "conv_1" and event.source.user_id == "claude-code"
    assert event.allow_gateway_control is False and not event.is_command()
    assert event.message_id == "msg_1"


async def test_the_allowlist_drops_other_senders_but_never_agentyggs_own_notices():
    SECRETS["AGENTYGG_ALLOWED"] = "@codex"
    a = _adapter()
    await a._deliver(_wake(sender="claude-code"))
    await a._deliver(_wake(sender="system.ygg", mid="msg_2", cid="conv_2"))
    await a._deliver(_wake(sender="codex", mid="msg_3", cid="conv_3"))
    assert [e.source.user_id for e in a.handled] == ["system.ygg", "codex"]


async def test_a_plain_reply_answers_the_request_it_was_woken_for_once():
    a = _adapter()
    await a._deliver(_wake())
    assert (await a.send("conv_1", "progress", metadata={})).success  # not the final reply
    await a.send("conv_1", "LGTM, two nits.", metadata={"notify": True})
    await a.send("conv_1", "LGTM, two nits.", metadata={"notify": True})  # never twice
    assert a.client.sent == [
        {
            "type": "response",
            "to": "claude-code",
            "text": "LGTM, two nits.",
            "conversation_id": "conv_1",
            "topic_id": "top_1",
            "in_reply_to": "msg_1",
            "state": "done",
        }
    ]


async def test_queued_messages_are_answered_in_order_each_by_its_own_turn():
    a = _adapter()
    await a._deliver(_wake("first", mid="m1"))
    await a._deliver(_wake("second", mid="m2", type_="inform"))
    await a.send("conv_1", "to the first", metadata={"notify": True})  # no reply_to: its turn
    await a.send("conv_1", "to the second", reply_to="m2", metadata={"notify": True})
    assert [(s["in_reply_to"], s["type"]) for s in a.client.sent] == [
        ("m1", "response"),
        (None, "inform"),
    ]


async def test_a_streamed_reply_is_sent_from_on_processing_complete():
    a = _adapter()
    await a._deliver(_wake())
    event = a.handled[0]
    event._streamed_final_response = "streamed answer"
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert (
        a.client.sent[0]["text"] == "streamed answer" and a.client.sent[0]["in_reply_to"] == "msg_1"
    )


async def test_the_tool_answers_the_current_message_and_the_final_text_is_not_sent_again():
    a = _adapter()
    await a._deliver(_wake())
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    out = await tools.ygg(
        {"action": "send", "text": "Which branch?", "state": "waiting", "type": "request"}
    )
    assert out.startswith("delivered, and the recipient was woken")
    sent = a.client.sent[0]
    assert (sent["to"], sent["conversation_id"], sent["topic_id"]) == (
        "claude-code",
        "conv_1",
        "top_1",
    )
    await a.send("conv_1", "(the turn's final words)", metadata={"notify": True})
    assert len(a.client.sent) == 1


async def test_a_response_from_the_tool_defaults_to_the_message_being_answered():
    a = _adapter()
    await a._deliver(_wake())
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    await tools.ygg({"action": "send", "type": "response", "text": "done", "state": "done"})
    assert a.client.sent[0]["in_reply_to"] == "msg_1"


async def test_outside_an_agentygg_session_a_new_exchange_names_its_recipient():
    a = _adapter()
    SESSION.update({"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "123"})
    assert "needs `to`" in await tools.ygg({"action": "send", "type": "request", "text": "hi"})
    await tools.ygg(
        {
            "action": "send",
            "to": "@claude-code",
            "type": "request",
            "text": "review PR 12",
            "topic_id": "new",
            "title": "PR 12",
            "state": "waiting",
            "waiting_for": "claude-code",
        }
    )
    sent = a.client.sent[0]
    assert (
        sent["conversation_id"] is None
        and sent["topic_id"] == "new"
        and sent["to"] == "@claude-code"
    )


async def test_the_tool_answers_the_message_handed_over_never_an_older_one():
    """Ray, 2026-11-06: no guessing. PoC test 3 bis: in follow-up turns Hermes reports no start,
    the tool fell back to the oldest message waiting (ball 1), marked it answered from a
    closure-poll turn, and ball 1 never got its pong. One message per conversation is in Hermes
    at a time: that one is the turn's message."""
    a = _adapter()
    await a._deliver(_wake("older, still waiting", mid="m_old"))
    await a.on_processing_complete(a.handled[0], ProcessingOutcome.FAILURE)  # no answer sent
    await a._deliver(_wake("the one being handled", mid="m_now"))
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    await tools.ygg({"action": "send", "type": "response", "text": "on it", "state": "working"})
    assert a.client.sent[0]["in_reply_to"] == "m_now"
    await a.on_processing_complete(a.handled[1], ProcessingOutcome.SUCCESS)
    assert a.conversations.turn.get("conv_1") is None


async def test_with_no_turn_in_progress_the_tool_guesses_nothing():
    a = _adapter()
    await a._deliver(_wake("answered already", mid="m1"))
    await a.send("conv_1", "done", reply_to="m1", metadata={"notify": True})
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    out = await tools.ygg({"action": "send", "type": "inform", "text": "fyi"})
    assert "needs `to`" in out and len(a.client.sent) == 1


async def test_a_message_waits_for_the_turn_before_it_so_hermes_never_merges_them():
    """PoC test 2, 2026-11-06: test 2 and a second message arrived while Betty was busy; Hermes
    merged them into one turn (dispatch_busy_burst), one reply went out, test 2 got none. The
    adapter now hands Hermes one message per conversation at a time."""
    a = _adapter()
    await a._deliver(_wake("first", mid="m1"))
    await a._deliver(_wake("second", mid="m2"))
    await a._deliver(_wake("elsewhere", mid="m3", cid="conv_2"))
    assert [e.message_id for e in a.handled] == ["m1", "m3"]  # other conversations go on
    await a.on_processing_complete(a.handled[0], ProcessingOutcome.SUCCESS)
    assert [e.message_id for e in a.handled] == ["m1", "m3", "m2"]


async def test_a_turn_that_never_reports_its_end_does_not_hold_the_conversation_forever(
    monkeypatch,
):
    """Betty's review: the hold limit was only checked when a new message came; a stuck
    conversation now frees itself on a timer, and the held message goes."""
    import asyncio

    monkeypatch.setattr(ad, "HOLD_MAX_S", 0.05)
    a = _adapter()
    await a._deliver(_wake("first", mid="m1"))
    await a._deliver(_wake("second", mid="m2"))
    assert [e.message_id for e in a.handled] == ["m1"]
    await asyncio.sleep(0.2)
    assert [e.message_id for e in a.handled] == ["m1", "m2"]


async def test_a_message_hermes_could_not_take_does_not_leave_the_conversation_busy():
    a = _adapter()
    calls = []

    async def fails_once(event):
        calls.append(event.message_id)
        if len(calls) == 1:
            raise RuntimeError("gateway not ready")
        a.handled.append(event)

    a.handle_message = fails_once
    with pytest.raises(RuntimeError):
        await a._deliver(_wake("first", mid="m1"))
    await a._deliver(_wake("second", mid="m2"))
    assert [e.message_id for e in a.handled] == ["m2"]


async def test_a_message_hermes_did_not_take_frees_the_conversation_at_once():
    """PoC test 3, 2026-11-06: the conversation stayed held after a message that opened no turn
    (no end is ever reported for it). Hermes marks such a message _gateway_accepted = False."""
    a = _adapter()

    async def drops(event):
        event._gateway_accepted = False

    a.handle_message = drops
    await a._deliver(_wake("closure poll", sender="system.ygg", mid="m_poll"))
    assert "conv_1" not in a._busy


async def test_any_turn_ending_in_the_conversation_frees_it():
    """A turn that ends on another message than the one we are waiting on (Hermes merged ours
    into it) still means the conversation is free."""
    a = _adapter()
    await a._deliver(_wake("first", mid="m1"))
    await a._deliver(_wake("second", mid="m2"))
    other = MessageEvent(text="x", message_id="m_other", source=a.handled[0].source)
    await a.on_processing_complete(other, ProcessingOutcome.SUCCESS)
    assert [e.message_id for e in a.handled] == ["m1", "m2"]


async def test_the_final_reply_frees_the_conversation_even_without_an_end_of_turn():
    """PoC test 3, 2026-11-06 (Betty's log: no adapter line at all for ball 1): test 2B was
    handed over while 2A's session was still active, Hermes ran it as a follow-up turn and its
    reply went out, but no end of turn was reported for it; ball 1 stayed held behind 2B. The
    final reply is the signal every turn gives."""
    a = _adapter()
    await a._deliver(_wake("2B", mid="m_2b"))
    await a._deliver(_wake("ball 1", mid="m_ball"))
    assert [e.message_id for e in a.handled] == ["m_2b"]
    await a.send("conv_1", "2B reçu", reply_to="m_2b", metadata={"notify": True})
    assert [e.message_id for e in a.handled] == ["m_2b", "m_ball"]


async def test_a_skipped_reply_also_frees_the_conversation():
    a = _adapter()
    notice = _wake("[system:resume]", sender="system.ygg", mid="m_sys")
    notice["envelope"]["topic_id"] = None
    await a._deliver(notice)
    await a._deliver(_wake("next", mid="m_next"))
    await a.send("conv_1", "nothing pending", reply_to="m_sys", metadata={"notify": True})
    assert [e.message_id for e in a.handled] == ["m_sys", "m_next"]


async def test_writing_back_by_handle_counts_as_the_answer():
    """PoC test 3b, 2026-11-06: Betty sent the ball back with the tool to "@claude-code" (the
    handle), the adapter compared it with the sender's id "claude-code", did not count it as the
    answer, and sent her final text as a second reply. A handle that differs from the id (a new
    Betty after a deleted one) is read from the frame's label."""
    a = _adapter()
    await a._deliver(_wake("ball 1", mid="m1"))
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    await tools.ygg({"action": "send", "to": "@Claude-Code", "type": "request", "text": "back 1"})
    await a.send("conv_1", "sent it back", reply_to="m1", metadata={"notify": True})
    assert [s["text"] for s in a.client.sent] == ["back 1"]
    assert (
        a.client.sent[0]["topic_id"] == "top_1" and a.client.sent[0]["conversation_id"] == "conv_1"
    )


async def test_a_handle_that_differs_from_the_id_is_recognised():
    a = _adapter()
    frame = _wake("hi", sender="betty-a1b2c3", mid="m1")
    frame["from"]["label"] = "Betty (@betty)"
    await a._deliver(frame)
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    await tools.ygg({"action": "send", "to": "@betty", "type": "inform", "text": "fyi"})
    assert a.client.sent[0]["topic_id"] == "top_1"
    await a.send("conv_1", "again", reply_to="m1", metadata={"notify": True})
    assert len(a.client.sent) == 1


class _Entry:
    def __init__(self, key, platform, minutes_ago, chat_type="dm"):
        import datetime as dt

        self.session_key, self.platform, self.chat_type = key, platform, chat_type
        self.origin = object()
        self.updated_at = dt.datetime(2026, 11, 6, 15, 0) - dt.timedelta(minutes=minutes_ago)


class _Store:
    def __init__(self, entries):
        self.entries = entries

    async def list_sessions(self, active_minutes=None):
        return self.entries


def _with_people(a, entries, accept=True):
    import types

    a.gateway_runner = types.SimpleNamespace(async_session_store=_Store(entries))
    injected = []

    class Ctx:
        def inject_message(self, content, role="user", *, session_key=None, origin=None):
            injected.append((session_key, content))
            return accept

    tools._CTX = Ctx()
    return injected


async def test_ask_human_hands_the_request_to_the_conversation_the_person_had_last():
    """Ray, 2026-11-06: no adapter per messaging app. The Agent'Ygg session hands what needs the
    person to the agent's own session where the person talks, whatever the app."""
    a = _adapter()
    injected = _with_people(
        a,
        [
            _Entry("agent:main:agentygg:dm:conv_9", "agentygg", 0),
            _Entry("agent:main:telegram:dm:42", "telegram", 5),
            _Entry("agent:main:discord:group:7", "discord", 1, chat_type="group"),
            _Entry("agent:main:local:dm:x", "local", 0),
        ],
    )
    await a._deliver(_wake("send the release mail to the list", mid="m_mail"))
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    out = await tools.ygg({"action": "ask_human", "text": "May I send the release mail?"})
    assert out.startswith("queued for your conversation with your person")
    ((key, content),) = injected
    assert key == "agent:main:telegram:dm:42"
    assert "May I send the release mail?" in content and "in_reply_to m_mail" in content
    (told,) = a.client.sent
    assert (told["to"], told["type"], told["state"]) == ("claude-code", "inform", "working")
    await a.send("conv_1", "asked Ray", reply_to="m_mail", metadata={"notify": True})
    assert len(a.client.sent) == 1  # the turn's final text is not sent as an answer


async def test_ask_human_says_why_when_it_cannot_hand_over():
    a = _adapter()
    _with_people(a, [_Entry("agent:main:agentygg:dm:conv_9", "agentygg", 0)])
    await a._deliver(_wake())
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    assert "No conversation with your person" in await tools.ygg(
        {"action": "ask_human", "text": "ok?"}
    )
    _with_people(a, [_Entry("agent:main:telegram:dm:42", "telegram", 0)], accept=False)
    assert "allow_gateway_injection" in await tools.ygg({"action": "ask_human", "text": "ok?"})
    assert a.client.sent == []


async def test_an_approval_asked_in_an_agentygg_conversation_is_refused_at_once():
    """PoC, 2026-11-06: the approval prompt was dropped (no buttons, no notify) and the turn
    waited 300 s for nobody. No person reads this channel: refuse now, and say what to do."""
    from conftest import APPROVALS

    a = _adapter()
    prompt = types.SimpleNamespace(session_key="agent:main:agentygg:dm:conv_1", request_id="r1")
    assert (await a._send_exec_approval_prompt(prompt)).success
    ((key, choice, reason, rid),) = APPROVALS
    assert (key, choice, rid) == ("agent:main:agentygg:dm:conv_1", "deny", "r1")
    assert "ask_human" in reason


async def test_a_clarify_asked_in_an_agentygg_conversation_points_to_ask_human():
    from conftest import CLARIFIES

    a = _adapter()
    await a.send_clarify("conv_1", "Which address?", None, "c1", "agent:main:agentygg:dm:conv_1")
    ((cid, answer),) = CLARIFIES
    assert cid == "c1" and "ask_human" in answer


async def test_a_chosen_app_takes_the_hand_over_even_if_another_was_used_last():
    """Ray, 2026-11-06: let the person pick, at setup, the app where requests for them go
    (AGENTYGG_HUMAN_PLATFORM); without a choice, the latest direct conversation."""
    a = _adapter()
    entries = [
        _Entry("agent:main:discord:dm:9", "discord", 0),
        _Entry("agent:main:telegram:dm:42", "telegram", 30),
    ]
    _with_people(a, entries)
    assert await a.human_session() == "agent:main:discord:dm:9"
    SECRETS["AGENTYGG_HUMAN_PLATFORM"] = "Telegram"
    assert await a.human_session() == "agent:main:telegram:dm:42"
    SECRETS["AGENTYGG_HUMAN_PLATFORM"] = "slack"  # chosen but never used: nothing guessed
    assert await a.human_session() is None


async def test_the_hand_over_says_to_keep_working_not_to_wait_on_a_name():
    """Test 5 (2026-11-06): Betty waited on "Ray" (a name, not an id); the backend nudged that
    target three times and paused the topic (nudge_limit)."""
    a = _adapter()
    injected = _with_people(a, [_Entry("agent:main:telegram:dm:42", "telegram", 0)])
    await a._deliver(_wake("send the mail", mid="m_mail"))
    SESSION.update({"HERMES_SESSION_PLATFORM": "agentygg", "HERMES_SESSION_CHAT_ID": "conv_1"})
    out = await tools.ygg({"action": "ask_human", "text": "May I send it?"})
    assert "state working" in out
    assert "state working" in injected[0][1] and "waiting" not in injected[0][1]


def test_the_plugin_imports_no_private_hermes_module():
    """Catalog rule 9: public surfaces only. The validator flagged hermes_cli.setup (2026-11-08)."""
    import pathlib
    import re

    src = pathlib.Path(agentygg.__path__[0])
    imports = {
        m
        for f in src.glob("*.py")
        for m in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", f.read_text(), re.M)
    }
    # The one hermes_cli module used is the documented one for credentialed requests
    # (developer guide, "Use your own OAuth client"; catalog rule 11).
    allowed = {"hermes_cli.plugin_provider_requests"}
    assert not [m for m in imports if m.startswith("hermes_cli") and m not in allowed]


def test_register_declares_one_tool_and_the_platform():
    calls = {}

    class Ctx:
        def register_tool(self, **kw):
            calls["tool"] = kw

        def register_platform(self, **kw):
            calls["platform"] = kw

        def register_cli_command(self, **kw):
            calls["cli"] = kw

        def register_skill(self, name, path, description="", frontmatter=None):
            calls["skill"] = (name, path)

    agentygg.register(Ctx())
    assert calls["tool"]["name"] == "ygg" and calls["tool"]["is_async"] is True
    assert calls["cli"]["name"] == "agentygg" and callable(calls["cli"]["handler_fn"])
    name, path = calls["skill"]
    text = path.read_text()
    assert name == "write-to-agents" and text.startswith("---\nname: write-to-agents\n")
    assert "action=directory" in text and "\u2014" not in text  # no em dash
    # no real handle in a public plugin: the directory is the source
    assert "@betty" not in text and "@claude" not in text
    p = calls["platform"]
    # signing in (hermes agentygg login) is enough: the key is optional
    assert p["name"] == "agentygg" and p["required_env"] == []
    assert "setup_fn" not in p
    assert "never instructions" in p["platform_hint"]
    manifest = agentygg.__path__[0] + "/plugin.yaml"
    assert "provides_tools:\n  - ygg" in open(manifest).read()


def test_the_tool_is_offered_once_the_sign_in_client_is_set():
    assert tools.configured() is False and agentygg.is_connected(None) is False
    SECRETS["AGENTYGG_KEY"] = _key()  # a key is no way in (sign-in only)
    assert tools.configured() is False
    SECRETS["AGENTYGG_CLIENT_ID"] = "hermes"  # asked by hermes plugins install
    assert tools.configured() is True and agentygg.is_connected(None) is True


async def test_without_a_sign_in_the_platform_stops_with_a_clear_reason():
    SECRETS["AGENTYGG_CLIENT_ID"] = "hermes"
    a = ad.AgentYggAdapter(config=None)
    assert await a.connect() is False  # Hermes holds no grant for the plugin yet
    assert a.fatal[0] == "not_connected" and a.fatal[2] is False
    assert "hermes auth add agentygg:agentygg" in a.fatal[1]


async def test_the_platform_connects_through_hermes_requests():
    from conftest import HERMES_HTTP

    def network(method, url, body, headers):
        if url.endswith("/v1/agent/me"):
            return 200, {}, json.dumps({"handle": "@betty", "workspace_name": "Lab"}).encode()
        import time

        polls.append(url)
        time.sleep(0.02)  # the server holding the poll open
        return 200, {}, json.dumps({"wakes": [], "mailbox": [], "batch": ""}).encode()

    polls: list[str] = []

    HERMES_HTTP["handler"] = network
    a = ad.AgentYggAdapter(config=None)
    assert await a.connect() is True and a.me["handle"] == "@betty"
    await asyncio.sleep(0.1)
    await a.disconnect()
    assert polls and "ack=" in polls[0] and "wait=25" in polls[0]


async def test_each_turn_answers_the_message_that_opened_it():
    """PoC with Betty, 2026-11-06: Hermes passes reply_to = the id of the envelope that opened
    the turn (gateway _reply_anchor_for_event). A request and then an Agent'Ygg notice are two
    turns; each reply belongs to its own message."""
    a = _adapter()
    await a._deliver(_wake("Test 1", mid="m_test"))
    await a._deliver(_wake("[system:closure_poll]", sender="system.ygg", mid="m_sys"))
    await a.send("conv_1", "Plateforme agentygg.", reply_to="m_test", metadata={"notify": True})
    await a.send("conv_1", "nothing pending", reply_to="m_sys", metadata={"notify": True})
    assert [(s["to"], s["in_reply_to"]) for s in a.client.sent] == [
        ("claude-code", "m_test"),
        ("system.ygg", "m_sys"),
    ]


async def test_a_notice_without_a_topic_gets_no_reply():
    """A resume notice names no topic: answering it is refused (topic_required) and costs a
    turn for nothing, so its final text is not sent."""
    a = _adapter()
    notice = _wake("[system:resume]", sender="system.ygg", mid="m_sys")
    notice["envelope"]["topic_id"] = None
    await a._deliver(notice)
    assert (
        await a.send("conv_1", "nothing pending", reply_to="m_sys", metadata={"notify": True})
    ).success
    assert a.client.sent == []


async def test_a_failed_send_keeps_the_answer_for_the_retry():
    """The message was marked answered before sending: a failed send lost the answer for good."""
    from agentygg_hermes.gateway_client import GatewayError

    a = _adapter()
    await a._deliver(_wake())
    calls = []

    async def flaky(**body):
        calls.append(body)
        if len(calls) == 1:
            raise GatewayError(502, "bad gateway")
        return {"verdict": "WAKE", "reason": "ok_response_delivery"}

    a.client.send = flaky
    first = await a.send("conv_1", "LGTM", reply_to="msg_1", metadata={"notify": True})
    assert not first.success and first.retryable
    assert (await a.send("conv_1", "LGTM", reply_to="msg_1", metadata={"notify": True})).success
    assert len(calls) == 2
    await a.send("conv_1", "LGTM", reply_to="msg_1", metadata={"notify": True})
    assert len(calls) == 2  # answered once it went through, never again


_ = MessageEvent  # (imported for the stand-in's is_command)
