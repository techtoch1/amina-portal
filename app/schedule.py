"""Turn medicines into (a) today's agenda and (b) the desired Wbiztool reminders."""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .messages import build_message

# every-N-days with these N use a real cron expression; everything else is
# one-off scheduled messages (cron can't express "every 28 days").
# (N=1 and N=7 are normalised to daily/weekly by the parser.)


def _hm(text):
    h, m = text.split(":")
    return time(int(h), int(m))


def occurs_on(med, d):
    if med.kind == "daily":
        return True
    if med.kind == "weekly":
        return d.weekday() in med.weekdays
    if med.kind == "every_n":
        delta = (d - med.anchor).days
        return delta >= 0 and delta % med.every_n == 0
    return False


def next_occurrence(med, now):
    """First datetime strictly after `now` (naive local) the medicine is due, or None."""
    if med.kind in ("as_needed", "chart_only"):
        return None
    horizon = (med.every_n or 7) + 2
    for offset in range(horizon + 1):
        d = now.date() + timedelta(days=offset)
        if not occurs_on(med, d):
            continue
        for t in sorted(med.times):
            at = datetime.combine(d, _hm(t))
            if at > now:
                return at
    return None


def today_agenda(meds, today):
    """[(HH:MM, Medicine)] for today, time-ordered."""
    items = [(t, m) for m in meds if occurs_on(m, today) for t in m.times]
    return sorted(items, key=lambda x: (x[0], x[1].name))


def next_injections(meds, now):
    """Every-N medicines with their next due datetime."""
    out = []
    for m in meds:
        if m.kind == "every_n":
            nxt = next_occurrence(m, now)
            if nxt:
                out.append((m, nxt))
    return sorted(out, key=lambda x: x[1])


@dataclass
class Desired:
    kind: str  # "cron" | "oneoff"
    cron: str  # for cron
    fire_at: str  # "YYYY-MM-DD HH:MM" local, for oneoff
    message: str
    names: list
    recipient: str  # "mom" | "me"
    phone: str
    group_key: str  # stable across dates, used for one-off roll-over
    fingerprint: str

    @property
    def when(self):
        return self.cron if self.kind == "cron" else self.fire_at


def _cron_dow(py_weekdays):
    return ",".join(str(d) for d in sorted({(w + 1) % 7 for w in py_weekdays}))


def build_desired(meds, now, tz_name, recipients):
    """recipients: [("mom", phone), ("me", phone)]. Groups medicines due at the same moment."""
    groups = {}  # key -> {"kind", "cron", "fire_at", "meds"}
    for m in meds:
        if m.kind in ("as_needed", "chart_only"):
            continue
        for t in m.times:
            hh, mm = int(t[:2]), int(t[3:])
            if m.kind == "daily":
                key = ("cron", f"{mm} {hh} * * *")
                groups.setdefault(key, {"kind": "cron", "cron": key[1], "fire_at": "", "meds": []})
            elif m.kind == "weekly":
                cron = f"{mm} {hh} * * {_cron_dow(m.weekdays)}"
                key = ("cron", cron)
                groups.setdefault(key, {"kind": "cron", "cron": cron, "fire_at": "", "meds": []})
            else:  # every_n -> next occurrence of this time only
                nxt = next_occurrence(_only_time(m, t), now)
                if not nxt:
                    continue
                fire = nxt.strftime("%Y-%m-%d %H:%M")
                key = ("oneoff", fire)
                groups.setdefault(key, {"kind": "oneoff", "cron": "", "fire_at": fire, "meds": []})
            groups[key]["meds"].append(m)

    out = []
    for g in groups.values():
        names = sorted({m.name for m in g["meds"]})
        for who, phone in recipients:
            if not phone:
                continue
            message = build_message(g["meds"], copy=(who == "me"))
            if g["kind"] == "oneoff":
                group_key = f"oneoff|{g['fire_at'][11:]}|{who}|" + "|".join(names)
            else:
                group_key = f"cron|{g['cron']}|{who}"
            fp = hashlib.sha256(
                json.dumps([g["kind"], g["cron"], g["fire_at"], tz_name, who, phone, message],
                           ensure_ascii=False).encode()
            ).hexdigest()[:32]
            out.append(Desired(g["kind"], g["cron"], g["fire_at"], message, names, who, phone, group_key, fp))
    return sorted(out, key=lambda d: (d.kind, d.when, d.recipient))


def _only_time(med, t):
    """A shallow copy of the medicine restricted to one time (for per-time next_occurrence)."""
    from copy import copy

    c = copy(med)
    c.times = [t]
    return c


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def describe(med):
    """One-line human description of a medicine's schedule."""
    times = ", ".join(med.times)
    if med.kind == "daily":
        return f"Every day at {times}"
    if med.kind == "weekly":
        return f"Every {', '.join(_DAYS[d] for d in med.weekdays)} at {times}"
    if med.kind == "every_n":
        return f"Every {med.every_n} days at {times} (cycle includes {med.anchor.strftime('%d %b %Y')})"
    if med.kind == "chart_only":
        return "On the chart only (no reminders)"
    return "As needed (no reminders)"
