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
