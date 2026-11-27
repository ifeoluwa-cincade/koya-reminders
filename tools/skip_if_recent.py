"""skip_if_recent.py: a late GitHub timer run steps aside when Koya HQ's clock already started this job (HQ maintenance, 27 Nov 2026).

GitHub's cron runs hours late (the "hourly" stage sweep ran 3-4 times a day; Call recall rounds, meant for 07:40 Lagos, ran in the
evening). Koya HQ's hourly clock (koya-app lib/clockjobs.js) now starts these workflows on time with workflow_dispatch. The GitHub
timers stay as a fallback, so a schedule run first asks: did a run of this same workflow start in the last WINDOW minutes (any event,
not this run, not cancelled)? Then it skips. Only schedule runs are gated: a manual or HQ dispatch always runs.

    python tools/skip_if_recent.py --window 45     # writes run=true|false to $GITHUB_OUTPUT

Needs GITHUB_TOKEN with actions: read. Any error reading the history -> run (never lose a run because of this check).
"""
from __future__ import annotations
import argparse, json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone


def parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def decide(runs, me: str, now: datetime, window_min: int, events=None) -> tuple[bool, str]:
    """-> (run?, why). runs: GitHub workflow_runs of this workflow, newest first. events: only these count (stage_sync: its
    deal_stage runs handle single deals, not the sweep, so only schedule / workflow_dispatch runs count)."""
    cut = now - timedelta(minutes=window_min)
    for r in runs or []:
        if str(r.get("id")) == str(me):
            continue
        if events and r.get("event") not in events:
            continue
        if r.get("conclusion") in ("cancelled", "skipped"):
            continue
        t = parse(r.get("run_started_at") or r.get("created_at"))
        if t and cut <= t <= now + timedelta(minutes=1):
            return False, f"run {r.get('id')} ({r.get('event')}) started {int((now - t).total_seconds() // 60)} min ago"
    return True, "no run in the last %d min" % window_min


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=45, help="minutes")
    ap.add_argument("--events", default="", help="comma-separated run events that count (default: all)")
    a = ap.parse_args()
    out = os.environ.get("GITHUB_OUTPUT")
    ok, why = True, "not a timer run"
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        try:
            repo, run_id = os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_RUN_ID", "")
            wf = os.environ["GITHUB_WORKFLOW_REF"].split("@")[0].split("/")[-1]
            req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?per_page=20",
                                         headers={"Authorization": "Bearer " + os.environ.get("GITHUB_TOKEN", ""),
                                                  "Accept": "application/vnd.github+json"})
            runs = json.load(urllib.request.urlopen(req, timeout=20)).get("workflow_runs", [])
            ok, why = decide(runs, run_id, datetime.now(timezone.utc), a.window, [e for e in a.events.split(",") if e] or None)
        except Exception as e:
            ok, why = True, "could not read the run history (%s): running" % type(e).__name__
    print(("RUN: " if ok else "SKIP (Koya HQ's clock already started it): ") + why)
    if out:
        with open(out, "a") as f:
            f.write("run=%s\n" % ("true" if ok else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
