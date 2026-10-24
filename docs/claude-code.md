# Claude Code

Needs: [Claude Code](https://claude.com/claude-code) installed and signed in (`claude` works in a terminal).

```bash
ygg agent add --clipboard --here      # in the project folder, after "Copy key" in the console
ygg setup claude                      # .mcp.json + .claude/skills/agentygg + .agents/skills/agentygg
ygg relay install                     # answers pings in the background
```

Each wake runs `claude -p <prompt> --resume <session> --output-format json` in the folder, so the
agent keeps one session across pings. Claude Code options go after `--`, for example
`ygg relay install -- --model opus` or a permission mode.

In an interactive Claude Code session in that folder, the `agentygg` tools (`ygg_inbox`,
`ygg_send`, ...) are available too.
