#!/usr/bin/env bash
# Open a VNC window onto the bot's browser so you can log in by hand.
#
#   ./scripts/vnc-display.sh
#
# Starts Xvfb + x11vnc if they are not already running, then tells you which
# VNC port to connect to. Use any VNC viewer (RealVNC, TigerVNC, macOS
# Screen Sharing, or a browser-based one).
#
# This is the recommended way to complete the *first* login: the bot never
# scripts the login form, so a human types the password and handles any
# two-factor prompt.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
GEOMETRY="${GEOMETRY:-1440x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
VNC_PASSWORD="${VNC_PASSWORD:-igbot}"

command -v Xvfb >/dev/null 2>&1 || { echo "Xvfb is missing: sudo apt install -y xvfb x11vnc" >&2; exit 1; }
command -v x11vnc >/dev/null 2>&1 || { echo "x11vnc is missing: sudo apt install -y xvfb x11vnc" >&2; exit 1; }

if ! xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
  Xvfb ":$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp >/tmp/xvfb-$DISPLAY_NUM.log 2>&1 &
  sleep 2
fi

# Stop any x11vnc already holding the port.
pkill -f "x11vnc.*-rfbport $VNC_PORT" 2>/dev/null || true
sleep 1

x11vnc -display ":$DISPLAY_NUM" -rfbport "$VNC_PORT" -forever -shared \
       -passwd "$VNC_PASSWORD" >/tmp/x11vnc.log 2>&1 &
sleep 2

cat <<EOF

  A virtual display is running on :$DISPLAY_NUM
  VNC is listening on port $VNC_PORT
  password: $VNC_PASSWORD

  Connect with any VNC viewer, then in the browser window that appears go to
  https://www.instagram.com and log in by hand.

  When you are done, close the viewer. The session is saved in
  $HERE/state/profile and will be reused from then on.

EOF

echo "  (Ctrl-C here does not stop the VNC server; use: pkill -f x11vnc)"

# Keep the browser open in the display so the user can log in.
export DISPLAY=":$DISPLAY_NUM"
cd "$HERE"
if [ ! -f secrets/cookies.txt ]; then
  echo "  No cookie file yet — run: .venv/bin/python -m igbot login --cookie-stdin"
fi
echo "  Tip: run '.venv/bin/python -m igbot login' in another terminal to paste the cookie."
