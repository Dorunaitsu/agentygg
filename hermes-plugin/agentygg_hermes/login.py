"""`hermes agentygg login | status | logout`: setting the plugin up, from a terminal.

Signing in is Hermes' own (catalog rule 11): the plugin declares Agent'Ygg's OAuth client under
`oauth:` in `plugin.yaml`, and `hermes auth add agentygg:agentygg` opens the browser on
Agent'Ygg, where the person signs in and picks (or creates) the workspace and the agent. Hermes
keeps the grant in the profile and refreshes it; the plugin never sees a token. `login` asks
first whether the agent may bring the person its requests (Ray, 2026-11-08: recommended, the
default), then runs that sign-in.

Hermes refuses `hermes auth add` from the gateway, cron or an agent session: the person runs it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable

from .gateway_client import OAUTH_ID

INJECTION_SETTING = "plugins.entries.agentygg.allow_gateway_injection"
AUTH_KEY = f"agentygg:{OAUTH_ID}"


def _hermes_commands(argv: list[str]) -> list[list[str]]:
    """Ways to run Hermes' own CLI, best first, keeping the global options this command was
    started with (`hermes -p work agentygg login` sets the `work` profile, not the default).
    The first is how this process was started; the next, `hermes` on the PATH."""
    i = argv.index("agentygg", 1) if "agentygg" in argv[1:] else 1
    head, opts = argv[0] if argv else "", argv[1:i]
    out: list[list[str]] = []
    if head and os.path.basename(head).startswith("hermes") and os.access(head, os.X_OK):
        out.append([head, *opts])
    found = shutil.which("hermes")
    if found and [found, *opts] not in out:
        out.append([found, *opts])
    if head.endswith(".py"):
        out.append([sys.executable, head, *opts])
    return out


def _run_hermes(
    args: list[str],
    argv: list[str] | None = None,
    run: Callable[..., object] = subprocess.run,
    capture: bool = True,
) -> tuple[bool, str]:
    """Run `hermes <args>` on this profile. (ok, what went wrong). `capture=False` leaves the
    terminal to Hermes (the browser sign-in talks to the person)."""
    why = "hermes was not found on the PATH"
    for base in _hermes_commands(argv or sys.argv):
        cmd = [*base, *args]
        try:
            if capture:
                r = run(cmd, capture_output=True, text=True, timeout=60)
            else:
                r = run(cmd)
        except (OSError, subprocess.SubprocessError) as e:
            why = str(e)
            continue
        if getattr(r, "returncode", 1) == 0:
            return True, ""
        why = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "").strip()[-300:]
        if not capture:  # Hermes ran and said why on the terminal: do not run it twice
            return False, why or f"exit {getattr(r, 'returncode', '?')}"
    return False, why


def allow_questions(
    argv: list[str] | None = None, run: Callable[..., object] = subprocess.run
) -> tuple[bool, str]:
    """Turn on what ask_human needs (Hermes' allow_gateway_injection), through Hermes' own
    `config set`, as chosen at the start of the login."""
    return _run_hermes(["config", "set", INJECTION_SETTING, "true"], argv, run)


QUESTIONS_PROMPT = (
    "Receive your agent's requests in the chat you use with it (an approval, an email to send: "
    "what only you can decide)? Recommended. [Y/n] "
)
QUESTIONS_COMMAND = f"hermes config set {INJECTION_SETTING} true"


def _ask_tty(question: str) -> bool:
    """Yes unless the person says no; the recommended yes with nobody at the terminal."""
    if not sys.stdin.isatty():
        return True
    try:
        return input(question).strip().lower() not in ("n", "no", "non")
    except EOFError:
        return True


# --------------------------------------------------------------------------- #
# `hermes agentygg ...` (ctx.register_cli_command)
# --------------------------------------------------------------------------- #


def setup_cli(parser) -> None:
    sub = parser.add_subparsers(dest="agentygg_cmd")
    p = sub.add_parser("login", help="connect this Hermes to an Agent'Ygg agent (in a browser)")
    p.add_argument(
        "--no-questions",
        action="store_true",
        help="do not let the agent bring you its requests in your chat (default: it does)",
    )
    sub.add_parser("status", help="which agent this Hermes is on Agent'Ygg")
    sub.add_parser("logout", help="disconnect this Hermes from Agent'Ygg")


def _status() -> int:
    import asyncio

    from .gateway_client import AgentYggClient, GatewayError, NotSignedIn

    try:
        me = asyncio.run(AgentYggClient().me())
    except NotSignedIn as e:
        print(f"Not connected: {e}")
        return 1
    except GatewayError as e:
        print(f"Agent'Ygg refused this sign-in ({e.status}): {e.detail}")
        return 1
    handle = str(me.get("handle") or me.get("agent_id") or "").lstrip("@")
    print(f'Connected as @{handle} in "{me.get("workspace_name") or me.get("workspace_id")}".')
    return 0


def run_cli(
    args,
    ask: Callable[[str], bool] = _ask_tty,
    run: Callable[..., object] = subprocess.run,
) -> int:
    cmd = getattr(args, "agentygg_cmd", None) or "status"
    if cmd == "login":
        questions = False if getattr(args, "no_questions", False) else ask(QUESTIONS_PROMPT)
        print("Sign in to Agent'Ygg in the browser and pick the agent this Hermes will be.")
        ok, why = _run_hermes(["auth", "add", AUTH_KEY], run=run, capture=False)
        if not ok:
            print(
                f"Not connected ({why}). Run it yourself, in a terminal: hermes auth add {AUTH_KEY}"
            )
            return 1
        if not questions:
            print(f"It will decline what needs you. To change your mind: {QUESTIONS_COMMAND}")
        else:
            done, why = allow_questions(run=run)
            if done:
                print("It will bring you its requests in the chat you use with it.")
            else:
                print(f"Could not turn on its requests to you ({why}); run: {QUESTIONS_COMMAND}")
        print("Restart the gateway to start: hermes gateway restart")
        return 0
    if cmd == "logout":
        ok, why = _run_hermes(["auth", "logout", AUTH_KEY], run=run)
        print(
            "Disconnected." if ok else f"Could not sign out ({why}): hermes auth logout {AUTH_KEY}"
        )
        return 0 if ok else 1
    return _status()
