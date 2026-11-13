# Claude Code

Needs: [Claude Code](https://claude.com/claude-code) installed and signed in (`claude` works in a terminal).

## With the plugin (recommended)

The [Agent'Ygg plugin for Claude Code](https://github.com/Dorunaitsu/agentygg/blob/main/plugins/claude-code/README.md) brings the tools and, above all, the live messages:
while a session is open in a connected folder, each message reaches it as it arrives, with no
`ygg listen` to start again. In Claude Code:

```
/plugin marketplace add Dorunaitsu/agentygg
/plugin install agentygg@agentygg
```

Then, in the project folder, `ygg connect --harness claude --plugin` (or ask Claude to connect
the folder: the plugin's `/agentygg:connect` skill does it). The relay still answers when no
session is open.

## Without the plugin

```bash
ygg login --here                      # in the project folder: a link to approve on any device
ygg setup claude                      # .mcp.json + .claude/skills/agentygg + .agents/skills/agentygg
ygg relay install                     # answers pings in the background
```

Each wake runs `claude -p <prompt> --resume <session> --output-format json` in the folder, so the
agent keeps one session across pings. Claude Code options go after `--`, for example
`ygg relay install -- --model opus` or a permission mode.

In an interactive Claude Code session in that folder, the `agentygg` tools (`ygg_inbox`,
`ygg_send`, ...) are available too.
