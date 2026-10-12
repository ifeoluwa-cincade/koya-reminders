"""
collect_metrics.py — one daily row per day, from all three systems.

WHY: the numbers that matter (leads contacted, reply rate, positive reply rate, booking rate,
pipeline) live in three places that cannot see each other. PlusVibe knows sends and replies,
CloudTalk knows dials and conversations, Attio knows the pipeline. Nothing joins them, so no
single screen has ever shown the funnel end to end.

OUTPUT: data/working/metrics_daily.json — {"days": [...], "snapshot": {...}}
The dashboard artifact reads these rows out of its own small database; this script is what
fills it. Read-only against every API, so there is no --commit flag.

    python crm/scripts/collect_metrics.py
    python crm/scripts/collect_metrics.py --days 60
"""
from __future__ import annotations
import argparse, base64, json, sys, time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from attio_client import AttioClient

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data/working/metrics_daily.json"
PV_BASE = "https://api.plusvibe.ai/api/v1"
PV_WS = "6a6e634971ad0f0e174a5471"
CT_BASE = "https://my.cloudtalk.io/api"


def env() -> dict:
    """Keys from .env on the Mac; on GitHub Actions there is no .env, so the job's env vars are used."""
    import os
    e = dict(os.environ)
    f = ROOT / ".env"
    if f.exists():
        e.update({l.split("=", 1)[0]: l.split("=", 1)[1].strip()
                  for l in f.read_text().splitlines() if "=" in l and not l.startswith("#")})
    return e


def pv_get(path: str, params: dict, hdr: dict, tries: int = 6):
    """429 on PlusVibe looks exactly like an empty result — always back off and retry."""
    delay = 2.0
    for _ in range(tries):
        try:
            r = requests.get(PV_BASE + path, headers=hdr, params=params, timeout=120)
            if r.status_code != 429:
                return r
        except requests.exceptions.RequestException:
            pass
        time.sleep(delay)
        delay *= 2
    return r


def plusvibe_days(e: dict, since: date) -> dict[str, dict]:
    """start_date and end_date are REQUIRED — without them the endpoint 400s."""
    hdr = {"x-api-key": e["PLUS_VIBE_API_KEY"]}
    r = pv_get("/account/email-stats",
               {"workspace_id": PV_WS, "start_date": since.isoformat(),
                "end_date": date.today().isoformat()}, hdr)
    r.raise_for_status()
    chart = r.json().get("chart") or []
    out = {}
    for row in chart:
        d = str(row.get("date", ""))[:10]
        if not d:
            continue
        out[d] = {
            "sent": int(row.get("total_sent_count") or 0),
            "contacted": int(row.get("total_new_lead_contacted_count") or 0),
            "replies": int(row.get("total_reply_count") or 0),
            "positive": int(row.get("total_pos_reply_count") or 0),
            "bounces": int(row.get("total_bounce_count") or 0),
            "ooo": int(row.get("total_ooo_reply_count") or 0),
        }
    return out


def cloudtalk_days(e: dict, since: date) -> dict[str, dict]:
    """Dials and real conversations per day. A dial is a call; a conversation is talking_time>0."""
    tok = base64.b64encode(
        f"{e['CLOUD_TALK_API_KEY_ID']}:{e['CLOUD_TALK_API_KEY']}".encode()).decode()
    hdr = {"Authorization": f"Basic {tok}"}
    rows, page = [], 1
    while True:
        r = requests.get(f"{CT_BASE}/calls/index.json", headers=hdr, timeout=120,
                         params={"limit": 1000, "page": page,
                                 "date_from": since.isoformat()})
        if r.status_code != 200:
            print(f"  cloudtalk page {page}: HTTP {r.status_code} — stopping", file=sys.stderr)
            break
        j = r.json().get("responseData", {})
        data = j.get("data") or []
        rows += data
        # Trust the API's own page count; never "stop after an empty page".
        if page >= int(j.get("pageCount") or 1) or not data:
            break
        page += 1
    out = defaultdict(lambda: {"dials": 0, "conversations": 0, "talk_minutes": 0.0})
    for row in rows:
        c = row.get("Cdr") or {}
        if (c.get("type") or "") != "outgoing":
            continue
        d = str(c.get("started_at") or "")[:10]
        if not d:
            continue
        talk = float(c.get("talking_time") or 0)
        out[d]["dials"] += 1
        out[d]["talk_minutes"] = round(out[d]["talk_minutes"] + talk / 60, 1)
        if talk >= 30:                      # same floor the transcription queue uses
            out[d]["conversations"] += 1
    return dict(out)


def attio_snapshot() -> dict:
    c = AttioClient(workers=8)
    deals = c.query_all("deals")
    people = c.query_all("people")

    def one(rec, slug):
        v = (rec.get("values") or {}).get(slug) or []
        if not v:
            return None
        x = v[0]
        return (x.get("status") or x.get("option") or {}).get("title") or x.get("value")

    stages = Counter(one(d, "stage") or "—" for d in deals)
    # The slug is relationship_stage, not relationship. Reading the wrong slug returns nothing
    # and the default silently reported all 13,115 people as COLD.
    rel = Counter(one(p, "relationship_stage") or "COLD" for p in people)
    due = Counter(one(p, "due_status") or "—" for p in people)
    dnc = sum(1 for p in people if (one(p, "dnc_status") or "") == "DO_NOT_CONTACT")
    booked = sum(1 for d in deals if (one(d, "stage") or "") not in ("Lost",))

    # Meetings per day, so the booking rate has a trend and not just a total.
    meetings = Counter()
    for p in people:
        v = (p.get("values") or {}).get("meeting_at") or []
        if v and v[0].get("value"):
            meetings[str(v[0]["value"])[:10]] += 1
    return {
        "dnc": dnc,
        "meetings_by_day": dict(meetings),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "people_total": len(people),
        "deals_total": len(deals),
        "stages": dict(stages),
        "relationship": dict(rel),
        "due": dict(due),
        "live_deals": booked,
    }


# Deals were bulk-imported on 1 Oct 2026, so every stage on an old deal reads "since 1 Oct".
# Only moves AFTER the import are real events; before it, only a Won close_date is a real date.
IMPORT_CUTOFF = "2026-10-01T21:00:00"
BOOKED = {"Meeting Booked", "Profiles Sent", "Trial", "Won 🎉"}
HELD = {"Profiles Sent", "Trial", "Won 🎉"}   # profiles only go out after a meeting happened


def deal_events(channel: str = "Cold email") -> list[dict]:
    """Dated pipeline events for one channel: meeting booked, meeting held, dev staffed.

    Ife's rule (2026-10-03): meetings are tracked on the Deals object, and each deal carries
    the campaign it came from. A deal counts the first time it reaches each stage group.
    """
    c = AttioClient(workers=8)

    def one(rec, k):
        v = (rec.get("values") or {}).get(k) or []
        if not v:
            return None
        x = v[0]
        return (x.get("status") or x.get("option") or {}).get("title") or x.get("value")

    out = []
    for d in c.query_all("deals"):
        if one(d, "source_channel") != channel:
            continue
        rid = d["id"]["record_id"]
        hist = c.req("GET", f"/objects/deals/records/{rid}/attributes/stage/values",
                     params={"show_historic": "true"}).json().get("data", [])
        moves = sorted(((v.get("active_from") or "")[:19], (v.get("status") or {}).get("title"))
                       for v in hist)
        moves = [(ts, st) for ts, st in moves if ts > IMPORT_CUTOFF]
        def first(group):
            return next((ts[:10] for ts, st in moves if st in group), None)
        b, h = first(BOOKED), first(HELD)
        w = one(d, "close_date") if one(d, "stage") == "Won 🎉" else None
        w = w or first({"Won 🎉"})
        camp = one(d, "source_campaign") or ""
        for kind, day in (("booked", b), ("held", h), ("staffed", w)):
            if day:
                out.append({"date": str(day)[:10], "kind": kind, "campaign": camp})
    return out


INSTANTLY_EXPORT = ROOT / "data/exports/instantly_full_export_2026-08-17"
PLUSVIBE_PULL = ROOT / "data/caches/plusvibe_sync_pull.json"
# Partner-recruitment campaigns book calls with affiliates, not clients — not sales meetings.
NOT_SALES = ("affiliate",)


def historical_meetings() -> list[dict]:
    """Cold email meetings booked BEFORE they were tracked on Attio deals.

    Instantly (Mar–Aug 2026, org now unsubscribed, so the 17 Aug export is the only copy):
    lt_interest_status 2 = meeting booked, 3 = meeting completed, 4 = won. The export has no
    'status changed at' date, so timestamp_updated is the best available — close, not exact.
    PlusVibe (from 20 Aug): label MEETING_BOOKED. label_history carries no timestamp for it,
    so the send date is used (the reply date isn't in the pull).
    """
    out = []
    lbc = INSTANTLY_EXPORT / "leads_by_campaign.json"
    if lbc.exists():
        camps = {c["id"]: c["name"] for c in json.loads((INSTANTLY_EXPORT / "campaigns.json").read_text())}
        for cid, leads in json.loads(lbc.read_text()).items():
            for l in leads:
                s = l.get("lt_interest_status")
                name = camps.get(l.get("campaign"), cid)
                if s not in (2, 3, 4) or any(w in name.lower() for w in NOT_SALES):
                    continue
                day = str(l.get("timestamp_updated") or "")[:10]
                out.append({"date": day, "kind": "booked", "campaign": name, "source": "instantly"})
                if s in (3, 4):
                    out.append({"date": day, "kind": "held", "campaign": name, "source": "instantly"})
                if s == 4:
                    out.append({"date": day, "kind": "staffed", "campaign": name, "source": "instantly"})
    if PLUSVIBE_PULL.exists():
        for email, v in json.loads(PLUSVIBE_PULL.read_text()).items():
            if v.get("label") == "MEETING_BOOKED":
                # `replied` in this pull is a reply COUNT, not a date — using it produced days
                # like "1" and "2". The send date is the only real date available.
                day = str(v.get("sent") or "")[:10]
                if day:
                    out.append({"date": day, "kind": "booked", "campaign": v.get("camp") or "",
                                "source": "plusvibe"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=31)
    ap.add_argument("--plusvibe-only", action="store_true",
                    help="cold email dashboard refresh: PlusVibe only, skip CloudTalk and the ~5-minute Attio pull")
    a = ap.parse_args()
    e = env()
    since = date.today() - timedelta(days=a.days)

    print("plusvibe email stats...")
    pv = plusvibe_days(e, since)
    print(f"  {len(pv)} days")

    if a.plusvibe_only:
        try:                                    # dials for the cold calling section; a failure leaves them at 0
            ct = cloudtalk_days(e, since)
        except Exception as ex:
            print(f"  cloudtalk failed ({type(ex).__name__}) - dials will read 0", file=sys.stderr)
            ct = {}
        days = [{"date": (date.today() - timedelta(days=i)).isoformat(), **{k: pv.get(
                    (date.today() - timedelta(days=i)).isoformat(), {}).get(k, 0)
                    for k in ("sent", "contacted", "replies", "positive", "bounces")},
                 **{k: ct.get((date.today() - timedelta(days=i)).isoformat(), {}).get(k, 0)
                    for k in ("dials", "conversations")}}
                for i in range(a.days, -1, -1)]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"days": days, "outreach_start": "2026-08-20", "snapshot": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}, indent=1))
        print(f"wrote {OUT.relative_to(ROOT)}  ({len(days)} days, PlusVibe only)")
        return 0

    print("cloudtalk calls...")
    try:
        ct = cloudtalk_days(e, since)
    except Exception as ex:
        print(f"  cloudtalk failed ({type(ex).__name__}) — days will have no call data", file=sys.stderr)
        ct = {}
    print(f"  {len(ct)} days, {sum(v['dials'] for v in ct.values())} outbound dials")

    print("attio snapshot...")
    snap = attio_snapshot()
    print(f"  {snap['people_total']} people, {snap['deals_total']} deals, {snap['stages']}")

    days = []
    for i in range(a.days, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        p = pv.get(d, {})
        c = ct.get(d, {})
        days.append({
            "date": d,
            "sent": p.get("sent", 0),
            "contacted": p.get("contacted", 0),
            "replies": p.get("replies", 0),
            "positive": p.get("positive", 0),
            "bounces": p.get("bounces", 0),
            "dials": c.get("dials", 0),
            "conversations": c.get("conversations", 0),
            "meetings": snap["meetings_by_day"].get(d, 0),
        })

    print("cold email deal events...")
    events = deal_events("Cold email") + historical_meetings()
    print(f"  {len(events)} dated events (booked / held / staffed)")
    for d in days:
        for k in ("booked", "held", "staffed"):
            d[k] = sum(1 for e in events if e["date"] == d["date"] and e["kind"] == k)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"days": days, "snapshot": snap, "deal_events": events,
                               "outreach_start": "2026-08-20"}, indent=1))

    # One file per day, ready for the dashboard artifact's database. The artifact page cannot
    # fetch anything (its CSP blocks it), so the rows are pushed in with ArtifactData; a batch
    # entry takes a file path, which is why each day is written out separately.
    rows = ROOT / "data/working/dbrows"
    rows.mkdir(parents=True, exist_ok=True)
    for r in days:
        (rows / f"{r['date']}.json").write_text(json.dumps(r))
    s = dict(snap); s.pop("meetings_by_day", None)
    (rows / "snapshot.json").write_text(json.dumps(s))

    print(f"\nwrote {OUT.relative_to(ROOT)}  ({len(days)} days)")
    print(f"wrote {rows.relative_to(ROOT)}/  — push with ArtifactData batch into")
    print("  https://claude.ai/artifact/XH2rR8r6ZVQ1ods5Leo1hD  (days/<date>, meta/snapshot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
