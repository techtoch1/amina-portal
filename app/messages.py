"""Arabic WhatsApp message text. Short: a header, then one block per medicine."""

HEADER = "تذكير: حان وقت الدواء 💊"
COPY_NOTE = "📋 نسخة للمتابعة"


def build_message(meds, copy=False):
    lines = [COPY_NOTE, HEADER] if copy else [HEADER]
    for m in sorted(meds, key=lambda m: m.name):
        lines.append("")
        lines.append(f"• {m.name} — {m.dose}" if m.dose else f"• {m.name}")
        if m.instruction:
            lines.append(f"   {m.instruction}")
    return "\n".join(lines)
