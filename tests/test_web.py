from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_core import FakeClient


def make(settings, xlsx):
    app = create_app(settings, FakeClient(settings))
    return TestClient(app, base_url="http://testserver")


def login(c, settings):
    r = c.post("/login", data={"password": settings.dashboard_password}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/").text
    return page.split('name="csrf" content="')[1].split('"')[0], page


def test_auth_required(settings, xlsx):
    c = make(settings, xlsx)
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/api/status").status_code == 401
    assert c.post("/login", data={"password": "nope"}).status_code == 401


def test_dashboard_and_csrf(settings, xlsx):
    c = make(settings, xlsx)
    token, page = login(c, settings)
    assert "MedA" in page and "Needs review (3)" in page and "As needed" in page
    assert "SECRETKEY123" not in page
    assert c.post("/api/sync/preview").status_code == 403  # no CSRF header
    r = c.post("/api/sync/preview", headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and r.json()["create"]
    r2 = c.post("/api/sync/apply", json={"hash": "wrong"}, headers={"X-CSRF-Token": token})
    assert r2.status_code == 409


def test_lockout(settings, xlsx):
    c = make(settings, xlsx)
    for _ in range(5):
        c.post("/login", data={"password": "x"})
    assert c.post("/login", data={"password": settings.dashboard_password}).status_code == 429
