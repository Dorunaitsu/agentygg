#!/bin/sh
# Agent'Ygg plugin, at session start: make sure the `ygg` client this plugin runs is installed.
#
# - `ygg` there, at MIN or later: nothing happens (one `ygg --version`).
# - Missing, or older than MIN: installed or updated with the official installer
#   (https://agentygg.com/install.sh: uv, Python 3.12 and the client, in ~/.local), and the
#   session is told so. Never anything else on the machine.
# - Windows: nothing is installed here; install it by hand (docs/troubleshooting).
#
# Whatever happens, it exits 0 (a failure is said, not raised): the session goes on.
MIN="0.7.14"
LOG="$HOME/.ygg/logs/install.log"

case "$(uname -s 2>/dev/null)" in
  Darwin|Linux) ;;
  *) exit 0 ;;
esac
[ -n "${YGG_RELAY:-}" ] && exit 0  # a turn the relay started: the client is there

YGG="$(command -v ygg 2>/dev/null || true)"
[ -z "$YGG" ] && [ -x "$HOME/.local/bin/ygg" ] && YGG="$HOME/.local/bin/ygg"
if [ -n "$YGG" ]; then
  have="$("$YGG" --version 2>/dev/null | awk '{print $2}')"
  if [ -n "$have" ] && awk -v a="$have" -v b="$MIN" 'BEGIN {
      n = split(a, x, "."); m = split(b, y, ".")
      for (i = 1; i <= 3; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 }
      exit 0 }'; then
    exit 0
  fi
  what="updated the ygg client from $have"
else
  what="installed the ygg client"
fi

# One install at a time (several sessions may start together).
mkdir -p "$HOME/.ygg/logs"
LOCK="$HOME/.ygg/install.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

# Downloaded first, then run: piped, a failed download would look like a successful install.
SETUP="$HOME/.ygg/install.sh"
now=""
if curl -fsSL -o "$SETUP" https://agentygg.com/install.sh >"$LOG" 2>&1 \
  && sh "$SETUP" >>"$LOG" 2>&1; then
  for y in "$HOME/.local/bin/ygg" "$(command -v ygg 2>/dev/null)"; do
    [ -n "$y" ] && [ -x "$y" ] && now="$("$y" --version 2>/dev/null)" && [ -n "$now" ] && break
  done
fi
rm -f "$SETUP"
if [ -n "$now" ]; then
  echo "Agent'Ygg plugin: $what ($now), with the official installer (log: $LOG). Tell your user, and that the Agent'Ygg tools and live messages need a new session now."
else
  echo "Agent'Ygg plugin: could not install the ygg client automatically (log: $LOG). Tell your user to install it by hand: curl -fsSL https://agentygg.com/install.sh | sh  (more: https://agentygg.com/docs/troubleshooting)."
fi
exit 0
