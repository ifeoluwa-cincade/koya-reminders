#!/usr/bin/env python3
"""Daily inbox allocator for the PlusVibe cold-email campaigns. REPORT-ONLY by default.

What it does, every weekday before the New York sending window (Mon-Fri 9-5 NY):
  1. Reads every ACTIVE top-level campaign (subsequences "| Cycle" and campaigns with a parent are skipped).
  2. Counts, per campaign:
       follow-ups due = leads with status CONTACTED (or RESCHEDULED), is_completed 0, is_paused 0,
                        sent_step < total_steps, and next_email_time at or before today's window end (17:00 NY).
                        Overdue ones count too.
       in flight      = the same leads with next_email_time later than today (owed on a later day).
       untouched      = NOT_CONTACTED, from /lead/count/lead-status.
  3. Works out the ceiling = sum of campaign_limit_today over ACTIVE mailboxes attached to a counted campaign
     (/account/list). Falls back to ceiling_fallback in the config.
  4. Allocates: new leads first in config order (main first, then TAM, then the rest), then follow-ups due,
     never more new sends than a campaign has untouched leads. Floors: floor_with_work for a campaign with work
     today, floor_waiting for one with follow-ups owed on a later day. A finished campaign (nothing untouched,
     nothing in flight) is left alone and flagged. The sum never goes over the ceiling.
  5. send_priority = followups / (followups + new), rounded to priority_step, clamped 0..1.

Writes a JSON report to data/working/allocator/<date>.json.

--apply changes the live campaigns, and refuses unless ALLOCATOR_LIVE=1 is also set. It prints every PATCH
body before sending it. Daily limits change ONLY through the `schedules` ARRAY (or `adv_schedule` when the
campaign uses the advanced schedule) on PATCH /campaign/update/campaign; a top-level daily_limit is rejected.
send_priority is a top-level field on the same PATCH.

Usage:
  python tools/allocator/plusvibe_allocate.py                  # report only
  ALLOCATOR_LIVE=1 python tools/allocator/plusvibe_allocate.py --apply   # live (needs Ife's go)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CONFIG = HERE / "allocator_config.json"
BASE = "https://api.plusvibe.ai/api/v1"
WORKSPACE_ID = "6a6e634971ad0f0e174a5471"
DAY_KEYS = {"Monday": "1", "Tuesday": "2", "Wednesday": "3", "Thursday": "4",
            "Friday": "5", "Saturday": "6", "Sunday": "7"}


# ---------------------------------------------------------------- API helpers

def key() -> str:
    """PLUS_VIBE_API_KEY from the environment, else the nearest .env walking up (works from a worktree)."""
    if os.environ.get("PLUS_VIBE_API_KEY"):
        return os.environ["PLUS_VIBE_API_KEY"].strip()
    for d in [ROOT, *ROOT.parents]:
        env = d / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("PLUS_VIBE_API_KEY="):
                    return line.split("=", 1)[1].strip()
    sys.exit("PLUS_VIBE_API_KEY not set and not in any .env")


def get(path: str, params: dict, hdr: dict, tries: int = 6):
    """429 on PlusVibe looks like empty data: always back off and retry (same as sync_plusvibe.py)."""
    delay, last, r = 2.0, None, None
    for _ in range(tries):
        try:
            r = requests.get(BASE + path, headers=hdr, params={"workspace_id": WORKSPACE_ID, **params}, timeout=120)
            if r.status_code != 429:
                time.sleep(0.25)          # stay well under 5 req/s
                return r
        except requests.exceptions.RequestException as ex:
            last = ex
        time.sleep(delay)
        delay *= 2
    if last:
        raise last
    return r


def status_counts(cid: str, hdr: dict) -> dict:
    r = get("/lead/count/lead-status", {"campaign_id": cid}, hdr)
    r.raise_for_status()
    body = r.json()                       # an ARRAY of {status, count, ...}
    return {x.get("status"): int(x.get("count") or 0) for x in body} if isinstance(body, list) else {}


def leads_with_status(cid: str, status: str, hdr: dict) -> list:
    rows, page = [], 1
    while True:
        r = get("/lead/workspace-leads", {"campaign_id": cid, "status": status, "limit": 1000, "page": page}, hdr)
        r.raise_for_status()
        batch = r.json() or []
        rows += batch
        if len(batch) < 1000:
            return rows
        page += 1


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- counting

def count_campaign(c: dict, cfg: dict, window_end: datetime, hdr: dict) -> dict:
    cid = c["id"]
    counts = status_counts(cid, hdr)
    due = later = overdue = 0
    day_start = window_end.replace(hour=0, minute=0)
    warn = []
    for st in cfg["followup_statuses"]:
        expect = counts.get(st, 0)
        if not expect:
            continue
        rows = leads_with_status(cid, st, hdr)
        rows = [x for x in rows if x.get("status") == st]      # never trust the server-side filter blindly
        if abs(len(rows) - expect) > max(5, expect * 0.02):
            warn.append(f"{st}: pulled {len(rows)} vs count {expect} (rate-limited?)")
        for x in rows:
            if x.get("is_completed") or x.get("is_paused"):
                continue
            if int(x.get("sent_step") or 0) >= int(x.get("total_steps") or 0):
                continue
            t = parse_ts(x.get("next_email_time"))
            if t is None:
                continue
            if t <= window_end:
                due += 1
                overdue += t < day_start
            else:
                later += 1
    return {"untouched": counts.get("NOT_CONTACTED", 0), "due": due, "overdue": overdue, "later": later,
            "status_counts": counts, "warnings": warn}


def mailbox_ceiling(camp_ids: set, cfg: dict, hdr: dict) -> tuple[int, str, int]:
    """Sum of campaign_limit_today over ACTIVE mailboxes attached to a counted campaign."""
    accts, skip = [], 0
    while True:
        r = get("/account/list", {"skip": skip, "limit": 100}, hdr)
        if not r.ok:
            break
        body = r.json()
        batch = body.get("accounts", []) if isinstance(body, dict) else (body or [])
        accts += batch
        if len(batch) < 100:
            break
        skip += 100
    if not accts:
        return cfg["ceiling_fallback"], "config fallback (no mailboxes read)", 0
    total, n = 0, 0
    for a in accts:
        if a.get("status") != "ACTIVE":
            continue
        p = a.get("payload") or {}
        cmps = {str(x.get("id")) for x in (p.get("cmps") or [])}
        if camp_ids and not (cmps & camp_ids):
            continue
        lim = p.get("campaign_limit_today")
        if lim is None:
            lim = p.get("daily_limit") or cfg["per_mailbox_fallback"]
        total += int(lim)
        n += 1
    if not n:
        return cfg["ceiling_fallback"], "config fallback (no active mailboxes)", 0
    return total, f"live: {n} active mailboxes, sum of campaign_limit_today", n


# ---------------------------------------------------------------- allocation

def round_step(x: float, step: float) -> float:
    return round(max(0.0, min(1.0, round(x / step) * step)), 2)


def allocate(rows: list, ceiling: int, cfg: dict) -> list:
    """rows are in priority order. Fills follow_alloc, new_alloc, proposed_limit, proposed_priority, note."""
    left = ceiling
    # 1. new leads first, by priority, never more than the untouched pool (Ife 2026-10-26: step 1 brings
    #    the replies; follow-ups come after and take whatever is left)
    for r in rows:
        cap = r["untouched"] if r["new_leads"] else 0
        if r["max_new"] is not None:
            cap = min(cap, r["max_new"])
        r["new_alloc"] = min(cap, max(left, 0))
        left -= r["new_alloc"]
    # 2. follow-ups due fill the rest, in priority order
    for r in rows:
        r["follow_alloc"] = min(r["due"], max(left, 0))
        left -= r["follow_alloc"]
    # 3. floors and the finished check
    for r in rows:
        work = r["follow_alloc"] + r["new_alloc"]
        if work > 0:
            floor = cfg["floor_with_work"]
        elif r["later"] > 0 or (r["untouched"] and not r["new_leads"]):
            floor = cfg["floor_waiting"]
        else:
            r["finished"] = True
            r["notes"].append("finished: nothing untouched, nothing in flight; left unchanged (consider pausing)")
            r["proposed_limit"] = r["cur_limit"]
            r["proposed_priority"] = r["cur_priority"]
            continue
        r["floor"] = floor
        r["proposed_limit"] = max(work, floor)
    # 4. floors may push the sum over the ceiling: take it back from the lowest priority first,
    #    follow-ups before new sends, never below a campaign's own floor
    counted = [r for r in rows if not r.get("finished")]
    over = sum(r["proposed_limit"] for r in counted) - ceiling
    for part in ("follow_alloc", "new_alloc"):
        for r in reversed(counted):
            if over <= 0:
                break
            give = min(over, r[part], r["proposed_limit"] - r["floor"])
            if give > 0:
                r[part] -= give
                r["proposed_limit"] -= give
                over -= give
    if over > 0:
        rows[0]["notes"].append(f"floors alone leave the plan {over} over the ceiling")
    for r in counted:
        if r["follow_alloc"] < r["due"]:
            r["notes"].append(f"only {r['follow_alloc']} of {r['due']} follow-ups due fit under the ceiling today "
                              f"(the rest roll to tomorrow)")
        f, n = r["follow_alloc"], r["new_alloc"]
        if f + n:
            r["proposed_priority"] = round_step(f / (f + n), cfg["priority_step"])
        elif r["due"] or r["later"]:
            r["proposed_priority"] = 1.0       # only follow-ups are left for its floor
        else:
            r["proposed_priority"] = r["cur_priority"]
    return rows


# ---------------------------------------------------------------- PATCH bodies

def patch_body(c: dict, limit: int, priority) -> dict:
    """Daily limit lives inside the schedule (array) or adv_schedule; send_priority is top level."""
    body = {"workspace_id": WORKSPACE_ID, "campaign_id": c["id"], "send_priority": priority}
    if c.get("use_adv_schedule"):
        adv = dict(c.get("adv_schedule") or {})
        adv["daily_limit"] = int(limit)
        adv.setdefault("daily_limit_new_lead", None)
        body["adv_schedule"] = adv
        return body
    s = c.get("schedule") or {}
    item = {
        "daily_limit": int(limit),
        "start_date": c.get("camp_st_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "end_date": c.get("camp_end_date") or "",
        "days": {DAY_KEYS[d]: True for d in (s.get("days") or []) if d in DAY_KEYS},
        "timezone": s.get("tz") or "America/New_York",
        "timing": {"from": s.get("from_time") or "09:00", "to": s.get("to_time") or "17:00"},
    }
    if c.get("daily_limit_new_lead") is not None:
        item["daily_limit_new_lead"] = int(c["daily_limit_new_lead"])
    body["schedules"] = [item]
    return body


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="send the PATCHes (also needs ALLOCATOR_LIVE=1)")
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args()

    if a.apply and os.environ.get("ALLOCATOR_LIVE") != "1":
        print("REFUSED: --apply needs ALLOCATOR_LIVE=1 in the environment as well. Nothing was changed.")
        return 2

    cfg = json.loads(Path(a.config).read_text())
    hdr = {"x-api-key": key()}
    tz = ZoneInfo(cfg["send_window"]["tz"])
    now_local = datetime.now(tz)
    hh, mm = (int(x) for x in cfg["send_window"]["to"].split(":"))
    window_end = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = now_local.strftime("%Y-%m-%d")

    camps = get("/campaign/list-all", {}, hdr).json()      # BARE LIST keyed id + camp_name
    by_id = {str(c.get("id")): c for c in camps}
    order = {x["id"]: i for i, x in enumerate(cfg["campaigns"])}
    meta = {x["id"]: x for x in cfg["campaigns"]}

    chosen = []
    for c in camps:
        cid, name = str(c.get("id")), c.get("camp_name") or ""
        if c.get("status") != "ACTIVE" or c.get("parent_camp_id"):
            continue
        if any(s in name for s in cfg["skip_name_contains"]):
            continue
        if cid not in meta and (cfg.get("other_active_campaigns") == "ignore" or not c.get("lead_count")):
            continue
        chosen.append(c)
    chosen.sort(key=lambda c: (order.get(str(c["id"]), 999), -(c.get("lead_count") or 0)))

    rows = []
    for c in chosen:
        cid = str(c["id"])
        m = meta.get(cid, {"label": c.get("camp_name"), "new_leads": True, "max_new_per_day": None})
        cnt = count_campaign(c, cfg, window_end, hdr)
        rows.append({
            "id": cid, "name": c.get("camp_name"), "label": m["label"], "new_leads": m["new_leads"],
            "max_new": m.get("max_new_per_day"),
            "cur_limit": c.get("daily_limit"), "cur_priority": c.get("send_priority"),
            "due": cnt["due"], "overdue": cnt["overdue"], "later": cnt["later"], "untouched": cnt["untouched"],
            "status_counts": cnt["status_counts"], "notes": list(cnt["warnings"]),
        })

    ceiling, ceiling_src, n_mail = mailbox_ceiling({r["id"] for r in rows}, cfg, hdr)
    allocate(rows, ceiling, cfg)

    # ------------------------------------------------ print the table
    w = 28
    print(f"\nPlusVibe inbox allocator  {today} ({now_local.strftime('%H:%M')} NY)  "
          f"{'APPLY' if a.apply else 'REPORT ONLY, nothing changed'}")
    print(f"Ceiling: {ceiling}/day ({ceiling_src}). Follow-ups due = next_email_time on or before "
          f"{cfg['send_window']['to']} NY today.\n")
    hd = f"{'Campaign':<{w}} {'Now lim':>7} {'Now pri':>7} {'FU due':>6} {'FU later':>8} {'Untouch':>7} {'New':>5} {'New lim':>7} {'New pri':>7}"
    print(hd)
    print("-" * len(hd))
    for r in rows:
        print(f"{r['label'][:w]:<{w}} {r['cur_limit'] or 0:>7} {r['cur_priority'] if r['cur_priority'] is not None else '':>7} "
              f"{r['due']:>6} {r['later']:>8} {r['untouched']:>7} {r['new_alloc']:>5} {r['proposed_limit']:>7} "
              f"{r['proposed_priority'] if r['proposed_priority'] is not None else '':>7}")
    tot_now = sum(r["cur_limit"] or 0 for r in rows)
    tot_new = sum(r["proposed_limit"] for r in rows)
    print("-" * len(hd))
    print(f"{'TOTAL':<{w}} {tot_now:>7} {'':>7} {sum(r['due'] for r in rows):>6} {sum(r['later'] for r in rows):>8} "
          f"{sum(r['untouched'] for r in rows):>7} {sum(r['new_alloc'] for r in rows):>5} {tot_new:>7}")
    print(f"\nNow: {tot_now}/{ceiling} ({tot_now - ceiling:+d} vs ceiling). "
          f"Proposed: {tot_new}/{ceiling} ({ceiling - tot_new} spare).")
    spare = ceiling - tot_new
    if spare > 0:
        print(f"Spare capacity {spare}/day: not enough due follow-ups or untouched leads to fill the inboxes. Load more leads.")
    for r in rows:
        if r["overdue"]:
            print(f"  {r['label']}: {r['overdue']} of the {r['due']} follow-ups due were already due before today")
    for r in rows:
        for n in r["notes"]:
            print(f"  note, {r['label']}: {n}")

    patches = [(r, patch_body(by_id[r["id"]], r["proposed_limit"], r["proposed_priority"]))
               for r in rows if not r.get("finished")
               and (r["proposed_limit"] != r["cur_limit"] or r["proposed_priority"] != r["cur_priority"])]

    print(f"\nPATCH /campaign/update/campaign bodies ({len(patches)}){'' if a.apply else ', NOT sent'}:")
    for r, b in patches:
        print(json.dumps(b, separators=(",", ":")))

    sent = []
    if a.apply:
        for r, b in patches:
            resp = requests.patch(BASE + "/campaign/update/campaign", headers={**hdr, "Content-Type": "application/json"},
                                  json=b, timeout=60)
            ok = resp.ok
            print(f"  {r['label']}: HTTP {resp.status_code}" + ("" if ok else f" {resp.text[:500]}"))
            sent.append({"id": r["id"], "status": resp.status_code, "ok": ok, "body": resp.text[:500]})
            time.sleep(0.5)
        # read back from the live system, not from our own request
        live = {str(c.get("id")): c for c in get("/campaign/list-all", {}, hdr).json()}
        for r, _ in patches:
            c = live.get(r["id"], {})
            good = c.get("daily_limit") == r["proposed_limit"] and c.get("send_priority") == r["proposed_priority"]
            print(f"  read back {r['label']}: limit {c.get('daily_limit')} priority {c.get('send_priority')} "
                  f"{'OK' if good else 'MISMATCH'}")

    out_dir = ROOT / "data/working/allocator"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "date": today, "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "apply" if a.apply else "report", "ceiling": ceiling, "ceiling_source": ceiling_src,
        "mailboxes": n_mail, "window_end_ny": window_end.isoformat(),
        "total_current": tot_now, "total_proposed": tot_new,
        "campaigns": rows, "patches": [b for _, b in patches], "sent": sent,
    }
    path = out_dir / f"{today}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    print(f"\nReport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
