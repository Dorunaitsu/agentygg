"""The plugins' ensure-ygg.sh: at session start, install or update the client only when it is
missing or older than the plugin needs, with the official installer, and say so."""

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
    marker = tmp_path / "installed"
    curl = bin_ / "curl"  # the "installer" it downloads leaves a mark and a ygg
    new = home / ".local" / "bin" / "ygg"
    installer = (
        f"touch {marker}; printf '#!/bin/sh\\necho agentygg 0.7.14\\n' > {new}; chmod +x {new}"
    )
    curl.write_text(f'#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\necho "{installer}" > "$2"\n')
    curl.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin"}
    r = subprocess.run(
        ["sh", str(SCRIPTS[0])], env=env, capture_output=True, text=True, timeout=30
    )
    return r, marker.exists()


def test_the_plugins_ship_the_same_script():
    assert len({p.read_text() for p in SCRIPTS}) == 1


def test_an_up_to_date_client_is_left_alone(tmp_path):
    r, installed = _run(tmp_path, "0.7.14")
    assert r.returncode == 0 and r.stdout == "" and not installed
    r, installed = _run(tmp_path, "0.10.0")
    assert r.returncode == 0 and r.stdout == "" and not installed


def test_an_older_client_is_updated_and_the_session_told(tmp_path):
    r, installed = _run(tmp_path, "0.7.9")
    assert r.returncode == 0 and installed
    assert "updated the ygg client from 0.7.9" in r.stdout and "new session" in r.stdout


def test_a_missing_client_is_installed_and_the_session_told(tmp_path):
    r, installed = _run(tmp_path, None)
    assert r.returncode == 0 and installed
    assert "installed the ygg client" in r.stdout
    assert not r.stdout.startswith(("[", "{"))  # Codex reads such output as JSON


def test_a_failed_install_says_how_to_do_it_by_hand(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "home").mkdir()
    curl = bin_ / "curl"
    curl.write_text("#!/bin/sh\nexit 22\n")
    curl.chmod(0o755)
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{bin_}:/usr/bin:/bin"}
    r = subprocess.run(["sh", str(SCRIPTS[0])], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "install it by hand" in r.stdout
    assert "agentygg.com/docs/troubleshooting" in r.stdout
