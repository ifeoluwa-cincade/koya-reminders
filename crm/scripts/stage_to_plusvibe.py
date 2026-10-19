"""
stage_to_plusvibe.py — a deal's stage drives the lead's PlusVibe sequences and tag (Ife, 2026-10-12).

The other half of deals_follow_labels.py. That script moves a deal when PlusVibe's label changes. This one does the
opposite: when a deal's stage is set (from the dashboard, a call, or Attio itself), PlusVibe is made to match, so the
stage is never one-sided.

    stage set          emails (main sequence AND subsequence)     PlusVibe tag
    Interested         untouched                                   untouched
    Multi-touch        untouched (the subsequence is how a lead gets here)   untouched
    Later              all stopped                                 LATER
    Meeting Booked     all stopped                                 MEETING_BOOKED
    Profiles Sent      all stopped                                 untouched
    Trial              all stopped                                 untouched
    Won                all stopped                                 CLOSED
    Lost               all stopped                                 NOT_INTERESTED, or WRONG_PERSON when the reason is Bad fit (never CLOSED: that tag means a meeting was booked and closed)

It acts on the stage the deal has NOW, once per request (a webhook names the deal), and only changes what does not
already match, so running it twice does nothing the second time. Every change is read back from PlusVibe.

    python crm/scripts/stage_to_plusvibe.py --deal <deal record id>             # dry run
    python crm/scripts/stage_to_plusvibe.py --deal <deal record id> --commit
    python crm/scripts/stage_to_plusvibe.py --sweep-closed --commit             # closed stages only: stop any sequence still running
    python crm/scripts/stage_to_plusvibe.py --sweep-closed --no-tags --commit   # same, but never touches a tag (daily safety net in crm_daily)

Public repo: only counts and status codes are printed, never names or emails.
"""
from __future__ import annotations
import argparse, os, sys, time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
sys.path.insert(0, str(ROOT / "crm" / "dashboard"))
from attio_client import AttioClient  # noqa: E402
import collect_ops as CO  # noqa: E402

PV = CO.PV
WS = CO.WS
# stage -> (stop every sequence, tag to set or None)
RULES = {
    "Later": (True, "LATER"),
    "Meeting Booked": (True, "MEETING_BOOKED"),
    "Profiles Sent": (True, None),
    "Trial": (True, None),
    "Won": (True, "CLOSED"),
    "Lost": (True, None),            # NOT_INTERESTED is added below when that is the lost reason
}
CLOSED_STAGES = ("Meeting Booked", "Profiles Sent", "Trial", "Won", "Lost")
DONE = {"COMPLETED", "REPLIED", "BOUNCED", "UNSUBSCRIBED", "SKIPPED"}      # nothing left to stop


def _why(r):
    try:
        j = r.json(); return str(j.get("code") or j.get("type") or j.get("message") or "")[:60]
    except Exception:
        return ""


def not_in_seq(r, is_sub) -> bool:
    """PlusVibe 400 'Lead not found' on a SUBSEQUENCE: the lead is not (or no longer) in it (never added, or removed via HQ),
    so the step is already satisfied. Only subsequences; on a main campaign it stays a real problem."""
    if not is_sub or r.status_code != 400:
        return False
    try:
        return "lead not found" in r.text.lower()
    except Exception:
        return False


def stage_of(d) -> str:
    v = (d.get("values") or {}).get("stage") or []
    t = ((v[0].get("status") or {}).get("title") if v else "") or ""
    return "Won" if t.startswith("Won") else t


def chosen_tag(d) -> str:
    v = (d.get("values") or {}).get("plusvibe_tag") or []
    return (v[0].get("value") if v else "") or ""


def lost_reason(d) -> str:
    v = (d.get("values") or {}).get("lost_reason") or []
    return ((v[0].get("option") or {}).get("title") if v else "") or ""


def email_of(c: AttioClient, d) -> str:
    for x in (d.get("values") or {}).get("associated_people") or []:
        r = c.req("GET", f"/objects/people/records/{x.get('target_record_id')}")
        if r.status_code < 300:
            for e in (r.json().get("data", {}).get("values", {}).get("email_addresses") or []):
                if e.get("email_address"):
                    return e["email_address"].strip().lower()
    return ""


def pv_post(path, body, hdr):
    delay = 2.0
    for _ in range(4):
        try:
            r = requests.post(PV + path, headers=hdr, json=body, timeout=60)
        except requests.exceptions.RequestException:
            time.sleep(delay); delay *= 2; continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay); delay *= 2; continue
        return r
    return r


def apply(stage: str, reason: str, email: str, hdr, camps, commit: bool, chosen: str = "", no_tags: bool = False) -> tuple[int, int]:
    """Returns (changes made or planned, problems)."""
    rule = RULES.get(stage)
    if not rule or not email:
        return 0, 0
    stop, tag = rule
    if stage == "Lost":                                        # the tag follows the lost reason, and a lost lead always gets one
        tag = "WRONG_PERSON" if reason.replace(" ", "_").upper() == "BAD_FIT" else "NOT_INTERESTED"
    if chosen == "KEEP":                                        # the dashboard said: leave the tag as it is
        tag = None
    elif chosen:                                                # the tag picked on the dashboard wins over the default
        tag = chosen
    if no_tags:                                                 # stop-only mode (daily safety net): never touch a tag
        tag = None
    rows = CO.pv_sequence(email, hdr, camps)
    todo, bad = 0, 0
    gone = set()                                             # subsequences the lead is not in: nothing to stop or tag, skip in the readback
    for x in rows:
        if not x.get("id") or not x.get("status"):      # no lead record in that campaign (mail history only): nothing to stop or tag
            continue
        if stop and x["status"] not in DONE:
            todo += 1
            if commit:
                r = pv_post("/lead/update/status", {"workspace_id": WS, "campaign_id": x["id"], "email": email, "new_status": "COMPLETED"}, hdr)
                ok = r.status_code < 300
                if not ok and not_in_seq(r, x["sub"]):
                    ok = True; gone.add(x["id"])
                    print("   stop subsequence -> lead not in it, already satisfied (note)")
                else:
                    print(f"   stop {'subsequence' if x['sub'] else 'main'} -> {r.status_code} {'' if ok else _why(r)}")
                bad += 0 if ok else 1
            else:
                print(f"   would stop {'subsequence' if x['sub'] else 'main'} (was {x['status']})")
        if tag and x["label"] != tag and x["status"] != "SKIPPED":
            todo += 1
            if commit:
                r = pv_post("/lead/data/update", {"workspace_id": WS, "campaign_id": x["id"], "email": email, "variables": {"label": tag}}, hdr)
                ok = r.status_code < 300 or (x["status"] == "SKIPPED" and r.status_code == 400)      # PlusVibe will not retag a skipped lead in a subsequence; the tag is lead-wide and the main sequence carries it
                if not ok and not_in_seq(r, x["sub"]):
                    ok = True; gone.add(x["id"])
                    print("   tag on subsequence -> lead not in it, already satisfied (note)")
                else:
                    print(f"   tag {x['label'] or 'none'} -> {tag}: {r.status_code} {'' if ok else _why(r)}")
                bad += 0 if ok else 1
            else:
                print(f"   would tag {x['label'] or 'none'} -> {tag}")
    if commit and todo:                                       # read back from PlusVibe
        time.sleep(3)
        after = CO.pv_sequence(email, hdr, camps)
        for x in after:
            if not x.get("status") or x.get("id") in gone:
                continue
            if stop and x["status"] not in DONE:
                bad += 1; print(f"   READBACK: a {'subsequence' if x['sub'] else 'main'} sequence is still {x['status']}")
            if tag and x["label"] != tag and x["status"] != "SKIPPED":
                bad += 1; print(f"   READBACK: a sequence still has tag {x['label'] or 'none'}, wanted {tag}")
    return todo, bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deal", action="append", default=[], help="deal record id (repeat for several)")
    ap.add_argument("--sweep-closed", action="store_true", help="every deal in a closed stage: stop sequences still running")
    ap.add_argument("--no-tags", action="store_true", help="only stop sequences, never set or change a PlusVibe tag (used by the daily safety-net sweep)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--pretend-stage", help="DRY RUN ONLY: show what this stage would do to the deal's lead")
    a = ap.parse_args()
    if a.pretend_stage and a.commit:
        sys.exit("--pretend-stage is for dry runs only")
    key = os.environ.get("PLUS_VIBE_API_KEY", "")
    if not key:
        sys.exit("PLUS_VIBE_API_KEY is not set")
    hdr = {"x-api-key": key}
    c = AttioClient(workers=2)
    camps = CO.pv_campaigns(hdr)
    deals = []
    for did in a.deal:
        r = c.req("GET", f"/objects/deals/records/{did}")
        if r.status_code >= 300:
            print(f"deal lookup failed {r.status_code}"); return 1
        deals.append(r.json()["data"])
    if a.sweep_closed:
        deals += [d for d in c.query_all("deals") if stage_of(d) in CLOSED_STAGES]
    print(f"{len(deals)} deal(s), {'COMMIT' if a.commit else 'DRY RUN'}")
    total = bad = 0
    for d in deals:
        st = a.pretend_stage or stage_of(d)
        if st not in RULES:
            continue
        em = email_of(c, d)
        if not em:
            print(f"   {st}: no email on the person, skipped"); continue
        ch = chosen_tag(d)
        t, b = apply(st, lost_reason(d), em, hdr, camps, a.commit, ch, a.no_tags)
        total += t; bad += b
        if a.commit and ch and not b and not a.no_tags and d.get("id", {}).get("record_id"):     # one-shot: the choice is used once, then cleared
            c.req("PATCH", f"/objects/deals/records/{d['id']['record_id']}", json={"data": {"values": {"plusvibe_tag": [{"value": ""}]}}})
    print(f"{total} change(s) {'made' if a.commit else 'planned'}, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
