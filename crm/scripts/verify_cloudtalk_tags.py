"""
verify_cloudtalk_tags.py — tag, read back, retry until CloudTalk agrees.

WHY THIS EXISTS: `PUT /contacts/addTags/{id}.json` returns 200 with
"Tags were successfully added" and then does not persist the tag. On 2026-10-02 a run of 1,184
tag writes reported 1,184 successes and 0 failures; reading the tags back showed 247 had
actually stuck. A success response from this endpoint means nothing. The only way to know a
contact is in a campaign is to ask which contacts carry the tag.

So this converges instead of trusting: read who carries the tag, write the difference, read
again, repeat. It stops when the set stops growing.

    python crm/scripts/verify_cloudtalk_tags.py              # report the gap only
    python crm/scripts/verify_cloudtalk_tags.py --commit
"""
from __future__ import annotations
import os
import argparse, base64, re, sys, time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient
import ct_client  # noqa: E402  (one CloudTalk pacer for every job)

CT = "https://my.cloudtalk.io/api"
# Must match FINISHED in calllist_to_cloudtalk.py: on 30-31 Oct this set lacked SEND_FOLLOW_UP and NOT_NOW, so this daily
# check put back the tags the call-list job had just removed (56 finished people back in the dialler queues).
DONE_OUTCOMES = {"MEETING_BOOKED", "NOT_INTERESTED", "DO_NOT_CALL", "WRONG_PERSON", "SEND_FOLLOW_UP", "NOT_NOW"}
SCHEMA = ROOT / "crm/schema/attio-schema.yaml"


def auth() -> dict:
    e = ({**{l.split("=", 1)[0]: l.split("=", 1)[1].strip()
             for l in ((ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else [])
             if "=" in l and not l.startswith("#")}, **os.environ})
    t = base64.b64encode(
        f"{e['CLOUD_TALK_API_KEY_ID']}:{e['CLOUD_TALK_API_KEY']}".encode()).decode()
    return {"Authorization": f"Basic {t}", "Content-Type": "application/json"}


_CLIENT = None


def ct(method: str, path: str, H: dict, tries: int = 5, **kw):
    """Through the shared CloudTalk pacer (ct_client.py). Returns a 429-like answer when the run has waited long enough,
    so callers that check status codes treat it as 'not now' instead of crashing."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ct_client.shared()      # the same pacer as anything else in this process (the dashboard build uses both)
    try:
        return _CLIENT.request(method, path, **kw)
    except ct_client.CTBusy:
        return ct_client.BusyResponse()


def ct_summary() -> str:
    return _CLIENT.summary() if _CLIENT is not None else "CloudTalk: no requests"


def first(ev: dict, key: str):
    """entry_values can hold an EMPTY list for an attribute that exists but was never set,
    so [0] blows up. Every read of an entry value goes through here."""
    v = (ev or {}).get(key) or []
    return v[0].get("value") if v else None


def contact_by_phone(phone: str, H: dict) -> str | None:
    """The CloudTalk contact that already holds this number (last 10 digits), or None. Raises if CloudTalk cannot be read,
    so a failed search is never taken as 'not there' (that is how a second contact gets made)."""
    d10 = re.sub(r"\D", "", str(phone or ""))[-10:]
    if len(d10) < 7:
        return None
    r = ct("GET", "/contacts/index.json", H, params={"keyword": d10, "limit": 10})
    if r.status_code >= 300:
        raise RuntimeError(f"CloudTalk contact search failed {r.status_code}")
    for row in (r.json().get("responseData", {}) or {}).get("data") or []:
        nums = row.get("ContactNumber") or []
        nums = nums if isinstance(nums, list) else [nums]
        if any(re.sub(r"\D", "", str(n.get("public_number") or ""))[-10:] == d10 for n in nums if isinstance(n, dict)):
            return str((row.get("Contact", row)).get("id"))
    return None


def carriers(tid: int, H: dict) -> set[str]:
    """Who carries this tag right now. This, not a 200, is the truth."""
    out, page = set(), 1
    while True:
        r = ct("GET", "/contacts/index.json", H, params={"tag_id": tid, "limit": 1000, "page": page})
        rows = (r.json().get("responseData", {}) or {}).get("data") or []
        before = len(out)
        out |= {str((x.get("Contact", x)).get("id")) for x in rows}
        if not rows or len(out) == before:
            return out
        page += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--pause", type=float, default=0.35,
                    help="seconds between writes; the drops look load-related")
    a = ap.parse_args()

    H = auth()
    spec = yaml.safe_load(SCHEMA.read_text())
    tag_ids = {}
    for row in (ct("GET", "/tags/index.json", H, params={"limit": 200})
                .json().get("responseData", {}).get("data") or []):
        tg = row.get("Tag", row)
        tag_ids[tg.get("name")] = tg.get("id")

    c = AttioClient(workers=8)
    lists = {l["api_slug"]: l["id"]["list_id"] for l in c.req("GET", "/lists").json()["data"]}

    for L in spec["call_lists"]:
        slug = ("call_" + re.sub(r"[^a-z0-9]+", "_", L["name"].lower()).strip("_"))[:50]
        lid = lists.get(slug)
        tid = tag_ids.get(L["cloudtalk_tag"])
        if not lid or not tid:
            print(f"  skip {L['name']} (list or tag missing)")
            continue

        ents, off = [], 0
        while True:
            e = c.req("POST", f"/lists/{lid}/entries/query",
                      json={"limit": 500, "offset": off}).json()["data"]
            ents += e
            if len(e) < 500:
                break
            off += 500
        want, orphans = set(), []
        for e in ents:
            ev = e.get("entry_values") or {}
            # A resolved lead (marked lost / won / booked from the dashboard, or by a call result) is never dialled again.
            oc = ((ev.get("call_outcome") or [{}])[0].get("option") or {}).get("title")
            if oc in DONE_OUTCOMES:
                continue
            if (ev.get("main_done") or [{}])[0].get("value"):      # tries used up: only the list's Redial rings them now
                continue
            cid = first(ev, "cloudtalk_contact_id")
            if cid:
                want.add(str(cid))
            else:
                orphans.append((e, ev))

        # An entry with no CloudTalk contact cannot be dialled at all, so it gets one. Until 2026-10-17 a new contact was created
        # every time without looking first, and the entry never learned the id, so the same person was created again on every
        # run (twins: rung twice). Now: look the number up first, reuse what is there, and write the id back to the entry.
        if orphans:
            n = {"no number": 0, "existing contact reused": 0, "created": 0, "would create": 0, "failed": 0}
            for e, ev in orphans:
                rid = e["parent_record_id"]
                p = c.req("GET", f"/objects/people/records/{rid}").json().get("data", {})
                nm = ((p.get("values", {}).get("name") or [{}])[0].get("full_name") or "").strip()
                mails = [x.get("email_address") for x in (p.get("values", {}).get("email_addresses") or [])]
                phone = first(ev, "phone") or next(
                    (x.get("original_phone_number") or x.get("phone_number")
                     for x in (p.get("values", {}).get("phone_numbers") or [])), None)
                if not phone:
                    n["no number"] += 1; continue
                try:
                    cid = contact_by_phone(phone, H)
                except Exception:
                    n["failed"] += 1; continue              # cannot tell: create nothing
                if cid:
                    n["existing contact reused"] += 1
                    if a.commit:
                        ct("PUT", f"/contacts/addTags/{cid}.json", H, json={"tags": [L["cloudtalk_tag"]]})
                elif not a.commit:
                    n["would create"] += 1; continue
                else:
                    r = ct("POST", "/bulk/contacts.json", H, json=[{
                        "action": "add_contact", "command_id": "1",
                        "data": {"name": nm or (mails[0] if mails else str(phone)),
                                 "ContactEmail": ([{"email": mails[0]}] if mails else []),
                                 "ContactNumber": [{"public_number": phone}],
                                 "ContactsTag": [{"name": L["cloudtalk_tag"]}]}}])
                    res = ((r.json().get("responseData") or {}).get("data") or [{}])[0] if r.status_code < 300 else {}
                    cid = str((res.get("data") or {}).get("id") or "") or None
                    if not cid:
                        n["failed"] += 1; continue
                    n["created"] += 1
                if a.commit:
                    c.req("PATCH", f"/lists/{lid}/entries/{e['id']['entry_id']}",
                          json={"data": {"entry_values": {"cloudtalk_contact_id": [{"value": str(cid)}]}}})
                    want.add(str(cid))
            print(f"   entries with no CloudTalk contact: {len(orphans)} -> {n}")

        have = carriers(tid, H)
        missing = want - have
        print(f"\n{L['name']:<30} want {len(want):>4}  have {len(have):>4}  missing {len(missing):>4}")
        if not a.commit or not missing:
            continue

        for rnd in range(1, a.rounds + 1):
            for cid in sorted(missing):
                ct("PUT", f"/contacts/addTags/{cid}.json", H, json={"tags": [L["cloudtalk_tag"]]})
                time.sleep(a.pause)
            have = carriers(tid, H)
            still = want - have
            print(f"   round {rnd}: have {len(have):>4}, still missing {len(still):>4}")
            if not still or len(still) == len(missing):
                if len(still) == len(missing) and still:
                    print("   no progress this round — stopping rather than looping")
                missing = still
                break
            missing = still
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
