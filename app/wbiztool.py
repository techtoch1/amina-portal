"""Thin Wbiztool API client.

!! UNVERIFIED !!  The paths and request/response field names below follow the
brief, but the official docs (https://wbiztool.com/docs/) were not reachable
when this was written. Check every entry in PATHS and FIELDS against the docs,
fix what differs, then set WBIZ_DOCS_VERIFIED=true. Until then every call that
creates, cancels or sends something is refused. (/me/ and read-only status
calls are always allowed, so you can test credentials first.)

Rules baked in:
  * auth (client_id as int, api_key, whatsapp_client) goes in the JSON body
  * every other value is sent as a string (never None / number -> avoids HTML 500)
  * the body is always parsed and "status" checked (1 = success, 0 = failed)
  * non-JSON bodies (e.g. HTML error pages) raise WbizError, never crash
  * the API key is never logged or included in an error message
"""
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("wbiztool")

PATHS = {
    "me": "/me/",
    "client_status": "/whatsapp_client/status/",
    "reminder_create": "/reminder/create/",
    "reminder_cancel": "/reminder/cancel/",
    "reminder_list": "/reminder/list/",
    "schedule_msg": "/schedule_msg/",
    "schedule_cancel": "/schedule_msg/cancel/",  # UNVERIFIED guess
    "send_msg": "/send_msg/",  # UNVERIFIED guess (immediate send, used for the test button)
}

FIELDS = {
    "name": "name",  # confirmed needed: "Reminder name cannot be null"
    "phone": "phone",
    "message": "message_template",  # confirmed needed: "Message template cannot be null"
    "cron": "cron",
    "timezone": "timezone",
    "schedule_time": "schedule_time",  # "YYYY-MM-DD HH:MM" in `timezone`
    "reminder_id": "reminder_id",
    "message_id": "msg_id",
}
# Wbiztool validates one missing field at a time and ignores keys it doesn't know (it accepted extra
# guessed keys without complaint), so each value is also sent under its other likely names. Once the
# docs have been checked, trim these lists to the names that are really used.
ALIASES = {
    "name": ("reminder_name", "title"),
    "phone": ("phone_number", "mobile", "recipient", "to"),
    "message": ("template", "msg", "message"),
    "cron": ("cron_expression", "schedule"),
    "timezone": ("time_zone", "tz"),
    # "Scheduled date & time is not in valid format" is also what an EMPTY date gives, i.e. the field name
    # may simply be wrong. Send the date under all the likely names.
    "schedule_time": ("scheduled_time", "scheduled_at", "schedule_at", "scheduled_date_time", "schedule_date_time",
                      "scheduled_datetime", "schedule_datetime", "datetime", "date_time", "send_at", "send_time",
                      "scheduled_date", "schedule_date"),
}
# Wbiztool answered "Scheduled date & time is not in valid format" for "2026-10-29 08:00", but never says
# which format it wants. A rejected date creates nothing, so the formats are tried in turn until one is
# accepted; the winner is remembered. "epoch" = Unix seconds for the local time.
SCHEDULE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
                    "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %I:%M %p", "%d-%m-%Y %I:%M %p",
                    "iso_tz", "epoch", "epoch_ms"]
# keys searched (recursively) in a response to find the id of a created item
ID_KEYS = ("reminder_id", "msg_id", "message_id", "id")


class WbizError(Exception):
    pass


def _s(value):
    """Everything except client_id goes out as a string; None becomes ''."""
    return "" if value is None else str(value)


def _find_id(data):
    if isinstance(data, dict):
        for key in ID_KEYS:
            if data.get(key) not in (None, ""):
                return str(data[key])
        for v in data.values():
            found = _find_id(v)
            if found:
                return found
    elif isinstance(data, list):
        for v in data:
            found = _find_id(v)
            if found:
                return found
    return ""


class WbizClient:
    def __init__(self, settings, session=None):
        self.s = settings
        self.http = session or requests.Session()
        self.remember = None  # optional callback(format) to persist the accepted date format

    # ---- plumbing ----
    def _require_config(self):
        if not self.s.wbiz_configured:
            raise WbizError("Wbiztool credentials are not set in .env")

    def _require_verified(self):
        if not self.s.docs_verified:
            raise WbizError(
                "Blocked: endpoint paths in app/wbiztool.py are not verified against the Wbiztool docs yet "
                "(set WBIZ_DOCS_VERIFIED=true after checking)"
            )

    def _post(self, path, fields=None):
        self._require_config()
        body = {
            "client_id": int(self.s.client_id),
            "api_key": _s(self.s.api_key),
            "whatsapp_client": _s(self.s.whatsapp_client),
        }
        body.update({k: _s(v) for k, v in (fields or {}).items()})
        url = self.s.base_url.rstrip("/") + path
        try:
            resp = self.http.post(url, json=body, timeout=25)
        except requests.RequestException as exc:
            raise WbizError(f"Network error calling {path}: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            snippet = re.sub(r"<[^>]+>|\s+", " ", resp.text or "").strip()[:160]
            raise WbizError(self.s.redact(f"Non-JSON response from {path} (HTTP {resp.status_code}): {snippet}")) from None
        if not isinstance(data, dict):
            raise WbizError(f"Unexpected response from {path} (HTTP {resp.status_code})")
        if str(data.get("status")) not in ("1", "True", "true"):
            msg = data.get("message") or data.get("msg") or data.get("error") or "no message"
            raise WbizError(self.s.redact(f"{path} failed (status={data.get('status')}): {msg}"))
        log.info("wbiztool %s ok", path)
        return data

    def probe(self, path):
        """Read-only diagnostic: POST the auth body to `path`, never raise.
        Returns (http_status, json_status_or_None, message_snippet)."""
        self._require_config()
        body = {"client_id": int(self.s.client_id), "api_key": _s(self.s.api_key),
                "whatsapp_client": _s(self.s.whatsapp_client)}
        try:
            resp = self.http.post(self.s.base_url.rstrip("/") + path, json=body, timeout=20)
        except requests.RequestException as exc:
            return None, None, type(exc).__name__
        try:
            data = resp.json()
        except ValueError:
            return resp.status_code, None, "not JSON (page not found?)" if resp.status_code == 404 else "not JSON"
        text = self.s.redact(str(data))[:160] if not isinstance(data, dict) else self.s.redact(
            str(data.get("message") or data.get("data") or data))[:160]
        return resp.status_code, (data.get("status") if isinstance(data, dict) else None), text

    # ---- read-only (always allowed) ----
    def me(self):
        return self._post(PATHS["me"])

    def client_status(self):
        return self._post(PATHS["client_status"])

    def list_reminders(self):
        return self._post(PATHS["reminder_list"])

    # ---- mutating (blocked until docs verified) ----
    @staticmethod
    def _fields(**logical):
        """{'phone': '9617..', 'message': '..'} -> every name each value is sent under."""
        out = {}
        for key, value in logical.items():
            for field in (FIELDS.get(key, key),) + ALIASES.get(key, ()):
                out.setdefault(field, value)
        return out

    def create_reminder(self, phone, message, cron, tz, name=""):
        self._require_verified()
        data = self._post(PATHS["reminder_create"], self._fields(
            name=name or "Medicine reminder", phone=phone, message=message, cron=cron, timezone=tz))
        return _find_id(data)

    def cancel_reminder(self, remote_id):
        self._require_verified()
        self._post(PATHS["reminder_cancel"], {FIELDS["reminder_id"]: remote_id})

    def _format_schedule(self, fire_at, fmt, tz):
        when = datetime.strptime(fire_at, "%Y-%m-%d %H:%M")
        local = when.replace(tzinfo=ZoneInfo(tz))
        if fmt == "epoch":
            return str(int(local.timestamp()))
        if fmt == "epoch_ms":
            return str(int(local.timestamp()) * 1000)
        if fmt == "iso_tz":
            return local.isoformat(timespec="seconds")  # 2026-10-29T08:00:00+02:00
        return when.strftime(fmt)

    def schedule_message(self, phone, message, fire_at, tz, name=""):
        """fire_at: 'YYYY-MM-DD HH:MM' local time. Tries date formats until Wbiztool accepts one."""
        self._require_verified()
        preferred = getattr(self.s, "schedule_format", "")
        order = ([preferred] if preferred in SCHEDULE_FORMATS else []) + [f for f in SCHEDULE_FORMATS if f != preferred]
        tried, last = [], None
        for fmt in order:
            try:
                data = self._post(PATHS["schedule_msg"], self._fields(
                    name=name or "Medicine reminder", phone=phone, message=message,
                    schedule_time=self._format_schedule(fire_at, fmt, tz), timezone=tz))
            except WbizError as exc:
                if "format" not in str(exc).lower():  # a different problem: retrying the date can't help
                    raise
                tried.append(fmt)
                last = exc
                continue
            if fmt != preferred:
                self.s.schedule_format = fmt
                if self.remember:
                    self.remember(fmt)
                log.info("wbiztool accepted schedule date format %s", fmt)
            return _find_id(data)
        raise WbizError(f"{last} (tried {len(tried)} date formats: " + ", ".join(tried) + ")")

    def cancel_scheduled(self, remote_id):
        self._require_verified()
        self._post(PATHS["schedule_cancel"], {FIELDS["message_id"]: remote_id})

    def send_message(self, phone, message):
        self._require_verified()
        return self._post(PATHS["send_msg"], self._fields(phone=phone, message=message))
