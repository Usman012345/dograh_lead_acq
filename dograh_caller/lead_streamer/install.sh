#!/usr/bin/env bash
# Install or update the lead streamer on the scraper server (next to the dataops database).
#
#   1. Copy this whole lead_streamer folder to the server (with its .env).
#   2. sudo bash install.sh
#
# Safe to run again after changing lead_streamer.py or .env: it updates /opt/lead_streamer
# and restarts the service. It does not touch the scraper, its containers or nginx.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/lead_streamer
SVC_USER=lead-streamer

say()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root: sudo bash install.sh"
[ -f "$SRC/.env" ] || fail "$SRC/.env is missing (copy .env.example to .env and fill in DATABASE_URL and STREAM_TOKEN)"
command -v python3 >/dev/null || fail "python3 is not installed"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 or newer is needed (found $(python3 -V 2>&1))"
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || fail "python3 venv support is missing: apt install python3-venv"

say "Service user $SVC_USER"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

say "Copying files to $DEST"
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  for f in lead_streamer.py requirements.txt .env .env.example lead-streamer.service \
           nginx-lead-streamer.conf README.md LEAD_STREAMER_SPEC.md install.sh; do
    if [ -f "$SRC/$f" ]; then install -m 0644 "$SRC/$f" "$DEST/$f"; fi
  done
  install -m 0640 "$SRC/.env" "$DEST/.env"
fi

say "Python environment"
[ -x "$DEST/.venv/bin/python" ] || python3 -m venv "$DEST/.venv"
# pypi.org can be slow over IPv6 from this server; short timeouts make pip fall back quickly.
"$DEST/.venv/bin/pip" install --quiet --disable-pip-version-check --timeout 20 --retries 5 \
  -r "$DEST/requirements.txt"

chown -R root:"$SVC_USER" "$DEST"
chmod 0750 "$DEST"
chmod 0640 "$DEST/.env"          # readable by the service, not by other users

say "Checking the database (nothing is changed)"
"$DEST/.venv/bin/python" "$DEST/lead_streamer.py" --check \
  || fail "database check failed - fix .env (DATABASE_URL) and run install.sh again"

say "systemd service"
install -m 0644 "$DEST/lead-streamer.service" /etc/systemd/system/lead-streamer.service
systemctl daemon-reload
systemctl enable lead-streamer >/dev/null
systemctl restart lead-streamer

PORT="$(grep -E '^BIND_PORT=' "$DEST/.env" | tail -1 | cut -d= -f2 | tr -d '[:space:]"' || true)"
PORT="${PORT:-8090}"
ok=""
for _ in $(seq 1 20); do
  if body="$(curl -s -m 5 "http://127.0.0.1:$PORT/health")" && [ -n "$body" ]; then ok=1; break; fi
  sleep 1
done
[ -n "$ok" ] || { journalctl -u lead-streamer -n 30 --no-pager; fail "service did not answer on 127.0.0.1:$PORT"; }

say "Done"
echo "health: $body"
journalctl -u lead-streamer -n 5 --no-pager -o cat
cat <<MSG

The streamer runs on 127.0.0.1:$PORT. To let the dograh caller reach it at
https://ai-sdr.grobots.ai, that name must point at this server and nginx must
proxy it here with a valid certificate (nginx-lead-streamer.conf, then certbot).
Logs: journalctl -u lead-streamer -f
MSG
