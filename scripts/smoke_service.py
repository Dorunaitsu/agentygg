"""CI smoke test of a REAL background relay on this OS (Task Scheduler / systemd / launchd).

Stores an agent whose key the gateway will refuse, installs its relay as a service with the
generic `command` harness, then checks: the service starts, `ygg relay list` shows it, the
relay reaches the gateway and stops cleanly on the refused key (service mode exits 0, no
restart loop), and `ygg relay remove` cleans up. Uses only the public gateway's 401.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agentygg import service, store

AGENT = "ci-smoke"


def ygg(*args: str) -> str:
    r = subprocess.run(["ygg", *args], check=False, capture_output=True, text=True)
    print(f"$ ygg {' '.join(args)}\n{r.stdout}{r.stderr}", flush=True)
    if r.returncode:
        raise SystemExit(f"ygg {args[0]} failed ({r.returncode})")
    return r.stdout


def main() -> None:
    try:
        kind = service.system()
    except RuntimeError as e:
        print(f"SKIP: {e}")
        return
    if kind == "systemd" and subprocess.run(
        ["systemctl", "--user", "status"], check=False, capture_output=True
    ).returncode not in (0, 3):
        print("SKIP: no systemd user session on this runner")
        return
    raw = json.dumps({"w": "ci-smoke-ws", "a": AGENT, "s": "not-a-real-secret"}).encode()
    key = "yga_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    store.save(store.Identity(AGENT, "ci-smoke-ws", key))
    work = Path(tempfile.mkdtemp(prefix="ygg-ci-"))
    store.write_pointer(work, AGENT)
    os.chdir(work)
    ygg("relay", "install", "--harness", "command", "--cmd", "echo hello")
    log = service.log_path(AGENT)
    stopped = False
    for _ in range(60):
        time.sleep(2)
        text = log.read_text(errors="replace") if log.exists() else ""
        if "relay stops" in text or "stopped:" in text:
            stopped = True
            break
    print("---- relay log ----")
    print(log.read_text(errors="replace") if log.exists() else "(no log)")
    listing = ygg("relay", "list")
    assert AGENT in listing, "the relay is not listed"
    assert stopped, "the relay did not reach the gateway and stop on the refused key"
    time.sleep(5)
    after = log.read_text(errors="replace").count("harness: ")
    assert after == 1, f"the relay was restarted {after - 1} time(s) after a clean stop"
    ygg("relay", "remove", AGENT)
    assert AGENT not in ygg("relay", "list"), "the relay is still listed after remove"
    print(f"OK: real {kind} relay started, stopped cleanly on a refused key, removed")


if __name__ == "__main__":
    sys.exit(main())
