#!/usr/bin/env bash
# Installs the Amina medicine-reminder portal on an Ubuntu server that already runs Nginx.
#
#   git clone <this repo> ~/amina-portal
#   sudo bash ~/amina-portal/deploy/install.sh
#
# Adds (and nothing else):
#   /opt/amina/                         app code + virtualenv
#   /etc/amina.env                      credentials, root:amina 640
#   /var/lib/amina/                     Excel file, SQLite database (health data)
#   /etc/systemd/system/amina.service   runs the app on 127.0.0.1:$PORT
#   /etc/nginx/sites-available/amina.conf (+ link) and an HTTPS certificate via certbot
# Never edits other Nginx sites; removes its own site file if the Nginx test fails. Safe to re-run.
set -euo pipefail

DOMAIN="${DOMAIN:-amina.aligned-tech.com}"
PORT="${PORT:-8010}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/amina; DATA=/var/lib/amina; ENVF=/etc/amina.env
UNIT=/etc/systemd/system/amina.service
SITE=/etc/nginx/sites-available/amina.conf; LINK=/etc/nginx/sites-enabled/amina.conf

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mStopped: %s\033[0m\n' "$*" >&2; exit 1; }
env_get() { [[ -f "$ENVF" ]] && sed -n "s/^$1=//p" "$ENVF" | tail -1 || true; }
env_set() { local t; t="$(mktemp)"; { [[ -f "$ENVF" ]] && grep -v "^$1=" "$ENVF" || true; printf '%s=%s\n' "$1" "$2"; } >"$t"
            install -m 640 -o root -g amina "$t" "$ENVF"; rm -f "$t"; }
# ask KEY "prompt" [secret]  -- keeps the saved value when you just press Enter; never echoes secrets
ask() { local cur v; cur="$(env_get "$1")"
        if [[ "${3:-}" == secret ]]; then read -rsp "$2${cur:+ [saved, Enter to keep]}: " v; echo; else read -rp "$2${cur:+ [$cur]}: " v; fi
        v="${v:-$cur}"; [[ -n "$v" ]] || fail "$1 is required"; [[ "$v" != *$'\n'* ]] || fail "bad value"; env_set "$1" "$v"; }

[[ $EUID -eq 0 ]] || fail "run with sudo: sudo bash $0"
command -v nginx >/dev/null || fail "Nginx isn't installed."
command -v python3 >/dev/null || fail "python3 isn't installed."
python3 -c 'import sys; sys.exit(sys.version_info < (3,10))' || fail "Python 3.10+ is needed."

say "Checking $DOMAIN and port $PORT are free"
OTHER="$(grep -rlE "server_name[^;]*[[:space:]]$DOMAIN[[:space:];]" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | grep -v 'amina.conf' || true)"
[[ -z "$OTHER" ]] || fail "$DOMAIN is already used by: $OTHER"
if ss -ltnH "sport = :$PORT" | grep -q . && ! systemctl is-active --quiet amina; then
  fail "port $PORT is in use. Re-run with PORT=8011 sudo -E bash $0"; fi

say "Installing app files"
id amina >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin amina
mkdir -p "$APP" "$DATA"
tar --exclude=.git --exclude=data --exclude=.venv --exclude=__pycache__ --exclude=.env -C "$SRC" -cf - . | tar -C "$APP" -xf -
# A venv left half-made by an earlier failed run (no pip) must be rebuilt, not reused.
if [[ ! -x "$APP/.venv/bin/pip" ]]; then
  rm -rf "$APP/.venv"
  python3 -m venv "$APP/.venv" || fail "couldn't create the virtualenv. Run: sudo apt install -y python3-venv   then re-run this script."
fi
[[ -x "$APP/.venv/bin/pip" ]] || fail "virtualenv has no pip. Run: sudo apt install -y python3-venv python3-pip   then re-run."
"$APP/.venv/bin/pip" install -q --upgrade pip || fail "pip upgrade failed (no internet from the server?)"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" || fail "installing Python packages failed (see the error above)"
"$APP/.venv/bin/python" -c "import fastapi, uvicorn, openpyxl, requests, dotenv, jinja2, itsdangerous, multipart" \
  || fail "Python packages are still missing after install"
chown -R root:root "$APP"; chown -R amina:amina "$DATA"; chmod 700 "$DATA"

say "Dashboard password (stored in $ENVF, readable by root and the service only)"
[[ -f "$ENVF" ]] || { install -m 640 -o root -g amina /dev/null "$ENVF"; }
ask DASHBOARD_PASSWORD "Dashboard password (10+ characters)" secret
[[ "$(env_get DASHBOARD_PASSWORD | wc -c)" -ge 11 ]] || fail "DASHBOARD_PASSWORD must be at least 10 characters"
[[ -n "$(env_get WBIZ_DOCS_VERIFIED)" ]] || env_set WBIZ_DOCS_VERIFIED false
env_set HOST 127.0.0.1; env_set PORT "$PORT"; env_set COOKIE_SECURE true
env_set DATA_DIR "$DATA"; env_set TIMEZONE Asia/Beirut
[[ -n "$(env_get SEND_COPY_TO_ME)" ]] || env_set SEND_COPY_TO_ME true

say "Service"
cat >"$UNIT" <<UNITEOF
[Unit]
Description=Amina medicine reminder portal
After=network-online.target
Wants=network-online.target

[Service]
User=amina
Group=amina
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=$APP/.venv/bin/python run.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload && systemctl enable amina >/dev/null && systemctl restart amina
sleep 2; systemctl is-active --quiet amina || { journalctl -u amina -n 20 --no-pager; fail "service didn't start"; }

say "Nginx + HTTPS for $DOMAIN"
cat >"$SITE" <<SITEEOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 1m;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_read_timeout 180s;   # applying reminders can take a while when Wbiztool is slow
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
SITEEOF
ln -sf "$SITE" "$LINK"
if ! nginx -t 2>/dev/null; then rm -f "$SITE" "$LINK"; fail "Nginx config test failed; removed amina.conf (other sites untouched)."; fi
systemctl reload nginx
command -v certbot >/dev/null || apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect -m "${CERTBOT_EMAIL:-admin@aligned-tech.com}" \
  || fail "certbot failed. Does the DNS record for $DOMAIN point at this server?"

say "Done: https://$DOMAIN"
cat <<MSG
Next:
  1. Sign in at https://$DOMAIN, open Settings and enter the Wbiztool client ID, API key, WhatsApp client and the
     two phone numbers, then press "Save & test connection".
  2. Add Mom's medicines in the Medicines tab (or import your Excel file) and fill in her profile.
  3. Check the Wbiztool endpoint paths in $APP/app/wbiztool.py against https://wbiztool.com/docs/, then tick the
     confirmation box in Settings.
  4. Reminders tab: Preview changes, read the list, approve.
MSG
