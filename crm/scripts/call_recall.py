"""Recall rounds: give people the dialler gave up on another go, like the TAM re-outreach does for email (Ife 2026-10-30).

The AI Dialer tries a person max_attempts times (4, two days apart) and then marks them "exhausted" for good; nothing ever
re-dials them. This job, per call list:
  One Redial campaign per call list ("Redial | NA | A+B" dials tag "Attio | Redial | NA | A+B"), so callers see where people came from.
  1. Clean-up (every run): anyone in a Redial whose call outcome is now FINISHED loses the tag (leaves the queue).
  2. A new round per list, at most once every ROUND_EVERY_DAYS: people exhausted in the list's main campaign (or in its last Redial)
     whose last call was REST_DAYS+ ago, who never got a real outcome, and who have had fewer than MAX_ROUNDS rounds,
     get the list's Redial tag. The previous Redial campaign is archived and a fresh one with the same name (the list's own
     settings and callers) dials the tag, because an exhausted person can never be reset inside an existing campaign.
     People from the last round the dialler has not used up yet carry over (no extra round).
     Each recalled person's "Recall rounds" count on their Attio call-list entry goes up by one.
  3. Prints the pool per list (COUNTS ONLY, public repo) and posts it to Discord when a round starts.

    python crm/scripts/call_recall.py            # dry run: counts only, writes nothing
    python crm/scripts/call_recall.py --commit
"""
import argparse, json, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from attio_client import AttioClient  # noqa: E402
import calllist_to_cloudtalk as CL  # noqa: E402  (auth, shared CloudTalk pacer, FINISHED, alert)

SPEC = json.loads(CL.SPEC.read_text())
R = SPEC["recall"]
REST_DAYS, ROUND_EVERY_DAYS, MAX_ROUNDS = R["rest_days"], R["round_every_days"], R["max_rounds"]
NOT_REACHED = {None, "NO_ANSWER", "VOICEMAIL", "NO_CONVERSATION"}      # anything else was a real outcome
NOW = datetime.now(timezone.utc)


def dialer(H, method, path, **kw):
    return CL.req(method, CL.DIALER + path, H, **kw)


def exhausted_ids(H, cid):
    """contact ids the campaign has used up, read through every page."""
    out, cursor = set(), None
    for _ in range(200):
        prm = {"limit": 100, "queueTag": "exhausted"}
        if cursor:
            prm["cursor"] = cursor
        j = CL.read_ok(dialer(H, "GET", f"/dialer/campaigns/{cid}/contacts", params=prm), f"campaign {cid} contacts").json()
        out |= {str(x["contact_id"]) for x in j.get("data") or []}
        cursor = (j.get("meta") or {}).get("nextCursor")
        if not cursor:
            break
    return out


def member_ids(H, cid):
    out, cursor = set(), None
    for _ in range(200):
        prm = {"limit": 100}
        if cursor:
            prm["cursor"] = cursor
        j = CL.read_ok(dialer(H, "GET", f"/dialer/campaigns/{cid}/contacts", params=prm), f"campaign {cid} contacts").json()
        out |= {str(x["contact_id"]) for x in j.get("data") or []}
        cursor = (j.get("meta") or {}).get("nextCursor")
        if not cursor:
            break
    return out


def when(v):
    """ISO time from Attio or CloudTalk (with Z, an offset, or nanoseconds) as an aware UTC datetime; None if unreadable."""
    try:
        return datetime.fromisoformat(v[:19]).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def create_like(H, model_id, name, tag_id):
    """A new active dialer campaign with the model campaign's settings and callers, dialling one tag. Returns its id or None."""
    tpl = dialer(H, "GET", f"/dialer/campaigns/{model_id}").json()
    assoc = dialer(H, "GET", f"/dialer/campaigns/{model_id}/associations").json()
    body = {"name": name, **{k: tpl[k] for k in ("mode", "calling_policy", "queue_policy", "retry_policy", "schedule_policy", "ttl_policy", "waterfall_order", "agent_permissions") if k in tpl}}
    r = dialer(H, "POST", "/dialer/campaigns", json=body)
    if r.status_code >= 300:
        print(f"  {name}: campaign NOT created: HTTP {r.status_code}"); return None
    new_id = r.json().get("id") or (r.json().get("data") or {}).get("id")
    ra = dialer(H, "PUT", f"/dialer/campaigns/{new_id}/associations", json={"target_tag": [{"ref_id": int(tag_id), "priority": 1}], "queue_group": [], "agent": assoc.get("agent", [])})
    rs = dialer(H, "PATCH", f"/dialer/campaigns/{new_id}", json={"status": "active"})
    print(f"  {name}: campaign {new_id} created, callers {assoc.get('agent')}, associations HTTP {ra.status_code}, activate HTTP {rs.status_code}")
    return new_id if ra.status_code < 300 and rs.status_code < 300 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--force-round", action="store_true", help="start a round now even if the last one is under ROUND_EVERY_DAYS old")
    a = ap.parse_args()
    H = CL.auth()
    c = AttioClient(workers=8)

    # Attio: every call-list entry, per list, by its CloudTalk contact id
    entry = {}                      # (list slug, contact id) -> entry facts
    for L in SPEC["lists"]:
        off = 0
        while True:
            rows = c.req("POST", f"/lists/{L['slug']}/entries/query", json={"limit": 500, "offset": off}).json()["data"]
            for e in rows:
                v = e.get("entry_values") or {}
                ct = (v.get("cloudtalk_contact_id") or [{}])[0].get("value")
                if not ct:
                    continue
                last = (v.get("called_at") or [{}])[0].get("value") or (v.get("queued_at") or [{}])[0].get("value") or ""
                entry[(L["slug"], str(ct))] = {
                    "entry_id": e["id"]["entry_id"],
                    "outcome": ((v.get("call_outcome") or [{}])[0].get("option") or {}).get("title"),
                    "last": when(last) if last else None,
                    "rounds": int((v.get("recall_rounds") or [{}])[0].get("value") or 0)}
            off += 500
            if len(rows) < 500:
                break
    print(f"Attio call-list entries with a CloudTalk contact: {len(entry):,}")

    camps = []
    cursor = None
    for _ in range(20):
        prm = {"limit": 100}
        if cursor:
            prm["cursor"] = cursor
        j = CL.read_ok(dialer(H, "GET", "/dialer/campaigns", params=prm), "dialer campaigns").json()
        camps += j.get("data") or []
        cursor = (j.get("meta") or {}).get("nextCursor")
        if not cursor:
            break
    tag_ids = {}
    for row in (CL.read_ok(CL.req("GET", "/tags/index.json", H, params={"limit": 200}), "tags").json().get("responseData", {}).get("data") or []):
        tg = row.get("Tag", row); tag_ids[tg.get("name")] = tg.get("id")

    posts = []
    for L in SPEC["lists"]:
        label = L["tag"].removeprefix("Attio | ")            # e.g. "NA | A+B"
        name, tag = f"Redial | {label}", f"Attio | Redial | {label}"     # one Redial per call list (Ife 30 Oct), no date
        mains = [cc["id"] for cc in L.get("campaigns", []) if cc["kind"] == "dialer"]
        if not mains:
            print(f"{label}: no dialer campaign, skipped"); continue
        recalls = sorted([x for x in camps if (x.get("name") or "") == name and x.get("status") in ("active", "inactive", "initializing")],
                         key=lambda x: x.get("created_at") or "")
        current = recalls[-1] if recalls else None
        cur_age = (NOW - when(current["created_at"])).days if current else None

        holders = member_ids(H, current["id"]) if current else set()
        cur_used = exhausted_ids(H, current["id"]) if current else set()
        finished_holders = {x for x in holders if (entry.get((L["slug"], x)) or {}).get("outcome") in CL.FINISHED}

        used_up = set(cur_used)
        for cid in mains:
            used_up |= exhausted_ids(H, cid)
        why = Counter()
        due = []
        for x in used_up:
            e = entry.get((L["slug"], x))
            if not e:
                why["not on this call list any more"] += 1; continue
            if e["outcome"] not in NOT_REACHED:
                why[f"had a real outcome ({e['outcome']})"] += 1; continue
            if x in holders and x not in cur_used:
                why["still has tries left in the current Redial"] += 1; continue
            if e["rounds"] >= MAX_ROUNDS:
                why["retired: max rounds reached"] += 1; continue
            if not e["last"] or e["last"] > NOW - timedelta(days=REST_DAYS):
                why[f"resting (< {REST_DAYS} days since last call)"] += 1; continue
            due.append(x)
        # people in the current round the dialler has not used up yet keep their place in the next one (no round added)
        carry = holders - cur_used - finished_holders - set(due)
        start_round = bool(due) and (a.force_round or current is None or cur_age >= ROUND_EVERY_DAYS)
        print(f"{label}: used up {len(used_up)} | due {len(due)} | not due {dict(why)} | Redial "
              f"{'none' if not current else f'{current['id']} ({cur_age} days old, {len(holders)} in it, {len(carry)} still to finish)'} | "
              f"finished to take out {len(finished_holders)} | {'NEW ROUND' if start_round else 'no new round'}")
        if not a.commit:
            continue

        out = 0
        for x in finished_holders:
            out += CL.req("DELETE", f"/contacts/removeTags/{x}.json", H, json={"tags": [tag]}).status_code < 300
        if finished_holders:
            print(f"  {label}: took {out} finished people out of Redial")
        if not start_round:
            continue

        # archive the previous round first, so tagged people only join the new campaign; untag only those who are done with it
        if current:
            st = dialer(H, "PATCH", f"/dialer/campaigns/{current['id']}", json={"status": "archived"}).status_code
            n = 0
            for x in holders - carry - set(due) - finished_holders:
                n += CL.req("DELETE", f"/contacts/removeTags/{x}.json", H, json={"tags": [tag]}).status_code < 300
            print(f"  {label}: previous Redial {current['id']} archived (HTTP {st}), {n} untagged, {len(carry)} carried over")
        ok = bad = 0
        for x in due:
            if CL.req("PUT", f"/contacts/addTags/{x}.json", H, json={"tags": [tag]}).status_code < 300:
                ok += 1
                e = entry[(L["slug"], x)]
                c.req("PATCH", f"/lists/{L['slug']}/entries/{e['entry_id']}",
                      json={"data": {"entry_values": {"recall_rounds": [{"value": e["rounds"] + 1}]}}})
            else:
                bad += 1
        print(f"  {label}: tagged {ok} for Redial, failed {bad}")
        if tag not in tag_ids:      # the first addTags creates the tag (30 Oct: /tags/add.json answers 405), so read its id now
            for row in (CL.req("GET", "/tags/index.json", H, params={"limit": 200}).json().get("responseData", {}).get("data") or []):
                tg = row.get("Tag", row); tag_ids[tg.get("name")] = tg.get("id")
        if tag not in tag_ids:
            print(f"  {label}: Redial tag missing after tagging, campaign NOT created"); posts.append(f"{name}: round FAILED (tag missing)"); continue
        new_id = create_like(H, mains[0], name, tag_ids[tag])
        if not new_id:
            posts.append(f"{name}: round FAILED (campaign not created)"); continue
        posts.append(f"{name}: new round, {ok} people who never picked up get another go"
                     f"{f', {len(carry)} carried over from the last round' if carry else ''}. "
                     f"Resting: {why.get(f'resting (< {REST_DAYS} days since last call)', 0)}, retired after {MAX_ROUNDS} rounds: {why.get('retired: max rounds reached', 0)}.")
    if posts and a.commit:
        CL.alert("Redial rounds\n" + "\n".join(posts))
    if not a.commit:
        print("DRY RUN: nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
