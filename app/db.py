import os
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
CREATE TABLE IF NOT EXISTS medicines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '', dose TEXT NOT NULL DEFAULT '', times TEXT NOT NULL DEFAULT '',
  frequency TEXT NOT NULL DEFAULT '', day TEXT NOT NULL DEFAULT '',
  start_date TEXT NOT NULL DEFAULT '', last_date TEXT NOT NULL DEFAULT '',
  instruction TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS status_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stock_items (
  medicine_id INTEGER PRIMARY KEY,
  tracking INTEGER NOT NULL DEFAULT 1,
  units_per_dose REAL NOT NULL DEFAULT 1,
  daily_use REAL,                          -- manual units per day, used when the medicine has no schedule
  through_day TEXT NOT NULL DEFAULT '',    -- last day whose scheduled use has been written to the moves
  alert_state TEXT NOT NULL DEFAULT '',    -- '' | low | out  (what the last WhatsApp alert said)
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  medicine_id INTEGER NOT NULL,
  day TEXT NOT NULL,                       -- local date the move applies to
  kind TEXT NOT NULL,                      -- start | buy | used | adjust | auto
  qty REAL NOT NULL,                       -- + adds stock, - uses stock
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_moves_med ON stock_moves(medicine_id, day);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        self.path = str(path)
        with self.conn() as c:
            c.executescript(SCHEMA)
            have = {r["name"] for r in c.execute("PRAGMA table_info(medicines)")}
            for col in ("when_en", "purpose_ar", "notes_en", "notes_ar", "form", "form_ar"):  # added after the first release
                if col not in have:
                    c.execute(f"ALTER TABLE medicines ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            slog = {r["name"] for r in c.execute("PRAGMA table_info(status_log)")}
            if "event_date" not in slog:  # the day it happened / will happen (entries used to have only a created time)
                c.execute("ALTER TABLE status_log ADD COLUMN event_date TEXT NOT NULL DEFAULT ''")
                c.execute("ALTER TABLE status_log ADD COLUMN event_time TEXT NOT NULL DEFAULT ''")
                c.execute("UPDATE status_log SET event_date=substr(ts,1,10) WHERE event_date=''")
            rem_cols = {r["name"] for r in c.execute("PRAGMA table_info(reminders)")}
            if "once" not in rem_cols:  # a one-off date sent as a date-specific recurring reminder
                c.execute("ALTER TABLE reminders ADD COLUMN once INTEGER NOT NULL DEFAULT 0")
            si_cols = {r["name"] for r in c.execute("PRAGMA table_info(stock_items)")}
            if si_cols and "unit" not in si_cols:  # what the stock is counted in; '' = the default (tablets)
                c.execute("ALTER TABLE stock_items ADD COLUMN unit TEXT NOT NULL DEFAULT ''")
            if "remind" not in have:  # reminders are now opt-in per medicine; keep the ones that already had times
                c.execute("ALTER TABLE medicines ADD COLUMN remind INTEGER NOT NULL DEFAULT 0")
                c.execute("UPDATE medicines SET remind=1 WHERE times != '' AND active=1")
        try:  # holds the API key and health data: owner-only
            os.chmod(self.path, 0o600)
        except OSError:
            pass

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

    def recent_events(self, limit=10, failed_only=False):
        where = "WHERE ok=0" if failed_only else ""
        with self.conn() as c:
            return [dict(r) for r in c.execute(f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (limit,))]

    def count_failed_since(self, iso):
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM events WHERE ok=0 AND ts>=?", (iso,)).fetchone()[0]

    def active(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM reminders WHERE status='active' ORDER BY id")]

    def add_reminder(self, d, remote_id, kind=None, cron=None, once=False):
        """Store what was created on Wbiztool. kind/cron override the plan's when a one-off date was
        sent as a date-specific recurring reminder (kind='cron', once=True)."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO reminders(fingerprint,group_key,kind,recipient,cron,fire_at,message,names,remote_id,status,created_at,once)"
                " VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
                (d.fingerprint, d.group_key, kind or d.kind, d.recipient, d.cron if cron is None else cron, d.fire_at,
                 d.message, ", ".join(d.names), remote_id, now_iso(), int(once)),
            )

    def end_reminder(self, row_id, status):
        with self.conn() as c:
            c.execute("UPDATE reminders SET status=?, ended_at=? WHERE id=?", (status, now_iso(), row_id))

    def has_history(self, group_key):
        with self.conn() as c:
            return c.execute("SELECT 1 FROM reminders WHERE group_key=? LIMIT 1", (group_key,)).fetchone() is not None

    # ---- medicines ----
    MED_FIELDS = ("name", "dose", "times", "frequency", "day", "start_date", "last_date", "instruction", "purpose",
                  "when_en", "purpose_ar", "notes_en", "notes_ar", "form", "form_ar")

    def list_medicines(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM medicines ORDER BY lower(name), id")]

    def get_medicine(self, mid):
        with self.conn() as c:
            row = c.execute("SELECT * FROM medicines WHERE id=?", (mid,)).fetchone()
        return dict(row) if row else None

    def add_medicine(self, rec, active=True, remind=False):
        cols = list(self.MED_FIELDS) + ["remind", "active", "created_at"]
        vals = [rec.get(f, "") for f in self.MED_FIELDS] + [int(remind), int(active), now_iso()]
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO medicines({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
            return cur.lastrowid

    def update_medicine(self, mid, fields):
        """Update only the given columns."""
        keys = [k for k in fields if k in self.MED_FIELDS]
        if not keys:
            return False
        with self.conn() as c:
            return c.execute(f"UPDATE medicines SET {','.join(k + '=?' for k in keys)} WHERE id=?",
                             [fields[k] for k in keys] + [mid]).rowcount > 0

    def set_remind(self, mid, on):
        with self.conn() as c:
            return c.execute("UPDATE medicines SET remind=? WHERE id=?", (int(on), mid)).rowcount > 0

    def delete_all_medicines(self):
        with self.conn() as c:
            c.execute("DELETE FROM stock_moves")
            c.execute("DELETE FROM stock_items")
            return c.execute("DELETE FROM medicines").rowcount

    def clear_status(self):
        with self.conn() as c:
            return c.execute("DELETE FROM status_log").rowcount

    def set_medicine_active(self, mid, active):
        with self.conn() as c:
            return c.execute("UPDATE medicines SET active=? WHERE id=?", (int(active), mid)).rowcount > 0

    def delete_medicine(self, mid):
        with self.conn() as c:
            c.execute("DELETE FROM stock_moves WHERE medicine_id=?", (mid,))
            c.execute("DELETE FROM stock_items WHERE medicine_id=?", (mid,))
            return c.execute("DELETE FROM medicines WHERE id=?", (mid,)).rowcount > 0

    # ---- mom's profile (key/value) and status log ----
    def profile(self):
        with self.conn() as c:
            return {r["key"][8:]: r["value"] for r in c.execute("SELECT * FROM settings WHERE key LIKE 'profile.%'")}

    def set_profile(self, values):
        for k, v in values.items():
            self.set_setting("profile." + k, v)

    def add_status(self, kind, value, note, event_date="", event_time=""):
        with self.conn() as c:
            c.execute("INSERT INTO status_log(ts,kind,value,note,event_date,event_time) VALUES(?,?,?,?,?,?)",
                      (now_iso(), kind, value, note, event_date or now_iso()[:10], event_time))

    def list_status(self, limit=60):
        """Newest event first (by the date it happened or is planned), not by when it was typed in."""
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM status_log ORDER BY event_date DESC, event_time DESC, id DESC LIMIT ?", (limit,))]

    def delete_status(self, sid):
        with self.conn() as c:
            return c.execute("DELETE FROM status_log WHERE id=?", (sid,)).rowcount > 0

    # ---- stock ----
    def stock_items(self):
        with self.conn() as c:
            return {r["medicine_id"]: dict(r) for r in c.execute("SELECT * FROM stock_items")}

    def stock_item(self, mid):
        with self.conn() as c:
            row = c.execute("SELECT * FROM stock_items WHERE medicine_id=?", (mid,)).fetchone()
        return dict(row) if row else None

    def ensure_stock_item(self, mid, through_day):
        """Create the tracking row; scheduled use is counted from the day AFTER `through_day`."""
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO stock_items(medicine_id, through_day, created_at) VALUES(?,?,?)",
                      (mid, through_day, now_iso()))
        return self.stock_item(mid)

    def update_stock_item(self, mid, **fields):
        keys = [k for k in fields if k in ("tracking", "units_per_dose", "daily_use", "through_day", "alert_state", "unit")]
        if not keys:
            return
        with self.conn() as c:
            c.execute(f"UPDATE stock_items SET {','.join(k + '=?' for k in keys)} WHERE medicine_id=?",
                      [fields[k] for k in keys] + [mid])

    def add_move(self, mid, day, kind, qty, note=""):
        with self.conn() as c:
            c.execute("INSERT INTO stock_moves(medicine_id,day,kind,qty,note,created_at) VALUES(?,?,?,?,?,?)",
                      (mid, day, kind, float(qty), note[:200], now_iso()))

    def stock_sum(self, mid, up_to_day=None):
        with self.conn() as c:
            if up_to_day:
                row = c.execute("SELECT COALESCE(SUM(qty),0) FROM stock_moves WHERE medicine_id=? AND day<=?", (mid, up_to_day)).fetchone()
            else:
                row = c.execute("SELECT COALESCE(SUM(qty),0) FROM stock_moves WHERE medicine_id=?", (mid,)).fetchone()
        return row[0]

    def has_moves(self, mid):
        with self.conn() as c:
            return c.execute("SELECT 1 FROM stock_moves WHERE medicine_id=? LIMIT 1", (mid,)).fetchone() is not None

    def moves(self, mid, limit=40):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM stock_moves WHERE medicine_id=? ORDER BY day DESC, id DESC LIMIT ?", (mid, limit))]

    def moves_between(self, start_day, end_day):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM stock_moves WHERE day>=? AND day<=? ORDER BY day, id", (start_day, end_day))]
