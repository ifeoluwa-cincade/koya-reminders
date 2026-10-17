"""Offline checks for lead_company.py (no network, made-up names). Run: python crm/dashboard/test_lead_company.py"""
import json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lead_company as LC  # noqa: E402

ok = fails = 0


def check(name, cond, got=None):
    global ok, fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  got {got!r}"))
    ok += bool(cond); fails += (not cond)


for c in json.loads((HERE / "lead_company_fixtures.json").read_text())["cases"]:
    got = LC.pick(c["in"])
    check("fixture " + c["name"], got == c["out"], got)

check("domain_of: work domain", LC.domain_of("Jo@WidgetWorks.example ") == "widgetworks.example")
check("domain_of: free mail is never a company", LC.domain_of("jo@gmail.com") is None and LC.domain_of("jo@outlook.com") is None)
check("domain_of: not an email", LC.domain_of("") is None and LC.domain_of("nope") is None)
check("needs_reads: a deal-name answer still asks PlusVibe / CloudTalk first",
      LC.needs_reads({"person": {}, "deals": [{"name": "A / B", "ext": "pv-1"}]}) and not LC.needs_reads({"person": {"company": {"name": "X"}}}))
check("placement deal never gives the developer",
      LC.deal_name_company([{"name": "Client / Developer", "ext": "aat-0123456789abcdef"}]) == "")
print(f"{ok} passed, {fails} failed")
sys.exit(1 if fails else 0)
