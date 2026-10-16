import hmac
from contextlib import asynccontextmanager
import logging
import secrets
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .db import DB
from io import BytesIO
from zoneinfo import ZoneInfo

from .excel import classify_frequency, form_to_rec, load_from_db, norm, read_sheet, rec_to_form
from .schedule import describe, next_injections, today_agenda
from .sync import apply_plan, compute_plan, copy_enabled, local_now, plan_to_json, rollover
from .wbiztool import WbizClient, WbizError

HERE = Path(__file__).parent
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PROFILE_FIELDS = {"name": 120, "age": 40, "conditions": 1000, "allergies": 1000, "doctor": 120, "doctor_phone": 40,
                  "emergency_contact": 200, "pharmacy": 200, "notes": 2000}
STATUS_KINDS = ["Blood pressure", "Blood sugar", "Weight", "Temperature", "Mood / condition", "Doctor visit", "Note"]
MAX_UPLOAD = 2 * 1024 * 1024


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
    settings.apply_overrides(db)
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
        return load_from_db(db)[0]

    def local_str(iso):
        try:
            dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
            return dt.strftime("%d %b %H:%M")
        except ValueError:
            return iso

    templates.env.filters["local"] = local_str

    def page(request, name, **extra):
        res, stopped = load_from_db(db)
        now = local_now(settings)
        plan = compute_plan(settings, db, res.medicines, now)
        base = {"csrf": request.session.get("csrf", ""), "now": now, "active_page": name, "profile": db.profile(),
                "pending": len(plan["create"]) + len(plan["cancel"]), "review_count": len(res.review),
                "configured": settings.wbiz_configured, "verified": settings.docs_verified,
                "res": res, "stopped": stopped}
        base.update(extra)
        return templates.TemplateResponse(request, name + ".html", base)

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

    # ---- pages ----
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def today(request: Request):
        res = medicines()
        now = local_now(settings)
        agenda = today_agenda(res.medicines, now.date())
        latest = {}
        for row in db.list_status(60):
            latest.setdefault(row["kind"], row)
        return page(request, "today", today_label=now.strftime("%A, %d %B %Y"),
                    agenda=[(t, m, t < now.strftime("%H:%M")) for t, m in agenda],
                    injections=next_injections(res.medicines, now),
                    as_needed=[m for m in res.medicines if m.kind == "as_needed"], latest=latest)

    @app.get("/medicines", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def medicines_page(request: Request):
        res, stopped = load_from_db(db)
        by_id = {m.row: m for m in res.medicines}
        review = {r.row: r for r in res.review}
        rows = []
        for r in db.list_medicines():
            m, rv = by_id.get(r["id"]), review.get(r["id"])
            rows.append({"r": r, "desc": describe(m) if m else "", "problems": rv.reasons if rv else [],
                         "stopped": not r["active"]})
        return page(request, "medicines", rows=rows, day_names=DAY_NAMES)

    CHART_LABELS = {
        "en": {"title": "Medicines", "dir": "ltr", "updated": "Updated", "prescriber": "Prescriber", "allergies": "Allergies",
               "cols": ["Medication", "Dose", "When to take", "For", "Notes"], "regular": "Regular",
               "as_needed": "As needed", "next": "Next", "print": "Print / save as PDF", "other": "العربية",
               "empty": "No medicines yet."},
        "ar": {"title": "الأدوية", "dir": "rtl", "updated": "آخر تحديث", "prescriber": "الطبيب", "allergies": "الحساسية",
               "cols": ["الدواء", "الجرعة", "متى يؤخذ", "لماذا", "ملاحظات"], "regular": "أدوية منتظمة",
               "as_needed": "عند الحاجة", "next": "الموعد القادم", "print": "طباعة / حفظ PDF", "other": "English",
               "empty": "لا توجد أدوية بعد."},
    }

    @app.get("/chart", dependencies=[Depends(auth)])
    def chart_redirect():
        return RedirectResponse("/chart/en", status_code=303)

    @app.get("/chart/{lang}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def chart_page(request: Request, lang: str):
        if lang not in CHART_LABELS:
            raise HTTPException(404, "Unknown language")
        res, _stopped = load_from_db(db)
        parsed = {m.row: m for m in res.medicines}
        now = local_now(settings)
        nxt = dict((m.row, at) for m, at in next_injections(res.medicines, now))
        groups = {"regular": [], "as_needed": []}
        for r in sorted(db.list_medicines(), key=lambda x: x["id"]):  # same order as entered / imported
            if not r["active"]:
                continue
            kind, _n, _p = classify_frequency(norm(r["frequency"]))
            m = parsed.get(r["id"])
            desc = describe(m) if m else ""
            ar = lang == "ar"
            groups["as_needed" if kind == "as_needed" else "regular"].append({
                "name": r["name"], "dose": r["dose"],
                "when": (r["instruction"] if ar else r["when_en"]) or desc,
                "purpose": (r["purpose_ar"] or r["purpose"]) if ar else r["purpose"],
                "notes": (r["notes_ar"] or r["notes_en"]) if ar else r["notes_en"],
                "next": nxt.get(r["id"]),
            })
        return page(request, "chart", lang=lang, L=CHART_LABELS[lang], groups=groups,
                    updated=now.strftime("%d %b %Y"))

    @app.get("/mom", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def mom_page(request: Request):
        return page(request, "mom", kinds=STATUS_KINDS, log=db.list_status(60), fields=list(PROFILE_FIELDS))

    @app.get("/reminders", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def reminders_page(request: Request):
        return page(request, "reminders", copy=copy_enabled(settings, db), events=db.recent_events(10),
                    active_reminders=db.active())

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
            except WbizError as exc:
                out["error"] = str(exc)
            else:
                try:
                    out["whatsapp"] = client.client_status()
                except WbizError as exc:
                    out["whatsapp_error"] = str(exc)
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

    # ---- settings (Wbiztool credentials) ----
    def _config_view():
        return {"client_id": settings.client_id or "", "whatsapp_client": settings.whatsapp_client,
                "mom_number": settings.mom_number, "my_number": settings.my_number,
                "api_key_set": bool(settings.api_key), "api_key_length": len(settings.api_key),
                "docs_verified": settings.docs_verified}

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def settings_page(request: Request):
        return page(request, "settings", conf=_config_view())

    @app.put("/api/config", dependencies=[Depends(auth), Depends(csrf)])
    async def save_config(request: Request):
        body = await request.json()

        def text(key, limit):
            v = body.get(key)
            return None if v is None else str(v).strip()[:limit]

        cid = text("client_id", 12)
        if cid is not None and cid != "" and not cid.isdigit():
            raise HTTPException(400, "Client ID must be a number")
        wa = text("whatsapp_client", 64)
        if wa and any(ch.isspace() for ch in wa):
            raise HTTPException(400, "WhatsApp client must not contain spaces")
        numbers = {}
        for key, label in (("mom_number", "Mom's number"), ("my_number", "Your number")):
            v = text(key, 30)
            if v is None:
                continue
            digits = "".join(ch for ch in v if ch.isdigit())
            if digits and not 8 <= len(digits) <= 15:
                raise HTTPException(400, f"{label} should be 8-15 digits including the country code")
            numbers[key] = digits
        key_value = text("api_key", 300)
        if key_value and (any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in key_value)):
            raise HTTPException(400, "API key has spaces or unusual characters — copy it again exactly from Wbiztool")
        if cid is not None:
            db.set_setting("conf.client_id", cid)
        if wa is not None:
            db.set_setting("conf.whatsapp_client", wa)
        for k, v in numbers.items():
            db.set_setting("conf." + k, v)
        if key_value:  # blank = keep the saved key
            db.set_setting("conf.api_key", key_value)
        if "docs_verified" in body:
            db.set_setting("conf.docs_verified", "1" if body["docs_verified"] else "0")
        settings.apply_overrides(db)
        cache["status"] = (0, None)
        db.log_event("settings", True, "Wbiztool settings updated")
        return _config_view()

    @app.post("/api/config/test", dependencies=[Depends(auth), Depends(csrf)])
    def test_config():
        cache["status"] = (0, None)
        if not settings.wbiz_configured:
            return JSONResponse({"error": "Enter the client ID, API key and WhatsApp client first"}, status_code=400)
        try:
            me = client.me()
        except WbizError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        out = {"ok": True, "account": str(me.get("name") or "")}
        try:
            wa = client.client_status()
            out["whatsapp"] = wa.get("data") or wa.get("message") or "ok"
        except WbizError as exc:  # credentials are fine; only the status page failed
            out["whatsapp_error"] = str(exc)[:200]
        return out

    # ---- medicines API ----
    def _med_or_404(mid):
        row = db.get_medicine(mid)
        if not row:
            raise HTTPException(404, "Medicine not found")
        return row

    @app.get("/api/medicines/{mid}", dependencies=[Depends(auth)])
    def get_medicine(mid: int):
        return rec_to_form(_med_or_404(mid))

    @app.post("/api/medicines", dependencies=[Depends(auth), Depends(csrf)])
    async def add_medicine(request: Request):
        rec, err = form_to_rec(await request.json())
        if err:
            raise HTTPException(400, err)
        return {"id": db.add_medicine(rec)}

    @app.put("/api/medicines/{mid}", dependencies=[Depends(auth), Depends(csrf)])
    async def edit_medicine(mid: int, request: Request):
        _med_or_404(mid)
        rec, err = form_to_rec(await request.json())
        if err:
            raise HTTPException(400, err)
        db.update_medicine(mid, rec)
        return {"ok": True}

    @app.post("/api/medicines/{mid}/active", dependencies=[Depends(auth), Depends(csrf)])
    async def medicine_active(mid: int, request: Request):
        _med_or_404(mid)
        db.set_medicine_active(mid, bool((await request.json()).get("active")))
        return {"ok": True}

    @app.delete("/api/medicines/{mid}", dependencies=[Depends(auth), Depends(csrf)])
    def delete_medicine(mid: int):
        _med_or_404(mid)
        db.delete_medicine(mid)
        return {"ok": True}

    @app.post("/api/medicines-import", dependencies=[Depends(auth), Depends(csrf)])
    def import_medicines(file: UploadFile = File(...)):
        raw = file.file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "File is too large (2 MB max)")
        rows, errors = read_sheet(BytesIO(raw))
        if errors:
            raise HTTPException(400, "; ".join(errors))
        have = {(m["name"].lower(), m["times"], m["frequency"].lower()) for m in db.list_medicines()}
        added = skipped = 0
        for _row, rec in rows:
            key = (rec["name"].lower(), rec["times"], rec["frequency"].lower())
            if key in have:
                skipped += 1
                continue
            db.add_medicine({k: v[:300] for k, v in rec.items()})
            have.add(key)
            added += 1
        return {"added": added, "skipped": skipped}

    # ---- mom: profile and status log ----
    @app.put("/api/profile", dependencies=[Depends(auth), Depends(csrf)])
    async def save_profile(request: Request):
        body = await request.json()
        db.set_profile({k: str(body.get(k) or "").strip()[:lim] for k, lim in PROFILE_FIELDS.items() if k in body})
        return {"ok": True}

    @app.post("/api/status-log", dependencies=[Depends(auth), Depends(csrf)])
    async def add_status(request: Request):
        body = await request.json()
        kind = body.get("kind")
        if kind not in STATUS_KINDS:
            raise HTTPException(400, "Unknown kind")
        value, note = str(body.get("value") or "").strip()[:80], str(body.get("note") or "").strip()[:500]
        if not value and not note:
            raise HTTPException(400, "Enter a value or a note")
        db.add_status(kind, value, note)
        return {"ok": True}

    @app.delete("/api/status-log/{sid}", dependencies=[Depends(auth), Depends(csrf)])
    def delete_status(sid: int):
        if not db.delete_status(sid):
            raise HTTPException(404, "Not found")
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
