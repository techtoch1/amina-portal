"""Arabic WhatsApp message text. Short: a header, then one block per medicine."""

HEADER = "تذكير: حان وقت الدواء 💊"
COPY_NOTE = "📋 نسخة للمتابعة"


PURPOSE_LABEL = "الغرض:"


def build_message(meds, copy=False, with_purpose=True):
    lines = [COPY_NOTE, HEADER] if copy else [HEADER]
    for m in sorted(meds, key=lambda m: m.name):
        lines.append("")
        lines.append(f"• {m.name} — {m.dose}" if m.dose else f"• {m.name}")
        if m.instruction:
            lines.append(f"   {m.instruction}")
        if with_purpose and m.purpose_ar:  # Arabic only: never put English text in Mom's message
            lines.append(f"   {PURPOSE_LABEL} {m.purpose_ar}")
    return "\n".join(lines)
