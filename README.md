# Amina medicine reminder portal

A small FastAPI + SQLite app with four tabs: **Today** (schedule, Mom's latest status),
**Medicines** (add/edit/stop medicines, or import an Excel file), **Mom** (profile, allergies, status log
such as blood pressure or sugar) and **Reminders** (Wbiztool connection, preview-and-approve sync).
Wbiztool sends the WhatsApp messages, so they go out even when the dashboard is closed.

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

Fill in `.env`: `DASHBOARD_PASSWORD` (10+ characters; you sign in with the user name `owner` and this password). The Wbiztool details and phone numbers can be entered
in the app's Settings tab (values saved there override `.env`).
For plain-http local testing set `COOKIE_SECURE=false`.

### 1. Test the credentials first

```bash
python -m app.cli check    # shows the settings' shape (never the key) and calls /me/
```

### 2. Add the medicines

Open the **Medicines** tab: *+ Add medicine* (name, dose, what it is for, Arabic instruction, how often,
times) or *Import from Excel*. Import columns are matched by header name (English or Arabic):
Name, Time and Frequency are required; Dose, Day, Start date, Last date, Instruction, Purpose are optional.
`Frequency`: `Daily`, `Weekly` (+ Day, e.g. `Friday` or `Mon, Thu`), `Every 28 days` (+ Start date `YYYY-MM-DD` or
`DD/MM/YYYY`, or Last date), `As needed`. Duplicates are skipped on re-import.

An imported row that is unclear (missing time, unknown frequency, weekly without a day, every-N-days without a
date) is flagged **Needs review** and gets no reminder until you fix it with *Edit*. *Stop taking* pauses a
medicine and cancels its reminders on the next sync.

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

In the **Reminders** tab press **Preview changes**, read the full list, tick the approval box, then
**Apply**. Apply only runs the exact previewed plan. Wbiztool reminders can't be edited, so a
change is a cancel + create; the reminder ids are stored in `data/reminders.sqlite3`.

## Deploy to amina.aligned-tech.com

On your Ubuntu server (Nginx already installed) with a DNS `A` record for
`amina.aligned-tech.com` pointing at it:

```bash
git clone <this repo> ~/amina-portal
sudo bash ~/amina-portal/deploy/install.sh
```

The installer asks for the dashboard password (not echoed), then sets up:

- `/opt/amina` (code + virtualenv), run by a locked-down `amina` systemd service on `127.0.0.1:8010`
- `/etc/amina.env` (credentials, readable by root and the service only)
- `/var/lib/amina/` (Excel file and SQLite database; this is health data)
- an Nginx site for the domain and a Let's Encrypt HTTPS certificate (certbot)

It refuses to run if the domain or port is already used, never edits other Nginx sites, and is
safe to re-run. Change defaults with `DOMAIN=... PORT=8011 sudo -E bash deploy/install.sh`.

Afterwards:
1. Sign in at https://amina.aligned-tech.com and open **Settings**: enter the Wbiztool client ID, API key,
   WhatsApp client and both phone numbers, then **Save & test connection**. The API key is stored in the
   database on the server (owner-only file permissions) and is never displayed again.
2. Add the medicines and fill in Mom's profile.
3. Verify the Wbiztool paths (see the top of this file), then tick the confirmation box in Settings.
4. Update later with `bash ~/amina-portal/deploy/update.sh`. Logs: `journalctl -u amina -f`.

If the connection test says **invalid api key**, copy the key again from Wbiztool and save it. To see what the
server has (key length and odd characters only, never the key):
`cd /opt/amina && sudo -u amina ENV_FILE=/etc/amina.env .venv/bin/python -m app.cli check`

Back up `/var/lib/amina/` (medicines, Mom's status log, reminder ids). It is health data: keep it private.

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
