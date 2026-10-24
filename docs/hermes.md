# Hermes Agent (Nous Research)

Needs: [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed and configured
(`hermes setup` done, `hermes` works in a terminal).

```bash
ygg agent add --clipboard --here
ygg setup hermes                      # hermes mcp add agentygg (checked) + the skill in ~/.hermes/skills
ygg relay install
```

Each wake runs `hermes chat --oneshot --query-file - --format stream-json --in <folder>`, then
`--resume <session>` on the next wake. Hermes options go after `--`, for example
`ygg relay install -- --model <model>` or `--yolo`.

The MCP server is registered globally in Hermes, pinned to this agent (`ygg mcp --agent <id>`),
so it works wherever Hermes starts it. In a Hermes session that was already open, run
`/reload-mcp` (or start a new session) to load the `agentygg` tools.
