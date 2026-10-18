from datetime import date, datetime

from app.db import DB
from app.excel import form_to_rec, load_medicines, parse_times, rec_to_form
from app.schedule import build_desired, next_occurrence, today_agenda
from app.sync import apply_plan, compute_plan, rollover
from app.wbiztool import WbizClient, WbizError

NOW = datetime(2026, 10, 16, 7, 0)  # a Friday


def test_parse_excel(xlsx):
    r = load_medicines(xlsx)
    assert [m.name for m in r.medicines] == ["MedA", "MedB", "MedC", "Shot", "Pain"]
    assert [x.name for x in r.review] == ["Bad1", "Bad2", "Bad3"]
    assert "missing time" in r.review[0].reasons
    assert any("unclear" in x for x in r.review[1].reasons)
    assert any("start date" in x for x in r.review[2].reasons)


def test_missing_columns(tmp_path):
    from openpyxl import Workbook
    p = tmp_path / "x.xlsx"
    wb = Workbook(); wb.active.append(["foo"]); wb.save(p)
    assert load_medicines(p).errors


def test_times():
    assert parse_times("8am, 9:30 PM")[0] == ["08:00", "21:30"]
    assert parse_times("٠٨:٣٠")[0] == ["08:30"]
    assert parse_times("8")[1]  # bare number is ambiguous


def test_agenda_and_next(xlsx):
    meds = load_medicines(xlsx).medicines
    ag = today_agenda(meds, date(2026, 10, 16))
    assert [(t, m.name) for t, m in ag] == [("08:00", "MedA"), ("08:00", "MedB"), ("09:00", "MedC"), ("20:00", "MedB")]
    shot = next(m for m in meds if m.name == "Shot")
    assert next_occurrence(shot, NOW) == datetime(2026, 10, 20, 10, 0)
    assert next_occurrence(shot, datetime(2026, 10, 20, 10, 1)) == datetime(2026, 11, 17, 10, 0)


def test_grouping(xlsx):
    meds = load_medicines(xlsx).medicines
    d = build_desired(meds, NOW, "Asia/Beirut", [("mom", "1"), ("me", "2")])
    crons = {(x.cron, x.recipient): x for x in d if x.kind == "cron"}
    assert ("0 8 * * *", "mom") in crons and set(crons[("0 8 * * *", "mom")].names) == {"MedA", "MedB"}
    assert ("0 9 * * 5", "mom") in crons  # Friday -> cron 5
    assert len([x for x in d if x.kind == "oneoff"]) == 2
    assert "نسخة" in crons[("0 8 * * *", "me")].message
    assert not any("Pain" in x.names for x in d)


class FakeClient(WbizClient):
    def __init__(self, s):
        super().__init__(s)
        self.calls, self.n = [], 0

    def _post(self, path, fields=None, retries=0):
        self.n += 1
        self.calls.append((path, fields))
        return {"status": 1, "data": {"reminder_id": f"r{self.n}"}}


def test_sync_flow(xlsx, settings, tmp_path, monkeypatch):
    db = DB(tmp_path / "t.db")
    meds = load_medicines(xlsx).medicines
    c = FakeClient(settings)
    plan = compute_plan(settings, db, meds, NOW)
    assert len(plan["create"]) == 8 and not plan["cancel"]
    try:
        apply_plan(c, settings, db, meds, "stale")
        assert False
    except WbizError:
        pass
    assert not c.calls  # nothing sent without a matching approval
    # patch local_now so apply sees the same "now"
    import app.sync as sync
    monkeypatch.setattr(sync, "local_now", lambda s: NOW)
    res = apply_plan(c, settings, db, meds, plan["hash"])
    assert all(r["ok"] for r in res) and len(db.active()) == 8
    assert not compute_plan(settings, db, meds, NOW)["create"]
    # string-only fields
    assert all(isinstance(v, str) for _, f in c.calls for v in f.values())
    # change a medicine -> old reminder cancelled, new created
    meds[0].dose = "2 tablets"
    p2 = compute_plan(settings, db, meds, NOW)
    assert len(p2["cancel"]) == 2 and len(p2["create"]) == 2
    # after the injection passes, rollover schedules the next one
    later = datetime(2026, 10, 20, 11, 0)
    monkeypatch.setattr(sync, "local_now", lambda s: later)
    made = rollover(c, settings, db, meds)
    assert len(made) == 2 and any("2026-11-17" in m for m in made)


def test_unverified_blocks_mutations(settings):
    settings.docs_verified = False
    c = FakeClient(settings)
    for fn in (lambda: c.create_reminder("1", "m", "0 8 * * *", "Asia/Beirut"), lambda: c.send_message("1", "m")):
        try:
            fn(); assert False
        except WbizError:
            pass
    assert c.me()["status"] == 1  # read-only still works


class Resp:
    def __init__(self, text, code=200, js=None):
        self.text, self.status_code, self._js = text, code, js

    def json(self):
        if self._js is None:
            raise ValueError
        return self._js


class Sess:
    def __init__(self, resp):
        self.resp, self.body = resp, None

    def post(self, url, json=None, timeout=0):
        self.body = json
        return self.resp


def test_response_handling(settings):
    c = WbizClient(settings, Sess(Resp("<html><h1>500</h1></html>", 500)))
    try:
        c.me(); assert False
    except WbizError as e:
        assert "Non-JSON" in str(e)
    c = WbizClient(settings, Sess(Resp("", 200, {"status": 0, "message": "bad key SECRETKEY123"})))
    try:
        c.me(); assert False
    except WbizError as e:
        assert "SECRETKEY123" not in str(e) and "failed" in str(e)
    sess = Sess(Resp("", 200, {"status": 1}))
    WbizClient(settings, sess).me()
    assert isinstance(sess.body["client_id"], int) and isinstance(sess.body["api_key"], str)


def test_form_roundtrip():
    rec, err = form_to_rec({"name": "X", "frequency": "every_n", "every_n": 28, "date": "2026-10-20",
                            "date_kind": "last", "times": ["10:00"]})
    assert err is None and rec["last_date"] == "2026-10-20" and rec["start_date"] == ""
    back = rec_to_form(rec)
    assert back["frequency"] == "every_n" and back["every_n"] == 28 and back["date_kind"] == "last"


def test_reminders_are_sent_with_a_name(settings):
    from datetime import datetime
    from app.schedule import build_desired
    from app.sync import reminder_name
    meds = load_medicines  # noqa: F841 (keeps import used)

    class Spy(WbizClient):
        def __init__(self, s):
            super().__init__(s)
            self.sent = []

        def _post(self, path, fields=None, retries=0):
            self.sent.append((path, dict(fields)))
            return {"status": 1, "reminder_id": "r1"}

    spy = Spy(settings)
    spy.create_reminder("96170000001", "رسالة", "0 8 * * *", "Asia/Beirut", "Meds 08:00 - Valtrex, Ator")
    spy.schedule_message("96170000001", "رسالة", "2026-11-17 10:00", "Asia/Beirut", "Meds 11-17 10:00 - Shot")
    spy.send_message("96170000001", "test")
    for _path, f in spy.sent:
        assert all(isinstance(v, str) and v for v in f.values())  # strings only, never null/empty
    first = spy.sent[0][1]
    # every required field Wbiztool has complained about is present, under its confirmed name and the aliases
    assert first["name"] == first["reminder_name"] == "Meds 08:00 - Valtrex, Ator"
    assert first["message_template"] == first["template"] == first["msg"] == "رسالة"
    assert first["phone"] == first["phone_number"] == "96170000001" and first["cron"] == "0 8 * * *" and first["timezone"] == "Asia/Beirut"
    assert spy.sent[1][1]["schedule_time"] == "2026-11-17 10:00:00"  # first format tried and spy.sent[2][1]["msg"] == "test"
    spy.create_reminder("96170000001", "msg", "0 8 * * *", "Asia/Beirut")  # a default name is always supplied
    assert spy.sent[-1][1]["name"] == "Medicine reminder"

    from app.excel import Medicine
    m = Medicine(row=1, name="Valtrex", dose="", instruction="", purpose="", kind="daily", times=["08:00", "20:00"])
    d = build_desired([m], datetime(2026, 10, 16, 7, 0), "Asia/Beirut", [("mom", "1"), ("me", "2")])
    assert [reminder_name(x) for x in d] == ["Meds 08:00 - Valtrex", "Copy Meds 08:00 - Valtrex", "Meds 20:00 - Valtrex", "Copy Meds 20:00 - Valtrex"]
    long = Medicine(row=2, name="A" * 200, dose="", instruction="", purpose="", kind="daily", times=["08:00"])
    assert len(reminder_name(build_desired([long], datetime(2026, 10, 16, 7, 0), "Asia/Beirut", [("mom", "1")])[0])) <= 80


def test_message_includes_arabic_purpose_only():
    from app.excel import Medicine
    from app.messages import build_message
    m = Medicine(row=1, name="Valtrex", dose="500 mg", instruction="حبة صباحاً ومساءً", purpose="Antiviral",
                 kind="daily", times=["08:00"], purpose_ar="مضاد للفيروسات")
    text = build_message([m])
    assert "   حبة صباحاً ومساءً\n   الغرض: مضاد للفيروسات" in text
    assert "الغرض" not in build_message([m], with_purpose=False)
    m.purpose_ar = ""  # no Arabic -> no line, and never the English purpose
    assert "الغرض" not in build_message([m]) and "Antiviral" not in build_message([m])


def test_schedule_date_format_is_discovered_and_remembered(settings):
    class Fussy(WbizClient):
        def __init__(self, s):
            super().__init__(s)
            self.values = []
            self.bodies = []

        def _post(self, path, fields=None, retries=0):
            self.values.append(fields["schedule_time"])
            self.bodies.append(dict(fields))
            if fields["schedule_time"] != "29-10-2026 08:00":
                raise WbizError("/schedule_msg/ failed (status=0): Scheduled date & time is not in valid format")
            return {"status": 1, "msg_id": "m1"}

    saved = []
    c = Fussy(settings)
    c.remember = saved.append
    assert c.schedule_message("96170000001", "x", "2026-10-29 08:00", "Asia/Beirut", "n") == "m1"
    assert c.values[:3] == ["2026-10-29 08:00:00", "2026-10-29T08:00:00", "2026-10-29T08:00"] and c.values[-1] == "29-10-2026 08:00"
    assert saved == ["%d-%m-%Y %H:%M"] and settings.schedule_format == "%d-%m-%Y %H:%M"
    # an empty/unknown date field looks like "invalid format", so the date goes out under every likely field name
    body = c.bodies[-1]
    assert body["schedule_time"] == body["scheduled_time"] == body["scheduled_at"] == body["datetime"] == "29-10-2026 08:00"
    c.values.clear()
    c.schedule_message("96170000001", "x", "2026-10-29 08:00", "Asia/Beirut", "n")
    assert c.values == ["29-10-2026 08:00"]  # the remembered format goes first: one call, no retries
    assert saved == ["%d-%m-%Y %H:%M"]

    class Nope(WbizClient):
        calls = 0

        def _post(self, path, fields=None, retries=0):
            Nope.calls += 1
            raise WbizError("/schedule_msg/ failed (status=0): Phone cannot be null")

    n = Nope(settings)
    settings.schedule_format = ""
    try:
        n.schedule_message("1", "x", "2026-10-29 08:00", "Asia/Beirut")
        assert False
    except WbizError as e:
        assert "Phone cannot be null" in str(e)
    assert Nope.calls == 1  # unrelated errors are not retried with other date formats

    class Never(WbizClient):
        def _post(self, path, fields=None, retries=0):
            raise WbizError("Scheduled date & time is not in valid format")

    try:
        Never(settings).schedule_message("1", "x", "2026-10-29 08:00", "Asia/Beirut")
        assert False
    except WbizError as e:
        from app.wbiztool import SCHEDULE_FORMATS
        assert f"tried {len(SCHEDULE_FORMATS)} date formats" in str(e)
    from datetime import datetime as _dt
    assert WbizClient(settings)._format_schedule("2026-10-29 08:00", "iso_tz", "Asia/Beirut") == "2026-10-29T08:00:00+02:00"
    assert WbizClient(settings)._format_schedule("2026-10-29 08:00", "%Y-%m-%d %I:%M %p", "Asia/Beirut") == "2026-10-29 08:00 AM"
    assert WbizClient(settings)._format_schedule("2026-10-29 08:00", "epoch_ms", "Asia/Beirut").endswith("000")
    assert WbizClient(settings)._format_schedule("2026-10-29 08:00", "epoch", "Asia/Beirut") == str(
        int(_dt(2026, 10, 29, 6, 0, tzinfo=__import__("datetime").timezone.utc).timestamp()))  # Beirut is UTC+2 after DST ends (25 Oct)


def test_one_off_date_falls_back_to_a_date_specific_reminder(settings, tmp_path, monkeypatch):
    from datetime import date
    import app.sync as sync
    from app.excel import Medicine
    from app.sync import apply_plan, compute_plan, rollover

    class Rejecting(WbizClient):
        def __init__(self, s):
            super().__init__(s)
            self.log = []

        def _post(self, path, fields=None, retries=0):
            self.log.append((path, dict(fields or {})))
            if path == "/schedule_msg/":
                raise WbizError("/schedule_msg/ failed (status=0): Scheduled date & time is not in valid format")
            return {"status": 1, "reminder_id": f"r{len(self.log)}"}

    db = DB(tmp_path / "t.db")
    shot = Medicine(row=1, name="Shot", dose="30 mg", instruction="إبرة", purpose="", kind="every_n", times=["08:00"],
                    every_n=28, anchor=date(2026, 10, 29), remind=True)
    c = Rejecting(settings)
    monkeypatch.setattr(sync, "local_now", lambda s: NOW)
    plan = compute_plan(settings, db, [shot], NOW)
    assert [d.kind for d in plan["create"]] == ["oneoff", "oneoff"]  # Mom + the copy to me
    res = apply_plan(c, settings, db, [shot], plan["hash"])
    assert all(r["ok"] for r in res)
    rows = db.active()
    assert [(r["kind"], r["once"], r["cron"], r["fire_at"]) for r in rows] == [("cron", 1, "0 8 29 10 *", "2026-10-29 08:00")] * 2
    assert db.get_setting("wbiz.schedule_mode") == "cron"
    tried = [p for p, _ in c.log if p == "/schedule_msg/"]
    assert len(tried) == 13  # the date formats are tried once; the second message goes straight to the fallback
    created = [f for p, f in c.log if p == "/reminder/create/"]
    assert created[0]["cron"] == "0 8 29 10 *" and created[0]["name"].startswith("Meds 10-29 08:00")
    assert not compute_plan(settings, db, [shot], NOW)["create"]  # in sync: the stored reminder matches the plan

    # the date passes: the finished reminder is cancelled (it would repeat next year) and the next date is scheduled
    later = datetime(2026, 10, 29, 9, 0)
    monkeypatch.setattr(sync, "local_now", lambda s: later)
    made = rollover(c, settings, db, [shot])
    assert len(made) == 2 and all("2026-11-26" in m for m in made)
    cancelled = [f for p, f in c.log if p == "/reminder/cancel/"]
    assert len(cancelled) == 2
    assert [(r["cron"], r["fire_at"]) for r in db.active()] == [("0 8 26 11 *", "2026-11-26 08:00")] * 2
    # nothing left to cancel, and a failing cancel is retried instead of lost
    assert rollover(c, settings, db, [shot]) == []


def test_network_timeouts_stop_the_apply_and_never_hide_uncertainty(settings, tmp_path, monkeypatch):
    import requests
    import app.sync as sync
    from app.excel import Medicine
    from app.sync import apply_plan, compute_plan
    from app.wbiztool import WbizNetworkError

    class Dead(WbizClient):
        def __init__(self, s):
            super().__init__(s)
            self.calls = 0

        def _post(self, path, fields=None, retries=0):
            self.calls += 1
            raise WbizNetworkError("Wbiztool did not answer within 30 seconds (calling /reminder/create/).")

    db = DB(tmp_path / "t.db")
    meds = [Medicine(row=i, name=f"M{i}", dose="", instruction="", purpose="", kind="daily", times=[f"0{i}:00"], remind=True)
            for i in range(1, 4)]
    monkeypatch.setattr(sync, "local_now", lambda s: NOW)
    plan = compute_plan(settings, db, meds, NOW)
    assert len(plan["create"]) == 6  # 3 times x (Mom + copy)
    c = Dead(settings)
    res = apply_plan(c, settings, db, meds, plan["hash"])
    assert c.calls == 1  # it gave up after the first timeout instead of waiting for all six
    assert res[0]["ok"] is False and "may or may not have been done" in res[0]["error"]
    assert all("not attempted" in r["error"] for r in res[1:]) and len(res) == 6
    assert db.active() == []  # nothing recorded as created, so nothing is wrongly cancelled later
    # a normal rejection (not a network problem) still lets the other items go ahead
    class Picky(WbizClient):
        def _post(self, path, fields=None, retries=0):
            if fields["cron"].startswith("0 1 "):
                raise WbizError("/reminder/create/ failed (status=0): bad")
            return {"status": 1, "reminder_id": "r"}
    res = apply_plan(Picky(settings), settings, db, meds, compute_plan(settings, db, meds, NOW)["hash"])
    assert [r["ok"] for r in res].count(False) == 2 and [r["ok"] for r in res].count(True) == 4


def test_read_only_calls_are_retried_but_creates_are_not(settings):
    import requests

    class Flaky:
        def __init__(self, fail_times):
            self.fail, self.calls = fail_times, 0

        def post(self, url, json=None, timeout=None):
            self.calls += 1
            assert timeout == (10, 30)
            if self.calls <= self.fail:
                raise requests.exceptions.ReadTimeout("slow")
            return Resp("", 200, {"status": 1, "name": "ok"})

    import app.wbiztool as w
    w.time.sleep = lambda s: None
    once = Flaky(1)
    assert WbizClient(settings, once).me()["name"] == "ok" and once.calls == 2  # one retry rescued it
    twice = Flaky(5)
    try:
        WbizClient(settings, twice).me()
        assert False
    except w.WbizNetworkError as e:
        assert "did not answer within 30 seconds" in str(e) and e.network and twice.calls == 2
    posts = Flaky(5)
    try:
        WbizClient(settings, posts).create_reminder("1", "m", "0 8 * * *", "Asia/Beirut", "n")
        assert False
    except w.WbizNetworkError:
        assert posts.calls == 1  # creating is never retried automatically: it could duplicate the reminder
    refused = type("R", (), {"post": lambda self, *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("x"))})()
    try:
        WbizClient(settings, refused).client_status()
        assert False
    except w.WbizNetworkError as e:
        assert "could not be reached" in str(e)
