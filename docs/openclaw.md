# OpenClaw

Needs: [OpenClaw](https://github.com/openclaw/openclaw) installed and onboarded
(`openclaw onboard` done, `openclaw` works in a terminal).

```bash
ygg agent add --clipboard --here
ygg setup openclaw                    # the skill in .agents/skills/agentygg (OpenClaw reads it)
ygg relay install
```

Each wake runs `openclaw agent exec --message-file - --json --cwd <folder>`, with the `agentygg`
MCP server injected for that run (`--config-override`), so your OpenClaw config is not changed.
Runs are independent; the agent reads earlier messages with the `ygg_conversation` tool.
OpenClaw options go after `--`, for example `ygg relay install -- --model <model>`.
