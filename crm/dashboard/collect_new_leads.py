"""
collect_new_leads.py - the hourly safety net for Koya HQ's "New leads added" cards (koya-app lib/newleads.js).

Counts NEW leads per day, so leads added by hand count too (the push scripts also report each push to Koya HQ at once):
  * email: every PlusVibe campaign, each lead's own created_at (the day it was added to that campaign);
  * calls: the six Attio call lists (crm/schema/call_lists.json), each entry's created_at. CloudTalk follows these lists by tag
    (calllist_to_cloudtalk.py), so a list entry is a lead added to the call list.
Days are Lagos days (WAT, UTC+1). Only the last --days days are kept.

OUTPUT: data/working/new_leads.json (counts and campaign / list names only), baked into the page by build_cold_email.py as DATA.newleads.
PUBLIC REPO: prints COUNTS ONLY and error STATUS CODES only. Read-only against every API.

    python crm/dashboard/collect_new_leads.py
"""
from __future__ import annotations
import json, os, re, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data/working/new_leads.json"
SPEC = ROOT / "crm/schema/call_lists.json"
PV = "https://api.plusvibe.ai/api/v1"
PV_WS = "6a6e634971ad0f0e174a5471"
LAGOS = timezone(timedelta(hours=1))
FOLLOW = re.compile(r"\bcycle\b|nurture|follow.?up", re.I)


def env(k: str) -> str:
    v = os.environ.get(k, "").strip()
    if v:
        return v
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip()
    return ""


def day_of(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(LAGOS).date().isoformat()
    except Exception:
        return ""


def pv_get(path: str, params: dict, hdr: dict):
    """429 on PlusVibe looks like an empty answer: always back off and retry."""
    delay = 2.0
    r = None
    for _ in range(6):
        try:
            r = requests.get(PV + path, headers=hdr, params=params, timeout=120)
            if r.status_code != 429:
                return r
        except requests.exceptions.RequestException:
            pass
        time.sleep(delay)
        delay *= 2
    return r


def email(since: str) -> dict:
    hdr = {"x-api-key": env("PLUS_VIBE_API_KEY")}
    r = pv_get("/campaign/list-all", {"workspace_id": PV_WS}, hdr)
    if r is None or r.status_code >= 300:
        raise RuntimeError(f"campaign list {getattr(r, 'status_code', 'no answer')}")
    j = r.json()
    camps = j if isinstance(j, list) else j.get("campaigns") or j.get("data") or []
    names, days = {}, defaultdict(lambda: defaultdict(int))
    read = 0
    for c in camps:
        cid, n = c.get("id"), int(c.get("lead_count") or 0)
        # a campaign untouched since before the window cannot have had leads added in it
        if not cid or not n or day_of(c.get("modified_at") or c.get("created_at") or "") < since:
            continue
        # follow-up cycles and nurture campaigns take leads that are already in a campaign: moving them is not a new lead
        if c.get("parent_camp_id") or FOLLOW.search(c.get("camp_name") or ""):
            continue
        names[cid] = c.get("camp_name") or cid
        page, got = 1, 0
        while True:
            g = pv_get("/lead/workspace-leads", {"workspace_id": PV_WS, "campaign_id": cid, "limit": 1000, "page": page}, hdr)
            if g is None or g.status_code >= 300:
                print(f"  a campaign could not be read ({getattr(g, 'status_code', 'no answer')}), its counts are partial", file=sys.stderr)
                break
            rows = g.json()
            rows = rows if isinstance(rows, list) else rows.get("data") or rows.get("leads") or []
            for x in rows:
                d = day_of(x.get("created_at") or "")
                if d and d >= since:
                    days[d][cid] += 1
            got += len(rows)
            if len(rows) < 1000 or got >= n or page >= 60:
                break
            page += 1
        read += 1
    print(f"  email: {read} campaigns read, {sum(sum(v.values()) for v in days.values())} leads added in the window")
    return {"names": names, "days": {d: dict(v) for d, v in sorted(days.items())}}


def calls(since: str) -> dict:
    key = env(env("ATTIO_KEY_VAR") or "ATTIO_API_KEY_2") or env("ATTIO_API_KEY_2")
    hdr = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    spec = json.loads(SPEC.read_text())
    lists = requests.get("https://api.attio.com/v2/lists", headers=hdr, timeout=60)
    titles = {l.get("api_slug"): l.get("name") for l in (lists.json().get("data") or [])} if lists.status_code < 300 else {}
    names, regions, days = {}, {}, defaultdict(lambda: defaultdict(int))
    for L in spec["lists"]:
        slug = L["slug"]
        names[slug] = titles.get(slug) or L.get("tag") or slug
        regions[slug] = L.get("region") or "Other"
        off = 0
        while True:
            r = requests.post(f"https://api.attio.com/v2/lists/{slug}/entries/query", headers=hdr, timeout=60,
                              json={"limit": 500, "offset": off})
            if r.status_code == 429:
                time.sleep(3); continue
            if r.status_code >= 300:
                print(f"  a call list could not be read ({r.status_code}), its counts are partial", file=sys.stderr)
                break
            data = r.json().get("data") or []
            for e in data:
                d = day_of(e.get("created_at") or "")
                if d and d >= since:
                    days[d][slug] += 1
            if len(data) < 500 or off >= 20000:
                break
            off += 500
    print(f"  calls: {len(spec['lists'])} lists read, {sum(sum(v.values()) for v in days.values())} added in the window")
    return {"names": names, "regions": regions, "days": {d: dict(v) for d, v in sorted(days.items())}}


def main() -> int:
    days = int(os.environ.get("NEW_LEADS_DAYS", "40"))
    since = (datetime.now(LAGOS).date() - timedelta(days=days)).isoformat()
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tz": "Africa/Lagos", "since": since}
    for k, fn in (("email", email), ("calls", calls)):
        try:
            out[k] = fn(since)
        except Exception as ex:   # one source failing leaves the other; the page then uses the push hooks alone for it
            print(f"  {k} failed ({type(ex).__name__})", file=sys.stderr)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
