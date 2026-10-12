"""
sync_plusvibe.py — pull PlusVibe's truth back into Attio. Run it daily.

This is the half that was leaking. On 2026-10-01 the audit found three people PlusVibe had tagged
LATER / CYCLE_1 / INTERESTED who were sitting in the CRM as COLD, because the tag lives in the
sequencer and nothing carried it across. It also found 422 bounced addresses still queued to be
sent to. Both are this script's job.

WHY A POLL AND NOT JUST THE WEBHOOK: PlusVibe's webhook events are replies and label changes only
(ALL_EMAIL_REPLIES, LEAD_MARKED_AS_INTERESTED, LEAD_MARKED_AS_X, SYSTEM_ALERT). There is NO event
for a send, an open or a bounce. So the 90-day clock cannot be webhook-driven — without this poll
`last_contacted_at` never advances and nobody ever becomes due again.

It never CREATES a person. Ife's rule: a lead only enters the CRM once the record is filled.
A reply from someone we do not hold is reported, not inserted.

    python crm/scripts/sync_plusvibe.py                # dry run
    python crm/scripts/sync_plusvibe.py --commit
    python crm/scripts/sync_plusvibe.py --campaign <id>
"""
from __future__ import annotations
import argparse, collections, json, sys, time
from datetime import datetime, timedelta, date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient

BASE = "https://api.plusvibe.ai/api/v1"
WORKSPACE_ID = "6a6e634971ad0f0e174a5471"
RECYCLE_DAYS = 90

# States the sequencer must NEVER overwrite. A past client, a closed-lost founder or someone we
# are already talking to has no PlusVibe label, so without this guard the "sent, no reply" branch
# would set them CONTACTED, un-pause them and drop them straight back into the cold queue —
# undoing the hold that stopped two paying clients being cold-emailed on 2026-10-01.
PROTECTED_REL = {"CUSTOMER", "LOST", "INTERESTED", "MEETING", "ENGAGED"}
PROTECTED_TAM = {"EXCLUDED"}

# PlusVibe label -> (relationship, outreach, nurture, paused, last_result)
LABEL = {
    "INTERESTED":     ("INTERESTED", "NURTURE", "ACTIVE",       True,  "POSITIVE_REPLY"),
    "MEETING_BOOKED": ("MEETING",    "PAUSED",  "NONE",         True,  "MEETING_BOOKED"),
    "CYCLE_1":        ("ENGAGED",    "NURTURE", "CYCLE_1",      True,  "POSITIVE_REPLY"),
    "CYCLE_2":        ("ENGAGED",    "NURTURE", "ACTIVE",       True,  "POSITIVE_REPLY"),
    "LATER":          ("ENGAGED",    "NURTURE", "ACTIVE",       True,  "POSITIVE_REPLY"),
    "NOT_INTERESTED": ("LOST",       "PAUSED",  "NONE",         True,  "NEGATIVE_REPLY"),
    "WRONG_PERSON":   ("LOST",       "EXCLUDED","NONE",         True,  "WRONG_PERSON"),
    # OUT_OF_OFFICE and AUTOMATIC_REPLY are deliberately absent — an auto-reply is not a reply,
    # and treating one as a verdict is how 69 people ended up reading COLD with a contact date.
}


REGISTRY = ROOT / "crm/schema/campaigns.json"
# PlusVibe label -> the `result` on the campaign-list entry
ENTRY_RESULT = {"INTERESTED": "POSITIVE_REPLY", "CYCLE_1": "POSITIVE_REPLY", "CYCLE_2": "POSITIVE_REPLY",
                "LATER": "POSITIVE_REPLY", "MEETING_BOOKED": "MEETING_BOOKED",
                "NOT_INTERESTED": "NEGATIVE_REPLY", "WRONG_PERSON": "WRONG_PERSON"}


def key() -> str:
    import os
    if os.environ.get("PLUS_VIBE_API_KEY"):
        return os.environ["PLUS_VIBE_API_KEY"].strip()
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("PLUS_VIBE_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("PLUS_VIBE_API_KEY not in .env")


def get(path: str, params: dict, hdr: dict, tries: int = 6):
    """429 on PlusVibe looks exactly like an empty campaign — always back off and retry.

    AND retry on a TIMEOUT or a dropped connection. The first --commit run died on a bare
    requests.ReadTimeout partway through the pull, taking the whole job with it before a single
    record was written. A transient network fault must never cost the entire run.
    """
    delay, last = 2.0, None
    for _ in range(tries):
        try:
            r = requests.get(BASE + path, headers=hdr, params=params, timeout=120)
            if r.status_code != 429:
                return r
        except requests.exceptions.RequestException as ex:
            last = ex
        time.sleep(delay)
        delay *= 2
    if last:
        raise last
    return r


def d(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--campaign")
    a = ap.parse_args()
    hdr = {"x-api-key": key()}

    # BOTH /campaign/list-all and /lead/workspace-leads return a BARE LIST, and campaigns are
    # keyed `id` + `camp_name`. The mirrored spec documents a single object and is wrong here —
    # this shape is copied from tools/plusvibe_reply_threads.py, which has worked live for weeks.
    camps = get("/campaign/list-all", {"workspace_id": WORKSPACE_ID}, hdr).json()
    if a.campaign:
        camps = [c for c in camps if str(c.get("id")) == a.campaign]
    print(f"{len(camps)} campaigns")

    # campaigns the CRM feeds (crm/schema/campaigns.json) also get their Attio LIST kept current:
    # push_to_plusvibe.py writes the entry as queued, this fills in the real send and the result.
    registered = json.loads(REGISTRY.read_text())["campaigns"] if REGISTRY.exists() else {}
    per_camp = collections.defaultdict(dict)       # campaign id -> email -> that campaign's row

    leads = {}
    for c in camps:
        cid = c.get("id") or c.get("_id")
        name = c.get("camp_name") or c.get("name") or str(cid)
        if not c.get("lead_count"):
            continue                       # empty campaign, skip the two round-trips
        # cheap truth check FIRST — if the paged pull disagrees with this, we were rate-limited
        cnt = get("/lead/count/lead-status", {"workspace_id": WORKSPACE_ID, "campaign_id": cid}, hdr)
        # /lead/count/lead-status returns an ARRAY of {status, count, sender_count,
        # recipient_count} — not an object. Read the mirrored spec, do not guess the shape.
        expect = None
        if cnt.ok:
            body = cnt.json()
            if isinstance(body, list):
                expect = sum(int(x.get("count") or 0) for x in body)
        got, page = 0, 1
        while True:
            r = get("/lead/workspace-leads",
                    {"workspace_id": WORKSPACE_ID, "campaign_id": cid, "limit": 1000, "page": page},
                    hdr)
            if not r.ok:
                print(f"   {name[:40]}: HTTP {r.status_code}")
                break
            rows = r.json() or []
            for x in rows:
                e = (x.get("email") or "").strip().lower()
                if not e:
                    continue
                cur = leads.setdefault(e, {"label": "", "status": "", "sent": None,
                                           "bounce": "", "camp": name, "replied": 0})
                lb = (x.get("label") or "").strip()
                if lb and lb in LABEL:
                    cur["label"] = lb              # a real verdict beats a blank
                st = (x.get("status") or "").strip()
                if st in ("BOUNCED", "UNSUBSCRIBED") or not cur["status"]:
                    cur["status"] = st or cur["status"]
                if (x.get("bounce_type") or ""):
                    cur["bounce"] = x["bounce_type"]
                t = d(x.get("last_sent_at"))
                if t and (not cur["sent"] or t > cur["sent"]):
                    cur["sent"], cur["camp"] = t, name
                cur["replied"] = max(cur["replied"], int(x.get("replied_count") or 0))
                if str(cid) in registered:
                    per_camp[str(cid)][e] = {"sent": t, "status": st, "label": lb,
                                             "bounce": x.get("bounce_type") or ""}
            got += len(rows)
            if len(rows) < 1000:
                break
            page += 1
        flag = ""
        if expect and abs(expect - got) > max(5, expect * 0.02):
            flag = f"   <-- MISMATCH, expected ~{expect:,} (rate-limited?)"
        print(f"   {name[:44]:<44} {got:>6,}{flag}")

    cache = ROOT / "data/caches/plusvibe_sync_pull.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    # a --campaign run sees one campaign only; saving it would wipe the full snapshot that
    # collect_metrics.py reads (it did, once, on 2026-10-04)
    if not a.campaign:
        cache.write_text(json.dumps({e: {**v, "sent": v["sent"].isoformat() if v["sent"] else None}
                                     for e, v in leads.items()}, indent=1))
    print(f"\n{len(leads):,} distinct leads pulled from PlusVibe  (saved -> {cache.name})")
    print("  labels:", dict(collections.Counter(v["label"] for v in leads.values() if v["label"])))
    print("  status:", dict(collections.Counter(v["status"] for v in leads.values() if v["status"])))

    c = AttioClient()
    byem = {}
    for p in c.query_all("people"):
        v = p["values"]
        for e in v.get("email_addresses", []):
            byem[e.get("email_address", "").lower()] = (p["id"]["record_id"], v)

    today = date.today()
    plan, stats = [], collections.Counter()
    for e, L in leads.items():
        if e not in byem:
            stats["not in the CRM — reported, not created"] += 1
            continue
        rid, v = byem[e]
        cur_dnc = ((v.get("dnc_status") or [{}])[0].get("option") or {}).get("title")
        cur_rel = ((v.get("relationship_stage") or [{}])[0].get("option") or {}).get("title")
        cur_tam = ((v.get("tam_status") or [{}])[0].get("option") or {}).get("title")
        protected = cur_rel in PROTECTED_REL or cur_tam in PROTECTED_TAM
        vals = {}
        if L["sent"]:
            vals["last_contacted_at"] = [{"value": L["sent"].isoformat()}]
            vals["last_outreach_channel"] = [{"option": "EMAIL"}]
            vals["current_campaign"] = [{"value": L["camp"][:200]}]
        bounced = L["status"] == "BOUNCED" or L["bounce"] == "RECIPIENT"
        if L["status"] == "UNSUBSCRIBED":
            vals |= {"dnc_status": [{"option": "DO_NOT_CONTACT"}],
                     "dnc_reason": [{"option": "UNSUBSCRIBED"}],
                     "dnc_source": [{"option": "LEAD_SOURCE"}],
                     "outreach_status": [{"option": "DO_NOT_CONTACT"}],
                     "relationship_stage": [{"option": "LOST"}],
                     "last_outreach_result": [{"option": "UNSUBSCRIBED"}]}
            stats["unsubscribed -> DNC"] += 1
        elif bounced:
            # a dead ADDRESS, not a person saying no — hold and re-enrich, never DNC
            vals |= {"email_status": [{"option": "BOUNCED"}],
                     "outreach_status": [{"option": "PAUSED"}],
                     "last_outreach_result": [{"option": "BOUNCED"}]}
            stats["bounced -> held"] += 1
        elif L["label"] in LABEL:
            rel, out, nur, _, res = LABEL[L["label"]]
            vals |= {"relationship_stage": [{"option": rel}], "outreach_status": [{"option": out}],
                     "nurture_status": [{"option": nur}], "last_outreach_result": [{"option": res}],
                     "legacy_reply_label": [{"value": f"PlusVibe: {L['label']}"}]}
            stats[f"label {L['label']}"] += 1
        elif L["sent"] and not protected:
            vals |= {"relationship_stage": [{"option": "CONTACTED"}],
                     "outreach_status": [{"option": "ACTIVE"}],
                     "last_outreach_result": [{"option": "NO_RESPONSE"}]}
            stats["sent, no reply"] += 1
        elif L["sent"]:
            # we still record WHEN they were last emailed, we just do not touch their state
            stats[f"protected, state left alone ({cur_rel or cur_tam})"] += 1

        held = (cur_dnc == "DO_NOT_CONTACT" or L["status"] == "UNSUBSCRIBED" or bounced
                or L["label"] in LABEL or protected)
        vals["outreach_paused"] = [{"value": bool(held)}]
        if held or not L["sent"]:
            vals["next_eligible_at"] = []
            vals["due_status"] = [{"option": "NOT_ELIGIBLE"}]
        else:
            nxt = L["sent"] + timedelta(days=RECYCLE_DAYS)
            vals["next_eligible_at"] = [{"value": nxt.isoformat()}]
            vals["due_status"] = [{"option": "OVERDUE" if nxt <= today else
                                   "DUE_SOON" if nxt <= today + timedelta(days=14) else "NOT_DUE"}]
        if vals:
            plan.append((rid, vals))

    # campaign-list entries: queued -> sent, with this campaign's own result
    entry_plan = []
    if per_camp:
        slugs = {l["api_slug"]: l["id"]["list_id"] for l in c.req("GET", "/lists").json()["data"]}
        for cid, rows in per_camp.items():
            lid = slugs.get(registered[cid]["list_slug"])
            if not lid:
                print(f"   list {registered[cid]['list_slug']} missing in Attio — entries not updated")
                continue
            for e, L in rows.items():
                if e not in byem:
                    continue
                if L["status"] == "UNSUBSCRIBED":
                    res = "UNSUBSCRIBED"
                elif L["status"] == "BOUNCED" or L["bounce"] == "RECIPIENT":
                    res = "BOUNCED"
                elif L["label"] in ENTRY_RESULT:
                    res = ENTRY_RESULT[L["label"]]
                elif L["sent"]:
                    res = "NO_RESPONSE"
                else:
                    continue                      # still queued — leave the entry as it is
                ev = {"campaign_id": [{"value": cid}], "platform": [{"option": "PLUSVIBE"}],
                      "result": [{"option": res}]}
                if L["sent"]:
                    ev["sent_at"] = [{"value": L["sent"].isoformat()}]
                if res == "BOUNCED":
                    ev["bounce_type"] = [{"option": "UNKNOWN"}]
                entry_plan.append((lid, byem[e][0], ev))
        print(f"\n{len(entry_plan):,} campaign-list entries to update "
              f"({dict(collections.Counter(x[2]['result'][0]['option'] for x in entry_plan))})")

    print(f"\n{len(plan):,} records to update")
    for k, n in stats.most_common():
        print(f"   {n:6,}  {k}")
    if not a.commit:
        print("\nDRY RUN — nothing written.")
        return 0
    res = c.map(lambda it: c.req("PATCH", f"/objects/people/records/{it[0]}",
                                 json={"data": {"values": it[1]}}).status_code, plan, label="synced ")
    bad = [x for x in res if x >= 300]
    print(f"\n{len(res)-len(bad):,} updated, {len(bad)} failed")
    if entry_plan:
        er = c.map(lambda it: c.req("PUT", f"/lists/{it[0]}/entries", json={"data": {
            "parent_object": "people", "parent_record_id": it[1], "entry_values": it[2]}}),
            entry_plan, label="entries ")
        ebad = [x for x in er if x.status_code >= 300]
        print(f"{len(er)-len(ebad):,} campaign-list entries updated, {len(ebad)} failed")
        if ebad:
            print(f"   first failure: {ebad[0].status_code}")
            return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
