#!/usr/bin/env bash
# Set up the dograh caller on a Linux server (or macOS), in this folder.
#
#   1. Copy the dograh_caller folder to the server, with its .env
#      (GEMINI_API_KEY, DOGRAH_API_KEY, DOGRAH_BASE_URL, LEAD_SOURCE_TOKEN).
#   2. bash install.sh
#   3. Start the dashboard inside tmux, so it survives a dropped SSH connection
#      (stays idle on startup until you click Start Batch; pulls from reverse proxy):
#        tmux new -s caller
#        .venv/bin/python dashboard_server.py
#
# Safe to run again (e.g. after updating the code). No root needed.
# It installs nothing system-wide and takes no leads from the streamer.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

say()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || fail "$PY is not installed (Python 3.10 or newer is needed)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 or newer is needed (found $("$PY" -V 2>&1)). Set PYTHON=/path/to/python3.x"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || fail "python venv support is missing: apt install python3-venv"

say "Settings (.env)"
[ -f .env ] || fail ".env is missing - copy .env.example to .env and fill it in"
chmod 600 .env
missing=""
grep -qE '^DOGRAH_API_KEY=.+' .env || missing="$missing DOGRAH_API_KEY"
grep -qE '^(LEAD_SOURCE_TOKEN|STREAM_TOKEN)=.+' .env || missing="$missing LEAD_SOURCE_TOKEN"
grep -qE '^(GEMINI_API_KEY|GEMINI_API)=.+' .env || missing="$missing GEMINI_API_KEY"
[ -z "$missing" ] || fail "empty or missing in .env:$missing"
echo ".env has the keys it needs (permissions set to 600)"

say "Python environment (.venv)"
[ -d .venv ] || "$PY" -m venv .venv
BIN=.venv/bin
[ -d .venv/Scripts ] && BIN=.venv/Scripts      # Windows (Git Bash)
"$BIN/python" -m pip install --quiet --disable-pip-version-check --timeout 20 --retries 5 -r requirements.txt
mkdir -p output

say "Checking keys, Dograh agents and the lead streamer (no leads are taken)"
if "$BIN/python" pull_leads.py --check; then
  cat <<'MSG'

Ready. Start the dashboard inside tmux (stays idle until you click Start Batch):
    tmux new -s caller
    .venv/bin/python dashboard_server.py
Detach with Ctrl+B then D; come back with: tmux attach -t caller
MSG
else
  cat <<MSG

Installed, but the check above failed. Fix what it says, then run:
    $BIN/python pull_leads.py --check
MSG
  exit 1
fi
