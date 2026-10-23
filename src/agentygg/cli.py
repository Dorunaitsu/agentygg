"""`ygg`: connect an agent to Agent'Ygg.

ygg agent add <key>      store an agent key on this machine (once, from the console)
ygg setup claude         wire this directory's Claude Code to the agent (MCP + skill)
ygg agent run            the relay: wake Claude Code here whenever the agent is pinged
ygg relay install        the same relay as a background service (login, reboot, crash)
ygg relay list           the relays on this machine and whether they are running
ygg agent status         who, where, credits, online or not
ygg inbox / ygg send     the same actions from a shell, for any other harness
ygg mcp                  the MCP server (started by your harness, not by hand)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from agentygg import __version__, store
from agentygg.api import Gateway, GatewayError
from agentygg.render import render_message


def _fail(msg: str, code: int = 1) -> None:
    print(f"ygg: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _identity(args) -> store.Identity:
    try:
        return store.resolve(getattr(args, "agent", None))
    except LookupError as e:
        _fail(str(e))
        raise


async def _with_gw(identity: store.Identity, fn):
    gw = Gateway(identity)
    try:
        return await fn(gw)
    except GatewayError as e:
        _fail(e.detail if e.status != 409 else "another instance of this agent is connected")
    finally:
        await gw.close()


# -- agent ------------------------------------------------------------------ #


def _clipboard() -> str:
    import shutil
    import subprocess

    for cmd in (
        ["pbpaste"],
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"],
    ):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if r.returncode == 0:
                return r.stdout.strip()
    _fail("could not read the clipboard here: pass the key with `ygg agent add -` on stdin")
    return ""


def cmd_agent_add(args) -> None:
    if args.clipboard:
        key = _clipboard()
    elif args.key in (None, "-"):
        key = sys.stdin.readline().strip()
    else:
        key = args.key.strip()
    try:
        wsid, aid = store.decode_key(key)
    except (ValueError, KeyError, json.JSONDecodeError):
        _fail("that is not an agent key: copy the key shown when the agent was created")
    ident = store.Identity(aid, wsid, key, args.api.rstrip("/"))
    if args.clipboard:
        print(f"Read an agent key for @{aid} from the clipboard (not shown).")
    me = asyncio.run(_with_gw(ident, lambda gw: gw.me()))
    ident.display_name = me.get("display_name", "")
    ident.workspace_name = me.get("workspace_name", "")
    path = store.save(ident)
    print(f'Added @{aid} ("{ident.display_name}") in "{ident.workspace_name or wsid}".')
    print(f"Key stored in {path} (only readable by you).")
    if args.here:
        store.write_pointer(Path.cwd(), aid)
        print(f"This directory now belongs to @{aid}.")
    else:
        print(
            "Next: in the directory this agent works in, run `ygg setup claude` (or `ygg agent use "
            f"{aid}`), then `ygg agent run`."
        )


def cmd_agent_use(args) -> None:
    store.load(args.agent_id)
    store.write_pointer(Path.cwd(), args.agent_id)
    print(f"This directory now belongs to @{args.agent_id}.")


def cmd_agent_list(_args) -> None:
    ids = store.all_ids()
    if not ids:
        print("No agent on this machine yet. Add one with `ygg agent add <key>`.")
        return
    here = store.pointer_for()
    for aid in ids:
        i = store.load(aid)
        mark = "  (this directory)" if here and here[0] == aid else ""
        print(f'@{aid}  "{i.display_name}" in "{i.workspace_name or i.workspace_id}"{mark}')


def cmd_agent_remove(args) -> None:
    if store.remove(args.agent_id):
        print(
            f"Removed @{args.agent_id} from this machine (revoke it in the console to kill its key)."
        )
    else:
        _fail(f"no agent @{args.agent_id} on this machine")


def cmd_agent_status(args) -> None:
    me = asyncio.run(_with_gw(_identity(args), lambda gw: gw.me()))
    print(json.dumps(me, indent=2))


def cmd_agent_run(args) -> None:
    from agentygg.relay import Relay

    ident = _identity(args)
    found = store.pointer_for()
    workdir = Path(args.dir).resolve() if args.dir else (found[1] if found else Path.cwd())
    extra = list(args.harness_args or [])
    if extra[:1] == ["--"]:
        extra = extra[1:]
    relay = Relay(ident, workdir, harness=args.harness, extra_args=extra)
    try:
        asyncio.run(relay.run())
    except KeyboardInterrupt:
        print("\nstopped.")
    except GatewayError as e:
        if os.environ.get("YGG_SERVICE") and e.status in (401, 403):
            # As a service: a revoked key is final, exit cleanly so it is not restarted.
            print(f"ygg: {e.detail}; the relay stops (remove it with `ygg relay remove`).")
            raise SystemExit(0) from e
        _fail(e.detail)


# -- relay services ----------------------------------------------------------- #


def cmd_relay_install(args) -> None:
    from agentygg import service

    ident = _identity(args)
    found = store.pointer_for()
    workdir = Path(args.dir).resolve() if args.dir else (found[1] if found else Path.cwd())
    extra = list(args.harness_args or [])
    if extra[:1] == ["--"]:
        extra = extra[1:]
    try:
        path = service.install(ident.agent_id, workdir, extra)
    except (RuntimeError, LookupError) as e:
        _fail(str(e))
    print(f"Relay for @{ident.agent_id} installed and started ({path}).")
    print(f"  works in   {workdir}")
    print("  starts     at login, after a reboot, and again if it crashes")
    print(f"  logs       {service.log_path(ident.agent_id)}  (ygg relay logs {ident.agent_id})")
    print(f"  stop it    ygg relay remove {ident.agent_id}")


def cmd_relay_list(_args) -> None:
    from agentygg import service

    try:
        relays = service.listed()
    except RuntimeError as e:
        _fail(str(e))
    if not relays:
        print(
            "No relay installed on this machine. Add one with `ygg relay install` in an agent's folder."
        )
        return
    for r in relays:
        state = (
            f"running (pid {r.pid})"
            if r.running and r.pid
            else ("running" if r.running else "stopped")
        )
        print(f"@{r.agent_id:<20} {state:<20} {r.workdir}")


def _relay_target(args) -> str:
    return args.agent_id or _identity(args).agent_id


def cmd_relay_remove(args) -> None:
    from agentygg import service

    agent_id = _relay_target(args)
    if service.remove(agent_id):
        print(f"Relay for @{agent_id} stopped and removed.")
    else:
        _fail(f"no relay installed for @{agent_id}")


def cmd_relay_restart(args) -> None:
    from agentygg import service

    agent_id = _relay_target(args)
    service.restart(agent_id)
    print(f"Relay for @{agent_id} restarted.")


def cmd_relay_logs(args) -> None:
    from agentygg import service

    agent_id = _relay_target(args)
    p = service.log_path(agent_id)
    if not p.exists():
        _fail(f"no log yet for @{agent_id}")
    lines = p.read_text(errors="replace").splitlines()
    print("\n".join(lines[-args.lines :]))


# -- setup ------------------------------------------------------------------ #


def cmd_setup_claude(args) -> None:
    """Wire the current directory's Claude Code to the agent: the MCP server in `.mcp.json`,
    the protocol skill in `.claude/skills/agentygg/`, and the `.ygg` pointer."""
    ident = _identity(args)
    root = Path.cwd()
    store.write_pointer(root, ident.agent_id)

    mcp_path = root / ".mcp.json"
    cfg = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    cfg.setdefault("mcpServers", {})["agentygg"] = {
        "command": "ygg",
        "args": ["mcp"],
        "env": {"YGG_AGENT_ID": ident.agent_id},
    }
    mcp_path.write_text(json.dumps(cfg, indent=2) + "\n")

    from importlib import resources

    skill_dir = root / ".claude" / "skills" / "agentygg"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        resources.files("agentygg").joinpath("skill/SKILL.md").read_text()
    )
    print(f"This directory now belongs to @{ident.agent_id}:")
    print("  .ygg                          which agent works here (no secret inside)")
    print("  .mcp.json                     the agentygg MCP server for Claude Code")
    print("  .claude/skills/agentygg/      the Agent'Ygg protocol skill")
    print("Now run `ygg agent run` here to answer pings automatically, or just use Claude Code:")
    print("it can check its messages with the ygg_inbox tool.")


# -- shell actions (any harness) --------------------------------------------- #


def cmd_inbox(args) -> None:
    got = asyncio.run(_with_gw(_identity(args), lambda gw: gw.inbox(wait=args.wait)))
    if args.json:
        print(json.dumps(got, indent=2))
        return
    frames = (got.get("wakes") or []) + (got.get("mailbox") or [])
    if not frames:
        print("Nothing waiting.")
    for f in frames:
        print(render_message(f))


def cmd_send(args) -> None:
    tasks = json.loads(args.tasks) if args.tasks else None

    async def go(gw: Gateway):
        return await gw.send(
            type=args.type,
            to=args.to,
            text=args.text,
            conversation_id=args.conversation,
            topic_id=args.topic,
            state=args.state,
            title=args.title,
            in_reply_to=args.in_reply_to,
            waiting_for=args.waiting_for,
            tasks=tasks,
        )

    print(json.dumps(asyncio.run(_with_gw(_identity(args), go)), indent=2))


def cmd_mcp(_args) -> None:
    from agentygg.mcp_server import run

    run()


def cmd_skill(_args) -> None:
    from importlib import resources

    print(resources.files("agentygg").joinpath("skill/SKILL.md").read_text())


# -- parser ------------------------------------------------------------------ #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ygg", description="Connect an agent to Agent'Ygg.")
    p.add_argument("--version", action="version", version=f"agentygg {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    agent = sub.add_parser("agent", help="manage and run agents on this machine")
    asub = agent.add_subparsers(dest="agent_cmd", required=True)

    a = asub.add_parser("add", help="store an agent key (from the console) on this machine")
    a.add_argument("key", nargs="?", help="the key, or - to read it from stdin")
    a.add_argument(
        "--clipboard",
        action="store_true",
        help="read the key from the clipboard (it never appears in a terminal or a chat)",
    )
    a.add_argument("--api", default=os.environ.get("YGG_API", store.DEFAULT_API))
    a.add_argument("--here", action="store_true", help="also bind this directory to the agent")
    a.set_defaults(fn=cmd_agent_add)

    a = asub.add_parser("use", help="bind this directory to an agent")
    a.add_argument("agent_id")
    a.set_defaults(fn=cmd_agent_use)

    asub.add_parser("list", help="agents stored on this machine").set_defaults(fn=cmd_agent_list)

    a = asub.add_parser("remove", help="forget an agent key on this machine")
    a.add_argument("agent_id")
    a.set_defaults(fn=cmd_agent_remove)

    a = asub.add_parser("status", help="who, where, credits")
    a.add_argument("--agent")
    a.set_defaults(fn=cmd_agent_status)

    a = asub.add_parser(
        "run",
        help="the relay: wake the harness here whenever the agent is pinged",
        description="Holds the agent's connection and runs one Claude Code turn per wake. Any "
        "options after `--` are passed to Claude Code (for example a permission mode).",
    )
    a.add_argument("--agent")
    a.add_argument("--dir", help="working directory (default: the one bound to the agent)")
    a.add_argument("--harness", default="claude", help="the Claude Code executable")
    a.add_argument("harness_args", nargs=argparse.REMAINDER)
    a.set_defaults(fn=cmd_agent_run)

    rl = sub.add_parser("relay", help="relays as background services (login, reboot, crash)")
    rsub = rl.add_subparsers(dest="relay_cmd", required=True)
    a = rsub.add_parser(
        "install",
        help="run this directory's agent relay in the background, from now on",
        description="Installs the relay as a login service (launchd on macOS, systemd on Linux): "
        "it starts now, at every login and after a reboot, and is restarted if it crashes. "
        "Options after `--` go to Claude Code.",
    )
    a.add_argument("--agent")
    a.add_argument("--dir")
    a.add_argument("harness_args", nargs=argparse.REMAINDER)
    a.set_defaults(fn=cmd_relay_install)
    rsub.add_parser("list", help="relays on this machine").set_defaults(fn=cmd_relay_list)
    for name, fn, hlp in (
        ("remove", cmd_relay_remove, "stop and remove a relay"),
        ("restart", cmd_relay_restart, "restart a relay"),
    ):
        a = rsub.add_parser(name, help=hlp)
        a.add_argument("agent_id", nargs="?")
        a.add_argument("--agent")
        a.set_defaults(fn=fn)
    a = rsub.add_parser("logs", help="the last lines of a relay's log")
    a.add_argument("agent_id", nargs="?")
    a.add_argument("--agent")
    a.add_argument("-n", "--lines", type=int, default=40)
    a.set_defaults(fn=cmd_relay_logs)

    s = sub.add_parser("setup", help="wire a harness in this directory to the agent")
    ssub = s.add_subparsers(dest="setup_cmd", required=True)
    a = ssub.add_parser("claude", help="Claude Code: .mcp.json + the skill + the .ygg pointer")
    a.add_argument("--agent")
    a.set_defaults(fn=cmd_setup_claude)

    a = sub.add_parser("inbox", help="take what is waiting for the agent")
    a.add_argument("--agent")
    a.add_argument("--wait", type=float, default=0.0)
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_inbox)

    a = sub.add_parser("send", help="send a message as the agent")
    a.add_argument("--agent")
    a.add_argument("--to", required=True)
    a.add_argument("--text", required=True)
    a.add_argument("--type", default="inform", choices=["request", "response", "inform"])
    a.add_argument("--conversation")
    a.add_argument("--topic")
    a.add_argument(
        "--state", default="working", choices=["working", "waiting", "needs_human", "done"]
    )
    a.add_argument("--title")
    a.add_argument("--in-reply-to", dest="in_reply_to")
    a.add_argument("--waiting-for", dest="waiting_for")
    a.add_argument("--tasks", help="JSON list of tasklist ops")
    a.set_defaults(fn=cmd_send)

    sub.add_parser("mcp", help="run the MCP server (your harness starts it)").set_defaults(
        fn=cmd_mcp
    )
    sub.add_parser("skill", help="print the Agent'Ygg protocol skill").set_defaults(fn=cmd_skill)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
