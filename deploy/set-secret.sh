#!/usr/bin/env bash
# Re-enter one secret (default WBIZ_API_KEY) without echoing it, then test it against Wbiztool.
#   sudo bash ~/amina-portal/deploy/set-secret.sh                 # the API key
#   sudo bash ~/amina-portal/deploy/set-secret.sh WBIZ_CLIENT_ID  # or another setting
set -euo pipefail
KEY="${1:-WBIZ_API_KEY}"
ENVF=/etc/amina.env
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -f "$ENVF" ]] || { echo "$ENVF not found - run deploy/install.sh first" >&2; exit 1; }
[[ "$KEY" =~ ^[A-Z_]+$ ]] || { echo "bad setting name" >&2; exit 1; }
if [[ "$KEY" == *KEY* || "$KEY" == *PASSWORD* ]]; then read -rsp "New value for $KEY (hidden): " V; echo; else read -rp "New value for $KEY: " V; fi
V="${V//$'\r'/}"; V="$(printf '%s' "$V" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
[[ -n "$V" && "$V" != *$'\n'* ]] || { echo "empty or invalid value" >&2; exit 1; }
T="$(mktemp)"; { grep -v "^$KEY=" "$ENVF" || true; printf '%s=%s\n' "$KEY" "$V"; } >"$T"
install -m 640 -o root -g amina "$T" "$ENVF"; rm -f "$T"
systemctl restart amina
echo "Saved. Testing against Wbiztool..."
sudo -u amina ENV_FILE="$ENVF" /opt/amina/.venv/bin/python -m app.cli check \
  || sudo -u amina env ENV_FILE="$ENVF" /opt/amina/.venv/bin/python -m app.cli check || true
