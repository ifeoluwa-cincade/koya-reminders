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
POSITIVE = {"INTERESTED", "MEETING_BOOKED", "CYCLE_1", "CYCLE_2"}
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--skip", default="", help="comma-separated emails Ife judged not real interest")
    a = ap.parse_args()
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
    people = c.query_all("people")
    pid = {}
    for p in people:
        for x in (p["values"].get("email_addresses") or []):
            pid.setdefault((x.get("email_address") or "").lower(), p["id"]["record_id"])
    def norm(s):
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())
    comp = {co["id"]["record_id"]: (co["values"].get("name") or [{}])[0].get("value") or ""
            for co in c.query_all("companies")}
    by_namecomp = {}
    for p in people:
        nm = ((p["values"].get("name") or [{}])[0].get("first_name") or "")
        cr = p["values"].get("company") or []
        cn = comp.get(cr[0]["target_record_id"], "") if cr else ""
        if nm and cn:
            by_namecomp.setdefault((norm(nm), norm(cn)), p["id"]["record_id"])
    has_deal = set()
    closed_ext = set()                    # deals already Lost or Won: the PUT below matches on this id and would reopen them (found 2026-10-11: Papa, Ben)
    for d in c.query_all("deals"):
        if one(d, "stage") == "Lost" or str(one(d, "stage") or "").startswith("Won"):
            closed_ext.add(one(d, "external_opportunity_id"))
            if one(d, "stage") == "Lost":
                continue
        for x in d["values"].get("associated_people") or []:
            has_deal.add(x.get("target_record_id"))
    closed_people = {p["id"]["record_id"] for p in people if one(p, "relationship_stage") in ("LOST", "CUSTOMER")}

    todo, skipped = [], 0
    for e, l in by_email.items():
        rid = pid.get(e) or by_namecomp.get((norm(l.get("first_name")), norm(l.get("company_name"))))
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
        full = " ".join(x for x in (l.get("first_name"), l.get("last_name")) if x).strip()
        if not rid:
            vals = {"email_addresses": [{"email_address": e}],
                    "relationship_stage": [{"option": "INTERESTED"}],
                    "current_campaign": [{"value": l["campaign"]}],
                    "legacy_source": [{"value": f"PlusVibe positive reply — {l['campaign']}"}]}
            if full:
                parts = full.split()
                vals["name"] = [{"first_name": first_name(full), "last_name": " ".join(parts[1:]), "full_name": full}]
            sl, st = linkedin_slug(l.get("linkedin_person_url") or "")
            if st == "VALID_PROFILE":
                vals["linkedin_slug"] = [{"value": sl}]
                # The dashboard and the call lists read the MAIN linkedin field, not the slug: fill both.
                # Attio rejects non-ASCII addresses in the main field, so those keep the slug only.
                _li = (l.get("linkedin_person_url") or "").strip()
                if _li.startswith("http") and _li.isascii():
                    vals["linkedin"] = [{"value": _li}]
            r = c.req("POST", "/objects/people/records", json={"data": {"values": vals}})
            if r.status_code >= 300:
                print(f"   PERSON FAIL {r.status_code} {_why(r)}"); fail += 1; continue
            rid = r.json()["data"]["id"]["record_id"]
        title = f"{full or e} / {l.get('company_name') or ''}".strip(" /")
        r = c.req("PUT", "/objects/deals/records?matching_attribute=external_opportunity_id", json={"data": {"values": {
            "name": [{"value": title}],
            "stage": [{"status": "Meeting Booked" if l["label"] == "MEETING_BOOKED" else "Interested"}],
            "owner": [{"referenced_actor_type": "workspace-member", "referenced_actor_id": owner}],
            "external_opportunity_id": [{"value": "pv-" + hashlib.sha1(e.encode()).hexdigest()[:16]}],
            "source_channel": [{"option": "Cold email"}],
            "source_campaign": [{"value": l["campaign"]}],
            "associated_people": [{"target_object": "people", "target_record_id": rid}]}}})
        if r.status_code >= 300:
            print(f"   DEAL FAIL {r.status_code} {_why(r)}"); fail += 1
        else:
            made += 1
    print(f"\n{made} deals created, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
