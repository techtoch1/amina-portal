import re

from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_core import FakeClient


def make(settings):
    return TestClient(create_app(settings, FakeClient(settings)), base_url="http://testserver")


def login(c, settings, password=None):
    r = c.post("/login", data={"password": password or settings.dashboard_password}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/medicines" if password is None else "/family").text
    return {"X-CSRF-Token": page.split('name="csrf" content="')[1].split('"')[0]}


def sheet(path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["", "Patient — Medicines"])
    ws.append(["", "Updated 14 Oct · Prescriber: Dr X"])
    ws.append(["Qty 15/10/2026", "Medication", "Dose", "Form", "When to take", "For", "Notes", "بالعربي"])
    ws.append([30, "Alpha", "160 mg", "Tablet", "5 tablets daily, without stopping", "Appetite", "Confirm dose with clinic", "٥ حبات يومياً"])
    ws.append([17, "Beta", "—", "Tablet", "1–2 tablets, 3 times daily when in pain", "Pain", "No other paracetamol", "حبة أو حبتين عند الوجع"])
    ws.append([None, "REGULAR"])
    ws.append([None, "Gamma", "30 mg", "Injection", "Every 28 days", "Control", "Essential; do not skip", "إبرة كل ٢٨ يوم"])
    ws.append([None, "Delta", "500 mg", "Tablet", "Twice daily (morning & evening)", "Antiviral", None, "حبة صباحاً ومساءً"])
    ws.append([None, "Epsilon", "10,000 IU", "Supplement", "Once weekly, lifelong", "Vitamin", None, "مرة في الأسبوع"])
    ws.append([None, "AS NEEDED"])
    ws.append([None, "Eta", "2 mg", "Tablet", "Up to twice daily", "Nausea", "الغثيان", "حتى مرتين في اليوم"])
    ws.append([None, "Theta", "—", "Tablet", "2 tablets every 8 hours", "Pain", None, "حبتين كل ٨ ساعات"])
    ws.append([None, "Iota", "1 mg", "", "1 tablet daily", None, "فقر الدم", "حبة كل يوم"])  # listed under AS NEEDED but says daily
    ws.append([None, "Note: Some other tablets are not on this list."])
    wb.save(path)


def imported(c, h, tmp_path, replace=False):
    p = tmp_path / "m.xlsx"
    sheet(p)
    return c.post(f"/api/medicines-import?replace={'true' if replace else 'false'}",
                  files={"file": ("m.xlsx", p.read_bytes())}, headers=h)


def med_id(c, name):
    html = c.get("/medicines").text
    return int(re.search(r'data-edit="(\d+)" dir="auto"><strong>' + re.escape(name) + "<", html).group(1))


def test_auth_required(settings):
    c = make(settings)
    for path in ("/", "/medicines", "/reminders", "/mom", "/settings", "/family", "/reminders/export.csv"):
        assert c.get(path, follow_redirects=False).status_code == 303, path
    assert c.get("/api/status").status_code == 401
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.get("/login").status_code == 200


def test_lockout(settings):
    c = make(settings)
    for _ in range(5):
        c.post("/login", data={"password": "x"})
    assert c.post("/login", data={"password": settings.dashboard_password}).status_code == 429


def test_navigation_and_pages(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    home = c.get("/", follow_redirects=False)
    assert home.headers["location"] == "/medicines"
    for path, needle in (("/medicines", "Medicines"), ("/reminders", "Reminders report"), ("/mom", "Log a reading"),
                         ("/settings", "Wbiztool connection")):
        r = c.get(path)
        assert r.status_code == 200 and needle in r.text
        assert 'href="/reminders"' in r.text and 'href="/mom"' in r.text and 'href="/settings"' in r.text  # nav everywhere


def test_import_and_medicines_page(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    p = tmp_path / "m.xlsx"
    sheet(p)
    assert c.post("/api/medicines-import", files={"file": ("m.xlsx", p.read_bytes())}).status_code == 403  # CSRF
    assert imported(c, h, tmp_path).json() == {"added": 8, "skipped": 0}
    assert imported(c, h, tmp_path).json() == {"added": 0, "skipped": 8}  # no duplicates
    page = c.get("/medicines").text
    assert page.count('role="switch"') == 8 and "Note: Some other" not in page
    assert 'class="switch on' not in page  # reminders are opt-in: nothing is switched on after an import
    reg, rest = page.split("As needed", 1)
    assert "Alpha" in reg and "Iota" in reg  # 'daily' in the text wins over the AS NEEDED heading
    assert "Eta" in rest and "Theta" in rest and "Beta" in rest  # 'when in pain' / 'up to' -> as needed
    assert "Tablet" in page and "Injection" in page
    ar = c.get("/medicines?lang=ar").text
    assert 'dir="rtl"' in ar and "حبة صباحاً ومساءً" in ar and "الأدوية" in ar and "عند الحاجة" in ar and "الغثيان" in ar
    assert imported(c, h, tmp_path, replace=True).json() == {"added": 8, "skipped": 0}
    assert c.get("/medicines").text.count('role="switch"') == 8


def test_suggestions_and_switch_flow(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    delta, eps, gamma, eta = (med_id(c, n) for n in ("Delta", "Epsilon", "Gamma", "Eta"))
    # nothing syncs until a reminder is on
    assert c.post("/api/sync/preview", headers=h).json()["create"] == []
    # "Twice daily (morning & evening)" -> one-click suggestion; weekly/every-N still need a day/date
    sug = c.get(f"/api/medicines/{delta}").json()["suggest"]
    assert sug == {"frequency": "daily", "times": ["08:00", "20:00"], "complete": True}
    assert c.get(f"/api/medicines/{eps}").json()["suggest"]["complete"] is False
    assert c.get(f"/api/medicines/{gamma}").json()["suggest"]["complete"] is False
    # custom default times change the suggestion
    assert c.put("/api/slots", json={"morning": "07:30", "evening": "21:00"}, headers=h).json()["morning"] == "07:30"
    assert c.get(f"/api/medicines/{delta}").json()["suggest"]["times"] == ["07:30", "21:00"]
    assert c.put("/api/slots", json={"morning": "7am"}, headers=h).status_code == 400
    # turning the switch on is what the page does with the suggestion
    r = c.post(f"/api/medicines/{delta}/reminder", json={"on": True, "frequency": "daily", "times": ["07:30", "21:00"]}, headers=h)
    assert r.status_code == 200
    assert c.post(f"/api/medicines/{eps}/reminder", json={"on": True, "times": ["09:00"]}, headers=h).status_code == 400  # needs a day
    assert c.post(f"/api/medicines/{eps}/reminder", json={"on": True, "times": ["09:00"], "weekdays": [4]}, headers=h).status_code == 200
    assert c.post(f"/api/medicines/{gamma}/reminder", json={"on": True, "times": ["10:00"], "every_n": 28,
                                                          "date": "2026-10-20"}, headers=h).status_code == 200
    assert c.post(f"/api/medicines/{eta}/reminder", json={"on": True, "times": ["08:00"]}, headers=h).status_code == 400  # as needed
    page = c.get("/medicines").text
    assert page.count('class="switch on') == 3 and "07:30 · 21:00" in page and "Fri 09:00" in page and "every 28 days" in page
    assert "not sent yet" in page and 'href="/reminders?review=1"' in page
    plan = c.post("/api/sync/preview", headers=h).json()
    assert len(plan["create"]) == 8 and "Eta" not in "".join(x["message"] for x in plan["create"])
    assert c.post("/api/sync/apply", json={"hash": "stale"}, headers=h).status_code == 409
    res = c.post("/api/sync/apply", json={"hash": plan["hash"]}, headers=h).json()["results"]
    assert len(res) == 8 and all(x["ok"] for x in res)
    assert "not sent yet" not in c.get("/medicines").text
    c.post(f"/api/medicines/{delta}/reminder", json={"on": False}, headers=h)
    plan = c.post("/api/sync/preview", headers=h).json()
    assert len(plan["cancel"]) == 4 and plan["create"] == []


def test_reminders_report_and_csv(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    delta = med_id(c, "Delta")
    c.post(f"/api/medicines/{delta}/reminder", json={"on": True, "frequency": "daily", "times": ["08:00"]}, headers=h)
    page = c.get("/reminders").text
    assert "Waiting to apply" in page and "Review &amp; apply" in page and "Nothing is scheduled yet" in page
    plan = c.post("/api/sync/preview", headers=h).json()
    c.post("/api/sync/apply", json={"hash": plan["hash"]}, headers=h)
    page = c.get("/reminders").text
    assert "Every day at 08:00" in page and "Mom" in page and "Copy to me" in page and "Delta" in page
    assert re.search(r'<span class="num">2</span><span class="lbl">Active reminders', page)
    csv_text = c.get("/reminders/export.csv").text
    assert csv_text.splitlines()[0].startswith("Recipient,Medicines,Schedule") and "Every day at 08:00" in csv_text
    assert "create" in c.get("/reminders").text  # activity log
    assert c.get("/reminders?show=failed").status_code == 200
    # spreadsheet formula injection is neutralised
    c.post("/api/medicines", json={"name": "=HYPERLINK(1)", "section": "regular", "when_en": "daily"}, headers=h)
    mid = med_id(c, "=HYPERLINK(1)")
    c.post(f"/api/medicines/{mid}/reminder", json={"on": True, "frequency": "daily", "times": ["09:00"]}, headers=h)
    plan = c.post("/api/sync/preview", headers=h).json()
    c.post("/api/sync/apply", json={"hash": plan["hash"]}, headers=h)
    assert "'=HYPERLINK" in c.get("/reminders/export.csv").text and ',=HYPERLINK' not in c.get("/reminders/export.csv").text


def test_family_read_only_access(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    c.put("/api/profile", json={"name": "Amina", "allergies": "Penicillin", "doctor": "Dr X", "doctor_phone": "+961 9 830 600"}, headers=h)
    delta = med_id(c, "Delta")
    c.post(f"/api/medicines/{delta}/reminder", json={"on": True, "frequency": "daily", "times": ["08:00"]}, headers=h)
    # setting the password: validation
    assert c.put("/api/family-access", json={"password": "short"}, headers=h).status_code == 400
    assert c.put("/api/family-access", json={"password": settings.dashboard_password}, headers=h).status_code == 400
    assert c.put("/api/family-access", json={"password": "family-pass-123"}, headers=h).json() == {"enabled": True}
    from app.db import DB
    stored = DB(settings.db_path).get_setting("family.hash")
    assert stored.startswith("scrypt$") and "family-pass-123" not in stored  # hashed, never plain
    f = TestClient(c.app, base_url="http://testserver")
    assert f.post("/login", data={"password": "wrong-password"}).status_code == 401
    fh = login(f, settings, "family-pass-123")
    assert f.get("/", follow_redirects=False).headers["location"] == "/family"
    fam = f.get("/family").text
    assert "Delta" in fam and "Penicillin" in fam and "tel:+9619830600" in fam and "08:00" in fam
    assert "data-toggle" not in fam and "/settings" not in fam and "/reminders" not in fam and "SECRET" not in fam
    assert "حبة صباحاً ومساءً" in f.get("/family?lang=ar").text
    # everything else is owner-only
    for path in ("/medicines", "/reminders", "/mom", "/settings", "/reminders/export.csv"):
        r = f.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/family" or r.status_code == 403, path
    assert f.get("/api/status").status_code == 403
    assert f.put("/api/profile", json={"name": "x"}, headers=fh).status_code == 403
    assert f.post("/api/medicines", json={"name": "x"}, headers=fh).status_code == 403
    assert f.put("/api/family-access", json={"password": "another-pass-1"}, headers=fh).status_code == 403
    # the owner's password still gives the owner role
    o = TestClient(c.app, base_url="http://testserver")
    assert login(o, settings) and o.get("/settings").status_code == 200
    # turn it off -> family can no longer sign in
    assert c.delete("/api/family-access", headers=h).json() == {"enabled": False}
    assert TestClient(c.app, base_url="http://testserver").post("/login", data={"password": "family-pass-123"}).status_code == 401


def test_edit_details_and_sections(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    delta = med_id(c, "Delta")
    c.post(f"/api/medicines/{delta}/reminder", json={"on": True, "times": ["08:00"]}, headers=h)
    assert c.put(f"/api/medicines/{delta}", json={"dose": "250 mg", "notes_ar": "ملاحظة"}, headers=h).status_code == 200
    assert "250 mg" in c.get("/medicines").text and c.get(f"/api/medicines/{delta}").json()["remind"] is True
    assert c.put(f"/api/medicines/{delta}", json={"name": ""}, headers=h).status_code == 400
    c.put(f"/api/medicines/{delta}", json={"section": "as_needed"}, headers=h)
    page = c.get("/medicines").text
    assert page.index("Delta") > page.index("As needed") and c.get(f"/api/medicines/{delta}").json()["remind"] is False
    new = c.post("/api/medicines", json={"name": "Aspirin <b>", "dose": "100mg", "when_en": "daily", "section": "regular"}, headers=h)
    assert "<b>" not in c.get("/medicines").text and c.get(f"/api/medicines/{new.json()['id']}").json()["frequency"] == "chart_only"
    assert c.post("/api/medicines", json={"name": ""}, headers=h).status_code == 400
    assert c.delete(f"/api/medicines/{new.json()['id']}", headers=h).status_code == 200
    assert c.get(f"/api/medicines/{new.json()['id']}").status_code == 404


def test_mom_page_profile_and_status(settings):
    c = make(settings)
    h = login(c, settings)
    c.put("/api/profile", json={"name": "Amina", "allergies": "Penicillin", "evil": "x"}, headers=h)
    assert c.post("/api/status-log", json={"kind": "Blood pressure", "value": "130/80"}, headers=h).status_code == 200
    assert c.post("/api/status-log", json={"kind": "Nope", "value": "1"}, headers=h).status_code == 400
    assert c.post("/api/status-log", json={"kind": "Note"}, headers=h).status_code == 400
    mom = c.get("/mom").text
    assert "130/80" in mom and "Latest readings" in mom and "Amina" in mom
    sid = mom.split('data-del-status="')[1].split('"')[0]
    assert c.delete(f"/api/status-log/{sid}", headers=h).status_code == 200
    c.post("/api/status-log", json={"kind": "Note", "note": "x"}, headers=h)
    assert c.delete("/api/status-log", headers=h).json()["deleted"] == 1


def test_settings_page(settings):
    settings.client_id = None
    settings.api_key = ""
    settings.whatsapp_client = ""
    settings.mom_number = ""
    settings.docs_verified = False
    c = make(settings)
    h = login(c, settings)
    r = c.put("/api/config", json={"client_id": "7", "api_key": "S3CRET-KEY_value", "whatsapp_client": "42",
                                   "mom_number": "+961 70 123456", "my_number": "96170999999", "docs_verified": True}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["api_key_set"] and body["api_key_length"] == 16 and body["mom_number"] == "96170123456"
    assert "S3CRET" not in r.text and "S3CRET" not in c.get("/settings").text and "S3CRET" not in c.get("/").text
    assert settings.client_id == 7 and settings.api_key == "S3CRET-KEY_value" and settings.docs_verified
    # blank key keeps the saved one
    c.put("/api/config", json={"api_key": "", "whatsapp_client": "43"}, headers=h)
    assert settings.api_key == "S3CRET-KEY_value" and settings.whatsapp_client == "43"
    for bad in ({"client_id": "abc"}, {"api_key": "has space"}, {"mom_number": "123"}, {"whatsapp_client": "a b"}):
        assert c.put("/api/config", json=bad, headers=h).status_code == 400
    assert c.put("/api/config", json={}).status_code == 403  # CSRF
    assert c.post("/api/config/test", headers=h).json()["ok"]
    # survives a restart: a fresh Settings object picks the values from the database
    from app.config import Settings
    from app.db import DB
    s2 = Settings(data_dir=settings.data_dir)
    s2.apply_overrides(DB(settings.db_path))
    assert s2.client_id == 7 and s2.api_key == "S3CRET-KEY_value"


def test_status_failure_does_not_hide_good_credentials(settings):
    from app.wbiztool import WbizError

    class NoStatus(FakeClient):
        def client_status(self):
            raise WbizError("Non-JSON response from /whatsapp_client/status/ (HTTP 404): Page not found")

    c = TestClient(create_app(settings, NoStatus(settings)), base_url="http://testserver")
    h = login(c, settings)
    r = c.post("/api/config/test", headers=h).json()
    assert r["ok"] and "404" in r["whatsapp_error"]
    st = c.get("/api/status").json()
    assert st["error"] is None and "404" in st["whatsapp_error"]


def test_migration_keeps_existing_reminders(tmp_path):
    import sqlite3
    from app.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE medicines (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', dose TEXT NOT NULL DEFAULT '', times TEXT NOT NULL DEFAULT '', frequency TEXT NOT NULL DEFAULT '', day TEXT NOT NULL DEFAULT '', start_date TEXT NOT NULL DEFAULT '', last_date TEXT NOT NULL DEFAULT '', instruction TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
    con.execute("INSERT INTO medicines(name,times,frequency,created_at) VALUES('WithTimes','08:00','daily','x')")
    con.execute("INSERT INTO medicines(name,created_at) VALUES('NoTimes','x')")
    con.commit(); con.close()
    db = DB(path)
    rows = {m["name"]: m for m in db.list_medicines()}
    assert rows["WithTimes"]["remind"] == 1 and rows["NoTimes"]["remind"] == 0 and rows["NoTimes"]["form"] == ""
    assert db.add_medicine({"name": "New", "notes_ar": "ملاحظة", "form": "Tablet"})


def test_status_failure_does_not_hide_good_credentials(settings):
    from app.wbiztool import WbizError

    class NoStatus(FakeClient):
        def client_status(self):
            raise WbizError("Non-JSON response from /whatsapp_client/status/ (HTTP 404): Page not found")

    c = TestClient(create_app(settings, NoStatus(settings)), base_url="http://testserver")
    h = login(c, settings)
    r = c.post("/api/config/test", headers=h).json()
    assert r["ok"] and "404" in r["whatsapp_error"]
    st = c.get("/api/status").json()
    assert st["error"] is None and "404" in st["whatsapp_error"]
