"""The plugins' ensure-ygg.sh: at session start, check the client. It installs nothing: when
`ygg` is missing or too old it tells the session, and the agent asks its user before
installing it (the harness's approval prompt is the consent)."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [
    p
    for p in (
        ROOT / "codex-plugin" / "scripts" / "ensure-ygg.sh",
        ROOT / "claude-plugin" / "scripts" / "ensure-ygg.sh",
        ROOT.parent / "plugins" / "codex" / "scripts" / "ensure-ygg.sh",
        ROOT.parent / "plugins" / "claude-code" / "scripts" / "ensure-ygg.sh",
    )
    if p.is_file()
]
pytestmark = pytest.mark.skipif(os.name == "nt" or not SCRIPTS, reason="POSIX plugins only")


def _run(tmp_path, ygg_version: str | None):
    tmp_path = tmp_path / f"run-{ygg_version}"
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    if ygg_version:
        y = bin_ / "ygg"
        y.write_text(f"#!/bin/sh\necho 'agentygg {ygg_version}'\n")
        y.chmod(0o755)
    called = tmp_path / "curl-called"
    curl = bin_ / "curl"  # must never run: nothing is installed without the user
    curl.write_text(f"#!/bin/sh\ntouch {called}\n")
    curl.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin"}
    r = subprocess.run(["sh", str(SCRIPTS[0])], env=env, capture_output=True, text=True)
    assert not called.exists()
    return r


def test_the_plugins_ship_the_same_script():
    assert len({p.read_text() for p in SCRIPTS}) == 1


def test_an_up_to_date_client_says_nothing(tmp_path):
    for v in ("0.7.20", "0.10.0"):
        r = _run(tmp_path, v)
        assert r.returncode == 0 and r.stdout == ""


def test_an_older_client_asks_the_user_first(tmp_path):
    r = _run(tmp_path, "0.7.9")
    assert r.returncode == 0 and "is 0.7.9, older than" in r.stdout
    assert "ask your user whether you may update it" in r.stdout
    assert "curl -fsSL https://agentygg.com/install.sh | sh" in r.stdout


def test_a_missing_client_asks_the_user_first(tmp_path):
    r = _run(tmp_path, None)
    assert r.returncode == 0 and "is not installed" in r.stdout
    assert "Nothing was installed" in r.stdout and "outside any sandbox" in r.stdout
    assert not r.stdout.startswith(("[", "{"))  # Codex reads such output as JSON


def test_a_relay_turn_is_left_alone(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "YGG_RELAY": "1"}
    r = subprocess.run(["sh", str(SCRIPTS[0])], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
