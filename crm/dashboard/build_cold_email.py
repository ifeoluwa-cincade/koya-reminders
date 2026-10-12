"""
build_cold_email.py — bake the PlusVibe numbers into the cold-email-vs-target page.

The targets are the cold email column of the $100k/month plan. PlusVibe numbers are baked in here;
meetings, clients and devs are typed into the page and live in its database (publish keeps {db:{}}).
Runs every weekday on GitHub Actions (.github/workflows/crm_dashboard.yml), which also publishes it.

    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/collect_metrics.py --days 40
    python crm/dashboard/build_cold_email.py
"""
from __future__ import annotations
import json, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "crm/dashboard/cold_email_template.html"
METRICS = ROOT / "data/working/metrics_daily.json"
OUT = ROOT / "data/working/koya_cold_email.html"
DOWNLOADS = Path.home() / "Downloads/Koya Cold Email.html"
# Tracking starts fresh on Monday 5 Oct 2026 (Ife, 2026-10-04): earlier days are not baked into the page.
TRACK_FROM = "2026-10-05"


def main() -> int:
    m = json.loads(METRICS.read_text())
    days = [{k: d.get(k, 0) for k in ("date", "contacted", "replies", "positive", "sent", "booked", "held", "staffed",
                        "dials")}
            for d in m["days"] if d.get("date", "") >= TRACK_FROM]
    # Real call conversations (read from transcripts) are only trusted up to the day they were fully processed.
    co = ROOT / "crm/dashboard/call_outcomes.json"
    outcomes = json.loads(co.read_text()) if co.exists() else {"through": "", "days": {}}
    through = outcomes.get("through", "")
    for d in days:
        covered = bool(through) and d["date"] <= through
        d["conversations"] = outcomes["days"].get(d["date"], {}).get("conversations", 0) if covered else 0
        d["odials"] = outcomes["days"].get(d["date"], {}).get("calls", 0) if covered else 0   # calls whose outcome is known
        d["cbooked_auto"] = outcomes["days"].get(d["date"], {}).get("booked", 0) if covered else 0   # meetings booked on calls, from the transcripts
    # Opportunities and system checks, collected by collect_ops.py in the same hourly job (empty if that part failed)
    ops_path = ROOT / "data/working/ops.json"
    ops = json.loads(ops_path.read_text()) if ops_path.exists() else {}
    hp = ROOT / "data/working/check_history.json"
    if not hp.exists():
        hp = ROOT / "crm/dashboard/check_history_seed.json"
    history = json.loads(hp.read_text()) if hp.exists() else {"runs": [], "names": {}}
    op = ROOT / "data/working/outreach_plan.json"           # counts only, built once a day by collect_outreach_plan.py
    outreach = json.loads(op.read_text()) if op.exists() else None
    npath = ROOT / "crm/dashboard/incident_notes.json"
    notes = json.loads(npath.read_text()) if npath.exists() else []
    payload = json.dumps({"days": days, "calls_through": through,
                          "opps": ops.get("opps", []), "checks": ops.get("checks", []), "history": history, "outreach": outreach, "notes": notes, "activity": ops.get("activity", []), "ops_at": ops.get("generated_at", ""),
                          "generated_at": m["snapshot"].get("generated_at", ""), "built_by": "auto" if os.environ.get("GITHUB_ACTIONS") else "manual",
                          "outreach_start": m.get("outreach_start", "")},
                         separators=(",", ":"))
    t = SRC.read_text()
    assert "__DATA__" in t
    page = t.replace("__DATA__", payload)
    page = page.replace("__LEAD_ACTIONS_URL__", os.environ.get("LEAD_ACTIONS_URL", "")).replace("__OWNER_EMAIL__", os.environ.get("OWNER_EMAIL", ""))   # kept out of the public code; set as a secret
    # Since 2026-10-04 the page uses the db capability (pipeline numbers are typed in). Publish with capabilities={"db":{}}.
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)
    if not DOWNLOADS.parent.exists():                     # GitHub Actions has no Downloads folder
        print(f"{OUT.relative_to(ROOT)}  ({len(days)} days)")
        return 0
    DOWNLOADS.write_text('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                         '<meta name="viewport" content="width=device-width,initial-scale=1">'
                         '</head><body style="margin:0">' + page + "</body></html>")
    print(f"{OUT.relative_to(ROOT)}  ({len(days)} days)\n{DOWNLOADS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
