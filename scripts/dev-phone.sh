#!/usr/bin/env bash
# Run the development server reachable from your phone over HTTPS through Tailscale.
#
#   scripts/dev-phone.sh            # port 8000
#   PORT=8001 scripts/dev-phone.sh
#
# Needs: Tailscale running on this computer and on the phone (same account), MagicDNS and
# HTTPS Certificates enabled in the Tailscale admin console, and the PostgreSQL started with
# Docker Compose (`docker compose up -d db`), reached with the usual POSTGRES_* settings.
# Ctrl+C stops Django and removes the Tailscale serve rule.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

die() { echo "error: $*" >&2; exit 1; }

command -v tailscale >/dev/null || die "tailscale is not installed (https://tailscale.com/download)."
command -v uv >/dev/null || die "uv is not installed."

status_json="$(tailscale status --json 2>/dev/null)" || die "tailscale is not running: start it with 'sudo tailscale up'."
read -r backend dns certs < <(python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("BackendState", ""), (d.get("Self") or {}).get("DNSName", "").rstrip(".") or "-", len(d.get("CertDomains") or []))
' <<<"$status_json")

[[ "$backend" == "Running" ]] || die "tailscale is '$backend': connect it with 'sudo tailscale up'."
[[ "$dns" != "-" ]] || die "this machine has no Tailscale DNS name: enable MagicDNS in the admin console."
[[ "$certs" -gt 0 ]] || die "HTTPS certificates are off: enable 'HTTPS Certificates' in the Tailscale admin console (DNS page)."

# Fail early with a clear message if something (another runserver, a container) already uses the port.
if ! python3 -c 'import socket, sys; s = socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1])))' "$PORT" 2>/dev/null; then
  die "port $PORT is already in use: stop what is listening there, or run with PORT=<another port>."
fi

# The Docker PostgreSQL must be up: give a clear hint instead of a Django traceback.
uv run python manage.py shell -c "from django.db import connection; connection.ensure_connection()" >/dev/null 2>&1 \
  || die "cannot reach PostgreSQL: start it with 'docker compose up -d db' (or check the POSTGRES_* variables)."

# Never change the schema from here (the database may be shared): only check it is up to date.
uv run python manage.py migrate --check >/dev/null 2>&1 \
  || die "unapplied migrations: run 'uv run python manage.py migrate' first."

export DJANGO_ALLOWED_HOSTS="${dns},localhost,127.0.0.1"
export DJANGO_CSRF_TRUSTED_ORIGINS="https://${dns}"
export DJANGO_BEHIND_HTTPS_PROXY=true

if ! tailscale serve --bg --yes --https=443 "http://127.0.0.1:${PORT}" >/dev/null; then
  die "tailscale serve failed. If it is a permission error, allow your user once with: sudo tailscale set --operator=\$USER"
fi
cleanup() { tailscale serve --https=443 off >/dev/null 2>&1 || true; }
trap cleanup EXIT

cat <<EOF

  MTG Ledger is on your tailnet:  https://${dns}
  No user yet? Create one with: uv run python manage.py createsuperuser

  Open that address on the computer, connect CardTrader in Profile and settings,
  start a session in Scan cards and scan the QR code with the phone (Tailscale on).
  Card recognition reads OPENROUTER_API_KEY from the environment or .env.
  Ctrl+C stops everything.

EOF

uv run python manage.py runserver "127.0.0.1:${PORT}"
