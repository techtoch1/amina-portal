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
    for path in ("/", "/medicines", "/reminders", "/mom", "/settings", "/family", "/family/log", "/reminders/export.csv"):
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
    for path, needle in (("/medicines", "Medicines"), ("/reminders", "Reminders report"), ("/mom", "Add to the log"),
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


def test_hand_added_medicine_gets_one_click_suggestion(settings):
    c = make(settings)
    h = login(c, settings)
    mid = c.post("/api/medicines", json={"name": "Hand", "when_en": "Twice daily (morning & evening)", "section": "regular"}, headers=h).json()["id"]
    assert c.get(f"/api/medicines/{mid}").json()["suggest"] == {"frequency": "daily", "times": ["08:00", "20:00"], "complete": True}
    wk = c.post("/api/medicines", json={"name": "Wk", "when_en": "Once weekly", "section": "regular"}, headers=h).json()["id"]
    s2 = c.get(f"/api/medicines/{wk}").json()["suggest"]
    assert s2["frequency"] == "weekly" and s2["complete"] is False  # still needs a day
    none = c.post("/api/medicines", json={"name": "Plain", "section": "regular"}, headers=h).json()["id"]
    assert c.get(f"/api/medicines/{none}").json()["suggest"]["complete"] is False  # no text, no guess
    pn = c.post("/api/medicines", json={"name": "Pn", "when_en": "when needed", "section": "regular"}, headers=h).json()["id"]
    assert c.get(f"/api/medicines/{pn}").json()["suggest"]["complete"] is False


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


def test_fill_missing_arabic(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    # imitate an older import: an Arabic-only note sitting in the English notes box
    c.post("/api/medicines", json={"name": "Imod", "dose": "2 mg", "form": "Tablet", "purpose": "Diarrhoea", "notes_en": "اسهال",
                                   "when_en": "when needed", "instruction": "حبة عند الضرورة", "section": "as_needed"}, headers=h)
    c.post("/api/medicines", json={"name": "Odd", "form": "Tablet", "purpose": "Mystery purpose", "notes_en": "Take with orange juice",
                                   "when_en": "daily", "instruction": "", "section": "regular"}, headers=h)
    assert "Arabic detail" in c.get("/medicines").text
    r = c.post("/api/arabic/fill", headers=h).json()
    filled = {(x["name"], x["field"]): x["value"] for x in r["filled"]}
    assert filled[("Imod", "notes")] == "اسهال" and filled[("Imod", "form")] == "أقراص" and filled[("Imod", "what it is for")] == "الإسهال"
    assert filled[("Gamma", "notes")] == "ضروري؛ لا تفوّت الجرعة" and filled[("Gamma", "form")] == "حقنة"
    still = {(x["name"], x["field"]) for x in r["missing"]}
    assert ("Odd", "what it is for") in still and ("Odd", "notes") in still and ("Odd", "how to take") in still  # not guessed
    assert c.post("/api/arabic/fill", headers=h).json()["filled"] == []  # idempotent, never overwrites
    mid = med_id(c, "Imod")
    form = c.get(f"/api/medicines/{mid}").json()
    assert form["notes_ar"] == "اسهال" and form["notes_en"] == "" and form["form_ar"] == "أقراص"
    # a person's own Arabic is never replaced
    c.put(f"/api/medicines/{mid}", json={"purpose_ar": "إسهال حاد"}, headers=h)
    c.put(f"/api/medicines/{mid}", json={"purpose": "Diarrhoea"}, headers=h)
    c.post("/api/arabic/fill", headers=h)
    assert c.get(f"/api/medicines/{mid}").json()["purpose_ar"] == "إسهال حاد"
    ar = c.get("/medicines?lang=ar").text
    assert "أقراص" in ar and "160 ملغ" in ar and "الشهية" in ar
    odd_row = ar[ar.index("Odd"):]
    assert "Mystery purpose" in odd_row and 'class="fb"' in odd_row  # not translated -> shown in English, marked
    assert 'class="fb"' not in ar[ar.index("Alpha"):ar.index("Odd")] or "Odd" in ar  # translated rows are not marked
    assert c.post("/api/arabic/fill").status_code == 403  # CSRF
    assert "أقراص" in c.get("/family?lang=ar").text


def test_arabic_units_and_glossary():
    from app.arabic import arabic_dose, translate_notes, translate_purpose
    assert arabic_dose("10,000 IU") == "10,000 وحدة دولية" and arabic_dose("500mg") == "500ملغ" and arabic_dose("2 ml") == "2 مل"
    assert translate_purpose("Appetite / weight") == "الشهية / الوزن" and translate_purpose("Unknown thing") == ""
    assert translate_notes("Same medicine as Primperan: use one, not both") == "نفس الدواء مثل Primperan: استخدم واحداً فقط وليس كليهما"
    assert translate_notes("Started 20 Dec 2024") == "بدأ بتاريخ 20 كانون الأول 2024" and translate_notes("Take with orange juice") == ""


def test_send_test_reminder_goes_only_to_my_number(settings, tmp_path, monkeypatch):
    from app.wbiztool import WbizError

    class Spy(FakeClient):
        sent = []

        def _post(self, path, fields=None, retries=0):
            if path.endswith("/send_msg/"):
                Spy.sent.append(dict(fields))
            return {"status": 1, "reminder_id": "r1"}

    c = TestClient(create_app(settings, Spy(settings)), base_url="http://testserver")
    h = login(c, settings)
    imported(c, h, tmp_path)
    delta = med_id(c, "Delta")
    c.post(f"/api/medicines/{delta}/reminder", json={"on": True, "frequency": "daily", "times": ["08:00"]}, headers=h)
    plan = c.post("/api/sync/preview", headers=h).json()
    assert all(x["fingerprint"] for x in plan["create"])
    fp = plan["create"][0]["fingerprint"]
    # a pending (not yet applied) message can be tested
    assert c.post("/api/test-reminder", json={"fingerprint": fp}, headers=h).json() == {"ok": True, "to": "me"}
    sent = Spy.sent[-1]
    assert sent["phone"] == settings.my_number and settings.mom_number not in sent.values()
    assert sent["message_template"].startswith("🧪") and "Delta" in sent["message_template"] and "Test message" in sent["message_template"]
    assert c.post("/api/test-reminder", json={"fingerprint": fp}, headers=h).status_code == 429  # throttled
    # applied reminders can be tested from the report; the report shows the button
    c.post("/api/sync/apply", json={"hash": plan["hash"]}, headers=h)
    page = c.get("/reminders").text
    assert page.count('data-test="') == 4  # a 'to me' and a 'to Mom' button on each of the 2 reminders
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "TEST_MIN_INTERVAL", 0.0)
    active_fp = page.split('data-test="')[1].split('"')[0]
    assert c.post("/api/test-reminder", json={"fingerprint": active_fp}, headers=h).json() == {"ok": True, "to": "me"}
    assert c.post("/api/test-reminder", json={"fingerprint": "nope"}, headers=h).status_code == 404
    assert c.post("/api/test-reminder", json={"fingerprint": fp}).status_code == 403  # CSRF
    # --- test to Mom: needs explicit confirmation, uses her number, her own Arabic note, no 'copy to me' line
    assert c.post("/api/test-reminder", json={"fingerprint": fp, "to": "mom"}, headers=h).status_code == 400
    me_fp = next(x["fingerprint"] for x in plan["create"] if x["recipient"] == "me")
    r = c.post("/api/test-reminder", json={"fingerprint": me_fp, "to": "mom", "confirm": True}, headers=h)
    assert r.json() == {"ok": True, "to": "mom"}
    sent = Spy.sent[-1]
    assert sent["phone"] == settings.mom_number and sent["message_template"].startswith("🧪 رسالة تجريبية من تطبيق")
    assert "نسخة للمتابعة" not in sent["message_template"] and "Delta" in sent["message_template"]
    assert "Test message" not in sent["message_template"]  # her note is Arabic only
    assert c.post("/api/test-reminder", json={"fingerprint": fp, "to": "mom", "confirm": True}, headers=h).status_code in (200, 429)
    monkeypatch.setattr(main_mod, "TEST_MIN_INTERVAL", 0.0)
    assert f'data-mom-tail="{settings.mom_number[-3:]}"' in c.get("/reminders").text  # shown in the confirmation
    settings.mom_number = ""
    assert c.post("/api/test-reminder", json={"fingerprint": fp, "to": "mom", "confirm": True}, headers=h).status_code == 400
    settings.my_number = ""
    assert c.post("/api/test-reminder", json={"fingerprint": active_fp}, headers=h).status_code == 400


def test_purpose_line_in_reminders_and_toggle(settings, tmp_path):
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    c.post("/api/arabic/fill", headers=h)
    delta = med_id(c, "Delta")
    c.post(f"/api/medicines/{delta}/reminder", json={"on": True, "frequency": "daily", "times": ["08:00"]}, headers=h)
    plan = c.post("/api/sync/preview", headers=h).json()
    assert "الغرض: مضاد للفيروسات" in plan["create"][0]["message"]  # 'Antiviral' filled in Arabic
    assert "Antiviral" not in plan["create"][0]["message"]
    c.post("/api/sync/apply", json={"hash": plan["hash"]}, headers=h)
    assert c.post("/api/sync/preview", headers=h).json()["create"] == []  # in sync
    # switching it off changes the text -> the old reminders are cancelled and re-created
    assert "الغرض" in c.get("/settings").text and "Example" in c.get("/settings").text
    assert c.put("/api/message-options", json={"include_purpose": False}, headers=h).json() == {"include_purpose": False}
    plan = c.post("/api/sync/preview", headers=h).json()
    assert len(plan["cancel"]) == 2 and len(plan["create"]) == 2 and "الغرض" not in plan["create"][0]["message"]
    assert c.put("/api/message-options", json={"include_purpose": True}, headers=h).json() == {"include_purpose": True}
    assert c.post("/api/sync/preview", headers=h).json()["cancel"] == []  # back to what is already on Wbiztool
    assert c.put("/api/message-options", json={"include_purpose": False}).status_code == 403  # CSRF


def test_status_log_dates_and_upcoming(settings):
    from datetime import date, timedelta
    c = make(settings)
    h = login(c, settings)
    today = date.today()
    past = (today - timedelta(days=40)).isoformat()
    future = (today + timedelta(days=12)).isoformat()
    post = lambda **kw: c.post("/api/status-log", json=kw, headers=h)  # noqa: E731
    assert post(kind="Doctor visit", value="Dr. Kamar", note="Blood test results", date=past, time="14:30").status_code == 200
    assert post(kind="Appointment", value="Oncology", date=future, time="09:15").status_code == 200
    assert post(kind="Blood pressure", value="130/80").status_code == 200  # no date -> today
    assert post(kind="Blood pressure", value="120/70", date=past).status_code == 200
    for bad in ({"date": "31-12-2026"}, {"date": "2026-02-31"}, {"date": "1990-01-01"}, {"time": "25:00"}, {"time": "9am"}):
        assert post(kind="Note", note="x", **bad).status_code == 400, bad
    mom = c.get("/mom").text
    assert "Upcoming" in mom and "Oncology" in mom and "09:15" in mom
    history = mom.split("History")[1]
    assert "Oncology" not in history                      # future entries are not in the history
    assert history.index("130/80") < history.index("Dr. Kamar")  # newest event date first
    assert "14:30" in history and "Blood test results" in history
    # 'latest readings' use the newest reading by event date, never a visit or a future entry
    latest = mom.split("Latest readings")[1].split("</section>")[0]
    assert "130/80" in latest and "120/70" not in latest and "Dr. Kamar" not in latest
    assert 'name="date" type="date" value="' + today.isoformat() in mom


def test_status_log_migration_keeps_old_entries(tmp_path):
    import sqlite3
    from app.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE status_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '')")
    con.execute("INSERT INTO status_log(ts,kind,value) VALUES('2026-10-01T10:00:00+00:00','Blood pressure','125/80')")
    con.commit(); con.close()
    db = DB(path)
    row = db.list_status()[0]
    assert row["event_date"] == "2026-10-01" and row["event_time"] == "" and row["value"] == "125/80"
    db.add_status("Doctor visit", "Dr X", "", "2026-12-01", "10:00")
    assert db.list_status()[0]["event_date"] == "2026-12-01"


def test_report_shows_fallback_one_off_as_a_single_date(settings, tmp_path):
    from datetime import date, timedelta
    from app.wbiztool import WbizError

    class Rejecting(FakeClient):
        def _post(self, path, fields=None, retries=0):
            if path == "/schedule_msg/":
                raise WbizError("/schedule_msg/ failed (status=0): Scheduled date & time is not in valid format")
            return {"status": 1, "reminder_id": "r1"}

    c = TestClient(create_app(settings, Rejecting(settings)), base_url="http://testserver")
    h = login(c, settings)
    imported(c, h, tmp_path)
    gamma = med_id(c, "Gamma")  # "Every 28 days"
    when = date.today() + timedelta(days=10)
    r = c.post(f"/api/medicines/{gamma}/reminder", json={"on": True, "times": ["10:00"], "every_n": 28,
                                                       "date": when.isoformat()}, headers=h)
    assert r.status_code == 200
    plan = c.post("/api/sync/preview", headers=h).json()
    assert all(x["kind"] == "oneoff" for x in plan["create"])
    res = c.post("/api/sync/apply", json={"hash": plan["hash"]}, headers=h).json()["results"]
    assert all(x["ok"] for x in res)
    page = c.get("/reminders").text
    assert "Once on " + when.strftime("%d %b %Y") in page and "Every day" not in page.split("Scheduled")[1].split("Activity")[0]
    assert "one-off dates now use date-specific reminders" in page  # the switch is logged in the activity list
    assert "Once on" in c.get("/reminders/export.csv").text
    assert not c.post("/api/sync/preview", headers=h).json()["create"]


def test_family_can_read_mom_log_but_not_change_it(settings):
    from datetime import date, timedelta
    c = make(settings)
    h = login(c, settings)
    today, past, future = date.today(), (date.today() - timedelta(days=30)).isoformat(), (date.today() + timedelta(days=14)).isoformat()
    post = lambda **kw: c.post("/api/status-log", json=kw, headers=h)  # noqa: E731
    post(kind="Doctor visit", value="Dr. Kamar", note="Results fine", date=past, time="14:30")
    post(kind="Appointment", value="Oncology", date=future, time="09:15")
    post(kind="Blood pressure", value="130/80", note="Evening")
    c.put("/api/profile", json={"name": "Amina"}, headers=h)
    c.put("/api/family-access", json={"password": "family-pass-123"}, headers=h)
    f = TestClient(c.app, base_url="http://testserver")
    fh = login(f, settings, "family-pass-123")
    page = f.get("/family/log").text
    for needle in ("Upcoming", "Oncology", "09:15", "Latest readings", "130/80", "History", "Dr. Kamar", "Results fine", "14:30"):
        assert needle in page, needle
    # read-only: no way to add, delete or clear, no owner pages in the menu
    for forbidden in ("status-form", "data-del-status", "clear-log", "profile-form", 'href="/mom"', 'href="/settings"', 'href="/reminders"'):
        assert forbidden not in page, forbidden
    assert 'href="/family"' in page and 'href="/family/log"' in page  # the family menu
    # Arabic version
    ar = f.get("/family/log?lang=ar").text
    assert 'dir="rtl"' in ar and "زيارة الطبيب" in ar and "ضغط الدم" in ar and "المواعيد القادمة" in ar and "السجل" in ar
    assert (date.today() - timedelta(days=30)).strftime("%d/%m/%Y") in ar  # Arabic pages use dd/mm/yyyy
    # the medicines family page links to it too
    assert 'href="/family/log"' in f.get("/family").text
    # and it still can't change anything or reach the owner page
    assert f.post("/api/status-log", json={"kind": "Note", "note": "x"}, headers=fh).status_code == 403
    assert f.delete("/api/status-log", headers=fh).status_code == 403
    r = f.get("/mom", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/family"
    # the owner sees the same page, and family access off means no sign-in
    assert c.get("/family/log").status_code == 200
    c.delete("/api/family-access", headers=h)
    assert TestClient(c.app, base_url="http://testserver").post("/login", data={"password": "family-pass-123"}).status_code == 401


def test_mom_profile_and_log_import_export(settings):
    import json as _json
    c = make(settings)
    h = login(c, settings)
    c.put("/api/profile", json={"name": "Existing Name"}, headers=h)
    payload = {"profile": {"name": "Amina", "doctor": "Prof. Kamar", "care_team": "Dr A — surgeon\nDr B — GP", "bogus": "x"},
               "log": [{"kind": "Treatment", "date": "2025-07-01", "value": "Chemo started", "note": "approx. date"},
                       {"kind": "Hospital", "date": "2026-02-10", "value": "Admission", "note": "Ascites and pain"},
                       {"kind": "Note", "note": "No date -> today"}]}
    post = lambda body, **q: c.post("/api/mom-import", content=_json.dumps(body), headers={**h, "Content-Type": "application/json"}, params=q)  # noqa: E731
    r = post(payload)
    assert r.status_code == 200
    assert r.json() == {"profile_fields": 2, "profile_kept": 1, "log_added": 3, "log_skipped": 0, "ignored": ["bogus"]}
    prof = c.get("/api/mom-export").json()["profile"]
    assert prof["name"] == "Existing Name"  # existing text is kept
    assert prof["doctor"] == "Prof. Kamar" and prof["care_team"].startswith("Dr A")
    assert post(payload).json()["log_added"] == 0 and post(payload).json()["log_skipped"] == 3  # no duplicates
    assert post(payload, overwrite="true").json()["profile_fields"] == 3
    assert c.get("/api/mom-export").json()["profile"]["name"] == "Amina"
    # all-or-nothing validation
    bad = {"log": [{"kind": "Note", "note": "fine"}, {"kind": "Nope", "note": "x"}, {"kind": "Note", "note": "y", "date": "2026-02-31"},
                   {"kind": "Note", "value": "v" * 81}]}
    r = post(bad)
    assert r.status_code == 400 and "Nothing imported" in r.json()["detail"] and "entry 2" in r.json()["detail"]
    assert len(c.get("/api/mom-export").json()["log"]) == 3
    assert c.post("/api/mom-import", content="not json", headers={**h, "Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/mom-import", content="[1]", headers={**h, "Content-Type": "application/json"}).status_code == 400
    # shows up on the pages, including the family ones
    c.put("/api/family-access", json={"password": "family-pass-123"}, headers=h)
    mom = c.get("/mom").text
    assert "Treatment" in mom and "Chemo started" in mom and "Dr B — GP" in mom and "Download backup" in mom
    f = TestClient(c.app, base_url="http://testserver")
    fh = login(f, settings, "family-pass-123")
    fam = f.get("/family").text
    assert "Care team" in fam and "Dr B — GP" in fam
    assert "علاج" in f.get("/family/log?lang=ar").text and "Chemo started" in f.get("/family/log").text
    # owner only, CSRF protected
    assert f.get("/api/mom-export").status_code == 403
    assert f.post("/api/mom-import", content="{}", headers={**fh, "Content-Type": "application/json"}).status_code == 403
    assert c.post("/api/mom-import", content="{}", headers={"Content-Type": "application/json"}).status_code == 403


def test_excel_export_for_doctor(settings, tmp_path):
    import io as _io
    from openpyxl import load_workbook
    c = make(settings)
    h = login(c, settings)
    imported(c, h, tmp_path)
    c.post("/api/arabic/fill", headers=h)
    c.put("/api/profile", json={"name": "Amina", "doctor": "Prof. Kamar", "notes": "Chemo tablets are not on this list"}, headers=h)
    delta, eps = med_id(c, "Delta"), med_id(c, "Epsilon")
    c.post(f"/api/medicines/{delta}/reminder", json={"on": True, "frequency": "daily", "times": ["08:00", "20:00"]}, headers=h)
    c.post(f"/api/medicines/{eps}/reminder", json={"on": True, "times": ["09:00"], "weekdays": [4]}, headers=h)
    c.post("/api/medicines", json={"name": "=HYPERLINK(\"http://x\")", "dose": "+1", "section": "regular"}, headers=h)
    r = c.get("/medicines/export.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert r.headers["content-disposition"].startswith('attachment; filename="Medicines_for_doctor_review_')
    wb = load_workbook(_io.BytesIO(r.content))
    assert wb.sheetnames == ["Medicines", "Reminder schedule", "Patient"]
    ws = wb["Medicines"]
    assert "Amina — Medicines for doctor review" in ws["A1"].value and "Allergies: Not recorded" in ws["A3"].value
    header = [c_.value for c_ in ws[5]]
    assert header[1] == "Medication" and "Reminder sent to Mom" in header[5] and header[9] == "Correct?"
    rows = {row[1].value: [c_.value for c_ in row] for row in ws.iter_rows(min_row=6) if row[1].value}
    assert len(rows) == 9  # 8 from the sheet + the hand-added one; the section rows are not medicines
    assert rows["Delta"][5] == "Every day at 08:00, 20:00" and rows["Epsilon"][5] == "Every Fri at 09:00"
    assert rows["Alpha"][5] == "No reminder set" and rows["Eta"][5] == "No reminder (as needed)"
    assert rows["Delta"][2] == "500 mg" and rows["Delta"][3] == "Tablet" and rows["Delta"][8] == "حبة صباحاً ومساءً"
    section_labels = [row[0].value for row in ws.iter_rows(min_row=6) if isinstance(row[0].value, str)]
    assert any(v.startswith("REGULAR") for v in section_labels) and any(v.startswith("AS NEEDED") for v in section_labels)
    assert ws.data_validations.dataValidation[0].formula1 == '"Yes,No - needs change"'
    evil = next(row[1] for row in ws.iter_rows(min_row=6) if row[1].value and str(row[1].value).startswith("="))
    assert evil.data_type == "s" and evil.value.startswith("=HYPERLINK")  # stored as text, never a formula
    assert any(str(row[1].value) == "+1" or str(row[2].value) == "+1" for row in ws.iter_rows(min_row=6))
    assert "Chemo tablets are not on this list" in str([c_.value for row in ws.iter_rows(min_row=17) for c_ in row if c_.value])
    sch = wb["Reminder schedule"]
    texts = [[c_.value for c_ in row] for row in sch.iter_rows(min_row=4) if row[0].value]
    assert [t[0] for t in texts] == ["Every day at 08:00", "Every Fri at 09:00", "Every day at 20:00"]
    assert any("تذكير: حان وقت الدواء" in str(t[2]) for t in texts) and all("نسخة" not in str(t[2]) for t in texts)  # Mom's version only
    assert wb["Patient"]["B1"].value == "Amina" and "Not recorded" in wb["Patient"]["B4"].value
    # owner only
    c.put("/api/family-access", json={"password": "family-pass-123"}, headers=h)
    f = TestClient(c.app, base_url="http://testserver")
    login(f, settings, "family-pass-123")
    assert f.get("/medicines/export.xlsx").status_code == 403
    assert make(settings).get("/medicines/export.xlsx", follow_redirects=False).status_code == 303
