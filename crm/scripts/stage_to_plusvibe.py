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
    python crm/scripts/stage_to_plusvibe.py --sweep                             # dry run: every deal at a stopping stage (Later too)
    python crm/scripts/stage_to_plusvibe.py --sweep --commit                    # the hourly safety net (stage_sync.yml schedule)
    python crm/scripts/stage_to_plusvibe.py --sweep --no-tags --commit          # same, but never touches a tag
    (--sweep-closed is the old name of --sweep and does the same.)

The sweep (Ife, 20 Nov 2026, "fix the root cause so it never happens"): three leads sat at Later for 8-9 days while a
second campaign still emailed them with no label. Their deals were CREATED at Later by positive_replies_to_deals.py, and Attio only
sends record.created for a new deal, never the record.updated stage change the stage job listens to, so the job never ran. The old
daily sweep only covered Meeting Booked..Lost (not Later) and never tagged. Now: (1) a script that creates a deal at a stopping stage
runs this job for it at once (run_for_deal), (2) the hourly sweep covers every stopping stage, stops what still runs and fills a
MISSING tag, (3) HQ's watchdog turns red (Discord) when a lead stays listed for over an hour.

Label precedence (rows are PlusVibe campaign rows; each has its own label):
  - the sweep only ever FILLS an empty label; it never replaces one (a person or the label sync may have set it on purpose: replacing
    INTERESTED with LATER could hide a lead warming up again before crm_sync moves the deal).
  - LATER never replaces MEETING_BOOKED or CLOSED, in the webhook job too: that label is the record that a meeting was booked
    (e.g. a deal booked on 8 Oct and moved to Later on 10 Oct: the booking still counts). Only a tag picked on the dashboard
    (plusvibe_tag) can replace it.
  - the sweep leaves a Later lead's nurture campaign running (Later leads are put into "Q4 Nurture" on purpose).

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
SWEEP_STAGES = tuple(RULES)                     # every stage that stops emails, Later included (it was missing until 20 Nov 2026)
MEETING_LABELS = {"MEETING_BOOKED", "CLOSED"}    # the record that a meeting was booked: LATER never replaces it
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


def row_tag(tag, label: str, sweep: bool = False, picked: bool = False):
    """The tag to write on one campaign row, or None to leave the row's label as it is (see Label precedence above).
    picked: the tag was chosen on the dashboard (plusvibe_tag), which beats the meeting protection."""
    label = label or ""
    if not tag or label == tag:
        return None
    if sweep and label:
        return None
    if tag == "LATER" and label in MEETING_LABELS and not picked:
        return None
    return tag


def is_nurture(camps, cid) -> bool:
    c = (camps or {}).get(cid) or {}
    names = [c.get("name") or "", ((camps or {}).get(c.get("parent")) or {}).get("name") or ""]
    return any("nurture" in n.lower() for n in names)


def apply(stage: str, reason: str, email: str, hdr, camps, commit: bool, chosen: str = "", no_tags: bool = False,
          sweep: bool = False) -> tuple[int, int]:
    """Returns (changes made or planned, problems). sweep: the hourly safety net (fill empty labels only, a Later lead's nurture runs on)."""
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
    if no_tags:                                                 # stop-only mode: never touch a tag
        tag = None
    picked = bool(chosen and chosen != "KEEP")
    rows = CO.pv_sequence(email, hdr, camps)
    todo, bad = 0, 0
    gone = set()                                             # subsequences the lead is not in: nothing to stop or tag, skip in the readback
    want = {}                                                # campaign row -> the tag planned for it (the readback checks the same thing)
    keepgo = set()                                           # rows left running on purpose (a Later lead's nurture, sweep only)
    for x in rows:
        if not x.get("id") or not x.get("status"):      # no lead record in that campaign (mail history only): nothing to stop or tag
            continue
        if sweep and stage == "Later" and is_nurture(camps, x["id"]):
            keepgo.add(x["id"])
        if stop and x["status"] not in DONE and x["id"] not in keepgo:
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
        t = row_tag(tag, x["label"], sweep, picked) if x["status"] != "SKIPPED" and x["id"] not in keepgo else None
        if tag and not t and x["label"] and x["label"] != tag and x["status"] != "SKIPPED":
            print(f"   keep tag {x['label']} (not replaced by {tag})")
        if t:
            want[x["id"]] = t
            todo += 1
            if commit:
                r = pv_post("/lead/data/update", {"workspace_id": WS, "campaign_id": x["id"], "email": email, "variables": {"label": t}}, hdr)
                ok = r.status_code < 300 or (x["status"] == "SKIPPED" and r.status_code == 400)      # PlusVibe will not retag a skipped lead in a subsequence; the tag is lead-wide and the main sequence carries it
                if not ok and not_in_seq(r, x["sub"]):
                    ok = True; gone.add(x["id"])
                    print("   tag on subsequence -> lead not in it, already satisfied (note)")
                else:
                    print(f"   tag {x['label'] or 'none'} -> {t}: {r.status_code} {'' if ok else _why(r)}")
                bad += 0 if ok else 1
            else:
                print(f"   would tag {x['label'] or 'none'} -> {t}")
    if commit and todo:                                       # read back from PlusVibe
        time.sleep(3)
        after = CO.pv_sequence(email, hdr, camps)
        for x in after:
            if not x.get("status") or x.get("id") in gone:
                continue
            if stop and x["status"] not in DONE and x.get("id") not in keepgo:
                bad += 1; print(f"   READBACK: a {'subsequence' if x['sub'] else 'main'} sequence is still {x['status']}")
            w = want.get(x.get("id"))
            if w and x["label"] != w and x["status"] != "SKIPPED":
                bad += 1; print(f"   READBACK: a sequence still has tag {x['label'] or 'none'}, wanted {w}")
    return todo, bad


_CAMPS = {}


def run_for_deal(c: AttioClient, deal, commit: bool = True) -> tuple[int, int]:
    """Run the stage job for ONE deal a script has just created or moved (record.created never reaches the stage webhook).
    deal: the deal record (dict) or its record id. Never raises: a failure is printed and the hourly sweep catches the deal.
    -> (changes, problems)."""
    try:
        key = os.environ.get("PLUS_VIBE_API_KEY", "")
        if not key:
            print("   stage job: PLUS_VIBE_API_KEY not set, the hourly sweep will stop this deal's emails"); return 0, 0
        if not isinstance(deal, dict):
            r = c.req("GET", f"/objects/deals/records/{deal}")
            if r.status_code >= 300:
                print(f"   stage job: deal lookup failed {r.status_code}"); return 0, 1
            deal = r.json()["data"]
        st = stage_of(deal)
        if st not in RULES:
            return 0, 0
        em = email_of(c, deal)
        if not em:
            print(f"   stage job: {st}, no email on the person"); return 0, 0
        hdr = {"x-api-key": key}
        if not _CAMPS:
            _CAMPS.update(CO.pv_campaigns(hdr))
        print(f"   stage job: {st}, {'COMMIT' if commit else 'DRY RUN'}")
        return apply(st, lost_reason(deal), em, hdr, _CAMPS, commit, chosen_tag(deal))
    except Exception as e:                                       # the deal is made either way; the sweep is the net
        print(f"   stage job failed ({type(e).__name__}); the hourly sweep will catch it"); return 0, 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deal", action="append", default=[], help="deal record id (repeat for several)")
    ap.add_argument("--sweep", "--sweep-closed", dest="sweep", action="store_true",
                    help="every deal at a stopping stage (Later included): stop what still runs, fill a missing tag, never replace one")
    ap.add_argument("--no-tags", action="store_true", help="only stop sequences, never set or change a PlusVibe tag")
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
    named = len(deals)
    if a.sweep:
        deals += [d for d in c.query_all("deals") if stage_of(d) in SWEEP_STAGES]
    print(f"{len(deals)} deal(s), {'COMMIT' if a.commit else 'DRY RUN'}{', sweep' if a.sweep else ''}")
    total = bad = 0
    off = []                                                  # sweep: deals whose PlusVibe did not match (short ids, no names: public repo)
    for i, d in enumerate(deals):
        sw = a.sweep and i >= named
        st = a.pretend_stage or stage_of(d)
        if st not in RULES:
            continue
        em = email_of(c, d)
        did = (d.get("id") or {}).get("record_id") or ""
        if not em:
            if not sw:
                print(f"   {st}: no email on the person, skipped")
            continue
        if sw:
            print(f"-- {st} deal {did[:8]}")
        ch = chosen_tag(d)
        t, b = apply(st, lost_reason(d), em, hdr, camps, a.commit, ch, a.no_tags, sweep=sw)
        total += t; bad += b
        if sw and t:
            off.append(f"{st}:{did[:8]}")
        if a.commit and not sw and ch and not b and not a.no_tags and did:     # one-shot: the choice is used once, then cleared
            c.req("PATCH", f"/objects/deals/records/{did}", json={"data": {"values": {"plusvibe_tag": [{"value": ""}]}}})
    if a.sweep:
        print(f"sweep: {len(off)} deal(s) did not match PlusVibe{': ' + ', '.join(off) if off else ''}")
    print(f"{total} change(s) {'made' if a.commit else 'planned'}, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
