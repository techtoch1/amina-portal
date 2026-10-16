"""python -m app.cli check  -> inspect the Wbiztool settings (never prints the key) and call /me/
   python -m app.cli me     -> just call /me/
   python -m app.cli plan   -> print every reminder a sync would create (no network)
   python -m app.cli import FILE.xlsx -> add medicines from an Excel file

On the server run it as the service user with the saved settings, e.g.
   sudo -u amina ENV_FILE=/etc/amina.env /opt/amina/.venv/bin/python -m app.cli check"""
import sys

from .config import Settings
from .db import DB
from .excel import load_from_db, read_sheet
from .sync import compute_plan
from .wbiztool import WbizClient, WbizError


def _key_report(s):
    k = s.api_key
    if not k:
        return ["api_key: EMPTY"]
    out = [f"api_key: {len(k)} characters"]
    if any(c.isspace() for c in k):
        out.append("  ! contains whitespace — it was probably pasted with extra characters")
    if any(c in k for c in "\"'`"):
        out.append("  ! contains quote characters — check it was copied exactly")
    if not k.isascii():
        out.append("  ! contains non-ASCII characters — copied from a formatted page?")
    return out


def main(argv):
    s = Settings.from_env()
    cmd = argv[:1]
    if cmd in (["me"], ["check"]):
        if cmd == ["check"]:
            print(f"client_id: {s.client_id if s.client_id else 'MISSING or not a number'}")
            print(f"whatsapp_client: {s.whatsapp_client or 'MISSING'}")
            print("\n".join(_key_report(s)))
            print(f"docs verified flag: {s.docs_verified}")
        try:
            print("/me/ OK:", WbizClient(s).me())
        except WbizError as exc:
            print("/me/ FAILED:", exc)
            return 1
        return 0
    if cmd == ["plan"]:
        res, _stopped = load_from_db(DB(s.db_path))
        for r in res.review:
            print(f"NEEDS REVIEW #{r.row} ({r.name or '?'}): {'; '.join(r.reasons)}")
        plan = compute_plan(s, DB(s.db_path), res.medicines)
        print(f"\n{len(plan['create'])} to create, {len(plan['cancel'])} to cancel, {len(plan['keep'])} unchanged\n")
        for d in plan["create"]:
            print(f"--- CREATE [{d.recipient}] {d.kind} {d.when}\n{d.message}\n")
        for r in plan["cancel"]:
            print(f"--- CANCEL [{r['recipient']}] {r['cron'] or r['fire_at']}")
        return 0
    if cmd == ["import"] and len(argv) == 2:
        rows, errors = read_sheet(argv[1])
        for e in errors:
            print("ERROR:", e)
        if errors:
            return 1
        db = DB(s.db_path)
        for _row, rec in rows:
            db.add_medicine(rec)
        print(f"Imported {len(rows)} row(s). Rows that need fixing are flagged in the Medicines tab.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
