import csv
import hashlib
import json
import hmac
import io
import logging
import math
import re
import secrets
import threading
import time as _time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
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
from .export import build_doctor_workbook, build_stock_workbook, build_usage_workbook
from .db import DB
from .excel import classify_frequency, details_from_form, load_from_db, norm, read_sheet, rec_to_form, schedule_to_rec
from .schedule import cron_human, next_cron_fire, next_injections, today_agenda
from .messages import COPY_NOTE
from . import stock as stk
from .suggest import DEFAULT_SLOTS, SLOT_LABELS, suggestion
from .messages import build_message
from .sync import apply_plan, compute_plan, copy_enabled, include_purpose, local_now, plan_to_json, rollover
from .wbiztool import WbizClient, WbizError

HERE = Path(__file__).parent
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PROFILE_FIELDS = {"name": 120, "age": 40, "conditions": 1000, "allergies": 1000, "doctor": 120, "doctor_phone": 40,
                  "emergency_contact": 200, "pharmacy": 200, "care_team": 2000, "notes": 2000}
STATUS_KINDS = ["Doctor visit", "Appointment", "Lab test", "Treatment", "Hospital", "Blood pressure", "Blood sugar", "Weight",
                "Temperature", "Mood / condition", "Note"]
LOG_KIND_AR = {"Doctor visit": "زيارة الطبيب", "Appointment": "موعد", "Lab test": "فحص مخبري", "Blood pressure": "ضغط الدم",
               "Blood sugar": "سكر الدم", "Weight": "الوزن", "Temperature": "الحرارة", "Mood / condition": "الحالة العامة",
               "Note": "ملاحظة", "Treatment": "علاج", "Hospital": "المستشفى"}
READING_KINDS_EXCLUDED = ("Doctor visit", "Appointment", "Lab test", "Treatment", "Hospital", "Note")
MAX_UPLOAD = 2 * 1024 * 1024
TEST_MIN_INTERVAL = 3.0  # seconds between manual test sends
TEST_HEADER = "🧪 رسالة تجريبية — Test message (not a real reminder)\n\n"
TEST_HEADER_MOM = "🧪 رسالة تجريبية من تطبيق تذكير الدواء — يرجى تجاهلها\n\n"
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
    client.remember = lambda fmt: db.set_setting("wbiz.schedule_format", fmt)
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
        if request.url.path.startswith("/api/") or request.url.path.endswith((".csv", ".xlsx")):
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

    def event_str(row, lang="en"):
        """'Fri 16 Oct 2026, 14:30' (Arabic: '16/10/2026، 14:30') for an entry's own date and optional time."""
        try:
            text = datetime.strptime(row["event_date"], "%Y-%m-%d").strftime("%d/%m/%Y" if lang == "ar" else "%a %d %b %Y")
        except (ValueError, KeyError):
            return local_str(row.get("ts", ""))
        return f"{text}{'،' if lang == 'ar' else ','} {row['event_time']}" if row.get("event_time") else text

    templates.env.filters["event"] = event_str
    templates.env.filters["kind_ar"] = lambda k: LOG_KIND_AR.get(k, k)

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

    # ---- stock ----
    def stock_parsed():
        """Parsed medicines (valid schedules only) by id, plus the active medicine rows in entry order."""
        res, _stopped = load_from_db(db)
        rows = [r for r in sorted(db.list_medicines(), key=lambda x: x["id"]) if r["active"]]
        return {m.row: m for m in res.medicines}, rows

    def stock_limits():
        return int(db.get_setting("stock.low_days", "10")), int(db.get_setting("stock.order_days", "30"))

    def stock_overview():
        by_id, rows = stock_parsed()
        now = local_now(settings)
        stk.materialize(db, by_id, now.date())
        low, order = stock_limits()
        return stk.overview(db, by_id, rows, now, low, order)

    def base_ctx(request, active, **extra):
        res, _stopped = load_from_db(db)
        plan = compute_plan(settings, db, [m for m in res.medicines if m.remind], local_now(settings))
        arabic_missing = sum(len(arabic_gaps(r)) for r in db.list_medicines() if r["active"])
        stock_rows = stock_overview()
        stock_attention = sum(1 for r in stock_rows if r["status"] in ("low", "out"))
        ctx = {"csrf": request.session.get("csrf", ""), "active": active, "role": request.session.get("role", "owner"),
               "arabic_missing": arabic_missing, "mom_tail": settings.mom_number[-3:], "stock_attention": stock_attention,
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

    def log_view():
        today = local_now(settings).date().isoformat()
        log = db.list_status(120)
        past = [r for r in log if r["event_date"] <= today]
        upcoming = sorted((r for r in log if r["event_date"] > today), key=lambda r: (r["event_date"], r["event_time"]))
        latest = {}
        for row in past:
            if row["kind"] not in READING_KINDS_EXCLUDED:  # readings only
                latest.setdefault(row["kind"], row)
        return today, past, upcoming, latest

    @app.get("/family/log", response_class=HTMLResponse, dependencies=[Depends(auth_any)])
    def family_log_page(request: Request, lang: str = "en"):
        """Read-only copy of Mom's log for family: upcoming appointments, latest readings, history."""
        lang = lang if lang in LABELS else "en"
        _today, past, upcoming, latest = log_view()
        return templates.TemplateResponse(request, "family_log.html", {
            "csrf": request.session.get("csrf", ""), "role": request.session.get("role", "owner"), "lang": lang,
            "L": LABELS[lang], "profile": db.profile(), "log": past, "upcoming": upcoming, "latest": latest,
            "active": "family_log"})

    @app.get("/mom", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def mom_page(request: Request):
        today, past, upcoming, latest = log_view()
        return templates.TemplateResponse(request, "mom.html", base_ctx(
            request, "mom", kinds=STATUS_KINDS, log=past, upcoming=upcoming, latest=latest, today=today))

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def settings_page(request: Request):
        res, _stopped = load_from_db(db)
        sample = next((m for m in res.medicines if m.purpose_ar and m.instruction), None) or next(iter(res.medicines), None)
        with_purpose = include_purpose(db)
        example = build_message([sample], with_purpose=with_purpose) if sample else ""
        return templates.TemplateResponse(request, "settings.html", base_ctx(
            request, "settings", conf=_config_view(), family_enabled=bool(db.get_setting(FAMILY_KEY)),
            with_purpose=with_purpose, example_message=example))

    def _reminder_rows():
        now = local_now(settings)
        rows = []
        for r in db.active():
            if r["kind"] == "cron" and not r["once"]:
                when, nxt = cron_human(r["cron"]), next_cron_fire(r["cron"], now)
            else:
                nxt = datetime.strptime(r["fire_at"], "%Y-%m-%d %H:%M")
                when = "Once on " + nxt.strftime("%d %b %Y at %H:%M")
            rows.append({"names": r["names"], "when": when, "next": nxt, "recipient": r["recipient"],
                         "message": r["message"], "kind": r["kind"], "fingerprint": r["fingerprint"]})
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

    # ---- stock pages ----
    def _range(preset, start_s, end_s, today):
        """(start, end) from ?preset=7|30|90|month or ?from=&to=; defaults to the last 30 days."""
        try:
            if start_s and end_s:
                start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
            elif preset == "month":
                start, end = today.replace(day=1), today
            else:
                n = int(preset) if str(preset).isdigit() and 1 <= int(preset) <= 730 else 30
                start, end = today - timedelta(days=n - 1), today
        except ValueError:
            raise HTTPException(400, "That date range is not valid")
        if end < start or (end - start).days > 730:
            raise HTTPException(400, "Choose a range of up to 2 years, with the start before the end")
        return start, end

    @app.get("/stock", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def stock_page(request: Request):
        rows = stock_overview()
        low, order = stock_limits()
        counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("out", "low", "ok", "none")}
        return templates.TemplateResponse(request, "stock.html", base_ctx(
            request, "stock", rows=rows, counts=counts, today=local_now(settings).date().isoformat(), low_days=low, order_days=order, num=stk.num, plural=stk.plural,
            to_order=[r for r in rows if r["order_qty"] and r["status"] in ("low", "out")],
            stock_number=settings.stock_number, alerts_ready=bool(settings.stock_number and settings.wbiz_configured and settings.docs_verified)))

    @app.get("/stock/report", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def stock_report_page(request: Request, preset: str = "30", start: str = "", end: str = ""):
        now = local_now(settings)
        a, b = _range(preset, start, end, now.date())
        by_id, rows = stock_parsed()
        stk.materialize(db, by_id, now.date())
        report = stk.usage_report(db, by_id, rows, a, b, now)
        return templates.TemplateResponse(request, "stock_report.html", base_ctx(
            request, "stock", report=report, start=a.isoformat(), end=b.isoformat(), preset=preset if not (start and end) else "",
            days=(b - a).days + 1, num=stk.num, plural=stk.plural, total_auto=round(sum(r["auto"] for r in report), 2),
            total_manual=round(sum(r["manual"] for r in report), 2), total_bought=round(sum(r["bought"] for r in report), 2)))

    @app.get("/stock/export.xlsx", dependencies=[Depends(auth)])
    def stock_export():
        now = local_now(settings)
        low, order = stock_limits()
        data = build_stock_workbook(stock_overview(), low, order, now, db.profile())
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="Medicine_stock_{now.strftime("%Y-%m-%d")}.xlsx"'})

    @app.get("/stock/report.xlsx", dependencies=[Depends(auth)])
    def stock_report_export(preset: str = "30", start: str = "", end: str = ""):
        now = local_now(settings)
        a, b = _range(preset, start, end, now.date())
        by_id, rows = stock_parsed()
        stk.materialize(db, by_id, now.date())
        data = build_usage_workbook(stk.usage_report(db, by_id, rows, a, b, now), a, b, db.profile())
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="Medicine_usage_{a.isoformat()}_to_{b.isoformat()}.xlsx"'})

    @app.get("/medicines/export.xlsx", dependencies=[Depends(auth)])
    def medicines_xlsx():
        """The medicines exactly as they are in the dashboard, laid out for a doctor to confirm."""
        data = build_doctor_workbook(db, settings, local_now(settings))
        stamp = local_now(settings).strftime("%Y-%m-%d")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="Medicines_for_doctor_review_{stamp}.xlsx"'})

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

    # ---- stock API ----
    def _finite(value, low=0.0, high=100000.0):
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        return x if math.isfinite(x) and low <= x <= high else None

    @app.get("/api/stock/{mid}", dependencies=[Depends(auth)])
    def stock_detail(mid: int):
        row = _med_or_404(mid)
        by_id, _rows = stock_parsed()
        item = db.stock_item(mid) or {}
        return {"name": row["name"], "unit": stk.unit_word(row["form"], item.get("unit", "")), "unit_default": stk.unit_word(row["form"]),
                "units_per_dose": item.get("units_per_dose", 1),
                "daily_use": item.get("daily_use"), "tracking": bool(item.get("tracking", 1)),
                "suggested_units": stk.suggest_units(row["when_en"]), "counted": db.has_moves(mid),
                "has_schedule": stk.has_schedule(by_id.get(mid)),
                "moves": [{"day": x["day"], "kind": stk.KINDS.get(x["kind"], x["kind"]), "qty": x["qty"], "note": x["note"]}
                          for x in db.moves(mid, 25)]}

    @app.put("/api/stock/{mid}/settings", dependencies=[Depends(auth), Depends(csrf)])
    async def stock_item_settings(mid: int, request: Request):
        _med_or_404(mid)
        body = await request.json()
        upd = _finite(body.get("units_per_dose"), 0.01, 1000)
        if upd is None:
            raise HTTPException(400, "Units per dose must be a number above 0")
        daily = body.get("daily_use")
        daily = None if daily in (None, "") else _finite(daily, 0, 1000)
        if body.get("daily_use") not in (None, "") and daily is None:
            raise HTTPException(400, "Daily use must be a number (0 or more)")
        unit = " ".join(str(body.get("unit") or "").split())[:20]
        if unit and not all(ch.isalpha() or ch in " -" for ch in unit):
            raise HTTPException(400, "'Counted in' should be a word such as tablets, capsules or drinks")
        if unit.lower() == stk.unit_word(_med_or_404(mid)["form"]):
            unit = ""  # same as the default: nothing to remember
        db.ensure_stock_item(mid, "")
        db.update_stock_item(mid, units_per_dose=upd, daily_use=daily, tracking=1 if body.get("tracking", True) else 0, unit=unit)
        return {"ok": True}

    @app.post("/api/stock/{mid}/move", dependencies=[Depends(auth), Depends(csrf)])
    async def stock_move(mid: int, request: Request):
        """kind: buy (add stock), used (taken outside the schedule), count (set the number actually on hand)."""
        row = _med_or_404(mid)
        body = await request.json()
        kind = body.get("kind")
        if kind not in ("buy", "used", "count"):
            raise HTTPException(400, "Unknown kind")
        qty = _finite(body.get("qty"))
        if qty is None or (qty <= 0 and kind != "count"):
            raise HTTPException(400, "Enter a number" + (" above 0" if kind != "count" else " (0 or more)"))
        now = local_now(settings)
        try:
            day = date.fromisoformat(str(body.get("day") or now.date().isoformat()))
        except ValueError:
            raise HTTPException(400, "That date is not valid")
        if day > now.date() or (now.date() - day).days > 800:
            raise HTTPException(400, "Choose today or an earlier date")
        note = str(body.get("note") or "").strip()[:200]
        by_id, _rows = stock_parsed()
        stk.materialize(db, by_id, now.date())
        item = db.stock_item(mid)
        counted = bool(item) and db.has_moves(mid)
        if kind == "count":
            if not counted:  # the very first count: scheduled use is counted from the day after it
                item = db.ensure_stock_item(mid, day.isoformat())
                db.update_stock_item(mid, through_day=day.isoformat())
                db.add_move(mid, day.isoformat(), "start", qty, note or "Starting count")
            else:
                current = stk.on_hand(db, mid, by_id.get(mid), item, now)
                db.add_move(mid, now.date().isoformat(), "adjust", qty - current, note or f"Counted {stk.num(qty)}")
        else:
            if not item:
                item = db.ensure_stock_item(mid, day.isoformat())
            if not counted and not item["through_day"]:
                db.update_stock_item(mid, through_day=day.isoformat())
            db.add_move(mid, day.isoformat(), kind, qty if kind == "buy" else -qty, note)
        db.log_event("stock", True, f"{row['name']}: {kind} {stk.num(qty)}")
        return {"ok": True}

    @app.post("/api/stock/import", dependencies=[Depends(auth), Depends(csrf)])
    def stock_import(file: UploadFile = File(...)):
        raw = file.file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "File is too large (2 MB max)")
        rows, errors = read_sheet(BytesIO(raw))
        if errors:
            raise HTTPException(400, "; ".join(errors))
        if not any(r.get("_quantity") for _n, r in rows):
            raise HTTPException(400, "No quantity column found (a header like 'Quantity' or 'الكمية')")
        names = {m["name"].strip().lower(): m["id"] for m in db.list_medicines()}
        res = stk.apply_counts(db, names, [(r["name"], r.get("_quantity", ""), r.get("_count_date", "")) for _n, r in rows])
        db.log_event("stock", True, f"Counts imported: {len(res['applied'])} set")
        return res

    @app.put("/api/stock/settings", dependencies=[Depends(auth), Depends(csrf)])
    async def stock_settings(request: Request):
        body = await request.json()
        low, order = _finite(body.get("low_days"), 1, 365), _finite(body.get("order_days"), 1, 365)
        if low is None or order is None:
            raise HTTPException(400, "Days must be numbers between 1 and 365")
        number = "".join(ch for ch in str(body.get("stock_number") or "") if ch.isdigit())
        if number and not 8 <= len(number) <= 15:
            raise HTTPException(400, "The alert number should be 8-15 digits including the country code")
        db.set_setting("stock.low_days", str(int(low)))
        db.set_setting("stock.order_days", str(int(order)))
        db.set_setting("conf.stock_number", number)
        settings.apply_overrides(db)
        return {"ok": True, "stock_number": number}

    @app.post("/api/stock/check-alerts", dependencies=[Depends(auth), Depends(csrf)])
    def stock_check_alerts():
        if not settings.stock_number:
            raise HTTPException(400, "Enter the stock alert number first")
        by_id, rows = stock_parsed()
        now = local_now(settings)
        stk.materialize(db, by_id, now.date())
        sent, new = stk.check_alerts(client, settings, db, by_id, rows, now)
        return {"sent": sent, "pending": [r["name"] for r in new]}

    @app.post("/api/stock/test-alert", dependencies=[Depends(auth), Depends(csrf)])
    async def stock_test_alert(request: Request):
        if (await request.json()).get("confirm") is not True:
            raise HTTPException(400, "Confirm that you want to send a message to the alert number")
        if not settings.stock_number:
            raise HTTPException(400, "Enter the stock alert number first")
        sample = [{"name": "Example medicine", "status": "low", "on_hand": 6, "unit": "tablets", "days_left": 3,
                   "run_out": local_now(settings).date() + timedelta(days=3), "order_qty": 54}]
        text = "🧪 TEST — this is only a test of the stock alert\n\n" + stk.alert_text(db.profile().get("name") or "Medicines", sample, 10, 30, local_now(settings).date())
        try:
            client.send_message(settings.stock_number, text)
        except WbizError as exc:
            raise HTTPException(502, str(exc))
        db.log_event("stock", True, "Test stock alert sent")
        return {"ok": True}

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

    @app.post("/api/reminders/clear", dependencies=[Depends(auth), Depends(csrf)])
    def clear_reminders():
        """Turn every reminder off. Wbiztool is not touched: the cancellations are previewed and approved
        in Review & apply like any other change."""
        n = db.clear_all_reminders()
        db.log_event("settings", True, f"All reminders turned off ({n}); waiting for Review & apply")
        return {"turned_off": n}

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
            db.add_medicine({k: v[:500] for k, v in rec.items() if not k.startswith("_")})
            have.add(key)
            added += 1
        counts = [(r["name"], r["_quantity"], r.get("_count_date", "")) for _n, r in rows if r.get("_quantity")]
        stocked = len(stk.apply_counts(db, {m["name"].strip().lower(): m["id"] for m in db.list_medicines()}, counts)["applied"]) if counts else 0
        return {"added": added, "skipped": skipped, "stock_counts": stocked}

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

    test_gate = {"last": 0.0}

    @app.post("/api/test-reminder", dependencies=[Depends(auth), Depends(csrf)])
    async def test_reminder(request: Request):
        """Send one of the app's own reminder messages as a test: to ME (default) or, after confirmation, to Mom."""
        body = await request.json()
        fingerprint = str(body.get("fingerprint") or "")
        to_mom = body.get("to") == "mom"
        number = settings.mom_number if to_mom else settings.my_number
        if not number:
            return JSONResponse({"error": ("Mom's" if to_mom else "Your") + " number is not set (Settings)"}, status_code=400)
        if to_mom and body.get("confirm") is not True:
            return JSONResponse({"error": "Confirm that you want to message Mom"}, status_code=400)
        if _time.time() - test_gate["last"] < (TEST_MIN_INTERVAL * (3 if to_mom else 1)):
            return JSONResponse({"error": "Wait a few seconds between tests"}, status_code=429)
        message = next((r["message"] for r in db.active() if r["fingerprint"] == fingerprint), None)
        if message is None:  # not applied yet: look it up in the pending plan
            plan = compute_plan(settings, db, planning_meds())
            message = next((d.message for d in plan["create"] if d.fingerprint == fingerprint), None)
        if message is None:
            return JSONResponse({"error": "That reminder no longer exists — reload the page"}, status_code=404)
        if to_mom and message.startswith(COPY_NOTE):  # the copy-to-me note makes no sense for her
            message = message[len(COPY_NOTE):].lstrip("\n")
        test_gate["last"] = _time.time()
        try:
            client.send_message(number, (TEST_HEADER_MOM if to_mom else TEST_HEADER) + message)
        except WbizError as exc:
            db.log_event("test", False, f"Test reminder to {'Mom' if to_mom else 'me'}: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=502)
        db.log_event("test", True, f"Test reminder sent to {'Mom' if to_mom else 'my number'}")
        return {"ok": True, "to": "mom" if to_mom else "me"}

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

    @app.put("/api/message-options", dependencies=[Depends(auth), Depends(csrf)])
    async def message_options(request: Request):
        body = await request.json()
        db.set_setting("msg.purpose", "1" if body.get("include_purpose") else "0")
        return {"include_purpose": include_purpose(db)}

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
        today = local_now(settings).date()
        raw_date = str(body.get("date") or "").strip()
        try:
            when = date.fromisoformat(raw_date) if raw_date else today
        except ValueError:
            raise HTTPException(400, "That date is not valid")
        if abs((when - today).days) > 3660:
            raise HTTPException(400, "Choose a date within 10 years")
        raw_time = str(body.get("time") or "").strip()
        if raw_time and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw_time):
            raise HTTPException(400, "That time is not valid")
        db.add_status(kind, value, note, when.isoformat(), raw_time)
        return {"ok": True}

    # ---- import / export of the profile and log (backup, or filling them in from a file) ----
    def _valid_time(t):
        return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t))

    @app.get("/api/mom-export", dependencies=[Depends(auth)])
    def mom_export():
        log = [{"kind": r["kind"], "date": r["event_date"], "time": r["event_time"], "value": r["value"], "note": r["note"]}
               for r in reversed(db.list_status(5000))]
        body = json.dumps({"version": 1, "profile": db.profile(), "log": log}, ensure_ascii=False, indent=1)
        return Response(body, media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="mom-profile-and-log.json"'})

    @app.post("/api/mom-import", dependencies=[Depends(auth), Depends(csrf)])
    async def mom_import(request: Request, overwrite: bool = False):
        """Add profile fields and log entries from a JSON file. Everything is checked first; nothing is
        imported if any entry is invalid. Profile fields that already have text are kept unless overwrite=true."""
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise HTTPException(413, "File is too large (1 MB max)")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "That is not a valid JSON file")
        if not isinstance(data, dict):
            raise HTTPException(400, "The file should contain a profile and/or a log")
        ignored, problems, prof = [], [], {}
        for key, value in (data.get("profile") or {}).items() if isinstance(data.get("profile"), dict) else []:
            if key in PROFILE_FIELDS:
                prof[key] = str(value or "").strip()[:PROFILE_FIELDS[key]]
            else:
                ignored.append(key)
        entries, today = [], local_now(settings).date()
        log_in = data.get("log") if isinstance(data.get("log"), list) else []
        if len(log_in) > 500:
            raise HTTPException(400, "Too many log entries (500 max)")
        for i, e in enumerate(log_in, 1):
            if not isinstance(e, dict) or e.get("kind") not in STATUS_KINDS:
                problems.append(f"entry {i}: unknown type")
                continue
            value, note = str(e.get("value") or "").strip(), str(e.get("note") or "").strip()
            if len(value) > 80 or len(note) > 500:
                problems.append(f"entry {i}: value over 80 or note over 500 characters")
            if not value and not note:
                problems.append(f"entry {i}: empty")
            try:
                when = date.fromisoformat(str(e.get("date") or today.isoformat()))
            except ValueError:
                problems.append(f"entry {i}: invalid date")
                continue
            if abs((when - today).days) > 3660:
                problems.append(f"entry {i}: date more than 10 years away")
            etime = str(e.get("time") or "").strip()
            if etime and not _valid_time(etime):
                problems.append(f"entry {i}: invalid time")
            entries.append((e["kind"], value, note, when.isoformat(), etime))
        if problems:
            raise HTTPException(400, "Nothing imported. " + "; ".join(problems[:6]) + (" …" if len(problems) > 6 else ""))
        have_profile = db.profile()
        applied = {k: v for k, v in prof.items() if v and (overwrite or not have_profile.get(k))}
        db.set_profile(applied)
        seen = {(r["kind"], r["event_date"], r["value"].lower(), r["note"]) for r in db.list_status(5000)}
        added = skipped = 0
        for kind, value, note, when, etime in entries:
            key = (kind, when, value.lower(), note)
            if key in seen:
                skipped += 1
                continue
            db.add_status(kind, value, note, when, etime)
            seen.add(key)
            added += 1
        db.log_event("settings", True, f"Imported profile ({len(applied)} fields) and {added} log entries")
        return {"profile_fields": len(applied), "profile_kept": len(prof) - len(applied), "log_added": added,
                "log_skipped": skipped, "ignored": ignored}

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
        _time.sleep(120)
        while True:
            try:
                rollover(client, settings, db, planning_meds())
            except Exception as exc:  # keep the thread alive
                logging.getLogger("rollover").error("rollover failed: %s", settings.redact(type(exc).__name__))
            try:
                by_id, rows = stock_parsed()
                now = local_now(settings)
                stk.materialize(db, by_id, now.date())
                stk.check_alerts(client, settings, db, by_id, rows, now)
            except Exception as exc:
                logging.getLogger("stock").error("stock check failed: %s", settings.redact(type(exc).__name__))
            _time.sleep(1800)

    @asynccontextmanager
    async def lifespan(_app):
        threading.Thread(target=loop, daemon=True).start()
        yield

    app.router.lifespan_context = lifespan
    return app
