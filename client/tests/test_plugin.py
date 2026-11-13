"""The Claude Code plugin (2026-11-13): its monitor keeps listening for the whole session
(`ygg listen --follow --here`), its MCP server acts only as the agent this folder belongs to, and
`ygg setup claude --plugin` leaves the MCP server and the skill to the plugin."""

import asyncio
import json
import types

import pytest
from test_listen import FakeGw, _frame

from agentygg import cli, mcp_server, store
from agentygg import listen as lst
from agentygg.api import GatewayError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("YGG_INSTANCE", raising=False)
    monkeypatch.delenv("YGG_AGENT_ID", raising=False)
    monkeypatch.delenv("YGG_RELAY", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _follow(gw, calls):
    """Run follow() until the gateway has been asked `calls` times; no real waiting."""
    out = []

    async def no_sleep(_s):
        return None

    code = asyncio.run(
        lst.follow(gw, out=out.append, alive=lambda: len(gw.waits) < calls, sleep=no_sleep)
    )
    return code, out


def test_follow_delivers_each_message_and_keeps_listening(home):
    poll = _frame("system.ygg", "system", "closure poll")
    gw = FakeGw(
        [
            {"wakes": [_frame("agent.betty", "agent", "first")], "mailbox": []},
            {"wakes": [poll], "mailbox": []},
            {"wakes": [], "mailbox": [_frame("human.ray", "human", "second")]},
        ]
    )
    code, out = _follow(gw, calls=4)
    assert code == lst.STOPPED  # only because the session is gone
    assert len(out) == 2  # one block per delivery; the poll waited for the next real message
    assert out[0].startswith(lst.FOLLOW_LEAD) and "first" in out[0]
    assert "closure poll" in out[1] and "second" in out[1]
    assert not store.listener_active("bot")  # handed back to the relay at once


def test_a_one_off_refusal_wakes_nobody_and_a_lasting_one_is_told_once(home):
    once = FakeGw(
        [GatewayError(401, "unauthorized"), {"wakes": [_frame("agent.x", "agent", "hi")]}]
    )
    _, out = _follow(once, calls=3)
    assert len(out) == 1 and "hi" in out[0]  # the hiccup never reached the session

    lasting = FakeGw([GatewayError(401, "unauthorized")] * 6)
    _, out = _follow(lasting, calls=6)
    assert len(out) == 1 and "not reaching this session" in out[0] and "unauthorized" in out[0]


def _listen_args(**kw):
    base = dict(agent=None, all=False, timeout=0.0, while_pid=None, follow=True, here=True)
    return types.SimpleNamespace(**{**base, **kw})


def test_the_monitor_leaves_quietly_in_a_folder_of_no_agent(home, capsys):
    store.save(store.Identity("solo", "lab"))  # the only agent here: still not this folder's
    with pytest.raises(SystemExit) as e:
        cli.cmd_listen(_listen_args(follow=False))
    assert e.value.code == 0
    assert capsys.readouterr() == ("", "")


def test_the_monitor_waits_quietly_until_the_folder_is_connected(home, capsys):
    """Connecting the folder in an open session needs no restart: the monitor was waiting."""
    store.save(store.Identity("solo", "lab"))
    naps = []

    def nap(_s):
        naps.append(_s)
        if len(naps) == 3:
            store.write_pointer(home, "solo")  # `ygg connect --plugin` ran meanwhile

    ident = cli._here_identity(wait=True, sleep=nap)
    assert ident.agent_id == "solo" and len(naps) == 3
    assert capsys.readouterr() == ("", "")
    assert cli._here_identity(wait=False).agent_id == "solo"


def test_several_agents_and_no_folder_agent_says_how_to_connect(home):
    store.save(store.Identity("one", "lab"))
    store.save(store.Identity("two", "lab"))
    with pytest.raises(LookupError) as e:
        store.resolve()
    assert "belongs to no agent" in str(e.value) and "ygg connect" in str(e.value)
    assert "@one" in str(e.value) and "@two" in str(e.value)


def test_the_plugin_server_acts_only_as_this_folders_agent(home):
    store.save(store.Identity("solo", "lab"))
    assert mcp_server.plugin_identity() is None  # never "the only agent on this machine"
    store.write_pointer(home, "solo")
    assert mcp_server.plugin_identity().agent_id == "solo"


def test_tools_act_under_the_live_listeners_lease(home):
    store.touch_listen("solo", "listener-nonce")
    gw = types.SimpleNamespace(identity=types.SimpleNamespace(agent_id="solo"), nonce="own")
    http = types.SimpleNamespace(headers={"X-Ygg-Instance": "own"})
    mcp_server.follow_listener(gw, http)
    assert gw.nonce == http.headers["X-Ygg-Instance"] == "listener-nonce"
    store.clear_listen("solo")
    gw.nonce = http.headers["X-Ygg-Instance"] = "own"
    mcp_server.follow_listener(gw, http)  # nobody listening: the session keeps its own
    assert gw.nonce == "own"


def test_setup_with_the_plugin_only_binds_the_folder(home, capsys):
    store.save(store.Identity("solo", "lab"))
    (home / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"agentygg": {"command": "ygg"}, "other": {"command": "x"}}})
    )
    skill = home / ".claude" / "skills" / "agentygg"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("old")
    cli.cmd_setup(
        types.SimpleNamespace(setup_cmd="claude", agent="solo", harness_path=None, plugin=True)
    )
    assert store.pointer_for(home) == ("solo", home)
    assert json.loads((home / ".mcp.json").read_text()) == {
        "mcpServers": {"other": {"command": "x"}}
    }
    assert not skill.exists() and not (home / ".agents").exists()
    assert "claude --continue" in capsys.readouterr().out


def _rendered(text, mid="msg_1"):
    from agentygg.render import render_message

    f = _frame("human.ray", "human", text)
    f["envelope"]["id"] = mid
    f["conversation_id"] = "conv_1"
    return render_message(f)


def test_a_long_message_is_shaped_to_fit_the_notification():
    """Claude Code cuts a monitor notification near 500 characters a line and 3,000 in all."""
    long = "mot " * 4000  # one 16,000-character line
    out = lst.fit_delivery([_rendered(long, "msg_long")])
    lines = out.split("\n")
    assert len(out) <= lst.MONITOR_TOTAL + 400
    assert all(len(ln) <= 500 for ln in lines)
    assert "read the whole message" in out and "msg_long" in out
    assert lines[-1].startswith("</agentygg-message")  # the frame stays whole


def test_short_messages_pass_whole_and_several_share_the_room():
    short = _rendered("bonjour, une question rapide")
    out = lst.fit_delivery([short])
    assert out == lst.FOLLOW_LEAD + "\n\n" + short
    many = lst.fit_delivery([_rendered("x " * 2000, f"m{i}") for i in range(3)])
    assert len(many) <= lst.MONITOR_TOTAL + 600
    assert all(f"m{i}" in many for i in range(3))  # each one says where to read it whole


def test_a_pointer_this_machine_did_not_write_is_ignored(home):
    """YGG-SEC-2026-11-13-01: a .agentygg shipped in a cloned repository (or a shared parent
    folder) must not make the plugin act as an agent signed in on this machine."""
    store.save(store.Identity("solo", "lab"))
    (home / ".agentygg").write_text("solo\n")  # came with the repository
    assert store.pointer_for(home) == ("solo", home)  # the CLI still sees it
    assert mcp_server.plugin_identity() is None
    assert cli._here_identity(wait=False) is None
    cli.cmd_agent_use(types.SimpleNamespace(agent_id="solo"))  # the user vouches for it
    assert mcp_server.plugin_identity().agent_id == "solo"
    (home / ".agentygg").write_text("other\n")  # swapped afterwards: not what was recorded
    assert mcp_server.plugin_identity() is None
