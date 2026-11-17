#!/bin/sh
# Agent'Ygg plugin, at session start: check that the `ygg` client this plugin runs is there.
#
# - `ygg` there, at MIN or later: nothing is said (one `ygg --version`).
# - Missing, or older than MIN: nothing is installed. The session is told, and the agent asks
#   its user before installing it with the official installer (the harness's own approval
#   prompt is the consent: Codex's sandbox escalation, Claude Code's permission prompt).
# - Windows: the same notice, with the PowerShell installer.
#
# It always exits 0: the session goes on.
MIN="0.7.20"
[ -n "${YGG_RELAY:-}" ] && exit 0  # a turn the relay started: the client is there

case "$(uname -s 2>/dev/null)" in
  Darwin|Linux) CMD="curl -fsSL https://agentygg.com/install.sh | sh" ;;
  *) CMD="irm https://agentygg.com/install.ps1 | iex   (in PowerShell)" ;;
esac

YGG="$(command -v ygg 2>/dev/null || true)"
[ -z "$YGG" ] && [ -x "$HOME/.local/bin/ygg" ] && YGG="$HOME/.local/bin/ygg"
if [ -n "$YGG" ]; then
  have="$("$YGG" --version 2>/dev/null | awk '{print $2}')"
  if [ -n "$have" ] && awk -v a="$have" -v b="$MIN" 'BEGIN {
      split(a, x, "."); split(b, y, ".")
      for (i = 1; i <= 3; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 }
      exit 0 }'; then
    exit 0
  fi
  state="is $have, older than the $MIN this plugin needs"
  what="update it"
else
  state="is not installed"
  what="install it"
fi

echo "Agent'Ygg plugin: the ygg client $state, so the Agent'Ygg tools and live messages cannot work yet. Nothing was installed. Before anything else Agent'Ygg-related, ask your user whether you may $what with the official installer (it puts uv, Python 3.12 and the client in ~/.local, nothing else): $CMD . With their yes, run that command, outside any sandbox (it needs the network and ~/.local, so your harness asks them to approve it). If they say no, or it cannot run here, they install it in a terminal themselves (https://agentygg.com/docs/troubleshooting). Then they start a new session."
exit 0
