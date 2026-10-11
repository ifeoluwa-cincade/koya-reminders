"""
apply_call_verdicts.py — write what was actually said on each call onto the person in Attio.

This closes the loop that the missing disposition click left open. Each verdict comes from the
transcript (see classify_calls.py), so it is evidence, not inference.

TWO THINGS GO ONTO THE RECORD:
  1. A NOTE per call — the verdict, what was discussed, the objection, agent, date, duration.
     Titled so it reads properly in the timeline, unlike the old "Call history — 7 calls" dump.
  2. The person's STATE, but only where the call is the most recent and strongest signal.

THE PRECEDENCE RULE, same as everywhere else in this CRM: the most RECENT real signal wins, and
a call where someone actually spoke outranks an older email verdict. That is how Theodore was
found — his email said WRONG_PERSON, his calls booked a meeting.

WHAT IT WILL NOT DO:
  - never downgrade a CUSTOMER (a client who took a cold call is still a client)
  - never un-block someone already DNC
  - NO_CONVERSATION writes a note but changes no state — a voicemail is not a verdict
  - creates a person ONLY for an OUTBOUND call that produced a real conversation (Ife, 2026-10-02).
    His rule that a lead enters the CRM only once the record is filled was about SCRAPED leads
    with no email — not about people we deliberately phoned. If we dialled someone and they
    talked to us, that is a relationship and it belongs on the record. One of these was a BOOKED
    MEETING sitting entirely outside the CRM. An INBOUND call from an unknown number still
    creates nothing: that is a stranger ringing us, which is why Ife turned that setting off in
    CloudTalk.

    python crm/scripts/apply_call_verdicts.py            # dry run
    python crm/scripts/apply_call_verdicts.py --commit
"""
from __future__ import annotations
import argparse, collections, json, os, re, sys
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient, api_key

WORK = Path(os.environ.get("CALL_DATA_DIR") or ROOT / "data/working")   # cloud job uses its own folder
IN = WORK / "call_verdicts.json"

# verdict -> (relationship, outreach, nurture, paused, last_result, deal_stage or None)
STATE = {
  "MEETING_BOOKED": ("MEETING",    "PAUSED",  "NONE",    True,  "MEETING_BOOKED", "Meeting Booked"),
  "SEND_FOLLOW_UP": ("INTERESTED", "NURTURE", "ACTIVE",  True,  "POSITIVE_REPLY", "Interested"),
  "CALLBACK":       ("ENGAGED",    "NURTURE", "ACTIVE",  True,  "POSITIVE_REPLY", None),
  # "send me an email and I'll look when I need it" is how people end a cold call politely, NOT
  # interest. Ife caught this: "it clearly says not a priority at the moment... how is that an
  # opportunity?" He was right — 18 SEND_FOLLOW_UP re-judged to 3. NOT_NOW goes to nurture and
  # stays OUT of the deal pipeline.
  "NOT_NOW":        ("ENGAGED",    "NURTURE", "ACTIVE",  True,  "NO_RESPONSE",    None),
  "NOT_INTERESTED": ("LOST",       "PAUSED",  "NONE",    True,  "NEGATIVE_REPLY", None),
  "DO_NOT_CALL":    ("LOST",       "DO_NOT_CONTACT", "UNSUBSCRIBED", True, "UNSUBSCRIBED", None),
  "WRONG_PERSON":   ("LOST",       "EXCLUDED","NONE",    True,  "WRONG_PERSON",   None),
}
PROTECTED = {"CUSTOMER", "LOST"}      # a lead someone closed as Lost keeps that state: this job used to overwrite it from the last call on every run (found 2026-10-11, Ed at Growth Right)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--workspace", default="ATTIO_API_KEY_2")
    a = ap.parse_args()
    v = json.loads(IN.read_text())
    if not v:                                  # the hourly job calls this every run; no calls yet means nothing to do
        print("no calls to write yet")
        return 0
    c = AttioClient(); c._key = api_key(a.workspace); c._local = type(c._local)()

    # MATCH ORDER: email, then PHONE, then (only then) create. Matching on email alone made
    # three real conversations look like strangers — including Benjamin Bohman, a CUSTOMER with
    # two placements, whose booked meeting appeared to be "outside the CRM". It was not; we were
    # looking with the wrong key. Phone is compared on the LAST TEN DIGITS because the same
    # number is stored with and without a country code in different systems.
    def dig(x):
        return re.sub(r"\D", "", x or "")[-10:]

    byem, byph = {}, {}
    for p in c.query_all("people"):
        for e in p["values"].get("email_addresses", []):
            byem[e.get("email_address", "").lower()] = p
        for ph in p["values"].get("phone_numbers", []):
            d = dig(ph.get("original_phone_number") or ph.get("phone_number"))
            if len(d) == 10:
                byph.setdefault(d, p)

    # --- bring in the outbound calls that produced a real conversation with someone we do not hold
    raw = json.loads((WORK / "cloudtalk_calls.json").read_text())
    direction = {str((x.get("Cdr") or {}).get("id")): (x.get("Cdr") or {}).get("type") for x in raw}
    numbers = {str((x.get("Cdr") or {}).get("id")): (x.get("Cdr") or {}).get("public_external")
               for x in raw}
    made = matched_by_phone = 0
    for cid, r in v.items():
        if r["verdict"] == "NO_CONVERSATION" or direction.get(cid) != "outgoing":
            continue
        ems = [e for e in (r.get("emails") or [])]
        if any(e in byem for e in ems):
            continue
        ph = byph.get(dig(numbers.get(cid)))
        if ph:                       # already in the CRM, just under a different key
            key = f"cid:{cid}"
            byem[key] = ph
            r.setdefault("emails", []).append(key)
            matched_by_phone += 1
            continue
        nm = (r.get("name") or "").strip()
        if not nm or nm.lower().startswith("lead from"):
            # no name at all — a number and a transcript is not enough to build a person on
            continue
        first, _, last = nm.partition(" ")
        vals = {"name": [{"first_name": first, "last_name": last, "full_name": nm}],
                "relationship_stage": [{"option": "ENGAGED"}],
                "outreach_status": [{"option": "PAUSED"}],
                "outreach_paused": [{"value": True}],
                "due_status": [{"option": "NOT_ELIGIBLE"}],
                "segment": [{"option": "AI_AGENCY"}],
                "source_type": [{"option": "OTHER"}],
                "legacy_source": [{"value": "CloudTalk outbound call"}],
                "legacy_reason": [{"value": f"Created from an outbound call on "
                                            f"{(r.get('started') or '')[:10]} — {r['verdict']}"}]}
        if ems:
            vals["email_addresses"] = [{"email_address": ems[0]}]
        if numbers.get(cid):
            vals["phone_numbers"] = [{"original_phone_number": numbers[cid]}]
        if not a.commit:
            made += 1
            continue
        resp = c.req("POST", "/objects/people/records", json={"data": {"values": vals}})
        if resp.status_code >= 300:
            print(f"   FAIL create: {resp.status_code}")
            continue
        made += 1
        np = resp.json()["data"]
        for e in ems:
            byem[e] = np
        if not ems:          # keyed by the call id so the note still finds its parent
            byem[f"cid:{cid}"] = np
            r.setdefault("emails", []).append(f"cid:{cid}")
    print(f"   {matched_by_phone} calls matched to an existing person BY PHONE")
    print(f"   {made} people created from outbound calls that had a real conversation")

    # one person can have several calls — the LAST real conversation decides their state
    per = collections.defaultdict(list)
    for cid, r in v.items():
        for e in r.get("emails") or []:
            if e in byem:
                per[e].append({**r, "cid": cid})
    for e in per:
        per[e].sort(key=lambda x: x.get("started") or "")

    # strongest first — used only to break a tie when someone was called twice on the same day.
    # EVERY key of STATE must appear here or this raises; adding a verdict to STATE and forgetting
    # this list silently crashed the whole run once.
    rank = ["DO_NOT_CALL", "MEETING_BOOKED", "SEND_FOLLOW_UP", "CALLBACK",
            "NOT_NOW", "WRONG_PERSON", "NOT_INTERESTED"]
    missing = set(STATE) - set(rank)
    assert not missing, f"verdicts in STATE but not in rank: {missing}"
    plan, stats = [], collections.Counter()
    for e, calls in per.items():
        p = byem[e]
        cur = ((p["values"].get("relationship_stage") or [{}])[0].get("option") or {}).get("title")
        real = [x for x in calls if x["verdict"] in STATE]
        best = None
        if real:
            # most recent first, then by how strong the signal is
            best = sorted(real, key=lambda x: (x.get("started") or "",
                                               -rank.index(x["verdict"])), reverse=True)[0]
        plan.append((p, calls, best, cur))
        stats[best["verdict"] if best else "NO_CONVERSATION only"] += 1

    unmatched = sum(1 for r in v.values() if not any(e in byem for e in (r.get("emails") or [])))
    print(f"{len(v)} calls -> {len(per)} people in the CRM  ({unmatched} calls have no matching person)")
    for k, n in stats.most_common():
        print(f"   {n:>4}  {k}")
    prot = sum(1 for _, _, b, cur in plan if b and cur in PROTECTED)
    print(f"   {prot} are CUSTOMERS and will keep their state (note only)")
    if not a.commit:
        print("\nDRY RUN — nothing written.")
        return 0

    def fmt(x):
        when = (x.get("started") or "")[:16].replace("T", " ")
        mm, ss = divmod(int(x.get("talk") or 0), 60)
        head = f"{when} · {x.get('agent') or 'unknown agent'} · {mm}m {ss}s · {x['verdict'].replace('_',' ').title()}"
        body = x.get("summary") or ""
        if x.get("objection"):
            body += f"\n\nObjection: {x['objection']}"
        if x.get("text"):
            body += "\n\nTranscript:\n" + x["text"][:18000]
        return head, body

    written = skipped = moved = 0
    for p, calls, best, cur in plan:
        rid = p["id"]["record_id"]
        have = {n.get("title") for n in c.req("GET", "/notes", params={
            "parent_object": "people", "parent_record_id": rid, "limit": 50}).json().get("data", [])}
        fresh = 0                       # calls that had no note yet: only a NEW call may change the person's state (a manual change made after the last call must stick)
        for x in calls:
            head, body = fmt(x)
            # date AND time in the title, because the same person can be rung several times a day with the same result
            title = f"Call · {(x.get('started') or '')[:10]} {(x.get('started') or '')[11:16]} · {x['verdict'].replace('_',' ').title()}"
            if title in have:
                skipped += 1
                continue
            r = c.req("POST", "/notes", json={"data": {
                "parent_object": "people", "parent_record_id": rid,
                "title": title, "format": "plaintext", "content": head + "\n\n" + body}})
            written += r.status_code < 300
            fresh += r.status_code < 300
        if not best or cur in PROTECTED or not fresh:
            continue
        rel, out, nur, paused, res, _ = STATE[best["verdict"]]
        vals = {"relationship_stage": [{"option": rel}], "outreach_status": [{"option": out}],
                "nurture_status": [{"option": nur}], "outreach_paused": [{"value": paused}],
                "last_outreach_result": [{"option": res}],
                "last_outreach_channel": [{"option": "CALL"}],
                "due_status": [{"option": "NOT_ELIGIBLE"}], "next_eligible_at": [],
                "last_reply_summary": [{"value": (best.get("summary") or "")[:900]}]}
        if best["verdict"] == "WRONG_PERSON":
            # The NUMBER reached the wrong person (or someone who is not them). That says nothing about the lead
            # itself, so only the phone is marked bad; the stage, email sequence and everything else stay as they were.
            vals = {"phone_status": [{"option": "INVALID"}]}
        if best["verdict"] == "DO_NOT_CALL":
            vals |= {"dnc_status": [{"option": "DO_NOT_CONTACT"}],
                     "dnc_reason": [{"option": "REQUESTED_REMOVAL"}],
                     "dnc_source": [{"option": "MANUAL"}],
                     "dnc_evidence": [{"value": (best.get("summary") or "")[:900]}]}
        rr = c.req("PATCH", f"/objects/people/records/{rid}", json={"data": {"values": vals}})
        if rr.status_code >= 300:
            print(f"   FAIL: {rr.status_code}")
        else:
            moved += 1
    print(f"\n{written} notes written, {skipped} already there, {moved} people updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
