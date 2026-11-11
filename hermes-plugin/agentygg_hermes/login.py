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
    "When another agent asks yours for something only you can decide (an approval, an email to "
    "send, a purchase), your agent can ask you in the chat you already use with it (Telegram, "
    "Discord...) and wait for your answer. Without this, it declines those requests. Allow it? "
    "Recommended: y. [y/N] "
)
QUESTIONS_COMMAND = f"hermes config set {INJECTION_SETTING} true"


def _ask_tty(question: str) -> bool:
    """Only an explicit yes; nobody at the terminal means no (Teknium, review of 5eb2be12)."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(question).strip().lower() in ("y", "yes", "o", "oui")
    except EOFError:
        return False


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
    _senders()
    return 0


def _senders() -> None:
    """Who may run turns of this agent (Hermes' allowlist, default deny), and the ids to pick from."""
    import asyncio

    from gateway.platforms._shared import get_scoped_secret

    from .gateway_client import AgentYggClient, GatewayError, NotSignedIn

    allowed = (get_scoped_secret("AGENTYGG_ALLOWED_USERS", "") or "").strip()
    everyone = (get_scoped_secret("AGENTYGG_ALLOW_ALL_USERS", "") or "").strip().lower()
    if everyone in ("1", "true", "yes", "on"):
        print("Senders: everyone in the workspace (AGENTYGG_ALLOW_ALL_USERS).")
    elif allowed:
        print(f"Senders allowed (AGENTYGG_ALLOWED_USERS): {allowed}")
        if "system.ygg" not in [x.strip() for x in allowed.split(",")]:
            print(
                "  system.ygg is missing: Agent'Ygg's own notices (a topic about to close, a "
                "reminder) are refused until you add it."
            )
    else:
        print(
            "No sender allowed yet: Hermes refuses every message until AGENTYGG_ALLOWED_USERS lists "
            "the Agent'Ygg ids that may write to this agent (comma-separated, in this profile's .env)."
        )
    try:
        found = asyncio.run(AgentYggClient().directory())
    except (GatewayError, NotSignedIn):
        return
    rows = [(p.get("id"), p.get("handle"), "person") for p in found.get("humans") or []]
    rows += [
        (a.get("id"), a.get("handle"), "agent")
        for a in found.get("agents") or []
        if not a.get("you")
    ]
    if rows:
        print("Ids in this workspace:")
        for rid, rhandle, kind in rows:
            print(f"  {rid}  ({rhandle}, {kind})")
    print(
        'To let someone in: hermes config set AGENTYGG_ALLOWED_USERS "<ids, comma-separated>", '
        "then restart the gateway."
    )


def _input_tty(question: str) -> str:
    """A typed answer, or "" with nobody at the terminal."""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(question).strip()
    except EOFError:
        return ""


SENDERS_PROMPT = (
    "Who may write to this agent on Agent'Ygg (each message runs a turn with its memory and "
    "tools)?\n  1) Only the ones you pick (recommended)\n  2) Everyone in the workspace\n"
    "(The Agent'Ygg console can also limit, on its server, who may write to this agent and who "
    "may read its conversations.) Choice [1]: "
)


def choose_senders(
    run: Callable[..., object] = subprocess.run, pick: Callable[[str], str] = _input_tty
) -> None:
    """After sign-in: restricted by default, picked one by one from the agent's workspace, or the
    whole workspace (Ray, 2026-11-11). Saved with Hermes' own config set; nobody at the terminal
    or a list already set changes nothing (Hermes' default deny stays)."""
    import asyncio

    from gateway.platforms._shared import get_scoped_secret

    from .gateway_client import AgentYggClient, GatewayError, NotSignedIn

    current = (get_scoped_secret("AGENTYGG_ALLOWED_USERS", "") or "").strip()
    everyone = (get_scoped_secret("AGENTYGG_ALLOW_ALL_USERS", "") or "").strip()
    if current or everyone:
        print(f"Senders kept as they are ({current or 'everyone in the workspace'}).")
        return
    try:
        found = asyncio.run(AgentYggClient().directory())
    except (GatewayError, NotSignedIn):
        found = {}
    rows = [(p.get("id"), p.get("handle"), "person") for p in found.get("humans") or []]
    rows += [
        (a.get("id"), a.get("handle"), "agent")
        for a in found.get("agents") or []
        if not a.get("you")
    ]
    if rows:
        print("In this agent's workspace:")
        for i, (_rid, rhandle, kind) in enumerate(rows, 1):
            print(f"  [{i}] {rhandle} ({kind})")
    later = 'Later: hermes config set AGENTYGG_ALLOWED_USERS "system.ygg,<ids>" (see hermes agentygg status).'
    choice = pick(SENDERS_PROMPT)
    if choice == "2":
        ok, why = _run_hermes(["config", "set", "AGENTYGG_ALLOW_ALL_USERS", "true"], run=run)
        print("Everyone in the workspace may write to it." if ok else f"Not saved ({why}).")
        return
    numbers = pick("Who may write, by number, comma-separated (empty: nobody yet): ")
    picked = []
    for n in numbers.replace(" ", "").split(","):
        if n.isdigit() and 1 <= int(n) <= len(rows) and rows[int(n) - 1][0]:
            picked.append(str(rows[int(n) - 1][0]))
    if not picked:
        print(f"Nobody may write to it yet: Hermes refuses every sender. {later}")
        return
    value = ",".join(["system.ygg", *dict.fromkeys(picked)])
    ok, why = _run_hermes(["config", "set", "AGENTYGG_ALLOWED_USERS", value], run=run)
    print(f"Allowed to write: {value}" if ok else f"Not saved ({why}). {later}")


def run_cli(
    args,
    ask: Callable[[str], bool] = _ask_tty,
    run: Callable[..., object] = subprocess.run,
    pick: Callable[[str], str] = _input_tty,
) -> int:
    cmd = getattr(args, "agentygg_cmd", None) or "status"
    if cmd == "login":
        questions = False if getattr(args, "no_questions", False) else ask(QUESTIONS_PROMPT)
        print(
            "Sign in to Agent'Ygg in the browser and pick the agent this Hermes will be "
            "(Hermes waits 3 minutes for it; run the command again if it times out)."
        )
        ok, why = _run_hermes(["auth", "add", AUTH_KEY], run=run, capture=False)
        if not ok:
            print(
                f"Not connected ({why}). Run it yourself, in a terminal: hermes auth add {AUTH_KEY}"
            )
            return 1
        choose_senders(run=run, pick=pick)
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
