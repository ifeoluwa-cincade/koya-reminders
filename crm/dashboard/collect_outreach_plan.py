"""collect_outreach_plan.py: the weekly outreach plan, as counts only.

WHAT IT ANSWERS (Ife, 2026-10-12):
  * Never contacted: leads sitting in a campaign who have not been emailed yet (first outreach, cycle 1).
  * Finished, never replied: a lead whose sequence ended and who has not replied in ANY campaign (active or paused).
    They qualify for a fresh re-outreach 90 days after the day their sequence FINISHED.
  * How many are due again now (already past 90 days), each week for the next 13 weeks, and this month.

HOW: PlusVibe gives each lead's status and the date of the last email sent. A lead's finish day is the last email sent
in a campaign where its status is COMPLETED. Anyone who replied, bounced or unsubscribed in any campaign, anyone still
inside a running sequence, and anyone Attio says must not be re-contacted (customer, lost, engaged, do-not-contact, nurture, paused, bad email) is left out.

PRIVACY: the output holds counts per week and per campaign only. No names, emails or phone numbers are written or printed.
Run:  python crm/dashboard/collect_outreach_plan.py [--no-attio]
"""
from __future__ import annotations
import argparse, collections, json, os, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
OUT = ROOT / "data/working/outreach_plan.json"
PV = "https://api.plusvibe.ai/api/v1"
WS = "6a6e634971ad0f0e174a5471"
GAP_DAYS = 90
WEEKS_AHEAD = 13
WEEKS_BACK = 12
STATUSES = ("COMPLETED", "REPLIED", "BOUNCED", "UNSUBSCRIBED", "NOT_CONTACTED", "CONTACTED")
# Attio says who may be contacted again. "Contacted" means emailed with no reply, which is exactly the group this plan is for.
OK_STAGES = {None, "", "COLD", "CONTACTED"}                         # out: LOST, ENGAGED, INTERESTED, MEETING, CUSTOMER
OUT_OUTREACH = {"DO_NOT_CONTACT", "EXCLUDED", "NURTURE", "PAUSED"}  # out: told not to contact, already in nurture, or paused on purpose
OUT_EMAIL = {"BOUNCED", "INVALID"}


def pv_get(path: str, params: dict, key: str):
    """GET with back-off. A 429 looks exactly like an empty answer, so it is retried, never read as 'no leads'."""
    delay = 2.0
    for _ in range(6):
        try:
            r = requests.get(PV + path, headers={"x-api-key": key}, params={"workspace_id": WS, **params}, timeout=120)
        except requests.exceptions.RequestException:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code >= 300:
            raise RuntimeError(f"PlusVibe {path} answered {r.status_code}")
        return r.json()
    raise RuntimeError(f"PlusVibe {path} kept failing")


def leads_of(camp_id: str, status: str, key: str) -> list[dict]:
    out, page = [], 1
    while True:
        rows = pv_get("/lead/workspace-leads", {"campaign_id": camp_id, "status": status, "page": page, "limit": 5000}, key)
        rows = rows if isinstance(rows, list) else (rows.get("data") or [])
        out += rows
        if len(rows) < 5000:
            return out
        page += 1


def parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def attio_not_cold_emails() -> set[str]:
    """Emails of people Attio says we must not re-contact (see the three sets above). Read-only."""
    from attio_client import AttioClient
    c = AttioClient(workers=6)

    def opt(v, k):
        return (((v.get(k) or [{}])[0].get("option") or {}).get("title"))
    out = set()
    for p in c.query_all("people"):
        v = p["values"]
        if opt(v, "relationship_stage") not in OK_STAGES or opt(v, "outreach_status") in OUT_OUTREACH or opt(v, "email_status") in OUT_EMAIL:
            for x in v.get("email_addresses") or []:
                e = (x.get("email_address") or "").strip().lower()
                if e:
                    out.add(e)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-attio", action="store_true", help="skip the Attio exclusion (faster, for a quick look)")
    a = ap.parse_args()
    key = os.environ.get("PLUS_VIBE_API_KEY", "")
    if not key:
        sys.exit("PLUS_VIBE_API_KEY is not set")
    today = datetime.now(timezone.utc).date()

    camps = pv_get("/campaign/list-all", {"limit": 100}, key)
    camps = camps if isinstance(camps, list) else (camps.get("data") or [])
    by_id = {c["id"]: c for c in camps}
    print(f"{len(camps)} campaigns ({sum(1 for c in camps if c.get('status') == 'ACTIVE')} active)")

    replied, dead, running, queued = set(), set(), set(), set()
    finish: dict[str, tuple[datetime, str]] = {}          # email -> (latest finish, campaign id)
    pulled = collections.Counter()
    for cid, c in by_id.items():
        for st in STATUSES:
            rows = leads_of(cid, st, key)
            pulled[st] += len(rows)
            for r in rows:
                e = (r.get("email") or "").strip().lower()
                if not e:
                    continue
                if st == "REPLIED" or int(r.get("replied_count") or 0) > 0:
                    replied.add(e)
                if st in ("BOUNCED", "UNSUBSCRIBED"):
                    dead.add(e)
                if st == "CONTACTED":
                    running.add(e)
                if st == "NOT_CONTACTED":
                    queued.add(e)
                if st == "COMPLETED":
                    d = parse(r.get("last_sent_at")) or parse(r.get("modified_at"))
                    if d and (e not in finish or d > finish[e][0]):
                        finish[e] = (d, cid)
    print("pulled (lead rows by status):", dict(pulled))

    not_cold = set() if a.no_attio else attio_not_cold_emails()
    print(f"excluded sets: replied {len(replied)}, bounced/unsub {len(dead)}, in a running sequence {len(running)}, Attio not cold {len(not_cold)}")

    eligible = {e: v for e, v in finish.items() if e not in replied and e not in dead and e not in running and e not in not_cold}
    print(f"finished leads (any campaign): {len(finish)}; eligible for re-outreach after the gap: {len(eligible)}")

    # --- counts only from here on
    monday = today - timedelta(days=today.weekday())
    due_week = collections.Counter()                       # week offset (0 = this week) -> count; negative never used, overdue kept apart
    overdue = 0
    beyond = 0
    finished_week = collections.Counter()                  # weeks back (0 = this week) -> count
    by_camp_overdue, by_camp_total = collections.Counter(), collections.Counter()
    oldest = None
    for e, (d, cid) in eligible.items():
        fd = d.date()
        due = fd + timedelta(days=GAP_DAYS)
        name = (by_id.get(cid) or {}).get("camp_name", "")[:70]
        by_camp_total[name] += 1
        wk_back = (monday - (fd - timedelta(days=fd.weekday()))).days // 7
        if 0 <= wk_back < WEEKS_BACK:
            finished_week[wk_back] += 1
        if due <= today:
            overdue += 1
            by_camp_overdue[name] += 1
            oldest = fd if oldest is None or fd < oldest else oldest
        else:
            off = ((due - timedelta(days=due.weekday())) - monday).days // 7
            if off < WEEKS_AHEAD:
                due_week[off] += 1
            else:
                beyond += 1
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    due_this_month = sum(1 for e, (d, cid) in eligible.items() if today < d.date() + timedelta(days=GAP_DAYS) <= month_end)
    due_next_30 = sum(1 for e, (d, cid) in eligible.items() if today < d.date() + timedelta(days=GAP_DAYS) <= today + timedelta(days=30))
    never_contacted = len(queued - replied - dead - running - set(finish))
    month_names = []
    m0 = today.replace(day=1)
    for i in range(7):                                   # this month and the next six
        y, mo = m0.year + (m0.month - 1 + i) // 12, (m0.month - 1 + i) % 12 + 1
        month_names.append(f"{y}-{mo:02d}")
    due_month = collections.Counter()
    elig_camp, first_due = collections.Counter(), {}
    for e, (d, cid) in eligible.items():
        due = d.date() + timedelta(days=GAP_DAYS)
        due_month[f"{due.year}-{due.month:02d}"] += 1
        nm = (by_id.get(cid) or {}).get("camp_name", "")[:70]
        elig_camp[nm] += 1
        if nm not in first_due or due < first_due[nm]:
            first_due[nm] = due
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gap_days": GAP_DAYS,
        "eligible_total": len(eligible),
        "overdue_now": overdue,
        "oldest_overdue_finish": oldest.isoformat() if oldest else None,
        "due_this_week": due_week.get(0, 0),
        "due_this_month": due_this_month,
        "due_next_30_days": due_next_30,
        "due_beyond_13_weeks": beyond,
        "due_by_week": [{"week_start": (monday + timedelta(weeks=i)).isoformat(), "count": due_week.get(i, 0)} for i in range(WEEKS_AHEAD)],
        "finished_by_week": [{"week_start": (monday - timedelta(weeks=i)).isoformat(), "count": finished_week.get(i, 0)} for i in range(WEEKS_BACK - 1, -1, -1)],
        "never_contacted": never_contacted,
        "in_sequence_now": len(running - replied - dead),
        "replied_anywhere": len(replied),
        "due_by_month": [{"month": m, "count": due_month.get(m, 0)} for m in month_names],
        "eligible_by_campaign": [{"campaign": k, "count": v, "first_due": first_due[k].isoformat()} for k, v in elig_camp.most_common(12)],
        "overdue_by_campaign": [{"campaign": k, "count": v} for k, v in by_camp_overdue.most_common(10)],
        "excluded": {"replied": len(replied & set(finish)), "bounced_or_unsubscribed": len(dead & set(finish)),
                     "in_running_sequence": len(running & set(finish)), "attio_not_cold": len(not_cold & set(finish))},
        "attio_checked": not a.no_attio,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))
    print(f"wrote {OUT.relative_to(ROOT)}: {out['eligible_total']} eligible, {out['overdue_now']} overdue now, {out['due_this_month']} due this month, {out['never_contacted']} never contacted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
