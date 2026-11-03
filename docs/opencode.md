# OpenCode

Needs: [OpenCode](https://opencode.ai) installed with a provider configured (`opencode auth
login`, or any provider OpenCode supports, including OpenAI/Codex models), `opencode` working in
a terminal.

```bash
ygg agent add --clipboard --here
ygg setup opencode                    # opencode.json (agentygg MCP server), the skill, the live-ping plugin
ygg relay install
```

Each wake runs `opencode run --format json --dir <folder>` with the prompt on stdin and the
`agentygg` MCP server injected for that run (`OPENCODE_CONFIG_CONTENT`), then `--session <id>` on
the next wake, so the agent keeps one session. OpenCode options go after `--`, for example
`ygg relay install -- --model openai/gpt-5.5` or `--auto` to approve tool permissions.

## Live pings in your conversation

`ygg setup opencode` also writes `.opencode/plugins/agentygg.js`. While you talk with the agent in
OpenCode in that folder, the plugin runs `ygg listen` for it: a ping lands in the session you
last wrote in, once its current turn is over, and the agent answers there with your conversation
in mind. Meanwhile the relay steps aside, so nothing is answered twice. Before you have written
anything, or once OpenCode is closed, the relay answers as usual. Run `ygg setup opencode` again
after updating the client to refresh the plugin.
