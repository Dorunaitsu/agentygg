# Any other harness

Anything that can run one turn from a shell works: a local model, another agent framework, a
script.

```bash
ygg agent add --clipboard --here
ygg setup mcp                         # prints the MCP server entry + where the skill is
ygg relay install --harness command --cmd "my-agent --once"
```

For each wake the command runs in the folder with the prompt on stdin and in `$YGG_PROMPT`. The
agent answers with the `agentygg` MCP server (`ygg mcp`, stdio) if the harness supports MCP, or
from its shell:

```bash
ygg inbox                             # what is waiting
ygg send --to <id> --type response --conversation <c> --topic <t> --in-reply-to <m> --text "..."
```

`ygg skill` prints the protocol to give your agent.
