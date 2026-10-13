"""
deals_follow_labels.py — a cold email deal follows the lead's PlusVibe label, both ways.

Ife, 2026-10-03/04: a deal must not be one-sided. When someone's label changes in PlusVibe, the CRM
moves with it, and every automatic move leaves a note saying what changed and where.

    label now          deal moves to
    MEETING_BOOKED  -> Meeting Booked
    LATER           -> Later
    NOT_INTERESTED  -> Lost  (lost reason NOT_INTERESTED)
    WRONG_PERSON    -> Lost  (lost reason BAD_FIT)
    INTERESTED / CYCLE_1 / CYCLE_2 -> back to Interested, but ONLY out of Later (a warm-up again).
                                      Never pulls a deal back from Meeting Booked.
    anything else (out of office, auto-reply, no label) -> leave it

Only deals with Source channel = Cold email that sit at Interested, Later or Meeting Booked are touched.
Profiles Sent, Trial and Won belong to people, and Lost is final: if a lost lead turns positive again,
positive_replies_to_deals.py opens a NEW deal, because a new conversation is a new opportunity.

Dry-run by default. Runs every 15 minutes in GitHub Actions (.github/workflows/crm_sync.yml).

    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/deals_follow_labels.py
    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/scripts/deals_follow_labels.py --commit
"""
from __future__ import annotations
import argparse, os, sys, time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient

PV = "https://api.plusvibe.ai/api/v1"
WS = "6a6e634971ad0f0e174a5471"
OWNED = {"Interested", "Later", "Meeting Booked"}
POSITIVE = {"INTERESTED", "CYCLE_1", "CYCLE_2"}
MOVE = {"MEETING_BOOKED": ("Meeting Booked", None), "LATER": ("Later", None),
        "NOT_INTERESTED": ("Lost", "NOT_INTERESTED"), "WRONG_PERSON": ("Lost", "BAD_FIT")}


def secret(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name].strip()
    envf = ROOT / ".env"
    for l in (envf.read_text().splitlines() if envf.exists() else []):
        if l.startswith(name + "="):
            return l.split("=", 1)[1].strip()
    sys.exit(f"{name} missing (not in the environment or .env)")


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


def live_labels(H) -> dict[str, tuple[str, str, str]]:
    """email -> (label, campaign, modified_at), the most recently changed row wins."""
    out = {}
    for c in pv_get("/campaign/list-all", {"workspace_id": WS}, H).json():
        name = c.get("name") or c.get("camp_name") or ""
        if "ai agency" not in name.lower():
            continue
        page = 1
        while True:
            rows = pv_get("/lead/workspace-leads", {"workspace_id": WS, "campaign_id": c["id"],
                          "status": "REPLIED", "limit": 500, "page": page}, H).json() or []
            if not isinstance(rows, list):
                break
            for l in rows:
                e = (l.get("email") or "").lower()
                m = l.get("modified_at") or ""
                if e and (e not in out or m > out[e][2]):
                    out[e] = (l.get("label") or "", name, m)
            if len(rows) < 500:
                break
            page += 1
    return out


def one(rec, k):
    v = (rec.get("values") or {}).get(k) or []
    if not v:
        return None
    x = v[0]
    return (x.get("status") or x.get("option") or {}).get("title") or x.get("value")


def already_acted(c, deal_id, label, label_changed_at) -> bool:
    """True if this script already moved the deal because of THIS label and the label has not changed since.
    Without this the sync undid every stage a person set by hand, every 15 minutes (found 2026-10-10: Noam Sitbon, Papa Moryba Kouate, Hans Oh).
    A label that changes again (a newer modified_at than our note) is acted on as before."""
    r = c.req("GET", "/notes", params={"parent_object": "deals", "parent_record_id": deal_id, "limit": 50})
    if r.status_code >= 300:
        return True        # cannot tell: do nothing rather than risk undoing a manual move
    def ts(x):
        from datetime import datetime, timezone
        x = (x or "").strip().replace("Z", "+00:00")
        if "." in x:
            head, rest = x.split(".", 1); frac = "".join(ch for ch in rest if ch.isdigit())[:6]; tail = rest[len("".join(ch for ch in rest if ch.isdigit())):]
            x = head + "." + frac.ljust(6, "0") + tail
        try:
            d = datetime.fromisoformat(x)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    latest = None
    for n in r.json().get("data", []):
        text = (n.get("title") or "") + " " + (n.get("content_plaintext") or n.get("content") or "")
        if "(automatic)" in (n.get("title") or "") and f"labelled {label} in PlusVibe" in text:
            t = ts(n.get("created_at"))
            if t and (latest is None or t > latest):
                latest = t
    if latest is None:
        return False
    changed = ts(label_changed_at)
    return changed is None or latest >= changed


def _why(r):
    """What went wrong, without echoing the response body (it can contain lead details)."""
    try:
        j = r.json(); return str(j.get('code') or j.get('type') or '')[:60]
    except Exception:
        return ''


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); a = ap.parse_args()
    labels = live_labels({"x-api-key": secret("PLUS_VIBE_API_KEY")})
    print(f"{len(labels)} replied leads with a live label in AI agency campaigns")

    c = AttioClient(workers=6)
    # only the deals this script owns need their people's emails: fetch those people one by one, in parallel, instead of reading all 16,000
    deals = [d for d in c.query_all("deals") if one(d, "source_channel") == "Cold email" and one(d, "stage") in OWNED]
    want = sorted({x.get("target_record_id") for d in deals for x in (d["values"].get("associated_people") or []) if x.get("target_record_id")})
    def _get(rid):
        r = c.req("GET", f"/objects/people/records/{rid}")
        return rid, ([e.get("email_address", "").lower() for e in r.json()["data"]["values"].get("email_addresses") or []] if r.status_code < 300 else [])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        emails = dict(ex.map(_get, want))
    print(f"{len(deals)} owned deals, {len(want)} people read (no full read)")
    plan, held = [], []
    for d in deals:
        stage = one(d, "stage")
        hit = None
        for x in d["values"].get("associated_people") or []:
            for e in emails.get(x.get("target_record_id"), []):
                if e in labels and (hit is None or labels[e][2] > hit[1][2]):
                    hit = (e, labels[e])
        if not hit:
            continue
        email, (label, camp, _) = hit
        target, reason = MOVE.get(label, (None, None))
        if label in POSITIVE and stage == "Later":
            target = "Interested"
        if not target or target == stage:
            continue
        if already_acted(c, d["id"]["record_id"], label, labels[email][2] if email in labels else ""):
            held.append((one(d, "name"), stage, label))      # we already moved this deal for this very label: a stage set by hand now stays
            continue
        plan.append((d, stage, target, reason, label, camp, email))

    for nm, st, lb in held:
        print(f"   LEFT ALONE one deal at {st} (already moved once for label {lb}; a stage set by hand stays)")
    for d, frm, to, reason, label, camp, email in plan:
        print(f"   {frm:<14} -> {to:<14} (PlusVibe label {label})")
    print(f"{len(plan)} deal(s) to move")
    if not a.commit or not plan:
        if not a.commit:
            print("DRY RUN — pass --commit.")
        return 0

    bad = 0
    for d, frm, to, reason, label, camp, email in plan:
        rid = d["id"]["record_id"]
        vals = {"stage": [{"status": to}]}
        if reason:
            vals["lost_reason"] = [{"option": reason}]
        r = c.req("PATCH", f"/objects/deals/records/{rid}", json={"data": {"values": vals}})
        if r.status_code >= 300:
            bad += 1; print(f"   MOVE FAIL {r.status_code} {_why(r)}"); continue
        n = c.req("POST", "/notes", json={"data": {
            "parent_object": "deals", "parent_record_id": rid, "format": "plaintext",
            "title": f"Moved {frm} → {to} (automatic)",
            "content": (f"{date.today().isoformat()}: {email} is now labelled {label} in PlusVibe "
                        f"({camp}), so this deal moved from {frm} to {to}"
                        + (f" with lost reason {reason}" if reason else "")
                        + ". Moved by crm/scripts/deals_follow_labels.py.")}})
        if n.status_code >= 300:
            bad += 1; print(f"   NOTE FAIL {n.status_code} {_why(n)}")
    print(f"{len(plan) - bad} moved, {bad} problems")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
