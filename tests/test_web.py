from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_core import FakeClient


def make(settings):
    return TestClient(create_app(settings, FakeClient(settings)), base_url="http://testserver")


def login(c, settings):
    r = c.post("/login", data={"password": settings.dashboard_password}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/medicines").text
    return {"X-CSRF-Token": page.split('name="csrf" content="')[1].split('"')[0]}


def test_auth_required(settings):
    c = make(settings)
    for path in ("/", "/medicines", "/mom", "/reminders"):
        assert c.get(path, follow_redirects=False).status_code == 303
    assert c.get("/api/status").status_code == 401
    assert c.post("/login", data={"password": "nope"}).status_code == 401


def test_lockout(settings):
    c = make(settings)
    for _ in range(5):
        c.post("/login", data={"password": "x"})
    assert c.post("/login", data={"password": settings.dashboard_password}).status_code == 429


def test_import_and_pages(settings, xlsx):
    c = make(settings)
    h = login(c, settings)
    assert c.post("/api/medicines-import", files={"file": ("m.xlsx", xlsx.read_bytes())}).status_code == 403  # no CSRF
    r = c.post("/api/medicines-import", files={"file": ("m.xlsx", xlsx.read_bytes())}, headers=h)
    assert r.json() == {"added": 8, "skipped": 0}
    again = c.post("/api/medicines-import", files={"file": ("m.xlsx", xlsx.read_bytes())}, headers=h)
    assert again.json() == {"added": 0, "skipped": 8}  # no duplicates
    today = c.get("/").text
    assert "MedA" in today and "need" in today and "SECRETKEY123" not in today
    meds = c.get("/medicines").text
    assert "Needs review" in meds and "Every Fri at 09:00" in meds
    bad = c.post("/api/medicines-import", files={"file": ("m.xlsx", b"not excel")}, headers=h)
    assert bad.status_code == 400
    r = c.post("/api/sync/preview", headers=h)
    assert r.status_code == 200 and r.json()["create"]
    assert c.post("/api/sync/apply", json={"hash": "wrong"}, headers=h).status_code == 409


def test_medicine_crud_and_validation(settings):
    c = make(settings)
    h = login(c, settings)
    ok = {"name": "Aspirin <b>", "dose": "100mg", "frequency": "daily", "times": ["08:00"], "instruction": "بعد الأكل"}
    r = c.post("/api/medicines", json=ok, headers=h)
    mid = r.json()["id"]
    assert "<b>" not in c.get("/medicines").text  # escaped
    form = c.get(f"/api/medicines/{mid}").json()
    assert form["frequency"] == "daily" and form["times"] == ["08:00"]
    for bad in ({**ok, "name": ""}, {**ok, "times": []}, {**ok, "times": ["25:00"]}, {**ok, "frequency": "weekly"},
                {**ok, "frequency": "every_n", "every_n": 28, "date": "nope"}, {**ok, "frequency": "x"}):
        assert c.post("/api/medicines", json=bad, headers=h).status_code == 400
    weekly = {**ok, "name": "W", "frequency": "weekly", "weekdays": [4], "times": ["09:00", "21:00"]}
    wid = c.post("/api/medicines", json=weekly, headers=h).json()["id"]
    assert c.get(f"/api/medicines/{wid}").json()["weekdays"] == [4]
    shot = {**ok, "name": "Shot", "frequency": "every_n", "every_n": 28, "date": "2026-10-20", "times": ["10:00"]}
    assert c.post("/api/medicines", json=shot, headers=h).status_code == 200
    assert "Shot" in c.get("/").text
    # stop -> excluded from reminders; resume; edit; delete
    c.post(f"/api/medicines/{mid}/active", json={"active": False}, headers=h)
    plan = c.post("/api/sync/preview", headers=h).json()
    assert all("Aspirin" not in " ".join(x["names"]) for x in plan["create"])
    c.post(f"/api/medicines/{mid}/active", json={"active": True}, headers=h)
    assert c.put(f"/api/medicines/{mid}", json={**ok, "dose": "200mg"}, headers=h).status_code == 200
    assert c.delete(f"/api/medicines/{mid}", headers=h).status_code == 200
    assert c.get(f"/api/medicines/{mid}").status_code == 404


def test_mom_profile_and_status(settings):
    c = make(settings)
    h = login(c, settings)
    c.put("/api/profile", json={"name": "Amina", "allergies": "Penicillin", "evil": "x"}, headers=h)
    assert c.post("/api/status-log", json={"kind": "Blood pressure", "value": "130/80"}, headers=h).status_code == 200
    assert c.post("/api/status-log", json={"kind": "Nope", "value": "1"}, headers=h).status_code == 400
    assert c.post("/api/status-log", json={"kind": "Note"}, headers=h).status_code == 400
    page = c.get("/").text
    assert "Amina" in page and "Penicillin" in page and "130/80" in page
    mom = c.get("/mom").text
    assert "130/80" in mom
    sid = mom.split('data-del-status="')[1].split('"')[0]
    assert c.delete(f"/api/status-log/{sid}", headers=h).status_code == 200


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


def chart_sheet(path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Patient — Medicines"])
    ws.append(["Updated 14 Oct · Prescriber: Dr X"])
    ws.append([])
    ws.append(["Qty", "Medication", "Dose", "When to take", "For", "Notes", "بالعربي"])
    ws.append([30, "Alpha", "160 mg", "5 tablets daily, without stopping", "Appetite", "Confirm dose with clinic", "٥ حبات يومياً"])
    ws.append([1, "Beta", None, "3 times daily", "Nutrition", "Between meals", "ثلاث مرات في اليوم"])
    ws.append([None, "REGULAR"])
    ws.append([None, "Gamma", "30 mg", "Every 28 days", "Symptom control", "Essential; do not skip", "إبرة كل ٢٨ يوم"])
    ws.append([None, "Delta", "500 mg", "Twice daily (morning & evening)", "Antiviral", None, "حبة صباحاً ومساءً"])
    ws.append([None, "Epsilon", "10,000 IU", "Once weekly, lifelong", "Vitamin", None, "مرة في الأسبوع"])
    ws.append([None, "Zeta", "10 mg", "One every morning", None, None, "حبة كل صباح"])
    ws.append([None, "AS NEEDED"])
    ws.append([None, "Eta", "2 mg", "Up to twice daily", "Nausea", None, "حتى مرتين في اليوم عند الحاجة"])
    ws.append([None, "Theta", "—", "2 tablets every 8 hours", "Pain", "Not with other paracetamol", "حبتين كل ٨ ساعات"])
    ws.append([None, "Note: Some other tablets are not on this list. Confirm with the clinic."])
    wb.save(path)


def test_import_chart_layout_and_chart_pages(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    p = tmp_path / "chart.xlsx"
    chart_sheet(p)
    r = c.post("/api/medicines-import", files={"file": ("c.xlsx", p.read_bytes())}, headers=h)
    assert r.json()["added"] == 8  # title rows, section rows and the footnote are not medicines
    meds = c.get("/medicines").text
    for reason in ("missing time", "needs a day of the week", "needs a start date"):
        assert reason in meds  # nothing gets a reminder until times/days/dates are filled in
    assert meds.count('class="card med-card') == 8 and "Note: Some other" not in meds
    plan = c.post("/api/sync/preview", headers=h).json()
    assert plan["create"] == []  # all imported rows still need review -> no reminders
    # as-needed section rows are recognised and need no times
    en = c.get("/chart/en").text
    assert "As needed" in en and "Regular" in en and "Twice daily (morning &amp; evening)" in en
    ar = c.get("/chart/ar").text
    assert 'dir="rtl"' in ar and "حبة صباحاً ومساءً" in ar and "الأدوية" in ar and "عند الحاجة" in ar
    assert c.get("/chart/fr").status_code == 404
    # fix one row: give Delta times -> becomes a daily reminder; chart-only keeps the rest off the reminders
    import re
    mid = int(re.search(r'id="med-(\d+)">\s*<div class="med-head">\s*<strong dir="auto">Delta', meds).group(1))
    form = c.get(f"/api/medicines/{mid}").json()
    assert form["frequency"] == "daily" and form["times"] == ["08:00"]
    for bad in ({**ok, "name": ""}, {**ok, "times": []}, {**ok, "times": ["25:00"]}, {**ok, "frequency": "weekly"},
                {**ok, "frequency": "every_n", "every_n": 28, "date": "nope"}, {**ok, "frequency": "x"}):
        assert c.post("/api/medicines", json=bad, headers=h).status_code == 400
    weekly = {**ok, "name": "W", "frequency": "weekly", "weekdays": [4], "times": ["09:00", "21:00"]}
    wid = c.post("/api/medicines", json=weekly, headers=h).json()["id"]
    assert c.get(f"/api/medicines/{wid}").json()["weekdays"] == [4]
    shot = {**ok, "name": "Shot", "frequency": "every_n", "every_n": 28, "date": "2026-10-20", "times": ["10:00"]}
    assert c.post("/api/medicines", json=shot, headers=h).status_code == 200
    assert "Shot" in c.get("/").text
    # stop -> excluded from reminders; resume; edit; delete
    c.post(f"/api/medicines/{mid}/active", json={"active": False}, headers=h)
    plan = c.post("/api/sync/preview", headers=h).json()
    assert all("Aspirin" not in " ".join(x["names"]) for x in plan["create"])
    c.post(f"/api/medicines/{mid}/active", json={"active": True}, headers=h)
    assert c.put(f"/api/medicines/{mid}", json={**ok, "dose": "200mg"}, headers=h).status_code == 200
    assert c.delete(f"/api/medicines/{mid}", headers=h).status_code == 200
    assert c.get(f"/api/medicines/{mid}").status_code == 404


def test_mom_profile_and_status(settings):
    c = make(settings)
    h = login(c, settings)
    c.put("/api/profile", json={"name": "Amina", "allergies": "Penicillin", "evil": "x"}, headers=h)
    assert c.post("/api/status-log", json={"kind": "Blood pressure", "value": "130/80"}, headers=h).status_code == 200
    assert c.post("/api/status-log", json={"kind": "Nope", "value": "1"}, headers=h).status_code == 400
    assert c.post("/api/status-log", json={"kind": "Note"}, headers=h).status_code == 400
    page = c.get("/").text
    assert "Amina" in page and "Penicillin" in page and "130/80" in page
    mom = c.get("/mom").text
    assert "130/80" in mom
    sid = mom.split('data-del-status="')[1].split('"')[0]
    assert c.delete(f"/api/status-log/{sid}", headers=h).status_code == 200


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


def chart_sheet(path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Patient — Medicines"])
    ws.append(["Updated 14 Oct · Prescriber: Dr X"])
    ws.append([])
    ws.append(["Qty", "Medication", "Dose", "When to take", "For", "Notes", "بالعربي"])
    ws.append([30, "Alpha", "160 mg", "5 tablets daily, without stopping", "Appetite", "Confirm dose with clinic", "٥ حبات يومياً"])
    ws.append([1, "Beta", None, "3 times daily", "Nutrition", "Between meals", "ثلاث مرات في اليوم"])
    ws.append([None, "REGULAR"])
    ws.append([None, "Gamma", "30 mg", "Every 28 days", "Symptom control", "Essential; do not skip", "إبرة كل ٢٨ يوم"])
    ws.append([None, "Delta", "500 mg", "Twice daily (morning & evening)", "Antiviral", None, "حبة صباحاً ومساءً"])
    ws.append([None, "Epsilon", "10,000 IU", "Once weekly, lifelong", "Vitamin", None, "مرة في الأسبوع"])
    ws.append([None, "Zeta", "10 mg", "One every morning", None, None, "حبة كل صباح"])
    ws.append([None, "AS NEEDED"])
    ws.append([None, "Eta", "2 mg", "Up to twice daily", "Nausea", None, "حتى مرتين في اليوم عند الحاجة"])
    ws.append([None, "Theta", "—", "2 tablets every 8 hours", "Pain", "Not with other paracetamol", "حبتين كل ٨ ساعات"])
    ws.append([None, "Note: Some other tablets are not on this list. Confirm with the clinic."])
    wb.save(path)


def test_import_chart_layout_and_chart_pages(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    p = tmp_path / "chart.xlsx"
    chart_sheet(p)
    r = c.post("/api/medicines-import", files={"file": ("c.xlsx", p.read_bytes())}, headers=h)
    assert r.json()["added"] == 8  # title rows, section rows and the footnote are not medicines
    meds = c.get("/medicines").text
    for reason in ("missing time", "needs a day of the week", "needs a start date"):
        assert reason in meds  # nothing gets a reminder until times/days/dates are filled in
    assert meds.count('class="card med-card') == 8 and "Note: Some other" not in meds
    plan = c.post("/api/sync/preview", headers=h).json()
    assert plan["create"] == []  # all imported rows still need review -> no reminders
    # as-needed section rows are recognised and need no times
    en = c.get("/chart/en").text
    assert "As needed" in en and "Regular" in en and "Twice daily (morning &amp; evening)" in en
    ar = c.get("/chart/ar").text
    assert 'dir="rtl"' in ar and "حبة صباحاً ومساءً" in ar and "الأدوية" in ar and "عند الحاجة" in ar
    assert c.get("/chart/fr").status_code == 404
    # fix one row: give Delta times -> becomes a daily reminder; chart-only keeps the rest off the reminders
    mid = next(int(x.split('"')[0]) for x in meds.split('data-edit="')[1:] if "Delta" in meds.split(f'data-edit="{x.split(chr(34))[0]}"')[0][-600:])
    form = c.get(f"/api/medicines/{mid}").json()
    assert form["frequency"] == "daily" and form["when_en"].startswith("Twice daily")
    form["times"] = ["08:00", "20:00"]
    assert c.put(f"/api/medicines/{mid}", json=form, headers=h).status_code == 200
    plan = c.post("/api/sync/preview", headers=h).json()
    assert len(plan["create"]) == 4 and "Delta" in plan["create"][0]["message"] + plan["create"][1]["message"]
    chart_only = {"name": "Iota", "frequency": "chart_only", "when_en": "5 tablets daily", "instruction": "٥ حبات يومياً"}
    assert c.post("/api/medicines", json=chart_only, headers=h).status_code == 200
    assert "Iota" in c.get("/chart/en").text and "Iota" not in str(c.post("/api/sync/preview", headers=h).json())
    assert "Needs review" not in c.get("/medicines").text.split("Iota")[1].split("</section>")[0]


def test_db_migration_adds_new_columns(tmp_path):
    import sqlite3
    from app.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE medicines (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', dose TEXT NOT NULL DEFAULT '', times TEXT NOT NULL DEFAULT '', frequency TEXT NOT NULL DEFAULT '', day TEXT NOT NULL DEFAULT '', start_date TEXT NOT NULL DEFAULT '', last_date TEXT NOT NULL DEFAULT '', instruction TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
    con.execute("INSERT INTO medicines(name,created_at) VALUES('Old','x')")
    con.commit(); con.close()
    db = DB(path)
    assert db.list_medicines()[0]["when_en"] == "" and db.add_medicine({"name": "New", "notes_ar": "ملاحظة"})
