import hmac
from contextlib import asynccontextmanager
import logging
import secrets
import threading
import time as _time
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .db import DB
from .excel import load_medicines
from .schedule import next_injections, today_agenda
from .sync import apply_plan, compute_plan, copy_enabled, local_now, plan_to_json, rollover
from .wbiztool import WbizClient, WbizError

HERE = Path(__file__).parent
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class _Redact(logging.Filter):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def filter(self, record):
        record.msg = self.settings.redact(record.getMessage())
        record.args = ()
        return True


class NeedLogin(Exception):
    pass


def create_app(settings=None, client=None):
    settings = settings or Settings.from_env()
    settings.validate()
    db = DB(settings.db_path)
    client = client or WbizClient(settings)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_Redact(settings))
    logging.getLogger("wbiztool").addFilter(_Redact(settings))

    app = FastAPI(title="Medicine reminders", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.cookie_secure,
                       same_site="lax", max_age=7 * 24 * 3600, session_cookie="mr_session")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    cache = {"xl": None, "xl_key": None, "status": (0, None)}
    failures = {}  # ip -> [count, first_ts]

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(NeedLogin)
    async def need_login(request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Not logged in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    def auth(request: Request):
        if not request.session.get("ok"):
            raise NeedLogin()

    def csrf(request: Request):
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, request.session.get("csrf", "-")):
            raise HTTPException(403, "Bad CSRF token")

    def medicines():
        path = settings.excel_path
        key = (path.stat().st_mtime if path.exists() else None)
        if cache["xl"] is None or cache["xl_key"] != key:
            cache["xl"], cache["xl_key"] = load_medicines(path), key
        return cache["xl"]

    # ---- login ----
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, password: str = Form("")):
        ip = request.client.host if request.client else "?"
        count, first = failures.get(ip, [0, _time.time()])
        if _time.time() - first > 300:
            count, first = 0, _time.time()
        if count >= 5:
            return templates.TemplateResponse(request, "login.html", {"error": "Too many attempts. Wait 5 minutes."}, status_code=429)
        if hmac.compare_digest(password.encode(), settings.dashboard_password.encode()):
            failures.pop(ip, None)
            request.session.clear()
            request.session.update(ok=True, csrf=secrets.token_urlsafe(24))
            return RedirectResponse("/", status_code=303)
        failures[ip] = [count + 1, first]
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)

    @app.post("/logout")
    def logout(request: Request, _=Depends(csrf)):
        request.session.clear()
        return JSONResponse({"ok": True})

    # ---- dashboard ----
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def dashboard(request: Request):
        res = medicines()
        now = local_now(settings)
        agenda = today_agenda(res.medicines, now.date())
        as_needed = [m for m in res.medicines if m.kind == "as_needed"]
        ctx = {
            "csrf": request.session.get("csrf", ""),
            "now": now, "today_label": now.strftime("%A, %d %B %Y"),
            "agenda": [(t, m, t < now.strftime("%H:%M")) for t, m in agenda],
            "injections": next_injections(res.medicines, now),
            "as_needed": as_needed, "review": res.review, "errors": res.errors,
            "total": len(res.medicines), "copy": copy_enabled(settings, db),
            "configured": settings.wbiz_configured, "verified": settings.docs_verified,
            "events": db.recent_events(8), "day_names": DAY_NAMES,
        }
        return templates.TemplateResponse(request, "dashboard.html", ctx)

    # ---- API ----
    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        ts, cached = cache["status"]
        if cached and _time.time() - ts < 30:
            return cached
        out = {"configured": settings.wbiz_configured, "me": None, "whatsapp": None, "error": None,
               "events": db.recent_events(8)}
        if settings.wbiz_configured:
            try:
                out["me"] = client.me()
                out["whatsapp"] = client.client_status()
            except WbizError as exc:
                out["error"] = str(exc)
        else:
            out["error"] = "Wbiztool credentials are not set in .env"
        cache["status"] = (_time.time(), out)
        return out

    @app.post("/api/sync/preview", dependencies=[Depends(auth), Depends(csrf)])
    def preview():
        res = medicines()
        if res.errors:
            raise HTTPException(400, "; ".join(res.errors))
        if not settings.mom_number:
            raise HTTPException(400, "MOM_NUMBER is not set in .env")
        if copy_enabled(settings, db) and not settings.my_number:
            raise HTTPException(400, "MY_NUMBER is not set in .env (or untick the copy option)")
        return plan_to_json(compute_plan(settings, db, res.medicines))

    @app.post("/api/sync/apply", dependencies=[Depends(auth), Depends(csrf)])
    async def apply(request: Request):
        body = await request.json()
        res = medicines()
        try:
            results = apply_plan(client, settings, db, res.medicines, str(body.get("hash", "")))
        except WbizError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        cache["status"] = (0, None)
        return {"results": results}

    @app.post("/api/test-message", dependencies=[Depends(auth), Depends(csrf)])
    def test_message():
        if not settings.my_number:
            return JSONResponse({"error": "MY_NUMBER is not set in .env"}, status_code=400)
        text = "اختبار: تذكير الدواء يعمل ✅\n" + datetime.now().strftime("%H:%M")
        try:
            client.send_message(settings.my_number, text)
        except WbizError as exc:
            db.log_event("test", False, str(exc))
            return JSONResponse({"error": str(exc)}, status_code=502)
        db.log_event("test", True, "test message sent to my number")
        cache["status"] = (0, None)
        return {"ok": True}

    @app.post("/api/settings", dependencies=[Depends(auth), Depends(csrf)])
    async def set_settings(request: Request):
        body = await request.json()
        db.set_setting("copy_to_me", "1" if body.get("copy_to_me") else "0")
        return {"ok": True}

    # ---- background roll-over of one-off messages (e.g. every 28 days) ----
    def loop():
        while True:
            _time.sleep(1800)
            try:
                res = medicines()
                if not res.errors:
                    rollover(client, settings, db, res.medicines)
            except Exception as exc:  # keep the thread alive
                logging.getLogger("rollover").error("rollover failed: %s", settings.redact(type(exc).__name__))

    @asynccontextmanager
    async def lifespan(_app):
        threading.Thread(target=loop, daemon=True).start()
        yield

    app.router.lifespan_context = lifespan
    return app
