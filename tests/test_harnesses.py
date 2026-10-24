"""Each harness builds the right non-interactive command and reads its own output format
(formats from each project's documented CLI output). No harness or network needed."""

import json
from pathlib import Path

import pytest

from agentygg import harnesses, service
from agentygg.harnesses import McpSpec

MCP = McpSpec("/py", ["-m", "agentygg.cli", "mcp"], {"YGG_AGENT_ID": "bot", "YGG_INSTANCE": "n1"})


@pytest.fixture(autouse=True)
def fake_exes(monkeypatch):
    monkeypatch.setattr(harnesses, "find_executable", lambda name, cands: f"/bin/{name}")


def test_claude_command_and_result():
    h = harnesses.build("claude")
    t = h.prepare("hi", MCP, "s1", ["--model", "opus"], "/w")
    assert (
        t.cmd[:3] == ["/bin/claude", "-p", "hi"]
        and "--resume" in t.cmd
        and t.cmd[-2:] == ["--model", "opus"]
    )
    assert (
        json.loads(Path(t.cmd[t.cmd.index("--mcp-config") + 1]).read_text())["mcpServers"][
            "agentygg"
        ]["env"]["YGG_INSTANCE"]
        == "n1"
    )
    r = h.parse(0, json.dumps({"session_id": "s2", "result": "done", "is_error": False}), "")
    assert r.ok and r.session_id == "s2"


def test_codex_new_and_resumed_turns():
    h = harnesses.build("codex")
    new = h.prepare("hi", MCP, None, [], "/w").cmd
    assert new[:3] == ["/bin/codex", "exec", "--json"] and new[-1].endswith("hi")
    assert "Protocol" in new[-1]  # a new session gets the protocol up front
    assert 'mcp_servers.agentygg.env={YGG_AGENT_ID="bot",YGG_INSTANCE="n1"}' in new
    resumed = h.prepare("hi", MCP, "th1", [], "/w").cmd
    assert resumed[:4] == ["/bin/codex", "exec", "resume", "th1"] and resumed[-1] == "hi"
    out = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "thread.started", "thread_id": "th9"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "PONG"}},
            {"type": "turn.completed"},
        ]
    )
    r = h.parse(0, out, "")
    assert r.ok and r.session_id == "th9" and r.summary == "PONG"
    bad = h.parse(1, json.dumps({"type": "turn.failed", "error": {"message": "token expired"}}), "")
    assert not bad.ok and "token expired" in bad.summary


def test_hermes_one_shot_with_stdin_prompt():
    h = harnesses.build("hermes")
    t = h.prepare("hi", MCP, "abc", [], "/w")
    assert t.cmd[:2] == ["/bin/hermes", "chat"] and "--oneshot" in t.cmd and "--query-file" in t.cmd
    assert t.cmd[t.cmd.index("--in") + 1] == "/w" and t.cmd[t.cmd.index("--resume") + 1] == "abc"
    assert t.stdin == b"hi"
    out = "\n".join(
        [
            json.dumps({"type": "text", "text": "PO"}),
            json.dumps({"type": "result", "session_id": "h2", "exit_code": 0, "text": "PONG"}),
        ]
    )
    r = h.parse(0, out, "session_id: h2")
    assert r.ok and r.session_id == "h2" and r.summary == "PONG"


def test_openclaw_exec_injects_the_mcp_server():
    h = harnesses.build("openclaw")
    t = h.prepare("hi", MCP, None, [], "/w")
    assert (
        t.cmd[:3] == ["/bin/openclaw", "agent", "exec"] and t.cmd[t.cmd.index("--cwd") + 1] == "/w"
    )
    override = json.loads(t.cmd[t.cmd.index("--config-override") + 1])
    assert override["mcp"]["servers"]["agentygg"]["command"] == "/py"
    assert t.stdin.endswith(b"hi")
    r = h.parse(0, json.dumps({"ok": True, "status": "ok", "final": "PONG", "sessionId": "o1"}), "")
    assert r.ok and r.summary == "PONG"
    fail = h.parse(
        1, json.dumps({"ok": False, "status": "error", "error": {"message": "no model"}}), ""
    )
    assert not fail.ok and fail.summary == "no model"


def test_command_harness_and_unknown_kind():
    h = harnesses.build("command", command="my-agent --once")
    t = h.prepare("hi", MCP, None, [], "/w")
    assert t.stdin == b"hi" and "my-agent --once" in t.cmd
    assert h.parse(0, "line1\nanswered\n", "").summary == "answered"
    with pytest.raises(ValueError):
        harnesses.build("command")
    with pytest.raises(ValueError):
        harnesses.build("nope")


def test_windows_launchers(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_HOME", str(tmp_path / "home"))
    cmd, vbs = service.windows_launchers(
        "bot", tmp_path / "proj", [r"C:\Tools\ygg.exe", "agent", "run", "--agent", "bot"]
    )
    assert ":loop" in cmd and "if %ERRORLEVEL% EQU 0 exit /b 0" in cmd and "YGG_SERVICE=1" in cmd
    assert r"C:\Tools\ygg.exe agent run --agent bot" in cmd and "bot.log" in cmd
    assert vbs.startswith('CreateObject("WScript.Shell").Run "cmd /c ""') and ", 0, False" in vbs


def test_service_argv_carries_the_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_ygg_executable", lambda: "/opt/bin/ygg")
    argv = service.relay_argv("bot", tmp_path, ["-m", "x"], harness="codex")
    assert (
        argv[argv.index("--harness") + 1] == "codex"
        and Path(argv[argv.index("--harness-path") + 1]).name == "codex"
    )
    argv = service.relay_argv("bot", tmp_path, [], harness="command", cmd="run-it")
    assert "--harness-path" not in argv and argv[argv.index("--cmd") + 1] == "run-it"
