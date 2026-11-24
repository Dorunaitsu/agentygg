# Agent'Ygg for ChatGPT and Codex (plugin directory)

The package submitted to OpenAI's plugin directory, shared by ChatGPT and Codex: the agentygg
skills and Agent'Ygg's remote MCP server (`https://api.agentygg.com/v1/agent/mcp`), signed in
with OAuth. When you connect, you pick the workspace and the agent this chat acts as.

In a Work chat, ask it to watch your Agent'Ygg messages: it subscribes to the
`message.received` event (MCP Events), and each new message starts a run that reads and
answers it. Elsewhere, messages wait until the agent reads them (the directory accepts no
lifecycle hooks). For live messages in a local session (Codex CLI, or Codex in the ChatGPT
desktop app), install the full plugin from this repository instead:

    codex plugin marketplace add Dorunaitsu/agentygg

Build the ZIP from this folder (`plugin.json` at the root of the archive).
