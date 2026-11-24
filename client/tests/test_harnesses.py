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
    # Unattended: the agentygg tools are approved (else ygg_send is refused), nothing else.
    approve = 'mcp_servers.agentygg.default_tools_approval_mode="approve"'
    assert approve in new and new[new.index(approve) - 1] == "-c"
    assert not any("sandbox" in a or "bypass" in a or "approval_policy" in a for a in new)
    resumed = h.prepare("hi", MCP, "th1", [], "/w").cmd
    assert resumed[:4] == ["/bin/codex", "exec", "resume", "th1"] and resumed[-1] == "hi"
    assert approve in resumed
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
    # a relay Windows will not start is not retried forever (Smart App Control, 2026-11-16)
    assert "set /a fails+=1" in cmd and "if %fails% GEQ 10" in cmd
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


@pytest.mark.parametrize("said", ["opencode v2.3.2", "2.4.0", "3.0.1"])
def test_opencode_2_runs_its_own_server_in_the_folder(monkeypatch, said):
    """2.x has no --dir (the turn runs in the folder anyway), and its background server does not
    see the MCP server given to this run (2.3.2: no agentygg tool at all): --standalone does."""
    monkeypatch.setattr(harnesses, "_version_text", lambda exe: said)
    t = harnesses.build("opencode").prepare("hi", MCP, "ses_1", ["--auto"], "/w")
    assert "--dir" not in t.cmd and "--standalone" in t.cmd
    assert t.cmd[t.cmd.index("--session") + 1] == "ses_1" and t.cmd[-1] == "--auto"
    assert "agentygg" in json.loads(t.env["OPENCODE_CONFIG_CONTENT"])["mcp"]


def test_opencode_version_is_asked_once(monkeypatch):
    asked = []
    monkeypatch.setattr(harnesses, "_version_text", lambda exe: asked.append(exe) or "2.4.0")
    h = harnesses.build("opencode")
    for _ in range(3):
        h.prepare("hi", MCP, None, [], "/w")
    assert len(asked) == 1


def test_a_version_that_cannot_be_read_is_empty():
    assert harnesses._version_text("/no/such/program") == ""


def test_the_cli_inside_the_opencode_app_is_a_candidate(monkeypatch, tmp_path):
    cli = tmp_path / "Library/Application Support/ai.opencode.desktop/cli/2.3.2-arm64/opencode-cli"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    monkeypatch.setattr(harnesses.Path, "home", lambda: tmp_path)
    seen = []
    monkeypatch.setattr(harnesses, "find_executable", lambda n, c: seen.extend(c) or "/x")
    harnesses.build("opencode").exe()
    assert cli in seen


@pytest.mark.parametrize("said", ["1.19.4", ""])
def test_opencode_run_with_injected_mcp_config(monkeypatch, said):
    monkeypatch.setattr(harnesses, "_version_text", lambda exe: said)
    h = harnesses.build("opencode")
    t = h.prepare("hi", MCP, "ses_1", ["--auto"], "/w")
    assert t.cmd[:4] == ["/bin/opencode", "run", "--format", "json"]
    assert (
        t.cmd[t.cmd.index("--dir") + 1] == "/w" and t.cmd[t.cmd.index("--session") + 1] == "ses_1"
    )
    assert t.cmd[-1] == "--auto" and t.stdin == b"hi"
    cfg = json.loads(t.env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["mcp"]["agentygg"]["command"] == ["/py", "-m", "agentygg.cli", "mcp"]
    assert cfg["mcp"]["agentygg"]["environment"]["YGG_INSTANCE"] == "n1"
    out = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "step_start", "sessionID": "ses_2"},
            {"type": "text", "sessionID": "ses_2", "part": {"text": "PONG"}},
        ]
    )
    r = h.parse(0, out, "")
    assert r.ok and r.session_id == "ses_2" and r.summary == "PONG"
    bad = h.parse(
        1, json.dumps({"type": "error", "sessionID": "x", "error": {"message": "no provider"}}), ""
    )
    assert not bad.ok and bad.summary == "no provider"


def test_harness_detected_from_the_process_tree():
    from agentygg import detect

    table = {
        100: (1, "/Users/x/.local/share/claude/versions/2.1.186"),
        200: (100, "/bin/zsh -c source /Users/x/.claude/shell-snapshots/s.sh && ygg connect"),
        300: (200, "/usr/bin/python3 /Users/x/.local/bin/ygg connect"),
        400: (1, "node /usr/lib/node_modules/opencode-ai/bin/opencode run --format json"),
        410: (400, "bash -c ygg connect"),
        500: (1, "/home/u/.hermes/venv/bin/python /home/u/.local/bin/hermes chat"),
        510: (500, "sh -c ygg connect"),
        600: (1, "codex exec --json"),
        700: (1, "/bin/zsh -l"),
        710: (700, "ygg connect"),
        800: (1, "vim /home/u/my-claude-notes/todo.md"),
        900: (1, "/bin/zsh -c cat > t.py <<EOF opencode run --format json EOF && ygg connect"),
        1000: (
            1,
            "/Users/x/.vscode/extensions/anthropic.claude-code-2.1.3/resources/native-binary/claude --output-format stream-json",
        ),
        1100: (1, "C:\\Users\\u\\AppData\\Roaming\\npm\\codex.cmd exec"),
        1200: (
            1,
            "node C:\\Users\\u\\AppData\\Roaming\\npm\\node_modules\\openclaw\\openclaw.mjs agent exec",
        ),
    }
    assert detect.from_ancestors(table, start=900) is None  # a harness NAME in arguments is not it
    assert detect.from_ancestors(table, start=1000) == "claude"
    assert detect.from_ancestors(table, start=1100) == "codex"
    assert detect.from_ancestors(table, start=1200) == "openclaw"
    assert detect.from_ancestors(table, start=300) == "claude"
    assert detect.from_ancestors(table, start=410) == "opencode"
    assert detect.from_ancestors(table, start=510) == "hermes"
    assert detect.from_ancestors(table, start=600) == "codex"
    assert detect.from_ancestors(table, start=710) is None  # a plain terminal: no harness above
    assert detect.from_ancestors(table, start=800) is None  # a folder name is not a harness
    assert detect.from_environment({"CLAUDECODE": "1"}) == "claude"
    assert detect.from_environment({}) is None


def test_opencode_says_what_its_error_is():
    """`opencode run --format json` nests it (1.19.4: an expired sign-in read as just "error")."""
    h = harnesses.build("opencode")
    for error, expected in (
        ({"name": "UnknownError", "data": {"message": "Token refresh failed: 401"}}, "Token refresh"),
        ({"message": "flat"}, "flat"),
        ({"name": "ProviderAuthError", "data": {}}, "ProviderAuthError"),
        ({}, "error"),
    ):
        r = h.parse(0, json.dumps({"type": "error", "sessionID": "s", "error": error}), "")
        assert not r.ok and expected in r.summary
