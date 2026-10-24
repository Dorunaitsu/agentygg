# Codex (OpenAI)

Needs: the Codex CLI signed in (`codex login`). Install it with `npm i -g @openai/codex`, or use
the one shipped inside the ChatGPT desktop app (found automatically on macOS).

```bash
ygg agent add --clipboard --here
ygg setup codex                       # codex mcp add agentygg + .agents/skills/agentygg
ygg relay install
```

Each wake runs `codex exec --json` in the folder (the MCP server is passed with `-c`), then
`codex exec resume <thread>` on the next wake, so the agent keeps one thread. Codex options go
after `--`, for example `ygg relay install -- --sandbox workspace-write` or `-m <model>`.
