"""Settings loaded from environment / .env. Secrets never appear in repr()."""
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(value, default):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _clean(value):
    """Trim whitespace and one pair of surrounding quotes (a common paste/EnvironmentFile mishap)."""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _digits(value):
    return "".join(ch for ch in (value or "") if ch.isdigit())


@dataclass
class Settings:
    client_id: int | None = None
    api_key: str = field(default="", repr=False)
    whatsapp_client: str = ""
    mom_number: str = field(default="", repr=False)
    my_number: str = field(default="", repr=False)
    dashboard_password: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    timezone: str = "Asia/Beirut"
    copy_to_me_default: bool = True
    excel_path: Path = BASE_DIR / "data" / "medicines.xlsx"
    data_dir: Path = BASE_DIR / "data"
    cookie_secure: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    docs_verified: bool = False
    base_url: str = "https://wbiztool.com/api/v1"

    @property
    def wbiz_configured(self):
        return bool(self.client_id and self.api_key and self.whatsapp_client)

    @property
    def db_path(self):
        return self.data_dir / "reminders.sqlite3"

    @classmethod
    def from_env(cls, load_file=True):
        if load_file:
            load_dotenv(os.environ.get("ENV_FILE") or BASE_DIR / ".env")
        e = os.environ.get
        raw_id = _clean(e("WBIZ_CLIENT_ID"))
        s = cls(
            client_id=int(raw_id) if raw_id.isdigit() else None,
            api_key=_clean(e("WBIZ_API_KEY")),
            whatsapp_client=_clean(e("WBIZ_WHATSAPP_CLIENT")),
            mom_number=_digits(e("MOM_NUMBER")),
            my_number=_digits(e("MY_NUMBER")),
            dashboard_password=e("DASHBOARD_PASSWORD") or "",
            secret_key=e("SECRET_KEY") or "",
            timezone=e("TIMEZONE") or "Asia/Beirut",
            copy_to_me_default=_bool(e("SEND_COPY_TO_ME"), True),
            cookie_secure=_bool(e("COOKIE_SECURE"), True),
            host=e("HOST") or "127.0.0.1",
            port=int(e("PORT") or 8000),
            docs_verified=_bool(e("WBIZ_DOCS_VERIFIED"), False),
        )
        xl = Path(e("EXCEL_PATH") or "data/medicines.xlsx")
        s.excel_path = xl if xl.is_absolute() else BASE_DIR / xl
        s.data_dir = Path(e("DATA_DIR") or BASE_DIR / "data")
        s.data_dir.mkdir(parents=True, exist_ok=True)
        if not s.secret_key:
            keyfile = s.data_dir / ".secret_key"
            if keyfile.exists():
                s.secret_key = keyfile.read_text().strip()
            else:
                s.secret_key = secrets.token_hex(32)
                keyfile.write_text(s.secret_key)
                keyfile.chmod(0o600)
        return s

    def validate(self):
        """Hard requirements to start the web app."""
        if len(self.dashboard_password) < 10:
            raise SystemExit("DASHBOARD_PASSWORD must be set in .env (at least 10 characters).")

    def redact(self, text):
        """Strip secrets from any string before it is logged or stored."""
        text = str(text)
        for secret in (self.api_key, self.dashboard_password, self.secret_key):
            if secret:
                text = text.replace(secret, "***")
        return text
