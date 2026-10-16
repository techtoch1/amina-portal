# Amina medicine reminder portal

A small FastAPI + SQLite app. It reads the medicine list from `data/medicines.xlsx`,
shows today's schedule, and keeps Wbiztool WhatsApp reminders in sync with it, so
messages go out even when the dashboard is closed (Wbiztool sends them, not this app).

Run it with one command: `python run.py`.

## Before you use it for real: verify the Wbiztool calls

This was written without access to https://wbiztool.com/docs/ (it could not be reached
from the build environment), and nothing has been run against the real API. The paths
and field names are in **one place**, `app/wbiztool.py` (`PATHS`, `FIELDS`, `ID_KEYS`).
Compare each with the docs, in particular: the cancel path for scheduled messages,
`/send_msg/` (used by the test button), the reminder field names (`phone`, `msg`, `cron`,
`timezone`), whether the phone must include `+`, and where the reminder id is returned.

Until you set `WBIZ_DOCS_VERIFIED=true` in `.env`, the app refuses to create, cancel or
send anything. `/me/` and status checks always work.

## Setup

```bash
cd medicine-reminder
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # then edit .env
```

Fill in `.env`: Wbiztool `client_id`, `api_key`, `whatsapp_client`, mom's and your number
(digits with country code), and `DASHBOARD_PASSWORD` (10+ characters).
For plain-http local testing set `COOKIE_SECURE=false`.

### 1. Test the credentials first

```bash
python -m app.cli me
```

### 2. Put your Excel file in `data/medicines.xlsx`

First sheet, header in row 1. Columns are matched by name (English or Arabic):

| Column | Accepted headers (examples) | Notes |
|---|---|---|
| Name (required) | Name, Medicine, اسم الدواء | |
| Time (required) | Time, Times, الوقت | `08:00`, `8am`, `08:00, 20:00`, or an Excel time cell |
| Frequency (required) | Frequency, التكرار | `Daily`, `Weekly`, `Every 28 days`, `Every 2 weeks`, `As needed` (Arabic: يومي، أسبوعي، كل 28 يوم، عند الحاجة) |
| Dose | Dose, الجرعة | |
| Day | Day, اليوم | weekly only: `Friday` or `Mon, Thu` |
| Start date | Start date, Next date | every-N-days: a date the cycle falls on (`YYYY-MM-DD`, or `DD/MM/YYYY`) |
| Last date | Last date, Last dose | every-N-days alternative: next = last + N |
| Instruction | Instruction, التعليمات | Arabic, goes into the message |
| Purpose | Purpose, For, الغرض | shown on the dashboard only |

A row is listed under **Needs review** (and gets no reminder) if the name is missing,
the time is missing or unclear, the frequency is unclear, a weekly row has no day, or an
every-N-days row has no start/last date. The file is re-read whenever it changes.

How rows become reminders:
- Daily / weekly: a Wbiztool recurring reminder (cron, `Asia/Beirut`). "Every 7 days" is a weekly cron.
- Every N days (N not 1 or 7, e.g. 28): cron can't express it, so the **next** date is scheduled as
  a one-off message. While the app is running it checks every 30 minutes and schedules the next one
  after a date passes (only for series you already approved). **The service must keep running for this.**
- As needed: dashboard only.
- Medicines due at the same moment are grouped into one message. If "copy to my number" is on, each
  reminder is created twice (mom and you).

### 3. Preview, approve, sync

```bash
python -m app.cli plan     # prints every reminder that would be created (no network)
python run.py              # open http://127.0.0.1:8000
```

In the dashboard press **Preview changes**, read the full list, tick the approval box, then
**Apply**. Apply only runs the exact previewed plan. Wbiztool reminders can't be edited, so a
change is a cancel + create; the reminder ids are stored in `data/reminders.sqlite3`.

## Deploy to amina.aligned-tech.com

On your Ubuntu server (Nginx already installed) with a DNS `A` record for
`amina.aligned-tech.com` pointing at it:

```bash
git clone <this repo> ~/amina-portal
sudo bash ~/amina-portal/deploy/install.sh
```

The installer asks for the Wbiztool credentials, both phone numbers and the dashboard
password (secrets are not echoed), then sets up:

- `/opt/amina` (code + virtualenv), run by a locked-down `amina` systemd service on `127.0.0.1:8010`
- `/etc/amina.env` (credentials, readable by root and the service only)
- `/var/lib/amina/` (Excel file and SQLite database; this is health data)
- an Nginx site for the domain and a Let's Encrypt HTTPS certificate (certbot)

It refuses to run if the domain or port is already used, never edits other Nginx sites, and is
safe to re-run. Change defaults with `DOMAIN=... PORT=8011 sudo -E bash deploy/install.sh`.

Afterwards:
1. `sudo install -m 640 -o amina -g amina medicines.xlsx /var/lib/amina/medicines.xlsx`
2. Verify the Wbiztool paths (see the top of this file), then set `WBIZ_DOCS_VERIFIED=true`
   in `/etc/amina.env` and `sudo systemctl restart amina`.
3. Update later with `bash ~/amina-portal/deploy/update.sh`. Logs: `journalctl -u amina -f`.

## Security notes

- Secrets live only in `.env`; `.env` and `data/` are git-ignored. The API key is never logged,
  shown on the dashboard, or included in stored error messages.
- Single-user login, constant-time password check, 5 failed attempts lock out an IP for 5 minutes,
  CSRF token on every state-changing request, CSP and `no-store` headers.
- `data/` holds health information; back it up and keep it private.

## Tests

```bash
pip install -r requirements-dev.txt && python -m pytest
```
