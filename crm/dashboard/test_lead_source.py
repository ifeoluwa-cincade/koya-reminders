"""Offline checks for lead_source.py (Source = the channel of the first touch). The same fixtures run in koya-app
test/leadsource.test.js against look/leadkey.js sourceOf, so the build and the page agree. Run: python crm/dashboard/test_lead_source.py"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lead_source as LS  # noqa: E402

bad = 0
for f in json.loads((Path(__file__).resolve().parent / "lead_source_fixtures.json").read_text()):
    got = LS.pick(f.get("email_at"), f.get("call_at"), f.get("deals"))
    ok = got == f["want"]
    bad += not ok
    print(("ok   " if ok else "FAIL ") + f["name"] + ("" if ok else f": got {got}, want {f['want']}"))
print(f"{'FAILED' if bad else 'all passed'}")
sys.exit(1 if bad else 0)
