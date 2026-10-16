"""python -m app.cli me   -> test credentials via /me/ (do this first)
   python -m app.cli plan -> print the full reminder list that a sync would create (no network)"""
import sys

from .config import Settings
from .db import DB
from .excel import load_medicines
from .sync import compute_plan
from .wbiztool import WbizClient, WbizError


def main(argv):
    s = Settings.from_env()
    if argv[:1] == ["me"]:
        try:
            print("OK:", WbizClient(s).me())
        except WbizError as exc:
            print("FAILED:", exc)
            return 1
        return 0
    if argv[:1] == ["plan"]:
        res = load_medicines(s.excel_path)
        for e in res.errors:
            print("ERROR:", e)
        for r in res.review:
            print(f"NEEDS REVIEW row {r.row} ({r.name or '?'}): {'; '.join(r.reasons)}")
        plan = compute_plan(s, DB(s.db_path), res.medicines)
        print(f"\n{len(plan['create'])} to create, {len(plan['cancel'])} to cancel, {len(plan['keep'])} unchanged\n")
        for d in plan["create"]:
            print(f"--- CREATE [{d.recipient}] {d.kind} {d.when}\n{d.message}\n")
        for r in plan["cancel"]:
            print(f"--- CANCEL [{r['recipient']}] {r['cron'] or r['fire_at']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
