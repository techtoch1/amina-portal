"""Turn the 'When to take' wording into a suggested reminder schedule.

Only a suggestion: the user sees it, can edit it, and nothing is sent to Wbiztool
until they approve the full list. Clock times come from the user's own
'default times' (Settings), never hardcoded medical advice.
"""
import re

from .excel import norm

DEFAULT_SLOTS = {"morning": "08:00", "noon": "13:00", "evening": "20:00", "night": "22:00"}
SLOT_LABELS = {"morning": "Morning", "noon": "Noon", "evening": "Evening", "night": "Bedtime"}

_KEYWORDS = {
    "morning": ["morning", "breakfast", "صباح", "الفطور"],
    "noon": ["noon", "midday", "lunch", "afternoon", "ظهر", "الغداء"],
    "evening": ["evening", "dinner", "supper", "مساء", "العشاء"],
    "night": ["night", "bedtime", "before sleep", "before bed", "at bedtime", "ليل", "النوم"],
}
_DEFAULT_BY_COUNT = {1: ["morning"], 2: ["morning", "evening"], 3: ["morning", "noon", "evening"],
                     4: ["morning", "noon", "evening", "night"]}


def _dose_count(t):
    if "مرتين" in t or re.search(r"\b(twice|2 times|two times|2x|2 a day)\b", t):
        return 2
    if "ثلاث مرات" in t or re.search(r"\b(3|three) times\b|\b3x\b", t) or re.search(r"3 مرات", t):
        return 3
    if re.search(r"\b(4|four) times\b|\b4x\b", t) or re.search(r"4 مرات", t):
        return 4
    m = re.search(r"every (\d+) hours?", t)
    if m and int(m.group(1)) > 0:
        return max(1, min(4, round(24 / int(m.group(1)))))
    return 1


def suggest_times(when_en, instruction, slots=None):
    """-> sorted list of 'HH:MM' strings (may be empty if the text has no timing at all)."""
    slots = {**DEFAULT_SLOTS, **(slots or {})}
    text = norm(f"{when_en or ''} {instruction or ''}")
    if not text:
        return []
    found = [slot for slot, words in _KEYWORDS.items() if any(w in text for w in words)]
    count = _dose_count(text)
    chosen = found if found and len(found) >= count else _DEFAULT_BY_COUNT.get(count, ["morning"])
    if found and len(found) < count:  # e.g. "3 times daily, one in the morning": keep the named one, fill the rest
        chosen = list(dict.fromkeys(found + _DEFAULT_BY_COUNT[count]))[:count]
    return sorted({slots[s] for s in chosen})


def suggestion(form, slots=None):
    """form: output of rec_to_form. Returns {'frequency', 'times', 'complete'}.
    complete = a daily schedule with times, so the bell can switch on in one click."""
    kind = form.get("frequency") or ""
    if kind in ("as_needed", "chart_only", ""):
        kind = "daily" if kind == "" and (form.get("when_en") or form.get("instruction")) else kind
    times = form.get("times") or suggest_times(form.get("when_en"), form.get("instruction"), slots)
    if kind in ("as_needed", "chart_only") or not times:
        return {"frequency": kind if kind in ("daily", "weekly", "every_n") else "", "times": times, "complete": False}
    complete = kind == "daily"
    return {"frequency": kind, "times": times, "complete": complete}
