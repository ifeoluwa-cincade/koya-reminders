"""
notion_month_sync.py - keeps the Notion "Month by month" database (Client Acquisition hub) in step with Koya HQ.

Once a day: reads Koya HQ's monthly numbers (GET /api/v1/numbers?month=YYYY-MM, the same numbers HQ shows) and writes them to that
month's row in Notion, creating the row on the first run of a new month. During the first 5 days of a month it also refreshes the
previous month, so its final numbers land. Human columns (Main bottleneck, Fix this month) are never touched.

Ife 2026-11-25: "we can do the daily job". Secrets: KOYA_HQ_API_KEY, NOTION_TOKEN (an internal integration with access to the
Client Acquisition page). PUBLIC REPO: prints counts and status codes only. Read-only against HQ; writes only that Notion database.

    python crm/dashboard/notion_month_sync.py            # write
    python crm/dashboard/notion_month_sync.py --dry-run  # print what would be written
"""
from __future__ import annotations
import calendar, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
HQ = "https://hq.koyatalent.com/api/v1/numbers"
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
DATA_SOURCE = "4239e57e-bc64-817a-b9c9-000b8b5c09be"   # Client Acquisition > Campaigns and results > Month by month
LAGOS = timezone(timedelta(hours=1))


def env(k: str) -> str:
    v = os.environ.get(k, "").strip()
    if v:
        return v
    for f in (ROOT / ".env", ROOT.parent / "sales-nav-pipeline" / ".env"):
        if f.exists():
            for line in f.read_text().splitlines():
                if line.startswith(k + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    return ""


def hq_month(month: str) -> dict:
    r = requests.get(HQ, params={"month": month}, headers={"Authorization": f"Bearer {env('KOYA_HQ_API_KEY')}"}, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"HQ numbers failed for {month}: HTTP {r.status_code}")
    return r.json()


def props_for(month: str, d: dict, today: str) -> dict:
    e, c = d.get("email") or {}, d.get("calls") or {}
    v = lambda x: (x or {}).get("v") if isinstance(x, dict) else x
    y, m = map(int, month.split("-"))
    p = {
        "Month": {"title": [{"text": {"content": month}}]},   # "2026-11": short and sorts right in charts
        "Month start": {"date": {"start": f"{month}-01"}},
        "Contacted": {"number": v(e.get("contacted"))},
        "Contacted goal": {"number": (e.get("contacted") or {}).get("target")},
        "Replies": {"number": v(e.get("replies"))},
        "Interested": {"number": v(e.get("interested"))},
        "Booked": {"number": v(e.get("booked"))},
        "Held": {"number": v(e.get("held"))},
        "Won": {"number": v(e.get("won"))},
        "Dials": {"number": v(c.get("dials"))},
        "Source": {"select": {"name": "Koya HQ"}},
        "Updated": {"date": {"start": today}},
    }
    nl = ((d.get("newLeads") or {}).get("email") or {}).get("month")   # HQ only reports new leads for the current month
    if nl is not None:
        p["Leads added"] = {"number": nl}
    return p


def notion(method: str, path: str, body: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    r = requests.request(method, NOTION + path, headers=h, json=body, timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"Notion {method} {path.split('/')[1]} failed: HTTP {r.status_code}")
    return r.json()


def find_row(month: str) -> str | None:
    q = notion("POST", f"/data_sources/{DATA_SOURCE}/query",
               {"filter": {"property": "Month start", "date": {"equals": f"{month}-01"}}, "page_size": 5})
    rows = q.get("results") or []
    return rows[0]["id"] if rows else None


def main() -> int:
    dry = "--dry-run" in sys.argv
    now = datetime.now(LAGOS)
    today = now.date().isoformat()
    months = [now.strftime("%Y-%m")]
    if now.day <= 5:
        months.append((now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    if not env("KOYA_HQ_API_KEY"):
        print("KOYA_HQ_API_KEY missing"); return 1
    if not dry and not env("NOTION_TOKEN"):
        print("NOTION_TOKEN missing"); return 1
    for month in months:
        props = props_for(month, hq_month(month), today)
        nums = {k: list(v.values())[0] for k, v in props.items() if "number" in v}
        if dry:
            print(month, json.dumps(nums)); continue
        row = find_row(month)
        if row:
            notion("PATCH", f"/pages/{row}", {"properties": props}); print(f"{month}: row updated", nums.get("Contacted"))
        else:
            notion("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE}, "properties": props})
            print(f"{month}: row created", nums.get("Contacted"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
