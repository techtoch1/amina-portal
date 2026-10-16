import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  group_key TEXT NOT NULL,
  kind TEXT NOT NULL,            -- cron | oneoff
  recipient TEXT NOT NULL,       -- mom | me
  cron TEXT NOT NULL DEFAULT '',
  fire_at TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  names TEXT NOT NULL DEFAULT '',
  remote_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,          -- active | cancelled | done
  created_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_rem_status ON reminders(status);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,            -- test | create | cancel | rollover
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        self.path = str(path)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def get_setting(self, key, default=None):
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, str(value)))

    def log_event(self, kind, ok, detail):
        with self.conn() as c:
            c.execute("INSERT INTO events(ts,kind,ok,detail) VALUES(?,?,?,?)", (now_iso(), kind, int(ok), detail[:500]))

    def recent_events(self, limit=10):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def active(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM reminders WHERE status='active' ORDER BY id")]

    def add_reminder(self, d, remote_id):
        with self.conn() as c:
            c.execute(
                "INSERT INTO reminders(fingerprint,group_key,kind,recipient,cron,fire_at,message,names,remote_id,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,'active',?)",
                (d.fingerprint, d.group_key, d.kind, d.recipient, d.cron, d.fire_at, d.message,
                 ", ".join(d.names), remote_id, now_iso()),
            )

    def end_reminder(self, row_id, status):
        with self.conn() as c:
            c.execute("UPDATE reminders SET status=?, ended_at=? WHERE id=?", (status, now_iso(), row_id))

    def has_history(self, group_key):
        with self.conn() as c:
            return c.execute("SELECT 1 FROM reminders WHERE group_key=? LIMIT 1", (group_key,)).fetchone() is not None
