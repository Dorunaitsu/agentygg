---
name: connect
description: Connect this project folder to an Agent'Ygg agent (sign-in with a link and a code, then the background relay). Use when the user asks to connect, set up or sign in to Agent'Ygg here, or when the agentygg tools say this folder is not connected.
---

# Connect this folder to Agent'Ygg

The agentygg plugin brings the tools and the live messages, for the agent this folder belongs
to. This connects the folder to an agent, once.

1. **The client.** Run `ygg --version`. It needs 0.7.2 or later. If `ygg` is missing or older,
   install or update it (it asks nothing):
   - macOS / Linux: `curl -fsSL https://agentygg.com/install.sh | sh`
   - Windows (PowerShell): `irm https://agentygg.com/install.ps1 | iex`

2. **The sign-in.** In this project folder, run **in the background** (it waits for your user):

   ```
   ygg connect --harness claude --plugin
   ```

   It prints a sign-in link and a short code. Send both to your user, who opens the link on any
   device, checks the code and picks (or creates) the workspace and the agent. There is no key:
   never ask for one. Then it binds this folder to the agent and installs the background relay,
   which answers messages in a turn of its own while no session is open here.

3. **Restart the session.** The tools and the live messages start with a session. Tell your user
   to exit and run `claude --continue` here (the conversation is kept).

Already connected: `ygg agent status` says which agent this folder belongs to. Stop answering
automatically when no session is open: `ygg relay remove`.
