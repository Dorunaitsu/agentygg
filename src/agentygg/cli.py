"""`ygg`: connect an agent to Agent'Ygg.

ygg connect              everything at once, in the agent's folder: the key from the
                         clipboard, the harness it runs in (detected), MCP + skill, relay

ygg agent add <key>      store an agent key on this machine (once, from the console)
ygg setup claude         wire this directory's harness to the agent (MCP + skill); also
                         codex, hermes, openclaw, opencode, mcp (any MCP harness)
ygg agent run            the relay: wake Claude Code here whenever the agent is pinged
ygg relay install        the same relay as a background service (login, reboot, crash)
ygg relay list           the relays on this machine and whether they are running
ygg agent status         who, where, credits, online or not
ygg inbox / ygg send     the same actions from a shell, for any other harness
ygg listen               in a live session: wait in the background for the next message
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


def _clipboard_raw() -> str | None:
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
    return None


def _clipboard() -> str:
    clip = _clipboard_raw()
    if clip is None:
        _fail("could not read the clipboard here: pass the key with `ygg agent add -` on stdin")
    return clip or ""


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


def _harness_for(args, ident: store.Identity) -> tuple[str, str | None]:
    """The harness to run: --harness, else the one `ygg setup` recorded for this agent, else
    `command` when --cmd is given, else Claude Code."""
    if args.harness:
        return args.harness, args.harness_path
    if args.cmd:
        return "command", None
    if ident.harness:
        return ident.harness, args.harness_path or ident.harness_path or None
    return "claude", args.harness_path


def cmd_agent_run(args) -> None:
    from agentygg.relay import Relay

    ident = _identity(args)
    found = store.pointer_for()
    workdir = Path(args.dir).resolve() if args.dir else (found[1] if found else Path.cwd())
    extra = list(args.harness_args or [])
    if extra[:1] == ["--"]:
        extra = extra[1:]
    try:
        kind, hpath = _harness_for(args, ident)
        relay = Relay(
            ident,
            workdir,
            harness=kind,
            harness_path=hpath,
            command=args.cmd,
            extra_args=extra,
        )
    except ValueError as e:
        _fail(str(e))
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


# -- connect: everything in one command --------------------------------------- #


def _obtain_key(args) -> str:
    """The agent key, from wherever it is: --key, the clipboard (what the console's Copy key
    puts there), $YGG_AGENT_KEY, a hidden prompt, or stdin. Never echoed."""
    import getpass

    if args.key and args.key != "-":
        return args.key.strip()
    if args.key != "-":
        clip = _clipboard_raw() or ""
        if clip.startswith(store.KEY_PREFIX):
            print("Using the agent key from the clipboard (not shown).")
            return clip
        env_key = os.environ.get("YGG_AGENT_KEY", "").strip()
        if env_key.startswith(store.KEY_PREFIX):
            return env_key
        if sys.stdin.isatty():
            return getpass.getpass("Agent key (from the console, Copy key; input hidden): ").strip()
    if args.key == "-" or not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return ""


def cmd_connect(args) -> None:
    """One command: store the key, find the harness we run in, wire it, start the relay."""
    import argparse as _ap

    from agentygg import detect, harnesses

    root = Path.cwd()
    # 1. The agent: the one already bound to this folder, else the key the user copied.
    found = store.pointer_for(root)
    ident = None
    if found and not args.key:
        try:
            ident = store.load(found[0])
            print(f"This folder already belongs to @{ident.agent_id}.")
        except LookupError:
            ident = None
    if ident is None:
        key = _obtain_key(args)
        try:
            wsid, aid = store.decode_key(key)
        except (ValueError, KeyError, json.JSONDecodeError):
            _fail(
                "no agent key found: in the console, create the agent and click Copy key, then "
                "run `ygg connect` again (or pass it on stdin: `ygg connect --key -`)"
            )
        ident = store.Identity(aid, wsid, key, args.api.rstrip("/"))
        me = asyncio.run(_with_gw(ident, lambda gw: gw.me()))
        ident.display_name = me.get("display_name", "")
        ident.workspace_name = me.get("workspace_name", "")
        store.save(ident)
        store.write_pointer(root, aid)
        print(f'Agent @{aid} ("{ident.display_name}") in "{ident.workspace_name or wsid}".')

    # 2. The harness: asked for, else detected.
    kind = args.harness
    if not kind:
        kind, how = detect.detect()
        if kind:
            print(f"Harness: {kind} ({how}).")
        elif sys.stdin.isatty():
            choices = [k for k in harnesses.KINDS if k != "command"]
            print(f"Which harness runs this agent? ({how})")
            for i, k in enumerate(choices, 1):
                print(f"  {i}. {k}")
            pick = input("Number: ").strip()
            if not pick.isdigit() or not 1 <= int(pick) <= len(choices):
                _fail("no harness chosen")
            kind = choices[int(pick) - 1]
        else:
            _fail(
                f"could not tell which harness to use ({how}); run `ygg connect --harness "
                "<claude|codex|hermes|openclaw|opencode>`, or for anything else "
                '`ygg connect --harness command --cmd "<one turn of your agent>"`'
            )

    # 3. Wire it (MCP server + skill), 4. start the relay.
    if kind != "command":
        cmd_setup(
            _ap.Namespace(setup_cmd=kind, agent=ident.agent_id, harness_path=args.harness_path)
        )
    else:
        cmd_setup(_ap.Namespace(setup_cmd="mcp", agent=ident.agent_id, harness_path=None))
    if args.no_relay:
        print("Skipped the relay (--no-relay): start it later with `ygg relay install`.")
        return
    cmd_relay_install(
        _ap.Namespace(
            agent=ident.agent_id,
            dir=str(root),
            harness=kind,
            harness_path=args.harness_path,
            cmd=args.cmd,
            harness_args=list(args.harness_args or []),
        )
    )
    print(f"Done: @{ident.agent_id} answers pings in the background. Check with `ygg relay list`.")


# -- relay services ----------------------------------------------------------- #


def cmd_relay_install(args) -> None:
    from agentygg import service

    ident = _identity(args)
    found = store.pointer_for()
    workdir = Path(args.dir).resolve() if args.dir else (found[1] if found else Path.cwd())
    extra = list(args.harness_args or [])
    if extra[:1] == ["--"]:
        extra = extra[1:]
    from agentygg import harnesses

    kind, hpath = _harness_for(args, ident)
    try:
        described = harnesses.build(kind, hpath, args.cmd).describe()
        path = service.install(
            ident.agent_id, workdir, extra, harness=kind, harness_path=hpath, cmd=args.cmd
        )
    except (RuntimeError, LookupError, ValueError, FileNotFoundError) as e:
        _fail(str(e))
    print(f"Harness: {described}")
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
        print(f"@{r.agent_id:<20} {state:<20} {r.harness or 'claude':<9} {r.workdir}")


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


def _skill_text_full() -> str:
    from importlib import resources

    return resources.files("agentygg").joinpath("skill/SKILL.md").read_text()


def _write_skill(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(_skill_text_full())
    return folder


def _ygg_cmd() -> str:
    import shutil

    found = shutil.which("ygg")
    return str(Path(found).absolute()) if found else "ygg"


def _mcp_json() -> str:
    return json.dumps(
        {"mcpServers": {"agentygg": {"command": _ygg_cmd(), "args": ["mcp"]}}}, indent=2
    )


def cmd_setup(args) -> None:
    """Wire this folder's agent into a harness: the `.agentygg` pointer, the protocol skill where the
    harness reads skills, and the `agentygg` MCP server in the harness' own config. The MCP
    server finds the agent from the folder it runs in, so one entry serves every agent."""
    import subprocess

    from agentygg import harnesses

    ident = _identity(args)
    root = Path.cwd()
    kind = args.setup_cmd
    store.write_pointer(root, ident.agent_id)
    if kind != "mcp":
        ident.harness = kind
        ident.harness_path = args.harness_path or ""
        store.save(ident)
    done = [".agentygg                     which agent works here (no secret inside)"]
    # The cross-harness skills location (agentskills.io): OpenClaw, Codex and others read it.
    _write_skill(root / ".agents" / "skills" / "agentygg")
    done.append(".agents/skills/agentygg/       the Agent'Ygg protocol skill")

    def run(cmd: list[str], answer: str | None = None) -> bool:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True, input=answer or "")
        return r.returncode == 0

    if kind == "claude":
        mcp_path = root / ".mcp.json"
        cfg = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
        cfg.setdefault("mcpServers", {})["agentygg"] = {
            "command": "ygg",
            "args": ["mcp"],
            "env": {"YGG_AGENT_ID": ident.agent_id},
        }
        mcp_path.write_text(json.dumps(cfg, indent=2) + "\n")
        _write_skill(root / ".claude" / "skills" / "agentygg")
        done += [
            ".mcp.json                     the agentygg MCP server for Claude Code",
            ".claude/skills/agentygg/      the skill, where Claude Code reads it",
        ]
    elif kind == "codex":
        try:
            exe = harnesses.Codex(args.harness_path).exe()
        except FileNotFoundError as e:
            _fail(str(e))
        run([exe, "mcp", "remove", "agentygg"])
        if not run(
            [exe, "mcp", "add", "agentygg", "--", _ygg_cmd(), "mcp", "--agent", ident.agent_id]
        ):
            _fail("`codex mcp add` failed; add the agentygg server by hand: " + _mcp_json())
        done.append("~/.codex/config.toml          the agentygg MCP server (codex mcp add)")
    elif kind == "hermes":
        try:
            exe = harnesses.Hermes(args.harness_path).exe()
        except FileNotFoundError as e:
            _fail(str(e))
        run([exe, "mcp", "remove", "agentygg"], "y\n")
        # `hermes mcp add` asks "Enable all N tools? [Y/n/select]": answer it, then CHECK that
        # the server was saved instead of trusting the exit code.
        run(
            [
                exe,
                "mcp",
                "add",
                "agentygg",
                "--command",
                _ygg_cmd(),
                "--args",
                "mcp",
                "--agent",
                ident.agent_id,
            ],
            "Y\n",
        )
        listed = subprocess.run([exe, "mcp", "list"], check=False, capture_output=True, text=True)
        if "agentygg" not in (listed.stdout + listed.stderr):
            _fail(
                "Hermes did not save the agentygg MCP server; add it by hand under mcp_servers in "
                f"~/.hermes/config.yaml with command {_ygg_cmd()} and args [mcp, --agent, "
                f"{ident.agent_id}]"
            )
        hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        _write_skill(hermes_home / "skills" / "agentygg")
        done += [
            "hermes config                 the agentygg MCP server (checked with hermes mcp list)",
            f"{hermes_home}/skills/agentygg/  the skill, where Hermes reads it",
        ]
    elif kind == "opencode":
        cfg_path = root / "opencode.json"
        try:
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        except json.JSONDecodeError:
            _fail(
                "opencode.json is not plain JSON (comments?); add this under its `mcp` key: "
                + json.dumps({"agentygg": {"type": "local", "command": [_ygg_cmd(), "mcp"]}})
            )
        cfg.setdefault("$schema", "https://opencode.ai/config.json")
        cfg.setdefault("mcp", {})["agentygg"] = {
            "type": "local",
            "command": [_ygg_cmd(), "mcp"],
            "enabled": True,
        }
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        done.append("opencode.json                 the agentygg MCP server for OpenCode")
    elif kind == "openclaw":
        done.append("(the relay injects the agentygg MCP server into each OpenClaw run)")
        print("For interactive OpenClaw sessions, also register the MCP server (see")
        print("https://docs.openclaw.ai/cli/mcp) with this stdio entry:")
        print(_mcp_json())
    else:  # mcp: any MCP-capable harness
        print("Add this stdio MCP server to your harness (most accept this JSON shape):")
        print(_mcp_json())
        print(
            f"and point it at the skill in {root / '.agents' / 'skills' / 'agentygg' / 'SKILL.md'}"
        )
    print(f"This directory now belongs to @{ident.agent_id}:")
    for line in done:
        print("  " + line)
    if kind == "mcp":
        print("Then `ygg relay install --harness command --cmd '<your harness, one turn>'` here.")
    else:
        print("Now run `ygg relay install` here so the agent answers pings in the background.")


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


def cmd_mcp(args) -> None:
    from agentygg.mcp_server import run

    run(getattr(args, "agent", None))


def cmd_listen(args) -> None:
    if os.environ.get("YGG_RELAY"):
        _fail(
            "ygg listen is for a session a human is having with the agent; this turn was "
            "started by the relay, which already delivers the pings"
        )
    from agentygg.listen import listen, listen_nonce

    ident = _identity(args)
    print(f"ygg: listening for @{ident.agent_id}", file=sys.stderr, flush=True)

    async def go() -> int:
        gw = Gateway(ident, nonce=listen_nonce(ident.agent_id))
        try:
            return await listen(gw, include_system=args.all, timeout=args.timeout)
        finally:
            await gw.close()

    try:
        code = asyncio.run(go())
    except KeyboardInterrupt:
        store.clear_listen(ident.agent_id)  # stopped by hand: the relay may resume at once
        code = 130
    raise SystemExit(code)


def cmd_directory(args) -> None:
    d = asyncio.run(_with_gw(_identity(args), lambda gw: gw.directory()))
    if args.json:
        print(json.dumps(d, indent=2))
        return
    print("Humans:")
    for h in d.get("humans") or []:
        print(f"  {h.get('handle') or h['id']:<24} {h.get('name')} ({h.get('role')})  id {h['id']}")
    print("Agents:")
    for a in d.get("agents") or []:
        you = "  <- you" if a.get("you") else ""
        print(f"  {a['handle']:<24} {a.get('name')} ({a.get('status')}){you}")


def cmd_skill(_args) -> None:
    from importlib import resources

    print(resources.files("agentygg").joinpath("skill/SKILL.md").read_text())


# -- parser ------------------------------------------------------------------ #


def _harness_opts(a: argparse.ArgumentParser) -> None:
    from agentygg.harnesses import KINDS

    a.add_argument(
        "--harness",
        choices=KINDS,
        help=f"which agent runs each turn: {', '.join(KINDS)} (default: the one chosen with "
        "`ygg setup <harness>`)",
    )
    a.add_argument("--harness-path", help="the harness executable, if not on PATH")
    a.add_argument(
        "--cmd",
        help="with --harness command: the shell command run per wake (prompt on stdin and in "
        "$YGG_PROMPT; answer with the agentygg MCP server or `ygg send`)",
    )


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
        description="Holds the agent's connection and runs one harness turn per wake (Claude "
        "Code by default; --harness codex|hermes|openclaw|command). Options after `--` go to "
        "the harness.",
    )
    a.add_argument("--agent")
    a.add_argument("--dir", help="working directory (default: the one bound to the agent)")
    _harness_opts(a)
    a.add_argument("harness_args", nargs=argparse.REMAINDER)
    a.set_defaults(fn=cmd_agent_run)

    a = sub.add_parser(
        "connect",
        help="everything in one command: key, harness, MCP + skill, background relay",
        description="Run it in the project folder (it is what you, or your agent, run after "
        "clicking Copy key in the console): reads the key from the clipboard, detects the harness "
        "it runs in, wires the MCP server and the skill, and installs the background relay. "
        "Options after `--` go to the harness.",
    )
    a.add_argument("--key", help="the agent key, or - to read it from stdin (default: clipboard)")
    a.add_argument("--api", default=os.environ.get("YGG_API", store.DEFAULT_API))
    _harness_opts(a)
    a.add_argument("--no-relay", action="store_true", help="wire the harness, do not start a relay")
    a.add_argument("harness_args", nargs=argparse.REMAINDER)
    a.set_defaults(fn=cmd_connect)

    rl = sub.add_parser("relay", help="relays as background services (login, reboot, crash)")
    rsub = rl.add_subparsers(dest="relay_cmd", required=True)
    a = rsub.add_parser(
        "install",
        help="run this directory's agent relay in the background, from now on",
        description="Installs the relay as a login service (launchd on macOS, systemd on Linux, "
        "Task Scheduler on Windows): it starts now, at every login and after a reboot, and is "
        "restarted if it crashes. Options after `--` go to the harness.",
    )
    a.add_argument("--agent")
    a.add_argument("--dir")
    _harness_opts(a)
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
    for name, hlp in (
        ("claude", "Claude Code: .mcp.json + the skill + the .agentygg pointer"),
        ("codex", "OpenAI Codex CLI: codex mcp add + the skill"),
        ("hermes", "Hermes Agent: hermes mcp add + the skill"),
        ("openclaw", "OpenClaw: the skill (the relay injects the MCP server per run)"),
        ("opencode", "OpenCode: opencode.json + the skill (.agents/skills)"),
        ("mcp", "any other MCP-capable harness: print the server entry + the skill"),
    ):
        a = ssub.add_parser(name, help=hlp)
        a.add_argument("--agent")
        a.add_argument("--harness-path", help="the harness executable, if not on PATH")
        a.set_defaults(fn=cmd_setup)

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

    a = sub.add_parser(
        "listen",
        help="in a live session: wait (in the background) until a human or an agent writes",
        description=(
            "Run it as a background command in a session a human is having with the agent: it "
            "exits as soon as a message arrives and prints it, so the session sees its pings "
            "live. Start it again after answering. Meanwhile the relay steps aside."
        ),
    )
    a.add_argument("--agent")
    a.add_argument("--all", action="store_true", help="also stop on system notices")
    a.add_argument(
        "--timeout", type=float, default=0.0, help="give up after this many seconds (0: never)"
    )
    a.set_defaults(fn=cmd_listen)

    a = sub.add_parser("directory", help="who is in the agent's workspace (humans and agents)")
    a.add_argument("--agent")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_directory)

    mcp_p = sub.add_parser("mcp", help="run the MCP server (your harness starts it)")
    mcp_p.add_argument("--agent", help="act as this agent wherever the harness starts the server")
    mcp_p.set_defaults(fn=cmd_mcp)
    sub.add_parser("skill", help="print the Agent'Ygg protocol skill").set_defaults(fn=cmd_skill)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
