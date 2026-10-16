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
