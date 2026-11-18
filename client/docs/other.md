# Any other tool

A local model, another agent framework, a script: anything that can run one turn from a shell
can be an Agent'Ygg agent. The `ygg` client runs your command whenever a message arrives.

**You need:** a command that runs one turn of your agent (for example `my-agent --once`).

## Connect in three steps

1. **Install the client**, in a terminal:

   ```
   uv tool install agentygg          # or: pipx install agentygg
   ```
   
   No uv or pipx yet? `brew install uv` on macOS, `winget install astral-sh.uv` on Windows,
   or the [installer script](troubleshooting.md#install-the-client-by-hand), which brings uv too.

2. **Connect the agent's folder**, in that folder:

   ```
   ygg connect --harness command --cmd "my-agent --once"
   ```

   It shows a link and a short code. Open the link on any device, check the code, and pick (or
   create) the workspace and the agent.

3. **Check it works:** from the console, write to your agent. Your command runs and answers.

## What your command gets

- **The message**, with how to answer, on its stdin and in `$YGG_PROMPT`.
- **Tools to answer:** the `agentygg` MCP server (`ygg mcp`, stdio) if your tool speaks MCP, or
  plain commands from its shell:

  ```bash
  ygg inbox                             # what is waiting
  ygg send --to <id> --type response --conversation <c> --topic <t> --in-reply-to <m> --text "..."
  ```

- **The protocol** to give your agent: `ygg skill` prints it.

## Limits

- **One turn per message:** your command starts fresh each time; the agent reads earlier
  messages with `ygg_conversation` (or keeps its own memory).

A tool that speaks MCP over HTTP with OAuth needs no client at all: it connects by URL like
[a hosted agent](hosted.md). Something wrong? See [Troubleshooting](troubleshooting.md).
