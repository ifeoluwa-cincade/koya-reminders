"""
positive_replies_to_deals.py — every positive cold email reply becomes a deal.

Ife, 2026-10-03: track the funnel clinically. The booking rate is meetings booked ÷ positive
replies, so it is only true if EVERY positive reply exists as a deal. Before this, deals were only
made for people Attio called INTERESTED; PlusVibe's own positive labels also include CYCLE_1,
CYCLE_2 and MEETING_BOOKED, and sync_plusvibe reads those as ENGAGED, so they never got a deal.

Positive = the CURRENT PlusVibe label is one of POSITIVE (the same set the Discord positive-reply
alert uses). A lead whose label has since been downgraded to NOT_INTERESTED is not an open
opportunity and gets no deal.

AI AGENCY CAMPAIGNS ONLY (Ife, 2026-10-03: "AI Agency Owners is the campaign", the other 38 stay
out of Attio). Freight, Ecom Ops, Marketing Ops etc. positive replies are NOT turned into deals —
doing so would pull those campaigns into the CRM by the back door.

People are matched by email first, then by first name + company name, because the same person can
reply from a different address (Ruben at Revenue AI Agency vs the Ruben Scholtz record).

One deal per person: anyone who already has a deal that isn't Lost is left alone, so this never
duplicates the int- deals or the placement-tracker deals. Idempotent via external_opportunity_id
"pv-<hash of email>".

    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/positive_replies_to_deals.py
    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/positive_replies_to_deals.py --commit
"""
from __future__ import annotations
import os
import argparse, hashlib, json, sys, time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient
from normalize import clean_email, first_name, linkedin_slug

PV = "https://api.plusvibe.ai/api/v1"
WS = "6a6e634971ad0f0e174a5471"
# LATER is included (Ife, 2026-10-12): a lead labelled Later is an opportunity too, as a deal in the Later stage. Before, a LATER lead who never had a deal never appeared.
POSITIVE = {"INTERESTED", "MEETING_BOOKED", "CYCLE_1", "CYCLE_2", "LATER"}
CACHE = ROOT / "data/caches/plusvibe_positive_for_deals.json"


def env() -> dict:
    return ({**{l.split("=", 1)[0]: l.split("=", 1)[1].strip()
             for l in ((ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else [])
             if "=" in l and not l.startswith("#")}, **os.environ})


def pv_get(path, params, H, tries=6):
    """A 429 from PlusVibe looks exactly like an empty campaign — always back off and retry."""
    delay = 2.0
    for _ in range(tries):
        try:
            r = requests.get(PV + path, headers=H, params=params, timeout=120)
            if r.status_code != 429:
                return r
        except requests.exceptions.RequestException:
            pass
        time.sleep(delay); delay *= 2
    return r


def positive_leads(H) -> list[dict]:
    out = []
    camps = pv_get("/campaign/list-all", {"workspace_id": WS}, H).json()
    for c in camps:
        cid, name = c.get("id"), c.get("name") or c.get("camp_name") or ""
        page = 1
        while True:
            rows = pv_get("/lead/workspace-leads", {"workspace_id": WS, "campaign_id": cid,
                          "status": "REPLIED", "limit": 500, "page": page}, H).json() or []
            if not isinstance(rows, list):
                break
            for l in rows:
                if (l.get("label") or "") in POSITIVE and "ai agency" in name.lower():
                    out.append({**{k: l.get(k) for k in ("email", "first_name", "last_name", "company_name",
                                "company_website", "linkedin_person_url", "label", "modified_at")},
                                "campaign": name})
            if len(rows) < 500:
                break
            page += 1
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(out, indent=1))
    return out


def one(rec, k):
    v = (rec.get("values") or {}).get(k) or []
    if not v:
        return None
    x = v[0]
    return (x.get("status") or x.get("option") or {}).get("title") or x.get("value")


def _why(r):
    """What went wrong, without echoing the response body (it can contain lead details)."""
    try:
        j = r.json(); return str(j.get('code') or j.get('type') or '')[:60]
    except Exception:
        return ''


_ASKS = ("send me", "send over", "send us", "more info", "more detail", "details", "information", "profile", "pricing", "price", "rates", "how much", "sample",
         "deck", "case stud", "interested", "tell me more", "would like", "love to", "let's talk", "lets talk", "call me", "book", "schedule", "available")


def stage_for(H, e: str, label: str) -> str:
    """The deal stage for a NEW deal (Ife, 2026-10-12). Cycle 1 / Cycle 2 mean we have already sent a response, so Multi-touch. For LATER the email decides:
    we already replied -> Multi-touch; they ask for details or a follow-up -> Interested; otherwise Later."""
    if label == "MEETING_BOOKED":
        return "Meeting Booked"
    if label in ("CYCLE_1", "CYCLE_2"):
        return "Multi-touch"
    if label != "LATER":
        return "Interested"
    try:
        r = pv_get("/unibox/emails", {"workspace_id": WS, "lead": e, "preview_only": "false"}, H)
        items = (r.json().get("data") or []) if r.status_code == 200 else []
    except Exception:
        items = []
    mine = [x for x in items if (x.get("from_address_email") or "").lower() == e.lower()]
    if not mine:
        return "Later"
    last_in = max(x.get("timestamp_created") or "" for x in mine)
    if any((x.get("from_address_email") or "").lower() != e.lower() and (x.get("timestamp_created") or "") > last_in for x in items):
        return "Multi-touch"                                   # we have already answered them
    newest = max(mine, key=lambda x: x.get("timestamp_created") or "")
    text = (newest.get("content_preview") or newest.get("body") or "")
    text = " ".join(str(text).replace("<", " <").split()).lower()
    return "Interested" if any(w in text for w in _ASKS) else "Later"


def create_for(c, owner, e, l, rid):
    """Create the person (when missing) and the deal for one positive reply. Returns (person_record_id, ok).
    Shared by the full sync and the one-reply fast path, so both behave identically. The deal is an upsert on its external id,
    so running it twice never makes two deals."""
    full = " ".join(x for x in (l.get("first_name"), l.get("last_name")) if x).strip()
    if not rid:
        vals = {"email_addresses": [{"email_address": e}],
                "relationship_stage": [{"option": "ENGAGED" if l["label"] == "LATER" else "INTERESTED"}],
                "current_campaign": [{"value": l["campaign"]}],
                "legacy_source": [{"value": f"PlusVibe positive reply — {l['campaign']}"}]}
        if full:
            parts = full.split()
            vals["name"] = [{"first_name": first_name(full), "last_name": " ".join(parts[1:]), "full_name": full}]
        sl, st = linkedin_slug(l.get("linkedin_person_url") or "")
        if st == "VALID_PROFILE":
            vals["linkedin_slug"] = [{"value": sl}]
            _li = (l.get("linkedin_person_url") or "").strip()
            if _li.startswith("http") and _li.isascii():
                vals["linkedin"] = [{"value": _li}]
        st, np, _ = c.create_person(vals, email=e)      # a retry after a timeout finds the person it made instead of failing on the taken email
        if st >= 300 or not np:
            print(f"   PERSON FAIL {st}")
            return None, False
        rid = np["id"]["record_id"]
    title = f"{full or e} / {l.get('company_name') or ''}".strip(" /")
    r = c.req("PUT", "/objects/deals/records?matching_attribute=external_opportunity_id", json={"data": {"values": {
        "name": [{"value": title}],
        "stage": [{"status": l.get("stage") or ("Meeting Booked" if l["label"] == "MEETING_BOOKED" else "Interested")}],
        "owner": [{"referenced_actor_type": "workspace-member", "referenced_actor_id": owner}],
        "external_opportunity_id": [{"value": "pv-" + hashlib.sha1(e.encode()).hexdigest()[:16]}],
        "source_channel": [{"option": "Cold email"}],
        "source_campaign": [{"value": l["campaign"]}],
        "associated_people": [{"target_object": "people", "target_record_id": rid}]}}})
    if r.status_code >= 300:
        print(f"   DEAL FAIL {r.status_code} {_why(r)}")
        return rid, False
    return rid, True


def fast(email: str, commit: bool) -> int:
    """ONE reply, handled in seconds. Looks up just this person instead of reading all 16,000. Anything unusual (no exact match in
    the CRM, no positive label yet, already has a deal, closed) is left to the full hourly sync, which uses the same rules."""
    H = {"x-api-key": env()["PLUS_VIBE_API_KEY"]}
    e, _ = clean_email(email)
    if not e:
        print("no usable email in the signal; the full sync will handle it"); return 0
    r = pv_get("/lead/get", {"workspace_id": WS, "email": e}, H)
    rows = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    lead = None
    for x in rows:
        ld = x.get("lead_data") or {}
        name = x.get("campaign_name") or ""
        label = ld.get("label") or x.get("label") or ""
        if label in POSITIVE and "ai agency" in name.lower() and (x.get("status") or ld.get("status")) == "REPLIED":
            lead = {"email": e, "first_name": ld.get("first_name"), "last_name": ld.get("last_name"), "company_name": ld.get("company_name"),
                    "company_website": ld.get("company_website"), "linkedin_person_url": ld.get("linkedin_person_url"),
                    "label": label, "campaign": name}
            if label == "MEETING_BOOKED":
                break
    if not lead:
        print("no positive label in an AI-agency campaign yet; the hourly sync will catch it"); return 0
    c = AttioClient(workers=2)
    ppl = c.req("POST", "/objects/people/records/query", json={"filter": {"email_addresses": e}, "limit": 5}).json().get("data", [])
    if len(ppl) != 1:
        print(f"{len(ppl)} exact matches in the CRM; the full sync decides (it also matches by name and company)"); return 0
    p = ppl[0]; rid = p["id"]["record_id"]
    if one(p, "relationship_stage") in ("LOST", "CUSTOMER"):
        print("closed person; nothing to do"); return 0
    ext = "pv-" + hashlib.sha1(e.encode()).hexdigest()[:16]
    same = c.req("POST", "/objects/deals/records/query", json={"filter": {"external_opportunity_id": ext}, "limit": 5}).json().get("data", [])
    if same:
        print("this reply already has a deal (open or closed); nothing to do"); return 0
    mine = c.req("POST", "/objects/deals/records/query", json={"filter": {"associated_people": {"target_object": "people", "target_record_id": rid}}, "limit": 50}).json().get("data", [])
    if any(one(d, "stage") != "Lost" for d in mine):
        print("this person already has a deal; nothing to do"); return 0
    lead["stage"] = stage_for(H, e, lead["label"])
    print(f"positive reply ({lead['label']}), exact match, no deal yet -> stage {lead['stage']}; {'creating the deal' if commit else 'DRY RUN, nothing created'}")
    if not commit:
        return 0
    owner = c.req("GET", "/workspace_members").json()["data"][0]["id"]["workspace_member_id"]
    _, ok = create_for(c, owner, e, lead, rid)
    print("deal created" if ok else "deal FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--only-email", default="", help="handle ONE reply fast (no full read of the CRM)")
    ap.add_argument("--from-event", action="store_true", help="read the replier's email from the GitHub event file (keeps it out of the logs)")
    ap.add_argument("--skip", default="", help="comma-separated emails Ife judged not real interest")
    a = ap.parse_args()
    if a.from_event:
        try:
            ev = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        except Exception:
            ev = {}
        a.only_email = ((ev.get("client_payload") or {}).get("email") or (ev.get("inputs") or {}).get("email") or "").strip()
        if not a.only_email:
            print("no email in the signal; the full sync will handle it"); return 0
    if a.only_email:
        return fast(a.only_email, a.commit)
    skip = {s.strip().lower() for s in a.skip.split(",") if s.strip()}
    H = {"x-api-key": env()["PLUS_VIBE_API_KEY"]}

    print("pulling positive replies from PlusVibe...")
    leads = positive_leads(H)
    by_email = {}
    for l in leads:                      # one row per person; MEETING_BOOKED outranks the rest
        e, st = clean_email(l.get("email"))
        if not e or e in skip:
            continue
        if e not in by_email or l["label"] == "MEETING_BOOKED":
            by_email[e] = l
    print(f"  {len(by_email)} people with a positive label right now")

    c = AttioClient(workers=6)
    owner = c.req("GET", "/workspace_members").json()["data"][0]["id"]["workspace_member_id"]
    def norm(s):
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())
    rid_of, closed_people = {}, set()
    if os.environ.get("FULL_INDEX"):
        people = c.query_all("people")                    # the old way: read everyone (kept to compare against)
        pid = {}
        for p in people:
            for x in (p["values"].get("email_addresses") or []):
                pid.setdefault((x.get("email_address") or "").lower(), p["id"]["record_id"])
        comp = {co["id"]["record_id"]: (co["values"].get("name") or [{}])[0].get("value") or ""
                for co in c.query_all("companies")}
        by_namecomp = {}
        for p in people:
            nm = ((p["values"].get("name") or [{}])[0].get("first_name") or "")
            cr = p["values"].get("company") or []
            cn = comp.get(cr[0]["target_record_id"], "") if cr else ""
            if nm and cn:
                by_namecomp.setdefault((norm(nm), norm(cn)), p["id"]["record_id"])
        for e, l in by_email.items():
            rid_of[e] = pid.get(e) or by_namecomp.get((norm(l.get("first_name")), norm(l.get("company_name"))))
        closed_people = {p["id"]["record_id"] for p in people if one(p, "relationship_stage") in ("LOST", "CUSTOMER")}
    else:
        # look up only the people who replied (about 140), one by one in parallel, instead of reading all 16,000 people and companies
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import attio_fast as AF
        from concurrent.futures import ThreadPoolExecutor
        emails = list(by_email)
        found = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            for e, p in zip(emails, ex.map(lambda e: AF.by_email(c, e), emails)):
                if p:
                    found[e] = p
            miss = [e for e in emails if e not in found]
            for e, p in zip(miss, ex.map(lambda e: AF.by_namecomp(c, by_email[e].get("first_name"), by_email[e].get("company_name"), norm), miss)):
                if p:
                    found[e] = p
        for e, p in found.items():
            rid_of[e] = p["id"]["record_id"]
            if one(p, "relationship_stage") in ("LOST", "CUSTOMER"):
                closed_people.add(p["id"]["record_id"])
        print(f"  {len(found)} of {len(emails)} replied people found in Attio by looking them up (no full read)")
    has_deal = set()
    closed_ext = set()                    # deals already Lost or Won: the PUT below matches on this id and would reopen them (found 2026-10-11: Papa, Ben)
    for d in c.query_all("deals"):
        if one(d, "stage") == "Lost" or str(one(d, "stage") or "").startswith("Won"):
            closed_ext.add(one(d, "external_opportunity_id"))
            if one(d, "stage") == "Lost":
                continue
        for x in d["values"].get("associated_people") or []:
            has_deal.add(x.get("target_record_id"))

    todo, skipped = [], 0
    for e, l in by_email.items():
        rid = rid_of.get(e)
        if rid and (rid in has_deal or rid in closed_people):
            skipped += 1
            continue
        if "pv-" + hashlib.sha1(e.encode()).hexdigest()[:16] in closed_ext:
            skipped += 1
            continue
        todo.append((e, l, rid))
    print(f"  {skipped} already have an open deal")
    print(f"  {len(todo)} need one ({sum(1 for _,_,r in todo if not r)} not yet in Attio as a person)\n")
    for e, l, rid in todo:
        nm = " ".join(x for x in (l.get("first_name"), l.get("last_name")) if x) or e
        print(f"   {l['label']:<15} {l['campaign'][:40]}")

    if not a.commit:
        print("\nDRY RUN — nothing created. Pass --commit.")
        return 0

    made = fail = 0
    for e, l, rid in todo:
        l["stage"] = stage_for(H, e, l["label"])
        _, ok = create_for(c, owner, e, l, rid)
        made += ok; fail += (not ok)
    print(f"\n{made} deals created, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
