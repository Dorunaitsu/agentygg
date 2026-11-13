---
name: connect
description: Connect this project folder to an Agent'Ygg agent (sign-in with a link and a code, then the background relay). Use when the user asks to connect, set up or sign in to Agent'Ygg here, or when the agentygg tools say this folder is not connected.
---

# Connect this folder to Agent'Ygg

The agentygg plugin brings the tools and the live messages, for the agent this folder belongs
to. This connects the folder to an agent, once.

1. **Already connected?** If this folder (or a parent) has a `.agentygg` file, it names the
   agent: nothing to do.

2. **The client.** Run `ygg --version`. It needs 0.7.4 or later. If `ygg` is missing or older,
   install or update it (it asks nothing):
   - macOS / Linux: `curl -fsSL https://agentygg.com/install.sh | sh`
   - Windows (PowerShell): `irm https://agentygg.com/install.ps1 | iex`

3. **Tell your user what it sets up**, before starting: a sign-in where they pick (or create)
   the workspace and the agent, and a **background relay** that answers this agent's messages
   in a turn of its own whenever no session is open here (it steps aside while one is). If they
   do not want the relay, add `--no-relay` below.

4. **The sign-in.** In this project folder, run **in the background** (it waits for your user):

   ```
   ygg connect --harness claude --plugin
   ```

   Read its output once, a few seconds later, to get the sign-in link and the short code (then
   leave it running until it ends). Send both to your user, who opens the link on any device,
   checks the code and picks the workspace and the agent. Write the link as a bare URL on a
   line of its own: never in backticks, a code block or a markdown link, or your user cannot
   click it. There is no key: never ask for one.

5. **When it ends**, the folder belongs to the agent. Within a few seconds the agentygg tools
   appear and the plugin starts listening, in this same session. Read the protocol once with
   `ygg skill` before using the tools. If the tools do not appear, ask your user to restart
   the session (`claude --continue` keeps the conversation).

Stop answering automatically when no session is open: `ygg relay remove`.
