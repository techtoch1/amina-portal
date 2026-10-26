from datetime import date, datetime, timedelta

from app.db import DB
from app.excel import Medicine
from app.stock import (alert_text, apply_counts, check_alerts, materialize, on_hand, overview, run_out_day,
                       usage_report)
from app.wbiztool import WbizError

DAY0 = date(2026, 10, 15)             # the day the stock was counted
NOW = datetime(2026, 10, 26, 7, 0)    # a Monday morning, before the 08:00 dose


def med(mid, name, kind="daily", times=("08:00", "20:00"), **kw):
    return Medicine(row=mid, name=name, dose="", instruction="", purpose="", kind=kind, times=list(times), **kw)


def setup_db(tmp_path):
    db = DB(tmp_path / "s.db")
    rows = []
    for name, form in (("Valtrex", "Tablet"), ("Megace", "Tablet"), ("EuroD", "Supplement"), ("Shot", "Injection"),
                       ("Pain", "Tablet"), ("Manual", "Tablet")):
        mid = db.add_medicine({"name": name, "form": form})
        rows.append(db.get_medicine(mid))
    return db, rows


def start(db, mid, qty, day=DAY0, **item):
    db.ensure_stock_item(mid, day.isoformat())
    if item:
        db.update_stock_item(mid, **item)
    db.add_move(mid, day.isoformat(), "start", qty)


def test_scheduled_use_is_written_once_per_finished_day(tmp_path):
    db, rows = setup_db(tmp_path)
    valtrex = med(rows[0]["id"], "Valtrex")                       # 2 doses a day
    megace = med(rows[1]["id"], "Megace", times=("08:00",))       # 1 dose of 5 tablets
    by_id = {valtrex.row: valtrex, megace.row: megace}
    start(db, valtrex.row, 100)
    start(db, megace.row, 300, units_per_dose=5)
    assert materialize(db, by_id, NOW.date()) == 20               # 10 days (16..25 Oct) x 2 medicines
    assert db.stock_sum(valtrex.row) == 100 - 10 * 2 and db.stock_sum(megace.row) == 300 - 10 * 5
    assert materialize(db, by_id, NOW.date()) == 0                # idempotent: nothing written twice
    item = db.stock_item(valtrex.row)
    assert item["through_day"] == "2026-10-25"
    # on hand: before 08:00 nothing of today has been used; after 20:00 both doses have passed
    assert on_hand(db, valtrex.row, valtrex, item, NOW) == 80
    assert on_hand(db, valtrex.row, valtrex, item, datetime(2026, 10, 26, 9, 0)) == 79
    assert on_hand(db, valtrex.row, valtrex, item, datetime(2026, 10, 26, 21, 0)) == 78
    assert on_hand(db, megace.row, megace, db.stock_item(megace.row), datetime(2026, 10, 26, 9, 0)) == 300 - 50 - 5


def test_changing_the_schedule_never_rewrites_the_past(tmp_path):
    db, rows = setup_db(tmp_path)
    m = med(rows[0]["id"], "Valtrex")
    start(db, m.row, 100)
    materialize(db, {m.row: m}, date(2026, 10, 20))               # 15 Oct count; 16..19 Oct written at 2 a day
    assert db.stock_sum(m.row) == 92
    m.times = ["08:00", "14:00", "20:00"]                         # now three a day
    materialize(db, {m.row: m}, date(2026, 10, 22))               # 20 and 21 Oct at the new rate
    assert db.stock_sum(m.row) == 92 - 6
    assert [x["qty"] for x in db.moves(m.row) if x["day"] <= "2026-10-19" and x["kind"] == "auto"] == [-2] * 4


def test_weekly_every_n_manual_and_as_needed(tmp_path):
    db, rows = setup_db(tmp_path)
    weekly = med(rows[2]["id"], "EuroD", kind="weekly", times=("09:00",), weekdays=[4])        # Fridays
    shot = med(rows[3]["id"], "Shot", kind="every_n", times=("10:00",), every_n=28, anchor=date(2026, 10, 29))
    pain = Medicine(row=rows[4]["id"], name="Pain", dose="", instruction="", purpose="", kind="as_needed")
    manual = Medicine(row=rows[5]["id"], name="Manual", dose="", instruction="", purpose="", kind="chart_only")
    by_id = {weekly.row: weekly, shot.row: shot, pain.row: pain, manual.row: manual}
    for mid in by_id:
        start(db, mid, 30)
    db.update_stock_item(manual.row, daily_use=1.5)               # no schedule: a manual daily figure
    materialize(db, by_id, NOW.date())
    assert db.stock_sum(weekly.row) == 30 - 2                     # Fridays 16 and 23 Oct
    assert db.stock_sum(shot.row) == 30                           # next shot is 29 Oct: nothing used yet
    assert db.stock_sum(pain.row) == 30                           # as needed: only manual moves change it
    assert db.stock_sum(manual.row) == 30 - 10 * 1.5
    db.add_move(pain.row, "2026-10-20", "used", -2, "two tablets")
    assert db.stock_sum(pain.row) == 28


def test_forecast_status_and_order_quantity(tmp_path):
    db, rows = setup_db(tmp_path)
    m = med(rows[0]["id"], "Valtrex")
    start(db, m.row, 100)
    materialize(db, {m.row: m}, NOW.date())
    ov = overview(db, {m.row: m}, rows[:1], NOW, low_days=10, order_days=30)[0]
    assert ov["on_hand"] == 80 and ov["daily"] == 2 and ov["status"] == "ok"
    assert ov["run_out"] == NOW.date() + timedelta(days=39)       # 80 -> 78 after today's two doses, 2 a day after
    assert ov["days_left"] == 39 and ov["order_qty"] == 0         # 60 needed for 30 days, 80 on hand
    db.add_move(m.row, NOW.date().isoformat(), "adjust", -62, "counted 18")
    ov = overview(db, {m.row: m}, rows[:1], NOW, low_days=10, order_days=30)[0]
    assert ov["on_hand"] == 18 and ov["days_left"] == 8 and ov["status"] == "low"
    assert ov["order_qty"] == 42                                  # 60 for 30 days - 18 on hand
    db.add_move(m.row, NOW.date().isoformat(), "adjust", -18, "")
    gone = overview(db, {m.row: m}, rows[:1], NOW)[0]
    assert gone["status"] == "out" and gone["on_hand"] == 0 and gone["days_left"] == 0
    db.add_move(m.row, NOW.date().isoformat(), "adjust", -30, "")        # the estimate goes below zero...
    below = overview(db, {m.row: m}, rows[:1], NOW)[0]
    assert below["status"] == "out" and below["on_hand"] == 0          # ...but is never shown as a negative stock
    # medicines with no count are reported as 'none', not guessed
    other = overview(db, {}, rows[1:2], NOW)[0]
    assert other["status"] == "none" and other["on_hand"] is None
    assert run_out_day(5, med(1, "x", times=()), {"units_per_dose": 1, "daily_use": None}, NOW) is None


def test_usage_report(tmp_path):
    db, rows = setup_db(tmp_path)
    m = med(rows[0]["id"], "Valtrex")
    start(db, m.row, 100)
    materialize(db, {m.row: m}, NOW.date())
    db.add_move(m.row, "2026-10-20", "buy", 30, "pharmacy")
    db.add_move(m.row, "2026-10-21", "used", -1, "dropped one")
    db.add_move(m.row, "2026-10-22", "adjust", -2, "recount")
    rep = usage_report(db, {m.row: m}, rows[:1], date(2026, 10, 18), date(2026, 10, 25), NOW)[0]
    assert rep["opening"] == 100 - 2 * 2 and rep["bought"] == 30 and rep["auto"] == 16  # 96 before 18 Oct
    assert rep["manual"] == 1 and rep["adjust"] == -2 and rep["closing"] == 96 + 30 - 16 - 1 - 2
    assert rep["per_day"] == round(17 / 8, 2)
    # a range that includes today counts the doses already passed today
    rep = usage_report(db, {m.row: m}, rows[:1], date(2026, 10, 26), date(2026, 10, 26), datetime(2026, 10, 26, 21, 0))[0]
    assert rep["auto"] == 2 and rep["closing"] == rep["opening"] - 2


class Spy:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, phone, text):
        if self.fail:
            raise WbizError("Wbiztool did not answer")
        self.sent.append((phone, text))


class S:  # the bits of Settings that the alerts need
    stock_number = "96170000009"
    wbiz_configured = True
    docs_verified = True


def test_low_stock_alerts_once_per_episode(tmp_path):
    db, rows = setup_db(tmp_path)
    m = med(rows[0]["id"], "Valtrex")
    start(db, m.row, 40)
    materialize(db, {m.row: m}, NOW.date())                        # 40 - 20 = 20 on hand: 9 days left: low (< 10)
    spy = Spy()
    sent, new = check_alerts(spy, S, db, {m.row: m}, rows[:1], NOW)
    assert sent and [r["name"] for r in new] == ["Valtrex"] and spy.sent[0][0] == "96170000009"
    text = spy.sent[0][1]
    assert "Running low" in text and "Valtrex" in text and "تنبيه المخزون" in text and "على وشك النفاد" in text
    assert db.stock_item(m.row)["alert_state"] == "low"
    sent, new = check_alerts(spy, S, db, {m.row: m}, rows[:1], NOW + timedelta(hours=2))
    assert not sent and not new and len(spy.sent) == 1             # same episode: not repeated
    # stock is bought -> recovers -> the flag resets -> a later low alerts again
    db.add_move(m.row, NOW.date().isoformat(), "buy", 200)
    check_alerts(spy, S, db, {m.row: m}, rows[:1], NOW + timedelta(hours=3))
    assert db.stock_item(m.row)["alert_state"] == ""
    db.add_move(m.row, NOW.date().isoformat(), "adjust", -210)     # counted 10 left
    sent, _ = check_alerts(spy, S, db, {m.row: m}, rows[:1], NOW + timedelta(hours=5))
    assert sent and len(spy.sent) == 2
    db.add_move(m.row, NOW.date().isoformat(), "adjust", -100)     # now out of stock: escalates, but only after an hour
    assert db.stock_item(m.row)["alert_state"] == "low"
    sent, _ = check_alerts(spy, S, db, {m.row: m}, rows[:1], NOW + timedelta(hours=5, minutes=20))
    assert not sent and len(spy.sent) == 2                         # one message per hour at most
    sent, _ = check_alerts(spy, S, db, {m.row: m}, rows[:1], NOW + timedelta(hours=7))
    assert sent and "Out of stock" in spy.sent[-1][1] and db.stock_item(m.row)["alert_state"] == "out"


def test_alerts_are_safe_when_something_is_missing(tmp_path):
    db, rows = setup_db(tmp_path)
    m = med(rows[0]["id"], "Valtrex")
    start(db, m.row, 10)
    materialize(db, {m.row: m}, NOW.date())
    spy = Spy()

    class NoNumber(S):
        stock_number = ""

    class Unverified(S):
        docs_verified = False

    assert check_alerts(spy, NoNumber, db, {m.row: m}, rows[:1], NOW)[0] is False
    assert check_alerts(spy, Unverified, db, {m.row: m}, rows[:1], NOW)[0] is False
    assert not spy.sent and db.stock_item(m.row)["alert_state"] == ""   # nothing marked as announced
    down = Spy(fail=True)
    assert check_alerts(down, S, db, {m.row: m}, rows[:1], NOW)[0] is False
    assert db.stock_item(m.row)["alert_state"] == "" and db.recent_events(1)[0]["ok"] == 0  # retried next time
    assert check_alerts(spy, S, db, {m.row: m}, rows[:1], NOW)[0] is True


def test_apply_counts_from_excel_never_overwrites(tmp_path):
    db, rows = setup_db(tmp_path)
    ids = {r["name"].lower(): r["id"] for r in rows}
    res = apply_counts(db, ids, [("Valtrex", "12", "2026-10-15"), ("Megace", "30+", "2026-10-15"), ("EuroD", "", "2026-10-15"),
                                 ("Ghost", "5", "2026-10-15")])
    assert res == {"applied": ["Valtrex", "Megace"], "skipped": [], "unmatched": ["Ghost"]}
    assert db.stock_sum(ids["megace"]) == 30 and "30+" in db.moves(ids["megace"])[0]["note"]
    assert db.stock_item(ids["valtrex"])["through_day"] == "2026-10-15"
    db.add_move(ids["valtrex"], "2026-10-16", "buy", 10)
    again = apply_counts(db, ids, [("Valtrex", "99", "2026-10-15")])
    assert again["skipped"] == ["Valtrex"] and db.stock_sum(ids["valtrex"]) == 22  # never overwritten
    db.delete_medicine(ids["valtrex"])
    assert db.stock_item(ids["valtrex"]) is None and not db.has_moves(ids["valtrex"])  # stock goes with the medicine


def test_alert_text_is_bilingual_and_suggests_an_order():
    rows = [{"name": "Valtrex", "status": "low", "on_hand": 6, "unit": "tablets", "days_left": 3, "run_out": date(2026, 10, 29), "order_qty": 54},
            {"name": "Magnesium", "status": "out", "on_hand": 0, "unit": "supplements", "days_left": 0, "run_out": None, "order_qty": 30}]
    text = alert_text("Amina", rows, 10, 30, date(2026, 10, 26))
    assert "Out of stock:\n• Magnesium" in text and "6 tablets left (~3 days, runs out 29 Oct)" in text
    assert "Suggested order for 30 days: Valtrex 54, Magnesium 30" in text and "الكمية المقترحة" in text and "نفدت" in text


def test_stock_is_counted_in_tablets_by_default():
    from app.stock import plural, unit_word
    for form in ("Tablet", "Supplement", "", None, "Capsule", "Nutrition"):
        assert unit_word(form) == "tablets", form            # never "units" / "supplements"
    assert unit_word("Injection") == "injections" and unit_word("Nutrition drink") == "drinks"
    assert unit_word("Tablet", "Capsules ") == "capsules"     # a medicine can be given its own word
    assert plural("tablets", 1) == "tablet" and plural("tablets", 25) == "tablets" and plural("tablets", 0) == "tablets"
    assert plural("tablets", 1.5) == "tablets" and plural("ml", 1) == "ml"


def test_overview_and_report_use_the_custom_word_and_alerts_read_naturally(tmp_path):
    db, rows = setup_db(tmp_path)
    m = med(rows[2]["id"], "EuroD", kind="weekly", times=("09:00",), weekdays=[4])
    assert rows[2]["form"] == "Supplement"
    start(db, m.row, 30)
    assert overview(db, {m.row: m}, rows[2:3], NOW)[0]["unit"] == "tablets"      # a supplement is still counted in tablets
    db.update_stock_item(m.row, unit="sachets")
    assert overview(db, {m.row: m}, rows[2:3], NOW)[0]["unit"] == "sachets"
    assert usage_report(db, {m.row: m}, rows[2:3], date(2026, 10, 16), date(2026, 10, 25), NOW)[0]["unit"] == "sachets"
    one = [{"name": "Valtrex", "status": "low", "on_hand": 1, "unit": "tablets", "days_left": 0, "run_out": date(2026, 10, 26), "order_qty": 59}]
    assert "1 tablet left" in alert_text("Amina", one, 10, 30, date(2026, 10, 26))
