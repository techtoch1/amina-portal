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
    "name": "name",  # Wbiztool answered "Reminder name cannot be null" when this was missing
    "phone": "phone",
    "message": "msg",
    "cron": "cron",
    "timezone": "timezone",
    "schedule_time": "schedule_time",  # "YYYY-MM-DD HH:MM" in `timezone`
    "reminder_id": "reminder_id",
    "message_id": "msg_id",
}
# Extra keys that carry the same name. The exact field name isn't confirmed (the error only says
# "Reminder name"), and unknown extra string fields are normally ignored, so both are sent.
NAME_ALIASES = ("reminder_name",)
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
    def _named(self, fields, name):
        fields[FIELDS["name"]] = name
        for alias in NAME_ALIASES:
            fields[alias] = name
        return fields

    def create_reminder(self, phone, message, cron, tz, name=""):
        self._require_verified()
        data = self._post(PATHS["reminder_create"], self._named({
            FIELDS["phone"]: phone, FIELDS["message"]: message, FIELDS["cron"]: cron, FIELDS["timezone"]: tz,
        }, name or "Medicine reminder"))
        return _find_id(data)

    def cancel_reminder(self, remote_id):
        self._require_verified()
        self._post(PATHS["reminder_cancel"], {FIELDS["reminder_id"]: remote_id})

    def schedule_message(self, phone, message, fire_at, tz, name=""):
        self._require_verified()
        data = self._post(PATHS["schedule_msg"], self._named({
            FIELDS["phone"]: phone, FIELDS["message"]: message,
            FIELDS["schedule_time"]: fire_at, FIELDS["timezone"]: tz,
        }, name or "Medicine reminder"))
        return _find_id(data)

    def cancel_scheduled(self, remote_id):
        self._require_verified()
        self._post(PATHS["schedule_cancel"], {FIELDS["message_id"]: remote_id})

    def send_message(self, phone, message):
        self._require_verified()
        return self._post(PATHS["send_msg"], {FIELDS["phone"]: phone, FIELDS["message"]: message})
