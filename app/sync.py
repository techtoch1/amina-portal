"""Plan = what must change on Wbiztool so it matches the Excel schedule.

compute_plan() never touches the network. apply_plan() only runs what the user
previewed and approved (the plan hash must still match).
"""
import hashlib
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from .schedule import build_desired
from .wbiztool import WbizError

_lock = threading.Lock()


def local_now(settings):
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None, microsecond=0)


def recipients(settings, db):
    out = [("mom", settings.mom_number)]
    if copy_enabled(settings, db):
        out.append(("me", settings.my_number))
    return out


def include_purpose(db):
    """Add 'الغرض: ...' (what the medicine is for, in Arabic) to each reminder. On by default."""
    return db.get_setting("msg.purpose", "1") == "1"


def copy_enabled(settings, db):
    return db.get_setting("copy_to_me", "1" if settings.copy_to_me_default else "0") == "1"


def expire_finished(db, now):
    """One-off messages whose time has passed are finished (nothing to cancel)."""
    for row in db.active():
        if row["kind"] == "oneoff" and row["fire_at"] and row["fire_at"] < now.strftime("%Y-%m-%d %H:%M"):
            db.end_reminder(row["id"], "done")


def compute_plan(settings, db, medicines, now=None):
    now = now or local_now(settings)
    expire_finished(db, now)
    desired = build_desired(medicines, now, settings.timezone, recipients(settings, db), with_purpose=include_purpose(db))
    active = db.active()
    have = {r["fingerprint"] for r in active}
    want = {d.fingerprint for d in desired}
    create = [d for d in desired if d.fingerprint not in have]
    cancel = [r for r in active if r["fingerprint"] not in want]
    keep = [r for r in active if r["fingerprint"] in want]
    digest = hashlib.sha256(
        json.dumps([sorted(d.fingerprint for d in create), sorted(r["id"] for r in cancel)]).encode()
    ).hexdigest()[:24]
    return {"create": create, "cancel": cancel, "keep": keep, "hash": digest}


def plan_to_json(plan):
    def c(d):
        return {"kind": d.kind, "when": d.when, "recipient": d.recipient, "names": d.names, "message": d.message,
                "fingerprint": d.fingerprint}

    def x(r):
        return {"kind": r["kind"], "when": r["cron"] or r["fire_at"], "recipient": r["recipient"],
                "names": r["names"].split(", "), "message": r["message"]}

    return {"hash": plan["hash"], "create": [c(d) for d in plan["create"]],
            "cancel": [x(r) for r in plan["cancel"]], "keep": len(plan["keep"])}


def reminder_name(d):
    """A short readable name, e.g. 'Meds 08:00 - Valtrex, Ator' (copies are prefixed 'Copy')."""
    if d.kind == "cron":
        minute, hour = d.cron.split()[:2]
        when = f"{int(hour):02d}:{int(minute):02d}"
    else:
        when = d.fire_at[5:]  # 'MM-DD HH:MM'
    text = f"{'Copy ' if d.recipient == 'me' else ''}Meds {when} - {', '.join(d.names)}"
    return text if len(text) <= 80 else text[:77] + "..."


def once_cron(fire_at):
    """'2026-10-29 08:00' -> '0 8 29 10 *': fires on that day and time, then again a year later unless cancelled."""
    when = datetime.strptime(fire_at, "%Y-%m-%d %H:%M")
    return f"{when.minute} {when.hour} {when.day} {when.month} *"


def _create(client, settings, db, d):
    name = reminder_name(d)
    if d.kind == "cron":
        rid = client.create_reminder(d.phone, d.message, d.cron, settings.timezone, name)
        db.add_reminder(d, rid)
        return rid
    # A single date. Prefer Wbiztool's scheduled message; if it rejects the date, use a date-specific
    # recurring reminder instead (that endpoint is known to work) and remember to do so from now on.
    if db.get_setting("wbiz.schedule_mode", "auto") != "cron":
        try:
            rid = client.schedule_message(d.phone, d.message, d.fire_at, settings.timezone, name)
            db.add_reminder(d, rid)
            return rid
        except WbizError as exc:
            if "format" not in str(exc).lower():
                raise
            db.set_setting("wbiz.schedule_mode", "cron")
            db.log_event("create", True, "Scheduled messages rejected the date; one-off dates now use date-specific reminders")
    cron = once_cron(d.fire_at)
    rid = client.create_reminder(d.phone, d.message, cron, settings.timezone, name)
    db.add_reminder(d, rid, kind="cron", cron=cron, once=True)
    return rid


def retire_fired_once_reminders(client, db, now):
    """A date-specific reminder repeats every year, so cancel it once its date has passed."""
    stamp = now.strftime("%Y-%m-%d %H:%M")
    retired = []
    for r in db.active():
        if r["once"] and r["fire_at"] and r["fire_at"] < stamp:
            try:
                if r["remote_id"]:
                    client.cancel_reminder(r["remote_id"])
                db.end_reminder(r["id"], "done")
                db.log_event("cancel", True, f"cancelled finished one-off reminder {r['fire_at']}")
                retired.append(r["fire_at"])
            except WbizError as exc:  # try again on the next pass
                db.log_event("cancel", False, f"finished one-off {r['fire_at']}: {exc}")
    return retired


def apply_plan(client, settings, db, medicines, approved_hash):
    """Cancel first, then create (a failure in between never leaves duplicates)."""
    with _lock:
        plan = compute_plan(settings, db, medicines)
        if plan["hash"] != approved_hash:
            raise WbizError("The plan changed since the preview — preview again and re-approve")
        client._require_config()
        client._require_verified()
        results = []
        for r in plan["cancel"]:
            label = f"cancel {r['recipient']} {r['cron'] or r['fire_at']}"
            try:
                if not r["remote_id"]:
                    raise WbizError("no remote id stored — cancel it manually in Wbiztool")
                (client.cancel_reminder if r["kind"] == "cron" else client.cancel_scheduled)(r["remote_id"])
                db.end_reminder(r["id"], "cancelled")
                results.append({"action": label, "ok": True})
                db.log_event("cancel", True, label)
            except WbizError as exc:
                results.append({"action": label, "ok": False, "error": str(exc)})
                db.log_event("cancel", False, f"{label}: {exc}")
        for d in plan["create"]:
            label = f"create {d.recipient} {d.when}"
            try:
                rid = _create(client, settings, db, d)
                results.append({"action": label, "ok": True, "warning": "" if rid else "no id returned — can't be cancelled by the app"})
                db.log_event("create", True, label)
            except WbizError as exc:
                results.append({"action": label, "ok": False, "error": str(exc)})
                db.log_event("create", False, f"{label}: {exc}")
        return results


def rollover(client, settings, db, medicines):
    """Schedule the next one-off message for series that were already approved.

    Only creates; never cancels; only for a group_key that was created before.
    """
    if not (settings.wbiz_configured and settings.docs_verified):
        return []
    with _lock:
        retire_fired_once_reminders(client, db, local_now(settings))
        plan = compute_plan(settings, db, medicines)
        out = []
        for d in plan["create"]:
            if d.kind == "oneoff" and db.has_history(d.group_key):
                label = f"rollover {d.recipient} {d.when}"
                try:
                    _create(client, settings, db, d)
                    db.log_event("rollover", True, label)
                    out.append(label)
                except WbizError as exc:
                    db.log_event("rollover", False, f"{label}: {exc}")
        return out
