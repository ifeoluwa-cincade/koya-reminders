"""
positive_replies_to_call_queue.py — an interested person with a phone number lands in the dialler by itself.

Ife, 2026-10-04: "the positive reply comes from PlusVibe, then the details get updated into our CRM, then the
numbers get updated into the CRM, then whoever has a number goes into CloudTalk." positive_replies_to_deals.py and
deals_follow_labels.py already turn a reply into an Interested / Later deal. This is the rest of the chain, run in
the same 20-minute GitHub job:

  open cold email deal (Interested or Later), not ruled out
      -> phone: the Attio person's own number, else their call-list entry, else AI Ark (key 2)
      -> number written onto the Attio person
      -> entry on the Attio "AI Agency Positive Replies" list
      -> CloudTalk contact found BY PHONE (created only if none exists), id written onto the entry
      -> tagged "Attio | Positive Replies", read back until CloudTalk agrees (addTags lies)

Ruled out (relationship LOST / CUSTOMER, do-not-contact, a NOT_INTERESTED / WRONG_PERSON label in PlusVibe, or
every deal closed) -> entry removed and the tag taken off, so they leave Temi's queue. People who are on the list
without an open deal (the old hand-built Replies queue) are left alone unless ruled out.

AI Ark is called once per person: hits and misses go to data/caches/aiark_phone_finder_cache.json (shared with
tools/plusvibe_positive_replies.py), which the GitHub job carries between runs with actions/cache.

    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/positive_replies_to_call_queue.py
    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/positive_replies_to_call_queue.py --commit
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient
from deals_follow_labels import live_labels
from verify_cloudtalk_tags import auth, carriers, ct, first

LIST = "call_ai_agency_positive_replies"
TAG = "Attio | Positive Replies"
OPEN = {"Interested", "Multi-touch", "Later"}
ARK_URL = "https://api.ai-ark.com/api/developer-portal/v2/people/mobile-phone-finder"
CACHE = ROOT / "data/caches/aiark_phone_finder_cache.json"
PERSONAL = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "me.com",
            "aol.com", "live.com", "proton.me", "protonmail.com", "gmx.com", "msn.com"}


def secret(n):
    if os.environ.get(n):
        return os.environ[n].strip()
    env = ROOT / ".env"
    for l in (env.read_text().splitlines() if env.exists() else []):
        if l.startswith(n + "="):
            return l.split("=", 1)[1].strip()
    sys.exit(f"{n} missing")


def one(rec, k):
    v = (rec.get("values") or {}).get(k) or []
    if not v:
        return None
    x = v[0]
    return (x.get("status") or x.get("option") or {}).get("title") or x.get("value")


def e164(raw: str) -> str:
    d = re.sub(r"[^\d+]", "", raw or "")
    if not d:
        return ""
    return d if d.startswith("+") else "+" + d.lstrip("0")


def sheet_phones() -> dict:
    """email -> phone from the 'Positive Reply Call List' tab, which plusvibe_auto_phone_lookup.py (the Discord job)
    fills the moment a positive reply is found (Prospeo first, AI Ark second). Reading it here means a number the
    Discord job already paid for lands on the Attio person instead of being looked up (and paid for) again.
    Fails soft: no credentials / no access -> {} and the old sources carry on."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if raw:
            creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(str(ROOT / "secrets/service_account.json"), scopes=scopes)
        ws = gspread.authorize(creds).open_by_key(os.environ["CALLLIST_SHEET_ID"]).worksheet("Positive Reply Call List")
        rows = ws.get_all_records()
    except Exception as ex:
        print(f"(positive reply sheet not readable: {type(ex).__name__} — skipping that source)")
        return {}
    out = {}
    for r in rows:
        em = str(r.get("Email") or "").strip().lower()
        ph = str(r.get("Phone") or "").strip() or str(r.get("Phone 2") or "").strip()
        if em and ph:
            out[em] = ph
    return out


def ark_phone(key, linkedin="", domain="", name=""):
    payload = {"linkedin": linkedin} if linkedin else {"domain": domain, "name": name}
    r = requests.post(ARK_URL, headers={"Content-Type": "application/json", "X-TOKEN": key}, json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        return None                                   # transient: don't cache, try next run
    if r.status_code >= 500 or r.status_code == 429:
        return None
    nested = (body.get("data") or {}).get("data") or [] if body.get("status") == 200 else []
    return nested[0][0] if nested and nested[0] else ""


def _why(r):
    """What went wrong, without echoing the response body (it can contain lead details)."""
    try:
        j = r.json(); return str(j.get('code') or j.get('type') or '')[:60]
    except Exception:
        return ''


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    H = auth()
    c = AttioClient(workers=6)

    # 1. who should be in the queue
    deal_of = {}
    for d in c.query_all("deals"):
        for x in d["values"].get("associated_people") or []:
            deal_of.setdefault(x.get("target_record_id"), []).append((one(d, "stage"), one(d, "source_channel")))
    no_in_pv = {e for e, (lab, _, _) in live_labels({"x-api-key": secret("PLUS_VIBE_API_KEY")}).items()
                if lab in ("NOT_INTERESTED", "WRONG_PERSON")}

    lid = next(l["id"]["list_id"] for l in c.req("GET", "/lists").json()["data"] if l["api_slug"] == LIST)
    entries, off = [], 0
    while True:
        e = c.req("POST", f"/lists/{lid}/entries/query", json={"limit": 500, "offset": off}).json()["data"]
        entries += e
        if len(e) < 500:
            break
        off += 500
    entry_of = {e["parent_record_id"]: e for e in entries}

    wanted = {rid for rid, ds in deal_of.items() if any(s in OPEN and ch == "Cold email" for s, ch in ds)}
    people = {}
    for rid in wanted | set(entry_of):
        r = c.req("GET", f"/objects/people/records/{rid}")
        if r.status_code < 300:
            people[rid] = r.json()["data"]

    def emails(p):
        return [(x.get("email_address") or "").lower() for x in p["values"].get("email_addresses") or []]

    def ruled_out(rid):
        p = people.get(rid)
        if not p:
            return True
        if one(p, "relationship_stage") in ("LOST", "CUSTOMER") or one(p, "dnc_status") == "DO_NOT_CONTACT":
            return True
        if any(e in no_in_pv for e in emails(p)):
            return True
        sts = {s for s, _ in deal_of.get(rid, [])}
        return bool(sts) and not (sts & OPEN)

    add = sorted(rid for rid in wanted if not ruled_out(rid))
    remove = [entry_of[rid] for rid in entry_of if ruled_out(rid)]

    # 2. phone numbers
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    ark_key = secret("AI_ARK_KEY_2")
    now = datetime.now(timezone.utc).isoformat()
    plan, nophone, looked = [], [], 0
    from_sheet_map = sheet_phones()
    for rid in add:
        p = people[rid]; v = p["values"]
        name = ((v.get("name") or [{}])[0].get("full_name") or "").strip()
        phone = next((x.get("original_phone_number") or x.get("phone_number") for x in v.get("phone_numbers") or []), "")
        on_person = bool(phone)                    # already on the Attio person -> nothing to write back
        ev = (entry_of.get(rid) or {}).get("entry_values") or {}
        phone = phone or first(ev, "phone") or ""
        if not phone:
            phone = next((from_sheet_map[e] for e in emails(p) if e in from_sheet_map), "")
        from_ark = False
        if not phone:
            li = (one(p, "linkedin") or "").strip()
            dom = next((e.split("@", 1)[1] for e in emails(p) if "@" in e and e.split("@", 1)[1] not in PERSONAL), "")
            key = li or (f"{dom}|{name}" if dom and name else "")
            if key:
                if key in cache:
                    phone = cache[key] or ""
                elif a.commit:
                    got = ark_phone(ark_key, linkedin=li, domain=dom, name=name); looked += 1
                    time.sleep(0.25)
                    if got is not None:
                        cache[key] = got
                        CACHE.parent.mkdir(parents=True, exist_ok=True)
                        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
                    phone, from_ark = got or "", bool(got)
                else:
                    nophone.append(f"{name} (would ask AI Ark)"); continue
        phone = e164(phone)
        if phone:
            plan.append((rid, name, phone, from_ark or not on_person, first(ev, "cloudtalk_contact_id")))
        else:
            nophone.append(name or rid)

    print(f"open interested/later people: {len(add)}  with a number: {len(plan)}  AI Ark lookups this run: {looked}")
    print(f"no number: {len(nophone)}")
    print(f"to remove (ruled out since): {len(remove)}")
    if not a.commit:
        print("DRY RUN — pass --commit."); return 0

    # 3. Attio person phone, CloudTalk contact, list entry
    bad = 0
    for rid, name, phone, write_person, cid in plan:
        if write_person:                       # the number came from the sheet / AI Ark / the queue entry, not the person
            c.req("PATCH", f"/objects/people/records/{rid}",
                  json={"data": {"values": {"phone_numbers": [{"original_phone_number": phone}]}}})
        if not cid:
            rows = (ct("GET", "/contacts/index.json", H, params={"keyword": phone, "limit": 5})
                    .json().get("responseData", {}) or {}).get("data") or []
            cid = str((rows[0].get("Contact", rows[0])).get("id")) if rows else None
        if not cid:
            mails = emails(people[rid])
            r = ct("POST", "/bulk/contacts.json", H, json=[{"action": "add_contact", "command_id": "1", "data": {
                "name": name or phone, "ContactEmail": [{"email": mails[0]}] if mails else [],
                "ContactNumber": [{"public_number": phone}], "ContactsTag": [{"name": TAG}]}}])
            res = ((r.json().get("responseData") or {}).get("data") or [{}])[0] if r.status_code < 300 else {}
            cid = str((res.get("data") or {}).get("id") or "") or None
            if not cid:
                bad += 1; print("   CloudTalk create failed", r.status_code, _why(r)); continue
            print("   created a CloudTalk contact")
        r = c.req("PUT", f"/lists/{lid}/entries", json={"data": {"parent_record_id": rid, "parent_object": "people",
                  "entry_values": {"phone": [{"value": phone}], "cloudtalk_contact_id": [{"value": str(cid)}],
                                   "cloudtalk_tag": [{"value": TAG}], "queued_at": [{"value": now}]}}})
        if r.status_code >= 300:
            bad += 1; print("   ENTRY FAIL", r.status_code, _why(r))
        elif rid not in entry_of:
            print("   + queued one person")

    tid = next(int((x.get("Tag", x))["id"]) for x in ct("GET", "/tags/index.json", H, params={"limit": 200})
               .json()["responseData"]["data"] if (x.get("Tag", x))["name"] == TAG)
    for e in remove:
        cid = first(e.get("entry_values") or {}, "cloudtalk_contact_id")
        if cid:
            ct("DELETE", f"/contacts/removeTags/{cid}.json", H, json={"tags": [TAG]})
        c.req("DELETE", f"/lists/{lid}/entries/{e['id']['entry_id']}")
        print(f"   - removed {e['parent_record_id']} (ruled out)")

    # 4. tag every entry on the list, then read back until CloudTalk agrees
    entries, off = [], 0
    while True:
        e = c.req("POST", f"/lists/{lid}/entries/query", json={"limit": 500, "offset": off}).json()["data"]
        entries += e
        if len(e) < 500:
            break
        off += 500
    want = {str(first(e.get("entry_values") or {}, "cloudtalk_contact_id")) for e in entries
            if first(e.get("entry_values") or {}, "cloudtalk_contact_id")}
    # anyone carrying the tag who is NOT on the list leaves the queue: people since ruled out, and the second
    # CloudTalk record of a person who exists twice there (else Temi rings them twice)
    extra = carriers(tid, H) - want
    for rnd in range(4):
        if not extra:
            break
        for cid in sorted(extra):
            ct("DELETE", f"/contacts/removeTags/{cid}.json", H, json={"tags": [TAG]}); time.sleep(0.35)
        extra = carriers(tid, H) - want
    if extra:
        print(f"   tag would not come off {len(extra)} contacts: {sorted(extra)}")
    missing = want - carriers(tid, H)
    for rnd in range(4):
        if not missing:
            break
        for cid in sorted(missing):
            ct("PUT", f"/contacts/addTags/{cid}.json", H, json={"tags": [TAG]}); time.sleep(0.35)
        missing = want - carriers(tid, H)
    print(f"Positive Replies: {len(want)} on the Attio list, {len(want) - len(missing)} tagged in CloudTalk"
          + (f", STILL MISSING {len(missing)}" if missing else ""))
    return 1 if (bad or missing or extra) else 0


if __name__ == "__main__":
    raise SystemExit(main())
