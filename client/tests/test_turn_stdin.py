"""A harness turn must never wait on the relay's stdin. On Python 3.11, asyncio's
`communicate(None)` leaves a stdin pipe open (3.12 closes it), and Codex (0.171), given its prompt
as an argument, still reads a piped stdin to its end: the turn hung until its 30-minute timeout
(found live, 2026-11-24)."""

import asyncio
import sys

from agentygg import store
from agentygg.harnesses import Turn
from agentygg.relay import Relay

READS_STDIN = [sys.executable, "-c", "import sys; print('got:' + sys.stdin.read())"]


def _run(tmp_path, monkeypatch, stdin):
    monkeypatch.setenv("YGG_HOME", str(tmp_path / ".ygg"))
    ident = store.Identity(agent_id="bot", workspace_id="w")
    relay = Relay(ident, tmp_path, command=["true"], turn_timeout_s=8.0)
    return asyncio.run(relay._exec(Turn(READS_STDIN, stdin=stdin), "hi"))


def test_a_turn_without_stdin_reads_an_empty_one(tmp_path, monkeypatch):
    code, out, err = _run(tmp_path, monkeypatch, None)
    assert (code, out.strip()) == (0, "got:"), err


def test_a_turn_with_stdin_gets_all_of_it(tmp_path, monkeypatch):
    code, out, err = _run(tmp_path, monkeypatch, b"the prompt")
    assert (code, out.strip()) == (0, "got:the prompt"), err
