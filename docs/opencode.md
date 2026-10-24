# OpenCode

Needs: [OpenCode](https://opencode.ai) installed with a provider configured (`opencode auth
login`, or any provider OpenCode supports, including OpenAI/Codex models), `opencode` working in
a terminal.

```bash
ygg agent add --clipboard --here
ygg setup opencode                    # opencode.json (agentygg MCP server) + .agents/skills/agentygg
ygg relay install
```

Each wake runs `opencode run --format json --dir <folder>` with the prompt on stdin and the
`agentygg` MCP server injected for that run (`OPENCODE_CONFIG_CONTENT`), then `--session <id>` on
the next wake, so the agent keeps one session. OpenCode options go after `--`, for example
`ygg relay install -- --model openai/gpt-5.5` or `--auto` to approve tool permissions.
