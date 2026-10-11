"""
calls_to_call_lists.py — put what happened on a call back onto the queue entry.

This is what makes the dialler queue self-cleaning. Without it an agent re-rings people who
already booked, already said no, or already turned out to be the wrong person, because the
entry has no memory of the call.

Sources, in order of authority:
  * data/working/call_verdicts.json — the transcript verdicts. CloudTalk has NO disposition
    tags configured and none of the 963 historical calls carried one, so a transcript is the
    only real record of how a call went.
  * data/working/cloudtalk_calls.json — attempts, talk time and the last call date, which
    exist even when a call was too short to transcribe.

A verdict that ends the relationship (MEETING_BOOKED, NOT_INTERESTED, DO_NOT_CALL,
WRONG_PERSON) is also pushed onto the Person, so they stop being eligible everywhere, not just
on this one list.

    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/calls_to_call_lists.py
    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/calls_to_call_lists.py --commit
"""
from __future__ import annotations
import argparse, json, os, re, sys
from datetime import datetime, timezone
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient

SCHEMA = ROOT / "crm/schema/attio-schema.yaml"
# CALL_DATA_DIR lets the hourly cloud job point these at its own folder, so the Mac's full history is never overwritten
WORK = Path(os.environ.get("CALL_DATA_DIR") or ROOT / "data/working")
VERDICTS = WORK / "call_verdicts.json"
CALLS = WORK / "cloudtalk_calls.json"

# verdict -> (relationship_stage, outreach_status, keep calling?)
ENDS_IT = {
    "MEETING_BOOKED": ("MEETING", "PAUSED", False),
    "NOT_INTERESTED": ("LOST", "PAUSED", False),
    "DO_NOT_CALL":    ("LOST", "EXCLUDED", False),
}
VALID = {"MEETING_BOOKED", "SEND_FOLLOW_UP", "CALLBACK", "NOT_NOW", "NOT_INTERESTED",
         "WRONG_PERSON", "DO_NOT_CALL", "NO_ANSWER", "VOICEMAIL", "NO_CONVERSATION", "UNKNOWN"}
REAL = {"MEETING_BOOKED", "SEND_FOLLOW_UP", "CALLBACK", "NOT_NOW", "NOT_INTERESTED", "WRONG_PERSON", "DO_NOT_CALL"}


def ts(x):
    """Parse CloudTalk (+02:00) and Attio (…Z with nanoseconds) timestamps into comparable aware datetimes."""
    x = str(x or "").replace("Z", "+00:00")
    m = re.match(r"(.*\.\d{6})\d*(.*)", x)
    if m:
        x = m.group(1) + m.group(2)
    try:
        d = datetime.fromisoformat(x)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pick(vs):
    """One disposition per lead: their latest REAL conversation if they had one, otherwise their latest call."""
    vs = sorted(vs, key=lambda v: v.get("started") or "")
    real = [v for v in vs if v["verdict"] in REAL]
    return (real or vs)[-1]


def phone_key(p) -> str:
    d = re.sub(r"\D", "", str(p or ""))
    return d[-10:] if len(d) >= 10 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--close-people", action="store_true",
                    help="also change people's status here. Off by default: apply_call_verdicts.py owns person status, and "
                         "doing it in both places let this script overwrite a DO_NOT_CONTACT with a weaker EXCLUDED")
    a = ap.parse_args()

    spec = yaml.safe_load(SCHEMA.read_text())
    c = AttioClient(workers=8)
    lists = {l["api_slug"]: l for l in c.req("GET", "/lists").json()["data"]}

    # --- what the phone knows, keyed by email and by last-10-digits ---
    stats = defaultdict(lambda: {"n": 0, "talk": 0, "at": ""})
    for row in json.loads(CALLS.read_text()):
        cdr = row.get("Cdr") or {}
        if (cdr.get("type") or "") != "outgoing":
            continue
        ct = row.get("Contact") or {}
        keys = [e.strip().lower() for e in (ct.get("contact_emails") or [])]
        keys += [phone_key(n) for n in (ct.get("contact_numbers") or [])]
        for k in [x for x in keys if x]:
            s = stats[k]
            s["n"] += 1
            s["talk"] = max(s["talk"], int(cdr.get("talking_time") or 0))
            s["at"] = max(s["at"], str(cdr.get("started_at") or ""))
            s.setdefault("times", []).append(str(cdr.get("started_at") or ""))

    calls_by_id = {str((r.get("Cdr") or {}).get("id")): r for r in json.loads(CALLS.read_text())}
    by_key = defaultdict(list)
    for cid, v in json.loads(VERDICTS.read_text()).items():
        if v.get("verdict") not in VALID:
            continue
        row = calls_by_id.get(str(cid)) or {}
        keys = [e.strip().lower() for e in (v.get("emails") or [])]
        # a lead with no email on the call (e.g. found by phone) is matched by the number dialled
        keys += [phone_key(n) for n in ((row.get("Contact") or {}).get("contact_numbers") or [])]
        for k in {x for x in keys if x}:
            by_key[k].append(v)
    verdicts = {k: pick(vs) for k, vs in by_key.items()}

    print(f"{len(stats):,} call keys, {len(verdicts):,} transcript verdicts")
    if not stats and not verdicts:             # nothing dialled in this window: skip loading all of Attio
        print("no calls to write yet")
        return 0

    print("loading Attio people...")
    people = {p["id"]["record_id"]: p for p in c.query_all("people")}
    emails_of = {}
    phones_of = {}
    for rid, p in people.items():
        emails_of[rid] = [e.get("email_address", "").lower()
                          for e in (p["values"].get("email_addresses") or [])]
        phones_of[rid] = [phone_key(x.get("original_phone_number") or x.get("phone_number"))
                          for x in (p["values"].get("phone_numbers") or [])]

    plan, person_updates = [], {}
    counts = Counter()
    for L in spec["call_lists"]:
        slug = ("call_" + re.sub(r"[^a-z0-9]+", "_", L["name"].lower()).strip("_"))[:50]
        meta = lists.get(slug)
        if not meta:
            continue
        lid = meta["id"]["list_id"]
        ents, off = [], 0
        while True:
            e = c.req("POST", f"/lists/{lid}/entries/query",
                      json={"limit": 500, "offset": off}).json()["data"]
            ents += e
            if len(e) < 500:
                break
            off += 500

        for e in ents:
            rid = e["parent_record_id"]
            ev = e.get("entry_values") or {}
            entry_phone = phone_key((ev.get("phone") or [{}])[0].get("value"))
            keys = [k for k in emails_of.get(rid, []) + phones_of.get(rid, []) + [entry_phone] if k]
            st = next((stats[k] for k in keys if k in stats), None)
            vd = next((verdicts[k] for k in keys if k in verdicts), None)
            if not st and not vd:
                continue

            vals = {}
            old_at = ts(((ev.get("called_at") or [{}])[0]).get("value"))
            old_n = int(((ev.get("attempts") or [{}])[0]).get("value") or 0)
            old_talk = int(((ev.get("talk_seconds") or [{}])[0]).get("value") or 0)
            old_out = (((ev.get("call_outcome") or [{}])[0]).get("option") or {}).get("title")
            if st:
                # The cloud run only sees recent calls, so attempts ADD the calls newer than the last one already counted
                # on the entry (re-running the same window adds nothing).
                new_calls = [t for t in st.get("times", []) if old_at is None or (ts(t) and ts(t) > old_at)]
                vals["called"] = [{"value": True}]
                vals["attempts"] = [{"value": old_n + len(new_calls)}]
                vals["talk_seconds"] = [{"value": max(old_talk, st["talk"])}]
                if st["at"]:
                    latest = max([ts(st["at"])] + ([old_at] if old_at else []))
                    vals["called_at"] = [{"value": latest.isoformat()}]
            if vd and old_out in REAL and vd["verdict"] not in REAL:
                vd = None                       # keep the real result on the entry: a later voicemail must not replace it
            if vd:
                vals["call_outcome"] = [{"option": vd["verdict"]}]
                if vd.get("summary"):
                    vals["call_summary"] = [{"value": vd["summary"][:2000]}]
                counts[vd["verdict"]] += 1
                if vd["verdict"] in ENDS_IT:
                    rel, out, _ = ENDS_IT[vd["verdict"]]
                    person_updates[rid] = (rel, out)
            elif st and st["talk"] < 30 and old_out not in REAL:
                vals["call_outcome"] = [{"option": "NO_ANSWER"}]
                counts["NO_ANSWER"] += 1
            plan.append((lid, e["id"]["entry_id"], rid, vals))

    print(f"\n{len(plan):,} entries would be stamped with call history")
    for k, n in counts.most_common():
        print(f"   {k:<16} {n:>4}")
    print(f"\n{len(person_updates):,} people would also be taken off the call rotation "
          f"(booked, not interested, wrong person or do-not-call)")

    if not a.commit:
        print("\nDRY RUN — nothing written. Pass --commit.")
        return 0

    def stamp(it):
        lid, eid, rid, vals = it
        return c.req("PATCH", f"/lists/{lid}/entries/{eid}",
                     json={"data": {"entry_values": vals}}).status_code

    res = c.map(stamp, plan, label="entries ")
    bad = [x for x in res if x >= 300]
    print(f"{len(res)-len(bad):,} entries stamped, {len(bad)} failed")

    def close(it):
        rid, (rel, out) = it
        return c.req("PATCH", f"/objects/people/records/{rid}", json={"data": {"values": {
            "relationship_stage": [{"option": rel}],
            "outreach_status": [{"option": out}],
            "outreach_paused": [{"value": True}],
        }}}).status_code

    if not a.close_people:
        print("people's status left to apply_call_verdicts.py")
        return 0
    res2 = c.map(close, list(person_updates.items()), label="people  ")
    bad2 = [x for x in res2 if x >= 300]
    print(f"{len(res2)-len(bad2):,} people updated, {len(bad2)} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
