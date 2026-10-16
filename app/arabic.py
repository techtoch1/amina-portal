"""Fill missing Arabic details from a small, reviewed glossary.

Only ever fills EMPTY Arabic fields and never guesses: text that is not in the
glossary is reported back so a person can type it. Everything it writes stays
editable in the medicine form.
"""
import re

_ARABIC = re.compile("[؀-ۿ]")
_LATIN = re.compile("[A-Za-z]")

FORMS = {
    "tablet": "أقراص", "tablets": "أقراص", "injection": "حقنة", "supplement": "مكمل غذائي",
    "nutrition drink": "مشروب غذائي", "capsule": "كبسولات", "capsules": "كبسولات", "syrup": "شراب",
    "drops": "نقط", "cream": "كريم", "ointment": "مرهم", "sachet": "أكياس", "patch": "لصقة",
    "inhaler": "بخاخ", "suppository": "تحاميل", "powder": "مسحوق", "gel": "جل", "spray": "بخاخ",
}

PURPOSES = {
    "appetite": "الشهية", "weight": "الوزن", "pain": "ألم", "nutrition": "التغذية", "calories": "السعرات الحرارية",
    "diarrhoea": "الإسهال", "diarrhea": "الإسهال", "symptom control": "السيطرة على الأعراض",
    "antiviral": "مضاد للفيروسات", "vitamin d": "فيتامين د", "vitamin": "فيتامين", "magnesium": "المغنيسيوم",
    "stomach protection": "حماية المعدة", "nausea": "الغثيان", "abdominal pain": "ألم البطن", "stomach": "المعدة",
    "anaemia": "فقر الدم", "anemia": "فقر الدم", "blood pressure": "ضغط الدم", "cholesterol": "الكوليسترول",
    "diabetes": "السكري", "infection": "العدوى", "antibiotic": "مضاد حيوي", "constipation": "الإمساك",
    "headache": "الصداع", "fever": "الحمى", "allergy": "الحساسية", "sleep": "النوم", "calcium": "الكالسيوم",
    "iron": "الحديد", "heart": "القلب", "thyroid": "الغدة الدرقية", "reflux": "الارتجاع", "cough": "السعال",
    "bones": "العظام", "vomiting": "التقيؤ", "indigestion": "عسر الهضم", "spasms": "التشنجات",
}

NOTES = {
    "as written on prescription (800 mg/day); confirm dose with clinic":
        "كما هو مكتوب في الوصفة (800 ملغ/يوم)؛ تأكد من الجرعة مع العيادة",
    "contains paracetamol: do not take with panadol or other paracetamol":
        "يحتوي على باراسيتامول: لا تتناوله مع بانادول أو أي دواء آخر يحتوي على باراسيتامول",
    "between meals": "بين الوجبات", "essential; do not skip": "ضروري؛ لا تفوّت الجرعة",
    "take with food": "يؤخذ مع الطعام", "after meals": "بعد الأكل", "before meals": "قبل الأكل",
    "on an empty stomach": "على معدة فارغة", "do not skip": "لا تفوّت الجرعة", "confirm dose with clinic": "تأكد من الجرعة مع العيادة",
}

_MONTHS = {"jan": "كانون الثاني", "feb": "شباط", "mar": "آذار", "apr": "نيسان", "may": "أيار", "jun": "حزيران",
           "jul": "تموز", "aug": "آب", "sep": "أيلول", "oct": "تشرين الأول", "nov": "تشرين الثاني", "dec": "كانون الأول"}
_UNITS = [(r"(?<![A-Za-z])(?:mcg|µg|ug)(?![A-Za-z])", "ميكروغرام"), (r"(?<![A-Za-z])mg(?![A-Za-z])", "ملغ"),
          (r"(?<![A-Za-z])ml(?![A-Za-z])", "مل"), (r"(?<![A-Za-z])IU(?![A-Za-z])", "وحدة دولية"), (r"(?<![A-Za-z])g(?![A-Za-z])", "غ")]


def _key(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".")


def has_arabic_only(text):
    return bool(text) and bool(_ARABIC.search(text)) and not _LATIN.search(text)


def split_mixed(text):
    """'Same medicine as X: use one الغثيان' -> ('Same medicine as X: use one', 'الغثيان')."""
    ar = " ".join(_ARABIC_RUN.findall(text or ""))
    en = re.sub(r"\s+", " ", _ARABIC_RUN.sub(" ", text or "")).strip(" -–—,;")
    return en, ar


_ARABIC_RUN = re.compile(r"[؀-ۿ][؀-ۿ\s\-ـ،]*[؀-ۿ]|[؀-ۿ]")


def translate_form(text):
    return FORMS.get(_key(text), "")


def translate_purpose(text):
    k = _key(text)
    if k in PURPOSES:
        return PURPOSES[k]
    parts = [p.strip() for p in re.split(r"\s*/\s*|\s*,\s*|\s+and\s+", k) if p.strip()]
    out = [PURPOSES.get(p) for p in parts]
    return " / ".join(out) if parts and all(out) else ""


def translate_notes(text):
    en, ar = split_mixed(text) if _ARABIC.search(text or "") else (text, "")
    k = _key(en)
    base = NOTES.get(k, "")
    if not base:
        m = re.match(r"same medicine as (.+?): use one, not both$", k)
        if m:
            name = re.sub(r"\s+", " ", en.split("as", 1)[1].split(":")[0]).strip()  # keep the brand's original spelling
            base = f"نفس الدواء مثل {name}: استخدم واحداً فقط وليس كليهما"
        m = re.match(r"started (\d{1,2}) ([a-z]{3})[a-z]* (\d{4})$", k)
        if m and m.group(2) in _MONTHS:
            base = f"بدأ بتاريخ {int(m.group(1))} {_MONTHS[m.group(2)]} {m.group(3)}"
    if not base:
        return ""
    return f"{base} — {ar}" if ar else base


def arabic_dose(dose):
    """'10,000 IU' -> '10,000 وحدة دولية' (display only)."""
    out = dose or ""
    for pattern, ar in _UNITS:
        out = re.sub(pattern, ar, out, flags=re.IGNORECASE if ar != "وحدة دولية" else 0)
    return out


FIELD_LABELS = {"form_ar": "form", "purpose_ar": "what it is for", "notes_ar": "notes", "instruction": "how to take"}


def fill_row(row):
    """-> (updates, filled, missing). `row` is a medicines table row (dict).
    filled: [(field, arabic)]; missing: [(field, english_text)] that a person must type."""
    updates, filled = {}, []
    cur = dict(row)

    def put(field, value):
        updates[field] = value
        cur[field] = value

    # 1) Arabic text that was typed or imported into an English field -> move it
    for en_field, ar_field in (("notes_en", "notes_ar"), ("purpose", "purpose_ar"), ("form", "form_ar")):
        if has_arabic_only(cur.get(en_field)) and not cur.get(ar_field):
            put(ar_field, cur[en_field].strip())
            put(en_field, "")
            filled.append((ar_field, cur[ar_field]))
    # 2) translate what the glossary knows, only into empty fields
    for ar_field, en_field, fn in (("form_ar", "form", translate_form), ("purpose_ar", "purpose", translate_purpose),
                                   ("notes_ar", "notes_en", translate_notes)):
        if cur.get(en_field) and not cur.get(ar_field):
            value = fn(cur[en_field])
            if value:
                put(ar_field, value)
                filled.append((ar_field, value))
    return updates, filled, gaps(cur)


def gaps(row):
    out = []
    for ar_field, en_field in (("form_ar", "form"), ("purpose_ar", "purpose"), ("notes_ar", "notes_en"), ("instruction", "when_en")):
        if row.get(en_field) and not row.get(ar_field):
            out.append((ar_field, row[en_field]))
    return out
