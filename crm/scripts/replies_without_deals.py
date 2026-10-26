"""
replies_without_deals.py — every reply since a date in an AI-agency campaign (reply_scope.in_scope) whose FINAL label makes a deal
but whose person has no open deal. The backfill list for Ife (2026-10-26, after Ramsey Sweis and Rachael Quisel got no deal).

Reads only: PlusVibe received emails per campaign (/unibox/emails, email_type=received), each replier's rows (/lead/get) for the
final label, and Attio (person by email, else first name + company; their deals). With --commit it creates the missing deals with the
same code as the reply fast path (positive_replies_to_deals.create_for): needs Ife's go first.

Public repo: the log prints counts only. Names go to --out (a local CSV; keep it out of the repo, e.g. under data/ or a scratch folder).

    python crm/scripts/replies_without_deals.py --since 2026-10-15 --out /tmp/x.csv          # dry run
    python crm/scripts/replies_without_deals.py --since 2026-10-15 --out /tmp/x.csv --commit # create the deals (after Ife's go)
"""
from __future__ import annotations
import argparse, csv, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
import reply_scope as RS  # noqa: E402
import positive_replies_to_deals as PRD  # noqa: E402
from normalize import clean_email  # noqa: E402

WS = PRD.WS


def received_since(H, cid: str, since) -> list[dict]:
    out, trail = [], ""
    for _ in range(60):
        r = PRD.pv_get("/unibox/emails", {"workspace_id": WS, "campaign_id": cid, "email_type": "received", "preview_only": "true",
                                          "page_trail": trail}, H)
        if r.status_code != 200:
            break
        j = r.json() or {}
        rows = j.get("data") or []
        out += rows
        old = any((RS.parse_ts(x.get("timestamp_created")) or since) < since for x in rows)
        trail = j.get("page_trail") or ""
        if not rows or not trail or old:
            break
        time.sleep(0.25)
    return [x for x in out if (RS.parse_ts(x.get("timestamp_created")) or since) >= since]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", default="", help="local CSV with the names (never commit it)")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    since = RS.parse_ts(a.since + "T00:00:00Z")
    H = {"x-api-key": PRD.env()["PLUS_VIBE_API_KEY"]}
    camps = [c for c in PRD.pv_get("/campaign/list-all", {"workspace_id": WS}, H).json() if RS.in_scope(c.get("name") or c.get("camp_name") or "")]
    print(f"{len(camps)} AI-agency / TAM / nurture campaigns")
    first_at = {}
    for c in camps:
        for x in received_since(H, c["id"], since):
            e, _ = clean_email(x.get("lead") or x.get("from_address_email"))
            if e and (e not in first_at or (x.get("timestamp_created") or "") < first_at[e]):
                first_at[e] = x.get("timestamp_created") or ""
    print(f"{len(first_at)} people replied since {a.since}")

    from attio_client import AttioClient
    import attio_fast as AF
    c = AttioClient(workers=4)
    norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())  # noqa: E731
    counts = {"hard_no": 0, "no_deal_label": 0, "open_deal": 0, "closed_before": 0, "missing": 0}
    other_labels: dict[str, int] = {}
    todo = []
    for e in sorted(first_at, key=lambda k: first_at[k]):
        r = PRD.pv_get("/lead/get", {"workspace_id": WS, "email": e}, H)
        rows = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        scoped = [x for x in rows if RS.in_scope(x.get("campaign_name") or "")]
        labels = [RS.final_label(x)[0] for x in scoped]
        if any(RS.is_hard_no(l) for l in labels):
            counts["hard_no"] += 1; continue
        got = PRD.pick_row(rows)
        if not got:
            counts["no_deal_label"] += 1
            for l in set(labels) or {"(none)"}:
                other_labels[l or "(none)"] = other_labels.get(l or "(none)", 0) + 1
            continue
        row, label, by, _ = got
        lead = PRD.lead_of(row, e, label)
        p = AF.by_email(c, e) or AF.by_namecomp(c, lead.get("first_name"), lead.get("company_name"), norm)
        rid = p["id"]["record_id"] if p else None
        deals = []
        if rid:
            deals = c.req("POST", "/objects/deals/records/query", json={"filter": {"associated_people": {"target_object": "people", "target_record_id": rid}}, "limit": 50}).json().get("data", [])
        if any(PRD.one(d, "stage") != "Lost" and not str(PRD.one(d, "stage") or "").startswith("Won") for d in deals):
            counts["open_deal"] += 1; continue
        if deals:
            counts["closed_before"] += 1                 # a Lost / Won deal: a new conversation, still listed
        counts["missing"] += 1
        lead["stage"] = PRD.stage_for(H, e, label)
        todo.append((e, lead, rid, by, first_at[e]))
        time.sleep(0.2)
    print("counts: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print("replies with no deal label, by label: " + ", ".join(f"{k} {v}" for k, v in sorted(other_labels.items(), key=lambda x: -x[1])))
    if a.out:
        with open(a.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["replied_at", "name", "company", "email", "final_label", "set_by", "campaign", "stage", "in_attio"])
            for e, l, rid, by, at in todo:
                w.writerow([at[:16].replace("T", " "), " ".join(x for x in (l.get("first_name"), l.get("last_name")) if x), l.get("company_name") or "",
                            e, l["label"], by or "plusvibe", l["campaign"], l["stage"], "yes" if rid else "no"])
        print(f"names written to {a.out}")
    if not a.commit:
        print(f"DRY RUN: {len(todo)} deals would be created. Pass --commit after Ife's go.")
        return 0
    owner = c.req("GET", "/workspace_members").json()["data"][0]["id"]["workspace_member_id"]
    made = bad = 0
    for e, l, rid, _, _ in todo:
        _, ok = PRD.create_for(c, owner, e, l, rid)
        made += ok; bad += (not ok)
    print(f"{made} deals created, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
