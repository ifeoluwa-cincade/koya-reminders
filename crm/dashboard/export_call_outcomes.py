"""
export_call_outcomes.py — per-day real call conversations for the dashboard.

Reads the call dispositions that live_call_dispositions.py worked out (data/working/live/live_state.json)
and writes crm/dashboard/call_outcomes.json, a small file that IS committed, because the hourly cloud
build cannot see data/working. build_cold_email.py merges it into the page.

A "conversation" is a call where a real person talked (anything that is not voicemail, no answer,
hung up, or "no conversation"). `through` is the last day whose calls were ALL processed; the page only
uses the file up to that day and shows the later days as pending, so a half-processed day never reads low.

    python crm/dashboard/export_call_outcomes.py [--through YYYY-MM-DD]
"""
from __future__ import annotations
import argparse, collections, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "data/working/live/live_state.json"
OUT = ROOT / "crm/dashboard/call_outcomes.json"
# A conversation is talking with the LEAD. A wrong number or the wrong person (WRONG_PERSON) is answered, not a conversation (Ife, 2026-10-09).
REAL = {"MEETING_BOOKED", "SEND_FOLLOW_UP", "NOT_NOW", "CALLBACK", "NOT_INTERESTED", "DO_NOT_CALL"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--through", help="last fully processed day, YYYY-MM-DD (default: the latest day in the state)")
    a = ap.parse_args()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}      # no calls worked out yet today: keep what is committed
    days: dict[str, dict] = collections.defaultdict(lambda: {"calls": 0, "conversations": 0, "booked": 0, "pending": 0})
    for r in state.values():
        if r.get("type") != "outgoing":
            continue
        d = (r.get("started") or "")[:10]
        if not d:
            continue
        b = days[d]
        if r["verdict"] == "UNKNOWN":                      # failed this run, retried on the next: not counted yet
            b["pending"] += 1
            continue
        b["calls"] += 1                                   # calls whose outcome is known: the connect rate's denominator
        b["conversations"] += r["verdict"] in REAL
        b["booked"] += r["verdict"] == "MEETING_BOOKED"
    through = a.through or (max(days) if days else "")
    days = {d: v for d, v in days.items() if d <= through}
    # keep older days already in the committed file; days seen in this state overwrite theirs
    old = json.loads(OUT.read_text()) if OUT.exists() else {"days": {}}
    merged = {**old.get("days", {})}
    for d, v in days.items():                          # a day's count can only grow: never replace it with a smaller, partial one
        if d not in merged or v["calls"] >= merged[d].get("calls", 0):
            merged[d] = v
    through = max(through, old.get("through", ""))
    OUT.write_text(json.dumps({"through": through, "days": dict(sorted(merged.items()))}, indent=1))
    for d, v in sorted(merged.items()):
        print(d, v)
    print(f"wrote {OUT.relative_to(ROOT)} (through {through})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
