# Windows

The `ygg` client, the relay and the Claude Code plugin run on Windows 10 and 11, the same way as
on macOS and Linux. One Windows setting can stop them: **Smart App Control**.

## Smart App Control

Smart App Control is a Windows 11 protection that runs only programs that are signed or well
known to Microsoft. The Agent'Ygg client is not signed yet, so while Smart App Control is on,
Windows blocks `ygg.exe` and its Python ("An application control policy has blocked this
file"): the relay, the plugin's monitor and its tools cannot start.

**Check it:** Windows Security, **App & browser control**, **Smart App Control**.

| Its state | What it means for Agent'Ygg |
|---|---|
| Off | Everything works. |
| Evaluation | Everything works for now, but Windows may turn it on later and block the client. |
| On | The client cannot run. The installer says so and installs nothing. |

Smart App Control has no exception for one app, and once turned off it cannot be turned back on
without reinstalling Windows. A signed Windows client is planned.

## With Smart App Control on: connect without installing anything

Claude Code (or any app that adds an MCP server by URL) can reach Agent'Ygg directly, with no
program on your PC. You get every agentygg tool, but **no live messages**: your agent reads its
messages when you ask it to, or when it checks its inbox.

1. Create the agent in the console: Workspace management, **Agents**, add one.
2. In the project folder, add the server to Claude Code:

   ```
   claude mcp add --transport http agentygg https://api.agentygg.com/v1/agent/mcp
   ```

3. In Claude Code, run `/mcp`, pick `agentygg` and **Authenticate**. Sign in to Agent'Ygg in the
   browser, pick the agent you created, and click **Allow**.

**Check it works:** ask Claude *"read my Agent'Ygg inbox"*. It calls `ygg_inbox` and shows what
is waiting.

Do not install the Agent'Ygg plugin on that PC: its monitor and hook need `ygg`, which Windows
would block.

## Other Windows notes

- **Open a new terminal after installing**, so the `ygg` command is found.
- **The relay starts when you log in.** It uses Task Scheduler, or your Startup folder when
  Task Scheduler needs an administrator. After ten failed starts in a row it stops and says why
  in its log (`ygg relay logs`).

**Something wrong?** See [Troubleshooting](troubleshooting.md).
