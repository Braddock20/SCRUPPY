#!/usr/bin/env bash
# Run igbot under a virtual display, so the browser has a real window to draw
# into on a machine with no monitor (a VPS, for example).
#
#   ./scripts/igbot-xvfb.sh watch
#
# The display number can be set with DISPLAY_NUM (default 99).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
GEOMETRY="${GEOMETRY:-1440x900x24}"
VNC_PORT="${VNC_PORT:-5900}"

if ! command -v Xvfb >/dev/null 2>&1; then
  echo "Xvfb is not installed." >&2
  echo "  Debian/Ubuntu: sudo apt install -y xvfb x11vnc" >&2
  echo "  Fedora:         sudo dnf install xorg-x11-server-Xvfb tigervnc-server" >&2
  exit 1
fi

# Start the virtual display if it is not already up.
if ! xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
  Xvfb ":$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp >/tmp/xvfb-$DISPLAY_NUM.log 2>&1 &
  XVFB_PID=$!
  sleep 2
  if ! xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
    echo "Xvfb failed to start; see /tmp/xvfb-$DISPLAY_NUM.log" >&2
    exit 1
  fi
  echo "Xvfb running on :$DISPLAY_NUM (pid $XVFB_PID)"
fi

export DISPLAY=":$DISPLAY_NUM"

# Optional VNC so a human can complete a login by hand.
if [ "${VNC:-0}" = "1" ] && command -v x11vnc >/dev/null 2>&1; then
  x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -forever -shared -nopw >/tmp/x11vnc.log 2>&1 &
  echo "VNC on port $VNC_PORT (no password) — connect with any VNC viewer"
fi

cd "$HERE"
exec .venv/bin/python -m igbot "$@"
