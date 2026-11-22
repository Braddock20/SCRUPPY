#!/usr/bin/env bash
# One-shot installer for igbot.
#
#   ./install.sh
#
# Creates a virtual environment, installs the two Python dependencies and
# downloads the Chromium build Playwright drives. Nothing else on your machine
# is touched.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- python
say "Checking Python"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || die "python3 not found. Install Python 3.10 or newer."
"$PYTHON" - <<'EOF' || die "Python 3.10 or newer is required."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
EOF
"$PYTHON" --version

# ------------------------------------------------------------------ venv
if [ ! -d ".venv" ]; then
  say "Creating the virtual environment in .venv"
  "$PYTHON" -m venv .venv
fi
VENV_PY="$HERE/.venv/bin/python"
[ -x "$VENV_PY" ] || die "the virtual environment was not created correctly"

# --------------------------------------------------------------- packages
say "Installing Python packages"
if ! "$VENV_PY" -m pip install --quiet --upgrade pip; then
  warn "could not upgrade pip; continuing with the bundled version"
fi

# Some networks need an explicit index. Set PIP_INDEX_URL to override.
if ! "$VENV_PY" -m pip install --quiet -r requirements.txt; then
  warn "install from requirements.txt failed, trying the packages directly"
  "$VENV_PY" -m pip install --quiet playwright PyYAML
fi

# --------------------------------------------------------------- browser
say "Downloading Chromium for Playwright (about 150 MB, one time only)"
if ! "$VENV_PY" -m playwright install chromium; then
  warn "Chromium download failed. Try: .venv/bin/python -m playwright install --with-deps chromium"
fi

# --------------------------------------------------------------- display
if ! command -v Xvfb >/dev/null 2>&1; then
  warn "Xvfb is not installed — needed only if this machine has no display."
  warn "  Debian/Ubuntu:  sudo apt install -y xvfb x11vnc"
  warn "  Fedora:          sudo dnf install xorg-x11-server-Xvfb tigervnc-server"
fi

mkdir -p state logs secrets
chmod 700 secrets
[ -f secrets/cookies.txt ] && chmod 600 secrets/cookies.txt

cat <<'EOF'

--------------------------------------------------------------------------
Installed.

Next steps:

  1. Get your Instagram cookie (see README.md, section "Logging in").
     Then:  .venv/bin/python -m igbot login --cookie-file secrets/cookies.txt

  2. Check everything works:
        .venv/bin/python -m igbot doctor

  3. Watch for a few days WITHOUT replying (baseline mode is on by
     default in config.yaml):
        .venv/bin/python -m igbot watch

  4. When you are happy, edit config.yaml and set:
        behaviour.baseline_only: false
     to start replying.
--------------------------------------------------------------------------
EOF
