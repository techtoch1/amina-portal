import csv
import hashlib
import hmac
import io
import logging
import secrets
import threading
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .arabic import arabic_dose, fill_row, gaps as arabic_gaps, FIELD_LABELS
from .config import Settings
from .db import DB
from .excel import classify_frequency, details_from_form, load_from_db, norm, read_sheet, rec_to_form, schedule_to_rec
from .schedule import cron_human, next_cron_fire, next_injections, today_agenda
from .suggest import DEFAULT_SLOTS, SLOT_LABELS, suggestion
from .sync import apply_plan, compute_plan, copy_enabled, local_now, plan_to_json, rollover
from .wbiztool import WbizClient, WbizError

HERE = Path(__file__).parent
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PROFILE_FIELDS = {"name": 120, "age": 40, "conditions": 1000, "allergies": 1000, "doctor": 120, "doctor_phone": 40,
                  "emergency_contact": 200, "pharmacy": 200, "notes": 2000}
STATUS_KINDS = ["Blood pressure", "Blood sugar", "Weight", "Temperature", "Mood / condition", "Doctor visit", "Note"]
MAX_UPLOAD = 2 * 1024 * 1024
FAMILY_KEY = "family.hash"

LABELS = {
    "en": {"dir": "ltr", "title": "Medicines", "updated": "Updated", "prescriber": "Prescriber", "allergies": "Allergies",
           "cols": ["Reminder", "Medication", "Dose", "When to take", "For", "Notes"], "regular": "Regular",
           "as_needed": "As needed", "next": "Next", "empty": "No medicines yet. Add one or import your Excel file.",
           "off": "Off", "set": "Reminder", "edit": "Edit times"},
    "ar": {"dir": "rtl", "title": "الأدوية", "updated": "آخر تحديث", "prescriber": "الطبيب", "allergies": "الحساسية",
           "cols": ["التذكير", "الدواء", "الجرعة", "متى يؤخذ", "لماذا", "ملاحظات"], "regular": "أدوية منتظمة",
           "as_needed": "عند الحاجة", "next": "الموعد القادم", "empty": "لا توجد أدوية بعد.",
           "off": "متوقف", "set": "التذكير", "edit": "تعديل"},
}


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


class NotOwner(Exception):
    pass


def _hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def _check_password(password, stored):
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got, bytes.fromhex(dk))
    except (ValueError, AttributeError):
        return False


def _csv_cell(value):
    text = str(value).replace("\n", " | ")
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text  # no spreadsheet formula injection


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
    cache = {"status": (0, None)}
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

    @app.exception_handler(NotOwner)
    async def not_owner(request, exc):
        if request.url.path.startswith("/api/") or request.url.path.endswith(".csv"):
            return JSONResponse({"error": "Not allowed"}, status_code=403)
        return RedirectResponse("/family", status_code=303)

    def auth_any(request: Request):
        if not request.session.get("ok"):
            raise NeedLogin()

    def auth(request: Request):
        """Owner only (sessions created before roles existed were all the owner)."""
        auth_any(request)
        if request.session.get("role", "owner") != "owner":
            raise NotOwner()

    def csrf(request: Request):
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, request.session.get("csrf", "-")):
            raise HTTPException(403, "Bad CSRF token")

    def planning_meds():
        """Only medicines whose reminder is switched on take part in the Wbiztool sync."""
        return [m for m in load_from_db(db)[0].medicines if m.remind]

    def local_str(iso):
        try:
            dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
            return dt.strftime("%d %b %H:%M")
        except ValueError:
            return iso

    templates.env.filters["local"] = local_str
    templates.env.filters["ar_dose"] = arabic_dose

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
        owner_ok = hmac.compare_digest(password.encode(), settings.dashboard_password.encode())
        stored = db.get_setting(FAMILY_KEY)
        family_ok = bool(stored) and _check_password(password, stored) and not owner_ok
        if owner_ok or family_ok:
            failures.pop(ip, None)
            request.session.clear()
            request.session.update(ok=True, role="owner" if owner_ok else "family", csrf=secrets.token_urlsafe(24))
            return RedirectResponse("/", status_code=303)
        failures[ip] = [count + 1, first]
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)

    @app.post("/logout")
    def logout(request: Request, _=Depends(csrf)):
        request.session.clear()
        return JSONResponse({"ok": True})

    # ---- the one page ----
    def _config_view():
        return {"client_id": settings.client_id or "", "whatsapp_client": settings.whatsapp_client,
                "mom_number": settings.mom_number, "my_number": settings.my_number,
                "api_key_set": bool(settings.api_key), "api_key_length": len(settings.api_key),
                "docs_verified": settings.docs_verified}

    def _remind_text(m):
        if m.kind == "daily":
            return " · ".join(m.times)
        if m.kind == "weekly":
            return ", ".join(DAY_NAMES[d] for d in m.weekdays) + " " + " · ".join(m.times)
        if m.kind == "every_n":
            return f"every {m.every_n} days · " + " · ".join(m.times)
        return ""

    def slots():
        return {k: db.get_setting("slot." + k, v) for k, v in DEFAULT_SLOTS.items()}

    def table_groups(lang):
        ar = lang == "ar"
        res, _stopped = load_from_db(db)
        now = local_now(settings)
        parsed = {m.row: m for m in res.medicines}
        nxt = {m.row: at for m, at in next_injections(res.medicines, now)}
        groups = {"regular": [], "as_needed": []}
        for r in sorted(db.list_medicines(), key=lambda x: x["id"]):  # same order as in the Excel file
            if not r["active"]:
                continue
            kind, _n, _p = classify_frequency(norm(r["frequency"]))
            m = parsed.get(r["id"])
            form_txt = (r["form_ar"] or r["form"]) if ar else r["form"]
            purpose_txt = (r["purpose_ar"] or r["purpose"]) if ar else (r["purpose"] or r["purpose_ar"])
            notes_txt = (r["notes_ar"] or r["notes_en"]) if ar else (r["notes_en"] or r["notes_ar"])
            groups["as_needed" if kind == "as_needed" else "regular"].append({
                "id": r["id"], "name": r["name"], "form": form_txt, "dose": arabic_dose(r["dose"]) if ar else r["dose"],
                "fb": {"form": ar and bool(r["form"]) and not r["form_ar"], "purpose": ar and bool(r["purpose"]) and not r["purpose_ar"],
                       "notes": ar and bool(r["notes_en"]) and not r["notes_ar"], "when": ar and not r["instruction"]},
                "when": ((r["instruction"] if ar else r["when_en"]) or r["when_en"] or r["instruction"]),
                "purpose": purpose_txt, "notes": notes_txt,
                "remind": bool(r["remind"] and m and m.remind), "remind_text": _remind_text(m) if m and r["remind"] else "",
                "next": nxt.get(r["id"]) if r["remind"] else None,
            })
        return res, now, groups

    def base_ctx(request, active, **extra):
        res, _stopped = load_from_db(db)
        plan = compute_plan(settings, db, [m for m in res.medicines if m.remind], local_now(settings))
        arabic_missing = sum(len(arabic_gaps(r)) for r in db.list_medicines() if r["active"])
        ctx = {"csrf": request.session.get("csrf", ""), "active": active, "role": request.session.get("role", "owner"),
               "arabic_missing": arabic_missing,
               "profile": db.profile(), "pending": len(plan["create"]) + len(plan["cancel"]),
               "configured": settings.wbiz_configured, "verified": settings.docs_verified, "slots": slots(),
               "slot_labels": SLOT_LABELS, "day_names": DAY_NAMES}
        ctx.update(extra)
        return ctx

    @app.get("/", dependencies=[Depends(auth_any)])
    def home(request: Request):
        return RedirectResponse("/medicines" if request.session.get("role", "owner") == "owner" else "/family", status_code=303)

    @app.get("/medicines", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def medicines_page(request: Request, lang: str = "en"):
        lang = lang if lang in LABELS else "en"
        _res, now, groups = table_groups(lang)
        return templates.TemplateResponse(request, "medicines.html", base_ctx(
            request, "medicines", lang=lang, L=LABELS[lang], groups=groups, updated=now.strftime("%d %b %Y"),
            total=sum(len(v) for v in groups.values())))

    @app.get("/family", response_class=HTMLResponse, dependencies=[Depends(auth_any)])
    def family_page(request: Request, lang: str = "en"):
        lang = lang if lang in LABELS else "en"
        res, now, groups = table_groups(lang)
        by_id = {m.row: m for m in res.medicines}
        ar = lang == "ar"
        today = []
        for t, m in today_agenda([m for m in res.medicines if m.remind], now.date()):
            row = db.get_medicine(m.row) or {}
            today.append({"time": t, "name": m.name, "dose": m.dose, "past": t < now.strftime("%H:%M"),
                          "when": (row.get("instruction") if ar else row.get("when_en")) or row.get("when_en") or ""})
        return templates.TemplateResponse(request, "family.html", {
            "csrf": request.session.get("csrf", ""), "role": request.session.get("role", "owner"), "lang": lang,
            "L": LABELS[lang], "groups": groups, "today": today, "profile": db.profile(),
            "updated": now.strftime("%d %b %Y"), "today_label": now.strftime("%A %d %B"),
            "total": sum(len(v) for v in groups.values()), "active": "family"})

    @app.get("/mom", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def mom_page(request: Request):
        log = db.list_status(60)
        latest = {}
        for row in log:
            latest.setdefault(row["kind"], row)
        return templates.TemplateResponse(request, "mom.html", base_ctx(request, "mom", kinds=STATUS_KINDS, log=log, latest=latest))

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html", base_ctx(
            request, "settings", conf=_config_view(), family_enabled=bool(db.get_setting(FAMILY_KEY))))

    def _reminder_rows():
        now = local_now(settings)
        rows = []
        for r in db.active():
            if r["kind"] == "cron":
                when, nxt = cron_human(r["cron"]), next_cron_fire(r["cron"], now)
            else:
                nxt = datetime.strptime(r["fire_at"], "%Y-%m-%d %H:%M")
                when = "Once on " + nxt.strftime("%d %b %Y at %H:%M")
            rows.append({"names": r["names"], "when": when, "next": nxt, "recipient": r["recipient"],
                         "message": r["message"], "kind": r["kind"]})
        return sorted(rows, key=lambda x: (x["next"] is None, x["next"] or now, x["recipient"]))

    @app.get("/reminders", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def reminders_page(request: Request, show: str = "all", review: int = 0):
        rows = _reminder_rows()
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        events = db.recent_events(60, failed_only=(show == "failed"))
        last_sync = next((e for e in db.recent_events(40) if e["kind"] in ("create", "cancel", "rollover")), None)
        return templates.TemplateResponse(request, "reminders.html", base_ctx(
            request, "reminders", rows=rows, events=events, show=show, auto_review=bool(review),
            copy=copy_enabled(settings, db), failed_week=db.count_failed_since(since), last_sync=last_sync,
            mom_count=sum(1 for r in rows if r["recipient"] == "mom"), me_count=sum(1 for r in rows if r["recipient"] == "me")))

    @app.get("/reminders/export.csv", dependencies=[Depends(auth)])
    def reminders_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Recipient", "Medicines", "Schedule", "Next send", "Message"])
        for r in _reminder_rows():
            w.writerow([_csv_cell("Mom" if r["recipient"] == "mom" else "Copy to me"), _csv_cell(r["names"]), _csv_cell(r["when"]),
                        r["next"].strftime("%Y-%m-%d %H:%M") if r["next"] else "", _csv_cell(r["message"])])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="reminders.csv"'})

    # ---- medicines API ----
    def _med_or_404(mid):
        row = db.get_medicine(mid)
        if not row:
            raise HTTPException(404, "Medicine not found")
        return row

    def _apply_section(fields, section, current_freq=""):
        """Regular / As needed. As needed never reminds; Regular keeps its schedule if it has one."""
        if section == "as_needed":
            fields.update(frequency="as needed", times="", day="", start_date="", last_date="")
            return True
        kind, _n, _p = classify_frequency(norm(current_freq))
        if kind in (None, "as_needed"):
            fields["frequency"] = "chart only"
        return False

    @app.get("/api/medicines/{mid}", dependencies=[Depends(auth)])
    def get_medicine(mid: int):
        form = rec_to_form(_med_or_404(mid))
        form["suggest"] = suggestion(form, slots())
        return form

    @app.post("/api/medicines", dependencies=[Depends(auth), Depends(csrf)])
    async def add_medicine(request: Request):
        body = await request.json()
        fields, err = details_from_form(body)
        if err:
            raise HTTPException(400, err)
        _apply_section(fields, body.get("section"))
        return {"id": db.add_medicine(fields)}

    @app.put("/api/medicines/{mid}", dependencies=[Depends(auth), Depends(csrf)])
    async def edit_medicine(mid: int, request: Request):
        row = _med_or_404(mid)
        body = await request.json()
        fields, err = details_from_form(body, require_name=False)
        if err or ("name" in fields and not fields["name"]):
            raise HTTPException(400, err or "Medicine name is required")
        if "section" in body:
            if _apply_section(fields, body["section"], row["frequency"]):
                db.set_remind(mid, False)
        db.update_medicine(mid, fields)
        return {"ok": True}

    @app.post("/api/medicines/{mid}/reminder", dependencies=[Depends(auth), Depends(csrf)])
    async def medicine_reminder(mid: int, request: Request):
        row = _med_or_404(mid)
        body = await request.json()
        if not body.get("on"):
            db.set_remind(mid, False)
            return {"ok": True, "remind": False}
        merged = rec_to_form(row)
        merged.update({k: body[k] for k in ("frequency", "times", "weekdays", "every_n", "date", "date_kind") if k in body})
        sched, err = schedule_to_rec(merged)
        if err:
            raise HTTPException(400, err)
        if sched["frequency"] in ("as needed", "chart only"):
            raise HTTPException(400, "Choose how often the reminder repeats")
        db.update_medicine(mid, sched)
        db.set_remind(mid, True)
        return {"ok": True, "remind": True}

    @app.post("/api/arabic/fill", dependencies=[Depends(auth), Depends(csrf)])
    def fill_arabic():
        """Fill EMPTY Arabic fields from the glossary; report what was filled and what still needs a person."""
        filled, missing = [], []
        for r in sorted(db.list_medicines(), key=lambda x: x["id"]):
            if not r["active"]:
                continue
            updates, done, still = fill_row(r)
            if updates:
                db.update_medicine(r["id"], updates)
            filled += [{"id": r["id"], "name": r["name"], "field": FIELD_LABELS[f], "value": v} for f, v in done]
            missing += [{"id": r["id"], "name": r["name"], "field": FIELD_LABELS[f], "english": e} for f, e in still]
        db.log_event("settings", True, f"Arabic fill: {len(filled)} filled, {len(missing)} still missing")
        return {"filled": filled, "missing": missing}

    @app.delete("/api/medicines/{mid}", dependencies=[Depends(auth), Depends(csrf)])
    def delete_medicine(mid: int):
        _med_or_404(mid)
        db.delete_medicine(mid)
        return {"ok": True}

    @app.post("/api/medicines-import", dependencies=[Depends(auth), Depends(csrf)])
    def import_medicines(replace: bool = False, file: UploadFile = File(...)):
        raw = file.file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "File is too large (2 MB max)")
        rows, errors = read_sheet(BytesIO(raw))
        if errors:
            raise HTTPException(400, "; ".join(errors))
        if replace:  # reminders of the removed medicines are cancelled by the next sync
            db.delete_all_medicines()
        have = {(m["name"].lower(), m["when_en"].lower()) for m in db.list_medicines()}
        added = skipped = 0
        for _row, rec in rows:
            key = (rec["name"].lower(), rec["when_en"].lower())
            if key in have:
                skipped += 1
                continue
            db.add_medicine({k: v[:500] for k, v in rec.items()})
            have.add(key)
            added += 1
        return {"added": added, "skipped": skipped}

    # ---- sync with Wbiztool ----
    @app.post("/api/sync/preview", dependencies=[Depends(auth), Depends(csrf)])
    def preview():
        if not settings.mom_number:
            raise HTTPException(400, "Mom's number is not set (Wbiztool settings)")
        if copy_enabled(settings, db) and not settings.my_number:
            raise HTTPException(400, "Your number is not set (Wbiztool settings), or untick the copy option")
        return plan_to_json(compute_plan(settings, db, planning_meds()))

    @app.post("/api/sync/apply", dependencies=[Depends(auth), Depends(csrf)])
    async def apply(request: Request):
        body = await request.json()
        try:
            results = apply_plan(client, settings, db, planning_meds(), str(body.get("hash", "")))
        except WbizError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        cache["status"] = (0, None)
        return {"results": results}

    @app.post("/api/settings", dependencies=[Depends(auth), Depends(csrf)])
    async def set_settings(request: Request):
        body = await request.json()
        db.set_setting("copy_to_me", "1" if body.get("copy_to_me") else "0")
        return {"ok": True}

    # ---- Wbiztool status, test, credentials ----
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
            out["error"] = "Wbiztool credentials are not set"
        cache["status"] = (_time.time(), out)
        return out

    @app.post("/api/test-message", dependencies=[Depends(auth), Depends(csrf)])
    def test_message():
        if not settings.my_number:
            return JSONResponse({"error": "Your number is not set"}, status_code=400)
        text = "اختبار: تذكير الدواء يعمل ✅\n" + datetime.now().strftime("%H:%M")
        try:
            client.send_message(settings.my_number, text)
        except WbizError as exc:
            db.log_event("test", False, str(exc))
            return JSONResponse({"error": str(exc)}, status_code=502)
        db.log_event("test", True, "test message sent to my number")
        cache["status"] = (0, None)
        return {"ok": True}

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

    @app.put("/api/slots", dependencies=[Depends(auth), Depends(csrf)])
    async def save_slots(request: Request):
        import re as _re
        body = await request.json()
        for key in DEFAULT_SLOTS:
            if key in body:
                v = str(body[key]).strip()
                if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                    raise HTTPException(400, f"'{v}' is not a valid time")
                db.set_setting("slot." + key, v)
        return slots()

    @app.put("/api/family-access", dependencies=[Depends(auth), Depends(csrf)])
    async def set_family_access(request: Request):
        password = str((await request.json()).get("password") or "")
        if len(password) < 8:
            raise HTTPException(400, "The family password must be at least 8 characters")
        if hmac.compare_digest(password.encode(), settings.dashboard_password.encode()):
            raise HTTPException(400, "It must be different from your own password")
        db.set_setting(FAMILY_KEY, _hash_password(password))
        db.log_event("settings", True, "Family password set")
        return {"enabled": True}

    @app.delete("/api/family-access", dependencies=[Depends(auth), Depends(csrf)])
    def clear_family_access():
        db.set_setting(FAMILY_KEY, "")
        db.log_event("settings", True, "Family access turned off")
        return {"enabled": False}

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

    @app.delete("/api/status-log", dependencies=[Depends(auth), Depends(csrf)])
    def clear_status():
        return {"deleted": db.clear_status()}

    # ---- background roll-over of one-off messages (e.g. every 28 days) ----
    def loop():
        while True:
            _time.sleep(1800)
            try:
                rollover(client, settings, db, planning_meds())
            except Exception as exc:  # keep the thread alive
                logging.getLogger("rollover").error("rollover failed: %s", settings.redact(type(exc).__name__))

    @asynccontextmanager
    async def lifespan(_app):
        threading.Thread(target=loop, daemon=True).start()
        yield

    app.router.lifespan_context = lifespan
    return app
