import sys
from datetime import time
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402

HEADER = ["Name", "Dose", "Time", "Frequency", "Day", "Start date", "Instruction", "Purpose"]
ROWS = [
    ["MedA", "1 tablet", time(8, 0), "Daily", None, None, "بعد الفطور", "Pressure"],
    ["MedB", "2 tablets", "08:00, 20:00", "daily", None, None, "مع الطعام", "Sugar"],
    ["MedC", "1", time(9, 0), "Weekly", "Friday", None, "على معدة فارغة", "Bones"],
    ["Shot", "1 ml", "10:00", "Every 28 days", None, "2026-10-20", "حقنة تحت الجلد", "Hormone"],
    ["Pain", "500mg", None, "As needed", None, None, "عند الألم", "Pain"],
    ["Bad1", "1", None, "Daily", None, None, "", ""],
    ["Bad2", "1", "09:00", "sometimes", None, None, "", ""],
    ["Bad3", "1", "09:00", "Every 14 days", None, None, "", ""],
    [None] * 8,
]


@pytest.fixture
def xlsx(tmp_path):
    p = tmp_path / "medicines.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    wb.save(p)
    return p


@pytest.fixture
def settings(tmp_path):
    s = Settings(client_id=7, api_key="SECRETKEY123", whatsapp_client="42", mom_number="96170000001",
                 my_number="96170000002", dashboard_password="correct-horse-battery", secret_key="k" * 32,
                 cookie_secure=False, docs_verified=True, excel_path=tmp_path / "medicines.xlsx", data_dir=tmp_path)
    return s
