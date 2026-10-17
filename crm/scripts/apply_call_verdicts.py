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

ALSO (2026-10-16): a new NOT_INTERESTED call stops every PlusVibe sequence the lead is still in
(call_stop_plusvibe.py). WRONG_PERSON does not: the number was wrong, the lead said nothing.
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
import argparse, collections, json, os, re, sys, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
sys.path.insert(0, str(ROOT / "crm" / "dashboard"))
from attio_client import AttioClient, api_key
import call_stop_plusvibe as PVSTOP  # noqa: E402  (a not-interested call stops the lead's PlusVibe emails)
import collect_ops as CO  # noqa: E402

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
    v = {k: x for k, x in v.items() if x.get("verdict") not in ("UNKNOWN", None) and not x.get("retry")}      # a call that could not be judged yet (transcription failed) is retried by the call job; it must never become a note with an error message in it
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

    # which calls does this run have to apply? Only the ones marked new in live_state.json: their people are looked up one by one (seconds),
    # instead of reading all 16,000 people (7 minutes). No flags or FULL_INDEX=1 means the old full read.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import attio_fast as AF
    pend = AF.pending_cids(WORK, "applied_a")
    raw = json.loads((WORK / "cloudtalk_calls.json").read_text())
    direction = {str((x.get("Cdr") or {}).get("id")): (x.get("Cdr") or {}).get("type") for x in raw}
    numbers = {str((x.get("Cdr") or {}).get("id")): (x.get("Cdr") or {}).get("public_external")
               for x in raw}
    if pend is None:
        def is_shell(p):          # 2026-10-13: an EMPTY duplicate made by the CloudTalk link must never win over the real person
            v = p["values"]
            return not ((v.get("relationship_stage") or [{}])[0].get("option")) and not v.get("company") and not v.get("email_addresses")

        byem, byph = {}, {}
        for p in c.query_all("people"):
            for e in p["values"].get("email_addresses", []):
                byem[e.get("email_address", "").lower()] = p
            for ph in p["values"].get("phone_numbers", []):
                d = dig(ph.get("original_phone_number") or ph.get("phone_number"))
                if len(d) == 10 and (d not in byph or (is_shell(byph[d]) and not is_shell(p))):
                    byph[d] = p
        scope = v
    else:
        scope = {cid: r for cid, r in v.items() if cid in pend}
        byem, byph = AF.index_people(c, [e for r in scope.values() for e in (r.get("emails") or [])],
                                     [AF.dig10(numbers.get(cid)) for cid in scope])
        print(f"   targeted: {len(scope)} new calls, {len(byem)} emails and {len(byph)} phones matched to people (no full read)")

    # --- bring in the outbound calls that produced a real conversation with someone we do not hold
    made = matched_by_phone = matched_by_name = 0
    phone_back = {}                  # record id -> (person, dialled number): people we reached on a number Attio does not hold
    import attio_fast as AF

    def want_phone(p, cid, r):
        # 2026-10-17: the number we dialled and had a real conversation on goes onto a person who has NO number,
        # so the next call matches by phone. Never on WRONG_PERSON (that number reached someone else).
        if p and r["verdict"] != "WRONG_PERSON" and numbers.get(cid) and not (p["values"].get("phone_numbers") or []):
            raw = str(numbers[cid]).strip()
            d = re.sub(r"\D", "", raw)
            if len(d) >= 10:
                phone_back.setdefault(p["id"]["record_id"], (p, raw if raw.startswith("+") else "+" + d))

    for cid, r in scope.items():
        ems = [e for e in (r.get("emails") or [])]
        if any(e in byem for e in ems):
            if r["verdict"] != "NO_CONVERSATION" and direction.get(cid) == "outgoing":
                want_phone(next(byem[e] for e in ems if e in byem), cid, r)
            continue
        # 2026-10-17: the phone match runs for EVERY call. It used to sit behind the "outgoing real conversation" test below, so a
        # NO_CONVERSATION or inbound call whose contact has no email never reached the person it belongs to, and got no note.
        ph = byph.get(dig(numbers.get(cid)))
        if ph:                       # already in the CRM, just under a different key
            key = f"cid:{cid}"
            byem[key] = ph
            r.setdefault("emails", []).append(key)
            matched_by_phone += 1
            continue
        if r["verdict"] == "NO_CONVERSATION" or direction.get(cid) != "outgoing":
            continue                 # naming or creating a person needs a real outbound conversation
        nm = (r.get("name") or "").strip()
        if not nm or nm.lower().startswith("lead from"):
            # no name at all — a number and a transcript is not enough to build a person on
            continue
        # 2026-10-17: the lead was dialled on a number found outside Attio (the positive-reply phone job) and the CloudTalk
        # contact has no email, so neither key matched and the call was dropped (or became a duplicate person). The ONE
        # person with exactly this full name is them: the note goes there and the number is added to them.
        pn = AF.by_fullname(c, nm)
        if pn:
            key = f"cid:{cid}"
            byem[key] = pn
            r.setdefault("emails", []).append(key)
            matched_by_name += 1
            want_phone(pn, cid, r)
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
        # never twice: a retry after a timeout first looks the person up by the email (else the phone) just sent
        st, np, _ = c.create_person(vals, email=ems[0] if ems else "", phone10=dig(numbers.get(cid))[-10:] if numbers.get(cid) else "")
        if st >= 300 or not np:
            print(f"   FAIL create: {st}")
            continue
        made += 1
        for e in ems:
            byem[e] = np
        if not ems:          # keyed by the call id so the note still finds its parent
            byem[f"cid:{cid}"] = np
            r.setdefault("emails", []).append(f"cid:{cid}")
    print(f"   {matched_by_phone} calls matched to an existing person BY PHONE")
    print(f"   {matched_by_name} calls matched to an existing person BY FULL NAME (no email on the contact, number not in Attio)")
    print(f"   {len(phone_back)} people reached on a number they do not hold in Attio: the number is added{'' if a.commit else ' (dry run)'}")
    if a.commit:
        for rid, (p, num) in phone_back.items():
            rr = c.req("PATCH", f"/objects/people/records/{rid}", json={"data": {"values": {"phone_numbers": [{"original_phone_number": num}]}}})
            if rr.status_code >= 300:
                print(f"   PHONE WRITE FAIL: {rr.status_code}")
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

    unmatched = sum(1 for r in (v if pend is None else scope).values() if not any(e in byem for e in (r.get("emails") or [])))   # in a targeted run only the new calls were looked up
    print(f"{len(v)} calls -> {len(per)} people in the CRM  ({unmatched} calls have no matching person)")
    for k, n in stats.most_common():
        print(f"   {n:>4}  {k}")
    prot = sum(1 for _, _, b, cur in plan if b and cur in PROTECTED)
    print(f"   {prot} are CUSTOMERS and will keep their state (note only)")

    # A NEW call whose newest real verdict is NOT_INTERESTED stops the lead's PlusVibe emails (Ife, 2026-10-16).
    # Never for a customer, never for WRONG_PERSON (that only marks the phone bad). See call_stop_plusvibe.py.
    pv_plan = [(p["id"]["record_id"], PVSTOP.person_emails(p)) for p, _, b, cur in plan
               if b and b["verdict"] in PVSTOP.STOP_VERDICTS and b["cid"] in scope and cur != "CUSTOMER"]
    hdr = PVSTOP.pv_header()
    camps = CO.pv_campaigns(hdr) if (hdr and pv_plan) else {}

    def pv_stops(commit: bool) -> set:
        """Runs the PlusVibe stops one person at a time. Returns the people whose stop did not fully work."""
        bad_people, n = set(), collections.Counter()
        if pv_plan and not hdr:
            print(f"   PLUS_VIBE_API_KEY not set: {len(pv_plan)} not-interested people NOT stopped in PlusVibe, kept for the next run")
            return {rid for rid, _ in pv_plan}
        for rid, ems in pv_plan:
            if not ems:
                n["no email"] += 1; continue
            res = PVSTOP.stop_person(ems, hdr, camps, commit, tag=rid)
            n["sequences live"] += res["live"]; n["stopped"] += res["stopped"]
            n["not in PlusVibe" if not res["found"] and not res["bad"] else "in PlusVibe"] += 1
            if res["bad"]:
                bad_people.add(rid); n["problems"] += res["bad"]
        if pv_plan:
            print(f"   PlusVibe ({'COMMIT' if commit else 'dry run'}): {len(pv_plan)} not-interested people, " + ", ".join(f"{k} {v}" for k, v in n.items()))
        return bad_people

    if not a.commit:
        pv_stops(False)
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

    # A booked meeting from a call opens a deal (Source channel = Cold call). Until 2026-10-12 only cold-email
    # replies made deals, so Charlie Lefever booked on the phone and never reached the pipeline. One deal per
    # person, ever: anyone who already has a deal of any stage (including Lost) is left alone so a manual close sticks.
    owner = c.req("GET", "/workspace_members").json()["data"][0]["id"]["workspace_member_id"]
    has_deal = {x.get("target_record_id") for d in c.query_all("deals")
                for x in (d["values"].get("associated_people") or [])}
    comp_name = {}
    deals_made = 0

    ctr = {"w": 0, "s": 0, "m": 0, "d": 0}
    lock = threading.Lock()
    failed = set()                      # people whose writes did not all go through: their calls stay pending for the next run

    def bump(k, n=1):
        with lock:
            ctr[k] += n

    def one(item):
        p, calls, best, cur = item
        rid = p["id"]["record_id"]
        if best and best["verdict"] == "MEETING_BOOKED" and cur not in PROTECTED and rid not in has_deal:
            cr = p["values"].get("company") or []
            cn = ""
            if cr:
                cid_ = cr[0]["target_record_id"]
                if cid_ not in comp_name:
                    rc = c.req("GET", f"/objects/companies/records/{cid_}")
                    comp_name[cid_] = ((rc.json()["data"]["values"].get("name") or [{}])[0].get("value") or "") if rc.status_code < 300 else ""
                cn = comp_name[cid_]
            pn = ((p["values"].get("name") or [{}])[0].get("full_name") or "").strip() or "Unknown"
            rd = c.req("PUT", "/objects/deals/records?matching_attribute=external_opportunity_id", json={"data": {"values": {
                "name": [{"value": f"{pn} / {cn}".strip(" /")}],
                "stage": [{"status": "Meeting Booked"}],
                "owner": [{"referenced_actor_type": "workspace-member", "referenced_actor_id": owner}],
                "external_opportunity_id": [{"value": "call-" + rid}],
                "source_channel": [{"option": "Cold call"}],
                "associated_people": [{"target_object": "people", "target_record_id": rid}]}}})
            if rd.status_code >= 300:
                print(f"   DEAL FAIL: {rd.status_code}"); failed.add(rid)
            else:
                bump("d")
                has_deal.add(rid)
        notes_now = c.req("GET", "/notes", params={
            "parent_object": "people", "parent_record_id": rid, "limit": 50}).json().get("data", [])
        have = {n.get("title") for n in notes_now}
        fresh = 0                       # calls that had no note yet: only a NEW call may change the person's state (a manual change made after the last call must stick)
        for x in calls:
            head, body = fmt(x)
            # date AND time in the title, because the same person can be rung several times a day with the same result
            title = f"Call · {(x.get('started') or '')[:10]} {(x.get('started') or '')[11:16]} · {x['verdict'].replace('_',' ').title()}"
            if title in have:
                bump("s")
                continue
            # a corrected result for the SAME call (the caller's note arrived or changed): replace the old note instead of leaving two that disagree
            prefix = title.rsplit(" · ", 1)[0] + " · "
            for n in notes_now:
                t_old = n.get("title") or ""
                nid = (n.get("id") or {}).get("note_id")
                if nid and t_old.startswith(prefix) and t_old != title and t_old.startswith("Call · "):
                    c.req("DELETE", f"/notes/{nid}")
                    have.discard(t_old)
            # create_note never posts twice: a retry first looks for this call's ref line (a timed-out POST may have written)
            st, _, _ = c.create_note({
                "parent_object": "people", "parent_record_id": rid,
                "title": title, "format": "plaintext", "content": head + "\n\n" + body},
                marker=f"ct-call-{x['cid']}-{x['verdict']}")
            bump("w", int(st < 300))
            fresh += st < 300
            if st >= 300:
                failed.add(rid)
        if not best or cur in PROTECTED or not fresh:
            return
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
            print(f"   FAIL: {rr.status_code}"); failed.add(rid)
        else:
            bump("m")

    with ThreadPoolExecutor(max_workers=6) as ex:       # people are independent: write six at a time
        list(ex.map(one, plan))
    failed |= pv_stops(True)                             # after the threads, one person at a time: PlusVibe 429s hard. A failed stop keeps the call pending
    written, skipped, moved, deals_made = ctr["w"], ctr["s"], ctr["m"], ctr["d"]
    if pend is not None:                                 # the calls of people whose writes all worked are done; the rest are tried again next run
        failed_cids = {x["cid"] for p, calls, best, cur in plan if p["id"]["record_id"] in failed for x in calls}
        print(f"   {AF.mark_done(WORK, 'applied_a', pend - failed_cids)} calls marked as applied, {len(failed_cids)} left for the next run")
    print(f"\n{written} notes written, {skipped} already there, {moved} people updated, {deals_made} deals opened from booked calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
