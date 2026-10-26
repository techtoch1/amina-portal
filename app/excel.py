"""Read ./data/medicines.xlsx into medicines + "needs review" rows.

Nothing is hardcoded: columns are found by header name (English or Arabic
aliases below). Any row that can't be understood unambiguously becomes a
ReviewRow and never gets a reminder.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from openpyxl import load_workbook

# canonical field -> accepted header names (compared after normalisation)
HEADERS = {
    "name": ["name", "medicine", "medication", "drug", "اسم الدواء", "الدواء", "الاسم"],
    "dose": ["dose", "dosage", "الجرعة", "الجرعه"],
    "times": ["time", "times", "time(s)", "الوقت", "الاوقات", "وقت"],
    "frequency": ["frequency", "freq", "schedule", "التكرار", "تكرار"],
    "day": ["day", "days", "weekday", "day of week", "اليوم", "الايام"],
    "start_date": ["start date", "start", "first dose", "next date", "next dose", "تاريخ البدء", "تاريخ البداية"],
    "last_date": ["last date", "last dose", "last taken", "اخر جرعة", "تاريخ اخر جرعة"],
    "instruction": ["instruction", "instructions", "arabic", "بالعربي", "التعليمات", "تعليمات", "الارشادات", "متى يؤخذ"],
    "purpose": ["purpose", "for", "used for", "indication", "what for"],
    "purpose_ar": ["purpose (arabic)", "for (arabic)", "الغرض", "الاستخدام", "لماذا", "دواعي الاستعمال"],
    "form": ["form", "dosage form", "type"],
    "form_ar": ["form (arabic)", "الشكل", "نوع الدواء"],
    "when_en": ["when to take", "when", "how to take", "directions", "how often"],
    "notes_en": ["notes", "note", "comments", "remarks"],
    "notes_ar": ["notes (arabic)", "ملاحظات", "ملاحظه"],
}

WEEKDAYS = {  # python weekday(): Mon=0
    0: ["monday", "mon", "الاثنين", "الاتنين"],
    1: ["tuesday", "tue", "tues", "الثلاثاء"],
    2: ["wednesday", "wed", "الاربعاء"],
    3: ["thursday", "thu", "thur", "thurs", "الخميس"],
    4: ["friday", "fri", "الجمعه"],
    5: ["saturday", "sat", "السبت"],
    6: ["sunday", "sun", "الاحد"],
}

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_TASHKEEL = re.compile("[ً-ٰٟـ]")
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "؛": ";", "،": ","})


def norm(value):
    """Lowercase, ASCII digits, folded Arabic letters, no diacritics, single spaces."""
    if value is None:
        return ""
    text = str(value).translate(_AR_DIGITS)
    text = _TASHKEEL.sub("", text).translate(_AR_FOLD).lower()
    return re.sub(r"[\s_]+", " ", text).strip()


@dataclass
class Medicine:
    row: int
    name: str
    dose: str
    instruction: str
    purpose: str
    kind: str  # daily | weekly | every_n | as_needed | chart_only
    times: list = field(default_factory=list)  # ["08:00", ...]
    weekdays: list = field(default_factory=list)  # python weekdays, for weekly
    every_n: int | None = None
    anchor: date | None = None  # a date the every_n cycle falls on
    when_en: str = ""
    purpose_ar: str = ""
    notes_en: str = ""
    notes_ar: str = ""
    form: str = ""
    remind: bool = False


@dataclass
class ReviewRow:
    row: int
    name: str
    reasons: list
    raw: dict


@dataclass
class ParseResult:
    medicines: list = field(default_factory=list)
    review: list = field(default_factory=list)
    errors: list = field(default_factory=list)  # file-level problems
    mtime: float = 0.0


# ---------- cell parsers ----------

_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|ص|م|صباحا|مساء)?$")


def _fmt(hour, minute):
    return f"{hour:02d}:{minute:02d}"


def parse_times(cell):
    """Return (times, problems). Accepts Excel time cells or text like '8:00, 8pm, 20:30'."""
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return [], ["missing time"]
    if isinstance(cell, (time, datetime)):
        return [_fmt(cell.hour, cell.minute)], []
    if isinstance(cell, (int, float)):
        if 0 <= cell < 1:  # raw Excel day fraction
            minutes = round(cell * 24 * 60)
            return [_fmt(minutes // 60 % 24, minutes % 60)], []
        return [], [f"time '{cell}' is unclear (use HH:MM)"]
    times, problems = [], []
    parts = re.split(r"[,;\n/]|\s+و\s+|\s+and\s+", norm(cell))
    for part in (p.strip() for p in parts):
        if not part:
            continue
        m = _TIME_RE.match(part)
        if not m or not (m.group(2) or m.group(3)):
            problems.append(f"time '{part}' is unclear (use HH:MM)")
            continue
        hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if mer:
            if not 1 <= hour <= 12:
                problems.append(f"time '{part}' is invalid")
                continue
            pm = mer in ("pm", "م", "مساء")
            hour = hour % 12 + (12 if pm else 0)
        if hour > 23 or minute > 59:
            problems.append(f"time '{part}' is invalid")
            continue
        times.append(_fmt(hour, minute))
    if not times and not problems:
        problems.append("missing time")
    return sorted(set(times)), problems


def parse_date(cell):
    """Return (date|None, problem|None). Text dates: ISO, or DD/MM/YYYY."""
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None, None
    if isinstance(cell, datetime):
        return cell.date(), None
    if isinstance(cell, date):
        return cell, None
    text = norm(cell)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", text)
        if not m:
            return None, f"date '{cell}' is unclear (use YYYY-MM-DD)"
        d, mo, y = map(int, m.groups())  # day first
    try:
        return date(y, mo, d), None
    except ValueError:
        return None, f"date '{cell}' is invalid"


def find_weekdays(text):
    found = []
    for wd, names in WEEKDAYS.items():
        if any(re.search(rf"(?<![a-z؀-ۿ]){re.escape(n)}(?![a-z؀-ۿ])", text) for n in names):
            found.append(wd)
    return found


AS_NEEDED = ["as needed", "prn", "when needed", "if needed", "when required", "when in pain", "in pain", "up to",
             "عند الحاجه", "اذا لزم", "حسب الحاجه", "عند اللزوم"]
NO_REMINDER = ["no reminder", "chart only", "بدون تذكير"]
DAILY = ["daily", "every day", "once a day", "everyday", "every morning", "every night", "every evening",
         "in the morning", "in the evening", "at night", "before sleep", "before bed", "times a day",
         "يوميا", "يومي", "كل يوم", "كل صباح", "كل ليله"]
WEEKLY = ["weekly", "every week", "once a week", "اسبوعيا", "اسبوعي", "كل اسبوع"]


def classify_frequency(text):
    """Return (kind, every_n, problems). kind: daily|weekly|every_n|as_needed|None."""
    if not text:
        return None, None, ["missing frequency"]
    if any(k in text for k in NO_REMINDER):
        return "chart_only", None, []
    if any(k in text for k in AS_NEEDED):
        return "as_needed", None, []
    m = re.search(r"every\s+(\d+)\s*(day|days|week|weeks)\b", text) or re.search(
        r"كل\s*(\d+)\s*(يوم|ايام|اسبوع|اسابيع)", text
    )
    if m:
        n = int(m.group(1))
        if m.group(2) in ("week", "weeks", "اسبوع", "اسابيع"):
            n *= 7
        if n < 1:
            return None, None, [f"frequency '{text}' is unclear"]
        return "every_n", n, []
    if any(k in text for k in DAILY):
        return "daily", None, []
    if any(k in text for k in WEEKLY):
        return "weekly", None, []
    return None, None, [f"frequency '{text}' is unclear"]


# ---------- sheet reading ----------

_QTY_WORDS = ("quantity", "quanity", "qty", "stock", "الكميه")


def _column_map(header_cells):
    alias = {norm(a): canon for canon, names in HEADERS.items() for a in names}
    cols = {}
    for idx, cell in enumerate(header_cells):
        canon = alias.get(norm(cell))
        if canon and canon not in cols:
            cols[canon] = idx
        elif "quantity" not in cols and any(w in norm(cell) for w in _QTY_WORDS):
            cols["quantity"] = idx  # e.g. "الكمية  quanity 15/10/2026": the count and the date it was taken
    return cols


def parse_quantity(value):
    """'30' -> (30.0, ''), '30+' -> (30.0, '30+'), '' / '—' -> (None, '')."""
    text = norm(value)
    import re as _re
    m = _re.match(r"^(\d+(?:[.,]\d+)?)\s*(\+?)", text)
    if not m:
        return None, ""
    return float(m.group(1).replace(",", ".")), (m.group(1) + "+" if m.group(2) else "")


def header_count_date(header_text):
    """The date written in the quantity header (DD/MM/YYYY), as ISO, or ''."""
    d, _ = parse_date(norm(header_text).split()[-1]) if norm(header_text) else (None, None)
    if d:
        return d.isoformat()
    import re as _re
    m = _re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", norm(header_text))
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return ""
    return ""


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def parse_row(row_no, rec):
    """rec: canonical field -> raw cell. Returns Medicine or ReviewRow."""
    name = _text(rec.get("name"))
    raw = {k: _text(v) for k, v in rec.items() if v not in (None, "")}
    problems = []
    if not name:
        problems.append("missing medicine name")

    freq_text = norm(rec.get("frequency"))
    day_text = norm(rec.get("day"))
    kind, n, fprob = classify_frequency(freq_text)
    problems += fprob
    if not freq_text and day_text:
        problems.append("frequency is empty (a day is given — is it weekly?)")

    times, anchor, weekdays = [], None, []
    if kind and kind not in ("as_needed", "chart_only"):
        times, tprob = parse_times(rec.get("times"))
        problems += tprob

    if kind == "weekly":
        weekdays = find_weekdays(day_text) or find_weekdays(freq_text)
        if not weekdays:
            problems.append("weekly medicine needs a day of the week")
    elif kind == "every_n":
        start, p1 = parse_date(rec.get("start_date"))
        last, p2 = parse_date(rec.get("last_date"))
        problems += [p for p in (p1, p2) if p]
        if start and last:
            problems.append("both start date and last date given — keep one")
        elif start:
            anchor = start
        elif last:
            anchor = last + timedelta(days=n)
        elif not (p1 or p2):
            problems.append(f"every {n} days needs a start date or last-dose date")

    if problems:
        return ReviewRow(row_no, name, problems, raw)

    if kind == "every_n" and n == 1:
        kind, anchor = "daily", None
    elif kind == "every_n" and n == 7:
        kind, weekdays, anchor = "weekly", [anchor.weekday()], None
        n = None
    return Medicine(
        row=row_no, name=name, dose=_text(rec.get("dose")), instruction=_text(rec.get("instruction")),
        purpose=_text(rec.get("purpose")), kind=kind, times=times, weekdays=weekdays,
        every_n=n if kind == "every_n" else None, anchor=anchor,
        when_en=_text(rec.get("when_en")), purpose_ar=_text(rec.get("purpose_ar")),
        notes_en=_text(rec.get("notes_en")), notes_ar=_text(rec.get("notes_ar")), form=_text(rec.get("form")),
    )


FIELDS = ["name", "dose", "times", "frequency", "day", "start_date", "last_date", "instruction", "purpose",
          "when_en", "purpose_ar", "notes_en", "notes_ar", "form", "form_ar"]


def cell_text(v):
    """Excel cell -> the plain string stored in the database."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%H:%M") if v.year <= 1900 else v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return _fmt(v.hour, v.minute)
    if isinstance(v, float):
        if 0 < v < 1:  # Excel day fraction
            minutes = round(v * 24 * 60)
            return _fmt(minutes // 60 % 24, minutes % 60)
        if v.is_integer():
            v = int(v)
    return str(v).strip()


_ARABIC = re.compile("[\u0600-\u06ff]")
AS_NEEDED_HEADS = ["as needed", "prn", "عند الحاجه"]
REGULAR_HEADS = ["regular", "scheduled", "منتظم"]


def infer_frequency(text):
    """Free text like 'Twice daily (morning & evening)' -> a canonical frequency, or ''."""
    kind, n, _ = classify_frequency(norm(text))
    return {"daily": "daily", "weekly": "weekly", "as_needed": "as needed",
            "every_n": f"every {n} days"}.get(kind, "")


def read_sheet(source):
    """Read the first sheet. source: a path or a file-like object.
    Handles title rows above the header, REGULAR / AS NEEDED section rows and free-text
    'When to take' columns. Returns ([(row_number, rec_of_strings)], [errors])."""
    try:
        wb = load_workbook(source, data_only=True, read_only=True)
    except Exception as exc:  # corrupt / not xlsx / locked
        return [], [f"Could not open the Excel file ({type(exc).__name__}). Is it a .xlsx file?"]
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return [], ["The Excel sheet is empty"]
    head, cols = None, {}
    for i, row in enumerate(rows[:25]):
        found = _column_map(row)
        if "name" in found and len(found) >= 2:
            head, cols = i, found
            break
    if head is None:
        return [], ["Couldn't find the header row. It needs a 'Medication' (or Name) column plus at least one more, e.g. Dose or When to take"]
    out, section = [], None
    count_date = header_count_date(cell_text(rows[head][cols["quantity"]])) if "quantity" in cols and cols["quantity"] < len(rows[head]) else ""
    for offset, cells in enumerate(rows[head + 1:], start=head + 2):
        rec = {f: cell_text(cells[cols[f]] if f in cols and cols[f] < len(cells) else None) for f in FIELDS}
        if not any(rec.values()):
            continue
        if rec["name"] and not any(v for k, v in rec.items() if k != "name"):
            low = norm(rec["name"])  # a section title or a footnote, not a medicine
            if any(k in low for k in AS_NEEDED_HEADS):
                section = "as_needed"
                continue
            if any(k in low for k in REGULAR_HEADS):
                section = "regular"
                continue
            if low.startswith(("note", "ملاحظ")) or len(low) > 60:
                continue
        if rec["dose"] in ("—", "–", "-"):
            rec["dose"] = ""
        if rec["notes_en"] and _ARABIC.search(rec["notes_en"]) and not re.search("[A-Za-z]", rec["notes_en"]):
            rec["notes_ar"], rec["notes_en"] = rec["notes_ar"] or rec["notes_en"], ""  # Arabic-only note
        if not rec["frequency"]:
            # what the text says wins; the section heading is only the fallback
            rec["frequency"] = infer_frequency(rec["when_en"]) if rec["when_en"] else ""
            if not rec["frequency"] and section == "as_needed":
                rec["frequency"] = "as needed"
        if "quantity" in cols and cols["quantity"] < len(cells):  # stock count, kept outside FIELDS (own tables)
            rec["_quantity"], rec["_count_date"] = cell_text(cells[cols["quantity"]]), count_date
        out.append((offset, rec))
    return out, []


def load_medicines(path):
    """Parse a spreadsheet straight into medicines + review rows (used by tests/CLI)."""
    path = Path(path)
    result = ParseResult()
    if not path.exists():
        result.errors.append(f"Excel file not found: {path.name}")
        return result
    rows, result.errors = read_sheet(path)
    for row_no, rec in rows:
        item = parse_row(row_no, rec)
        (result.medicines if isinstance(item, Medicine) else result.review).append(item)
    return result


def load_from_db(db):
    """Medicines stored in the app database -> (ParseResult, stopped rows)."""
    result, stopped = ParseResult(), []
    for r in db.list_medicines():
        if not r["active"]:
            stopped.append(r)
            continue
        item = parse_row(r["id"], {f: r[f] for f in FIELDS})
        if isinstance(item, Medicine):
            item.remind = bool(r.get("remind"))
        (result.medicines if isinstance(item, Medicine) else result.review).append(item)
    return result, stopped


# ---------- form <-> stored record ----------

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
TEXT_LIMITS = {"name": 120, "dose": 80, "form": 60, "form_ar": 60, "when_en": 300, "purpose": 200, "notes_en": 500,
               "instruction": 300, "purpose_ar": 200, "notes_ar": 500}


def details_from_form(data, require_name=True):
    """The descriptive fields (everything except the reminder schedule). Returns (dict, error)."""
    out = {}
    for key, limit in TEXT_LIMITS.items():
        if key in data:
            out[key] = str(data.get(key) or "").strip()[:limit]
    if require_name and not out.get("name"):
        return None, "Medicine name is required"
    return out, None


def schedule_to_rec(data):
    """Strictly validated reminder schedule. Returns (fields, error)."""
    freq = str(data.get("frequency") or "").strip()
    if freq not in ("daily", "weekly", "every_n", "as_needed", "chart_only"):
        return None, "Choose how often the medicine is taken"
    times = []
    if freq not in ("as_needed", "chart_only"):
        for t in data.get("times") or []:
            if not isinstance(t, str) or not _HHMM.match(t):
                return None, f"'{t}' is not a valid time"
            times.append(t)
        if not times:
            return None, "Add at least one time"
        times = sorted(set(times))
    rec = {"times": ", ".join(times), "frequency": "", "day": "", "start_date": "", "last_date": ""}
    if freq == "daily":
        rec["frequency"] = "daily"
    elif freq == "as_needed":
        rec["frequency"] = "as needed"
    elif freq == "chart_only":
        rec["frequency"] = "chart only"
    elif freq == "weekly":
        days = sorted({d for d in (data.get("weekdays") or []) if isinstance(d, int) and 0 <= d <= 6})
        if not days:
            return None, "Choose at least one day of the week"
        rec["frequency"], rec["day"] = "weekly", ", ".join(DAY_NAMES[d] for d in days)
    else:
        try:
            n = int(data.get("every_n"))
        except (TypeError, ValueError):
            return None, "Enter how many days between doses"
        if not 1 <= n <= 365:
            return None, "Days between doses must be 1-365"
        try:
            d = date.fromisoformat(str(data.get("date") or ""))
        except ValueError:
            return None, "Choose the date of the next (or last) dose"
        rec["frequency"] = f"every {n} days"
        rec["last_date" if data.get("date_kind") == "last" else "start_date"] = d.isoformat()
    return rec, None


def form_to_rec(data):
    """Details + schedule in one go (used when both are submitted together)."""
    details, err = details_from_form(data)
    if err:
        return None, err
    sched, err = schedule_to_rec(data)
    if err:
        return None, err
    return {**{k: "" for k in TEXT_LIMITS}, **details, **sched}, None


def rec_to_form(rec):
    """Stored record -> values for the edit/reminder forms (best effort for imported rows)."""
    kind, n, _ = classify_frequency(norm(rec.get("frequency")))
    times, _p = parse_times(rec.get("times") or None)
    out = {k: rec.get(k, "") for k in TEXT_LIMITS}
    out.update({
        "times": times, "frequency": kind or "", "every_n": n or "",
        "weekdays": find_weekdays(norm(rec.get("day"))) or (find_weekdays(norm(rec.get("frequency"))) if kind == "weekly" else []),
        "date": rec.get("start_date") or rec.get("last_date") or "",
        "date_kind": "last" if (rec.get("last_date") and not rec.get("start_date")) else "start",
        "remind": bool(rec.get("remind")),
    })
    return out
