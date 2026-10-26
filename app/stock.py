"""Medicine stock: counts, automatic use from the schedule, forecasts, alerts and reports.

Stock on hand = the sum of dated moves (start count, bought, used, corrections) + automatic moves.
Automatic moves are written once per finished day, from the schedule at that time, so changing a
schedule later never rewrites the past. Today's already-passed doses are subtracted on the fly.
"""
import math
import re
from datetime import date, datetime, timedelta

from .schedule import occurs_on

SCHEDULED = ("daily", "weekly", "every_n")
KINDS = {"start": "Starting count", "buy": "Bought / added", "used": "Used (not scheduled)",
         "adjust": "Correction", "auto": "Scheduled use"}
MAX_BACKFILL = 400


def has_schedule(m):
    return m is not None and m.kind in SCHEDULED and bool(m.times)


def units_on(m, item, day):
    """Units used on a day: scheduled doses x units per dose, else the manual daily use, else 0."""
    if has_schedule(m):
        return len(m.times) * item["units_per_dose"] if occurs_on(m, day) else 0
    return item.get("daily_use") or 0


def units_left_today(m, item, now):
    """Scheduled units still to be taken later today (doses whose time has not passed)."""
    if has_schedule(m):
        if not occurs_on(m, now.date()):
            return 0
        return sum(1 for t in m.times if t > now.strftime("%H:%M")) * item["units_per_dose"]
    return item.get("daily_use") or 0


def units_so_far_today(m, item, now):
    if has_schedule(m):
        if not occurs_on(m, now.date()):
            return 0
        return sum(1 for t in m.times if t <= now.strftime("%H:%M")) * item["units_per_dose"]
    return 0  # a manual daily use is only counted at the end of the day


def materialize(db, medicines_by_id, today):
    """Write the scheduled use of every finished day since the last count. Idempotent."""
    wrote = 0
    for mid, item in db.stock_items().items():
        if not item["tracking"] or not item["through_day"]:
            continue
        m = medicines_by_id.get(mid)
        last = date.fromisoformat(item["through_day"])
        yesterday = today - timedelta(days=1)
        if last >= yesterday:
            continue
        day = max(last + timedelta(days=1), yesterday - timedelta(days=MAX_BACKFILL))
        while day <= yesterday:
            used = units_on(m, item, day)
            if used:
                db.add_move(mid, day.isoformat(), "auto", -used, "")
                wrote += 1
            day += timedelta(days=1)
        db.update_stock_item(mid, through_day=yesterday.isoformat())
    return wrote


def on_hand(db, mid, m, item, now):
    total = db.stock_sum(mid)
    if item["through_day"] and item["through_day"] < now.date().isoformat():
        total -= units_so_far_today(m, item, now)  # today's doses that have already passed
    return round(total, 2)


def average_daily(m, item, now, days=28):
    total = sum(units_on(m, item, now.date() + timedelta(days=i)) for i in range(days))
    return total / days


def run_out_day(stock, m, item, now):
    """The first day the stock is used up, simulating the schedule day by day (None = never / no use)."""
    for i in range(0, 400):
        day = now.date() + timedelta(days=i)
        used = units_left_today(m, item, now) if i == 0 else units_on(m, item, day)
        if used > 0:
            if stock - used <= 0.0001:
                return day
            stock -= used
    return None


def unit_word(form, custom=""):
    """What the stock is counted in. Tablets by default; only an injection or a drink gets its own word,
    and a medicine can always be given its own ('Counted in' on the Stock page)."""
    custom = (custom or "").strip().lower()
    if custom:
        return custom
    text = (form or "").lower()
    if "inject" in text:
        return "injections"
    if "drink" in text:
        return "drinks"
    return "tablets"


def plural(unit, qty):
    """'tablets' with a quantity of exactly 1 reads '1 tablet'."""
    if qty is not None and abs(qty - 1) < 0.0001 and unit.endswith("s") and len(unit) > 3:
        return unit[:-1]
    return unit


def overview(db, medicines_by_id, rows, now, low_days=10, order_days=30):
    """One dict per active medicine: count, daily use, days left, run-out date, status, order quantity."""
    items = db.stock_items()
    out = []
    today = now.date()
    for r in rows:
        mid = r["id"]
        m = medicines_by_id.get(mid)
        item = items.get(mid)
        base = {"id": mid, "name": r["name"], "form": r["form"], "unit": unit_word(r["form"], (items.get(mid) or {}).get("unit", "")), "tracked": False,
                "on_hand": None, "daily": None, "days_left": None, "run_out": None, "status": "none", "order_qty": 0,
                "units_per_dose": 1, "daily_use": None, "source": "none", "tracking": False}
        if item:
            base.update(units_per_dose=item["units_per_dose"], daily_use=item["daily_use"], tracking=bool(item["tracking"]))
        if not item or not item["tracking"] or not db.has_moves(mid):
            out.append(base)
            continue
        hand = max(0.0, on_hand(db, mid, m, item, now))  # shown as 0 when the estimate says it already ran out
        daily = average_daily(m, item, now)
        run = run_out_day(hand, m, item, now) if hand > 0 else today
        left = (run - today).days if run else None
        if hand <= 0:
            status = "out"
        elif left is not None and left <= low_days:
            status = "low"
        else:
            status = "ok"
        need = math.ceil(max(0.0, daily * order_days - hand)) if daily else 0
        base.update(tracked=True, on_hand=hand, daily=daily, days_left=left, run_out=run if daily or hand <= 0 else None,
                    status=status, order_qty=need,
                    source="schedule" if has_schedule(m) else ("manual" if item.get("daily_use") else "none"))
        out.append(base)
    return out


def usage_report(db, medicines_by_id, rows, start, end, now):
    """Per medicine over [start, end]: opening, bought, scheduled use, other use, corrections, closing, average/day."""
    moves = db.moves_between(start.isoformat(), end.isoformat())
    items = db.stock_items()
    today = now.date()
    out = []
    days = (end - start).days + 1
    for r in rows:
        mid = r["id"]
        item = items.get(mid)
        if not item or not db.has_moves(mid):
            continue
        m = medicines_by_id.get(mid)
        mine = [x for x in moves if x["medicine_id"] == mid]
        opening = db.stock_sum(mid, (start - timedelta(days=1)).isoformat())
        bought = sum(x["qty"] for x in mine if x["kind"] in ("buy", "start"))
        auto = -sum(x["qty"] for x in mine if x["kind"] == "auto")
        manual = -sum(x["qty"] for x in mine if x["kind"] == "used")
        adjust = sum(x["qty"] for x in mine if x["kind"] == "adjust")
        if start <= today <= end and item["through_day"] and item["through_day"] < today.isoformat():
            auto += units_so_far_today(m, item, now)  # today's doses so far are not written yet
        closing = opening + bought - auto - manual + adjust
        out.append({"id": mid, "name": r["name"], "unit": unit_word(r["form"], item.get("unit", "")), "opening": round(opening, 2),
                    "bought": round(bought, 2), "auto": round(auto, 2), "manual": round(manual, 2),
                    "adjust": round(adjust, 2), "closing": round(closing, 2),
                    "per_day": round((auto + manual) / days, 2) if days else 0})
    return out


def num(x):
    """30.0 -> '30', 1.5 -> '1.5'."""
    if x is None:
        return "—"
    return str(int(round(x))) if abs(x - round(x)) < 0.005 else f"{x:.1f}".rstrip("0").rstrip(".")


# ---------------- alerts ----------------
def alert_text(name, rows, low_days, order_days, today):
    """English + Arabic WhatsApp text for medicines that just became low or ran out."""
    low = [r for r in rows if r["status"] == "low"]
    out = [r for r in rows if r["status"] == "out"]
    en = [f"⚠️ Medicine stock — {name}"]
    ar = [f"⚠️ تنبيه المخزون — {name}"]
    if out:
        en.append("Out of stock:")
        ar.append("نفدت:")
        for r in out:
            en.append(f"• {r['name']}")
            ar.append(f"• {r['name']}")
    if low:
        en.append(f"Running low (under {low_days} days):")
        ar.append(f"على وشك النفاد (أقل من {low_days} يوم):")
        for r in low:
            when = r["run_out"].strftime("%d %b") if r["run_out"] else ""
            en.append(f"• {r['name']}: {num(r['on_hand'])} {plural(r['unit'], r['on_hand'])} left (~{r['days_left']} days{', runs out ' + when if when else ''})")
            ar.append(f"• {r['name']}: تبقّى {num(r['on_hand'])} (حوالي {r['days_left']} يوم"
                      + (f"، تنفد {r['run_out'].strftime('%d/%m')}" if r["run_out"] else "") + ")")
    need = [r for r in low + out if r["order_qty"]]
    if need:
        en.append(f"Suggested order for {order_days} days: " + ", ".join(f"{r['name']} {r['order_qty']}" for r in need))
        ar.append(f"الكمية المقترحة لـ {order_days} يوم: " + "، ".join(f"{r['name']} {r['order_qty']}" for r in need))
    return "\n".join(en) + "\n\n" + "\n".join(ar)


def check_alerts(client, settings, db, medicines_by_id, rows, now, send=True):
    """Message the stock-alert number about medicines that just became low/out. Returns (sent, new_rows).
    Each medicine is announced once per episode; it is announced again only after it recovers."""
    low_days = int(db.get_setting("stock.low_days", "10"))
    order_days = int(db.get_setting("stock.order_days", "30"))
    ov = overview(db, medicines_by_id, rows, now, low_days, order_days)
    items = db.stock_items()
    new = [r for r in ov if r["status"] in ("low", "out") and (items.get(r["id"]) or {}).get("alert_state") != r["status"]]
    for r in ov:  # recovered: allow a future alert
        if r["status"] == "ok" and (items.get(r["id"]) or {}).get("alert_state"):
            db.update_stock_item(r["id"], alert_state="")
    if not new or not send or not settings.stock_number:
        return False, new
    if not (settings.wbiz_configured and settings.docs_verified):
        return False, new
    last = db.get_setting("stock.last_alert", "")
    if last and (now - datetime.fromisoformat(last)).total_seconds() < 3600:
        return False, new  # at most one alert message per hour
    from .wbiztool import WbizError
    name = db.profile().get("name") or "Medicines"
    try:
        client.send_message(settings.stock_number, alert_text(name, new, low_days, order_days, now.date()))
    except WbizError as exc:
        db.log_event("stock", False, f"Low-stock alert not sent: {exc}")
        return False, new
    for r in new:
        db.update_stock_item(r["id"], alert_state=r["status"])
    db.set_setting("stock.last_alert", now.isoformat(timespec="seconds"))
    db.log_event("stock", True, "Low-stock alert sent: " + ", ".join(r["name"] for r in new))
    return True, new


# ---------------- starting counts ----------------
def apply_counts(db, name_to_id, counts):
    """counts: [(medicine name, quantity text, 'YYYY-MM-DD' count date)] from the Excel quantity column.
    Only medicines without any stock record yet are set; nothing already counted is overwritten."""
    from .excel import parse_quantity

    applied, skipped, unmatched = [], [], []
    for name, text, day in counts:
        qty, note = parse_quantity(text)
        if qty is None:
            continue
        mid = name_to_id.get((name or "").strip().lower())
        if mid is None:
            unmatched.append(name)
            continue
        if db.has_moves(mid):
            skipped.append(name)
            continue
        day = day or date.today().isoformat()
        db.ensure_stock_item(mid, day)
        db.add_move(mid, day, "start", qty, f"Count from Excel ({note})" if note else "Count from Excel")
        applied.append(name)
    return {"applied": applied, "skipped": skipped, "unmatched": unmatched}


def suggest_units(when_en):
    """'5 tablets daily' -> 5, '1-2 tablets' -> 1 (the lower figure), no number -> 1. A suggestion only."""
    m = re.search(r"(\d+)(?:\s*[–-]\s*\d+)?\s*(?:tablets?|tabs?|capsules?|pills?|drinks?|ampoules?|sachets?|injections?)",
                  (when_en or "").lower())
    return max(1, int(m.group(1))) if m else 1
