"""The plugins' ensure-ygg.sh: at session start, check the client. It installs nothing: when
`ygg` is missing or too old it tells the session, and the agent asks its user before
installing it (the harness's approval prompt is the consent)."""

import os
import shutil
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


def _tools(tmp_path):
    """Only what the script needs from the system, so the runner's own uv/pipx/brew stay out."""
    sys_bin = tmp_path / "sys"
    sys_bin.mkdir(exist_ok=True)
    for tool in ("awk", "uname"):
        link = sys_bin / tool
        if not link.exists():
            link.symlink_to(shutil.which(tool))
    return sys_bin


def _run(tmp_path, ygg_version: str | None, have: tuple[str, ...] = ()):
    tmp_path = tmp_path / f"run-{ygg_version}-{'-'.join(have) or 'bare'}"
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    called = tmp_path / "called"
    if ygg_version:
        y = bin_ / "ygg"
        y.write_text(f"#!/bin/sh\necho 'agentygg {ygg_version}'\n")
        y.chmod(0o755)
    for tool in ("curl", *have):  # none may run: nothing is installed without the user
        t = bin_ / tool
        t.write_text(f"#!/bin/sh\necho {tool} >> {called}\n")
        t.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_}:{_tools(tmp_path.parent)}"}
    r = subprocess.run(["/bin/sh", str(SCRIPTS[0])], env=env, capture_output=True, text=True)
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


def test_a_missing_client_asks_the_user_first(tmp_path):
    r = _run(tmp_path, None)
    assert r.returncode == 0 and "is not installed" in r.stdout
    assert "Nothing was installed" in r.stdout and "outside any sandbox" in r.stdout
    assert not r.stdout.startswith(("[", "{"))  # Codex reads such output as JSON


def test_the_install_offer_follows_what_the_machine_has(tmp_path):
    uv = _run(tmp_path, None, ("uv",)).stdout
    assert "uv tool install --force --python 3.12 agentygg" in uv and "install.sh" not in uv
    pipx = _run(tmp_path, None, ("pipx",)).stdout
    assert "pipx install --force agentygg" in pipx and "install.sh" not in pipx
    brew = _run(tmp_path, None, ("brew",)).stdout
    assert "brew install uv" in brew and "uv tool install" in brew and "install.sh" not in brew
    bare = _run(tmp_path, None).stdout
    assert "last resort" in bare and "curl -fsSL https://agentygg.com/install.sh | sh" in bare


def test_the_script_names_no_client_version():
    """Codex hashes the whole plugin and asks to trust its hooks again on any change: a client
    release must not touch it (2026-11-24)."""
    script = SCRIPTS[0].read_text()
    version = (ROOT / "pyproject.toml").read_text().split('version = "')[1].split('"')[0]
    assert "LATEST" not in script and "agentygg==" not in script and version not in script


def test_a_relay_turn_is_left_alone(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "YGG_RELAY": "1"}
    r = subprocess.run(["sh", str(SCRIPTS[0])], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
