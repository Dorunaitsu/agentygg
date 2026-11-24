#!/bin/sh
# Agent'Ygg plugin, at session start: check that the `ygg` client this plugin runs is there.
#
# - `ygg` there, at MIN or later: nothing is said (one `ygg --version`).
# - Missing, or older than MIN: nothing is installed. The session is told, and the agent asks
#   its user before installing it (the harness's own approval prompt is the consent: Codex's
#   sandbox escalation, Claude Code's permission prompt). The offer follows what the machine
#   already has, PyPI first: uv, then pipx, then uv from Homebrew or winget, and only as a last
#   resort Agent'Ygg's installer script.
#
# It always exits 0: the session goes on.
MIN="0.7.20"
LATEST="0.7.28"  # the version offered; the release bumps it with pyproject.toml
[ -n "${YGG_RELAY:-}" ] && exit 0  # a turn the relay started: the client is there

UV_CMD="uv tool install --force --python 3.12 agentygg==$LATEST"
case "$(uname -s 2>/dev/null)" in
  Darwin|Linux) SCRIPT="curl -fsSL https://agentygg.com/install.sh | sh" ;;
  *) SCRIPT="irm https://agentygg.com/install.ps1 | iex   (in PowerShell)" ;;
esac

have() { command -v "$1" >/dev/null 2>&1; }
if have uv || [ -x "$HOME/.local/bin/uv" ]; then
  PLAN="uv is already here, so it is one command that installs agentygg $LATEST from PyPI: $UV_CMD ."
elif have pipx; then
  PLAN="pipx is already here, so it is one command that installs agentygg $LATEST from PyPI (it needs Python 3.12 or later): pipx install --force agentygg==$LATEST ."
elif have brew; then
  PLAN="it takes two commands, say both: uv (Astral's Python tool installer) from Homebrew, then agentygg $LATEST from PyPI: brew install uv  then  $UV_CMD ."
elif have winget; then
  PLAN="it takes two commands, say both: uv (Astral's Python tool installer) from winget, then, in a new terminal, agentygg $LATEST from PyPI: winget install --id astral-sh.uv -e  then  $UV_CMD ."
else
  PLAN="neither uv, pipx nor a package manager is here, so the last resort is Agent'Ygg's installer script (it puts uv, Python 3.12 and the client in ~/.local, nothing else): $SCRIPT ."
fi

YGG="$(command -v ygg 2>/dev/null || true)"
[ -z "$YGG" ] && [ -x "$HOME/.local/bin/ygg" ] && YGG="$HOME/.local/bin/ygg"
if [ -n "$YGG" ]; then
  have_v="$("$YGG" --version 2>/dev/null | awk '{print $2}')"
  if [ -n "$have_v" ] && awk -v a="$have_v" -v b="$MIN" 'BEGIN {
      split(a, x, "."); split(b, y, ".")
      for (i = 1; i <= 3; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 }
      exit 0 }'; then
    exit 0
  fi
  state="is $have_v, older than the $MIN this plugin needs"
  what="update it"
else
  state="is not installed"
  what="install it"
fi

echo "Agent'Ygg plugin: the ygg client $state, so the Agent'Ygg tools and live messages cannot work yet. Nothing was installed. Before anything else Agent'Ygg-related, ask your user whether you may $what, telling them exactly what will run: $PLAN With their yes, run it, outside any sandbox (it needs the network and ~/.local, so your harness asks them to approve it). If they say no, or it cannot run here, they install it in a terminal themselves (https://agentygg.com/docs/troubleshooting). Then they start a new session."
exit 0
