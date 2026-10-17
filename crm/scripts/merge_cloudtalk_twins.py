"""
merge_cloudtalk_twins.py — remove the empty duplicate people CloudTalk's Attio link creates (Ife, 2026-10-14).

WHY: when CloudTalk cannot match a call to a person by email, its Attio integration creates a NEW person with only a name and a phone number,
and writes the call notes on that empty record. Many of our leads have no email, so this happens on most calls. The real person never sees the notes.

WHAT it does, per empty twin (a person with a phone number and nothing else: no relationship stage, email, company or LinkedIn):
  1. finds the real person with the same phone number (last 10 digits) and a matching first name; anything unclear is left alone and counted
  2. copies the twin's notes to the real person (skipping any that are already there), and the twin's call-list entries the real person does not have
  3. only then deletes the twin

    python crm/scripts/merge_cloudtalk_twins.py                 # dry run: counts only
    python crm/scripts/merge_cloudtalk_twins.py --commit
    python crm/scripts/merge_cloudtalk_twins.py --commit --days 3   # only twins created in the last 3 days (what the daily job uses)

Public repo: only counts are printed, never names, numbers or note text.
"""
from __future__ import annotations
import argparse, collections, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient  # noqa: E402


def l10(x) -> str:
    d = re.sub(r"\D", "", str(x or ""))
    return d[-10:] if len(d) >= 7 else ""


def val(p, k):
    v = (p.get("values") or {}).get(k) or []
    return v[0] if v else None


def is_empty(p) -> bool:
    v = p.get("values") or {}
    return not (v.get("relationship_stage") or v.get("email_addresses") or v.get("company") or v.get("linkedin"))


def first(p) -> str:
    n = val(p, "name") or {}
    return str(n.get("first_name") or n.get("full_name") or "").strip().lower().split(" ")[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--days", type=int, default=0, help="only twins created in the last N days (0 = all)")
    a = ap.parse_args()
    c = AttioClient(workers=6)
    cut = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat() if a.days else ""
    by, allp = collections.defaultdict(list), {}
    for p in c.query_all("people"):
        pid = p["id"]["record_id"]; allp[pid] = p
        for x in (p.get("values") or {}).get("phone_numbers") or []:
            t = l10(x.get("phone_number") or x.get("original_phone_number"))
            if t:
                by[t].append(pid)
    plan, skipped = [], collections.Counter()
    for t, ids in by.items():
        if len(ids) < 2:
            continue
        empties = [i for i in ids if is_empty(allp[i]) and (not cut or allp[i]["created_at"] >= cut)]
        reals = [i for i in ids if not is_empty(allp[i])]
        for e in empties:
            m = [r for r in reals if first(allp[r]) and first(allp[r]) == first(allp[e])] or ([reals[0]] if len(reals) == 1 and (not first(allp[e]) or first(allp[reals[0]]).startswith(first(allp[e])[:3])) else [])
            if len(m) != 1:
                skipped["no single matching real person"] += 1; continue
            plan.append((e, m[0]))
    plan = list({e: (e, r) for e, r in plan}.values())
    print(f"{len(plan)} empty twin(s) to merge, skipped: {dict(skipped) or 'none'}")
    if not a.commit:
        print("DRY RUN, nothing changed. Pass --commit.")
        return 0

    def notes(pid):
        return c.req("GET", "/notes", params={"parent_object": "people", "parent_record_id": pid, "limit": 50}).json().get("data", [])

    def entries(pid):
        return c.req("GET", f"/objects/people/records/{pid}/entries").json().get("data", [])

    def one(pair):
        e, r = pair
        out = collections.Counter()
        # the twin must not own a deal
        if c.req("POST", "/objects/deals/records/query", json={"filter": {"associated_people": {"target_object": "people", "target_record_id": e}}, "limit": 1}).json().get("data"):
            out["twin has a deal, left alone"] += 1; return out
        have = {(n["title"], n["created_at"][:16]) for n in notes(r)}
        for n in notes(e):
            if (n["title"], n["created_at"][:16]) in have:
                out["note already on the real person"] += 1; continue
            body = {"parent_object": "people", "parent_record_id": r, "title": n["title"], "format": "markdown", "content": n.get("content_markdown") or n.get("content_plaintext") or "", "created_at": n["created_at"]}
            mk = f"copy-{(n.get('id') or {}).get('note_id') or n['created_at']}"     # a retried copy never lands twice
            st, _, _ = c.create_note(body, marker=mk, check_first=True)
            if 400 <= st < 500 and st != 429:                                       # created_at refused: copy without it
                body.pop("created_at"); st, _, _ = c.create_note(body, marker=mk)
            if st >= 300:
                out["FAILED copying a note"] += 1; return out
            out["note copied"] += 1
        real_lists = {x.get("list_api_slug") for x in entries(r)}
        for x in entries(e):
            slug = x.get("list_api_slug")
            if slug in real_lists:
                out["entry already on the real person"] += 1; continue
            ev = {k: v for k, v in (x.get("entry_values") or {}).items() if v}
            # PUT = "this person has an entry in this list" (matched on the parent record): a retry after a timeout updates the entry
            # the first attempt made instead of adding a second one, as the old POST did
            res = c.req("PUT", f"/lists/{slug}/entries", json={"data": {"parent_record_id": r, "parent_object": "people", "entry_values": ev}})
            if res.status_code >= 300:
                out["FAILED copying a list entry"] += 1; return out
            out["entry copied"] += 1
        d = c.req("DELETE", f"/objects/people/records/{e}")
        out["twin deleted" if d.status_code < 300 else "FAILED deleting the twin"] += 1
        return out

    tot = collections.Counter()
    with ThreadPoolExecutor(4) as ex:
        for o in ex.map(one, plan):
            tot.update(o)
    print(dict(tot))
    return 1 if any(k.startswith("FAILED") for k in tot) else 0


if __name__ == "__main__":
    sys.exit(main())
