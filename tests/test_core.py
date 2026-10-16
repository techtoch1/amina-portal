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

    def _post(self, path, fields=None):
        self.n += 1
        self.calls.append((path, fields))
        return {"status": 1, "data": {"reminder_id": f"r{self.n}"}}


def test_sync_flow(xlsx, settings, tmp_path):
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
    sync.local_now = lambda s: NOW
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
    sync.local_now = lambda s: later
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
