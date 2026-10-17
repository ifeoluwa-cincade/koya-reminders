"""
ct_hook_calls.py - which calls did CloudTalk's own webhook just report, and which of them still need work?

CloudTalk (Account > Webhooks) posts call.ended / call.recording_ready / call.tags_modified to the team app
(/hook/cloudtalk). The app saves each one as an event doc in its database (docs, coll "event", source "cloudtalk",
id "cloudtalk:<call id>:<kind>", with call_id, talk and recorded as plain fields) and starts this job through the relay.
This script reads those events and prints two lines for $GITHUB_ENV:

    HOOK_CALLS=<comma-separated call ids that still need an outcome or a call review>   (newest first, at most HOOK_MAX)
    HOOK_RECENT=1|0      1 = CloudTalk's webhook delivered something in the last HOOK_RECENT_MIN minutes (it is working)

The job then works on just those calls (--call: one CloudTalk read each, no listing). With no unfinished calls but a
working webhook, it reads only today's calls (the Attio-note path and Refresh still catch a call the webhook missed);
otherwise it runs exactly as before. Counts only on stderr: this repository is public.

    python crm/scripts/ct_hook_calls.py >> "$GITHUB_ENV"

A call counts as unfinished when the call memory (CALL_DATA_DIR/live_state.json) has no outcome for it or marks it for a
retry (no recording yet), when its tags changed in the last HOOK_RECENT_MIN minutes, or when it is a real conversation
(REVIEW_MIN_TALK s or more, recorded, not a voicemail) with no Call review yet. Calls already done cost nothing: the job's
memory skips them anyway, so this list only keeps the run short.
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

WORK = Path(os.environ.get("CALL_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data/working/live")
LOOKBACK_H = float(os.environ.get("HOOK_LOOKBACK_H") or 6)
RECENT_MIN = float(os.environ.get("HOOK_RECENT_MIN") or 30)
MAX = int(os.environ.get("HOOK_MAX") or 25)
REVIEW_MIN_TALK = int(os.environ.get("REVIEW_MIN_TALK") or 45)
NOT_TALK = {"VOICEMAIL", "NO_ANSWER", "NO_CONVERSATION"}
AI_KINDS = {"transcript_ready", "summary_ready", "score_ready"}


def say(s: str) -> None:
    print(s, file=sys.stderr, flush=True)


def _sb():
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return (url, key) if url and key else (None, None)


def _get(url: str, key: str, path: str, rng: str | None = None) -> list:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if rng:
        h["Range"] = rng
    r = requests.get(url + path, headers=h, timeout=20)
    r.raise_for_status()
    return r.json()


def read_events(url: str, key: str, since_iso: str) -> list[dict]:
    sel = "id,kind:data->>kind,call_id:data->>call_id,talk:data->>talk,recorded:data->>recorded,received_at:data->>received_at"
    return _get(url, key, f"/rest/v1/docs?coll=eq.event&data->>source=eq.cloudtalk&data->>received_at=gte.{requests.utils.quote(since_iso)}"
                          f"&select={sel}&order=data->>received_at.desc&limit=500")


def reviewed(url: str, key: str) -> set[str]:
    out, start = set(), 0
    while True:
        rows = _get(url, key, "/rest/v1/docs?coll=eq.callreview&select=id", f"{start}-{start + 999}")
        out |= {x["id"] for x in rows}
        if len(rows) < 1000:
            return out
        start += 1000


def pick(events: list[dict], state: dict, have_review: set[str], skip: dict, now: datetime) -> tuple[list[str], bool]:
    """-> (unfinished call ids newest first, webhook delivered recently). Pure: the unit test calls it."""
    recent_cut = now - timedelta(minutes=RECENT_MIN)
    by: dict[str, dict] = {}
    recent = False
    for e in events:                                   # newest first
        cid = str(e.get("call_id") or "").strip()
        try:
            at = datetime.fromisoformat(str(e.get("received_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at >= recent_cut:
            recent = True
        if not cid.isdigit() or e.get("kind") in AI_KINDS:     # CloudTalk's own AI results start no work here
            continue
        b = by.setdefault(cid, {"at": at, "talk": 0, "recorded": False, "tags_recent": False})
        try:
            b["talk"] = max(b["talk"], int(float(e.get("talk") or 0)))
        except ValueError:
            pass
        b["recorded"] = b["recorded"] or str(e.get("recorded")).lower() == "true" or e.get("kind") == "recording_ready"
        if e.get("kind") == "tags_modified" and at >= recent_cut:
            b["tags_recent"] = True
    out = []
    for cid, b in sorted(by.items(), key=lambda kv: kv[1]["at"], reverse=True):
        st = state.get(cid)
        need = (not isinstance(st, dict)) or bool(st.get("retry")) or b["tags_recent"]
        if not need and isinstance(st, dict):
            talk = max(b["talk"], int(st.get("talk") or 0))
            need = (talk >= REVIEW_MIN_TALK and b["recorded"] and st.get("verdict") not in NOT_TALK
                    and cid not in have_review and cid not in skip)
        if need:
            out.append(cid)
    return out[:MAX], recent


def main() -> int:
    url, key = _sb()
    if not url:
        say("no database address: the job runs as before")
        print("HOOK_CALLS=\nHOOK_RECENT=0")
        return 0
    now = datetime.now(timezone.utc)
    try:
        events = read_events(url, key, (now - timedelta(hours=LOOKBACK_H)).isoformat(timespec="seconds"))
        have = reviewed(url, key)
    except Exception as ex:                            # never stop the job: it falls back to the normal run
        say(f"could not read the webhook events ({type(ex).__name__}): the job runs as before")
        print("HOOK_CALLS=\nHOOK_RECENT=0")
        return 0
    def load(name: str) -> dict:
        f = WORK / name
        try:
            return json.loads(f.read_text()) if f.exists() else {}
        except Exception:
            return {}
    ids, recent = pick(events, load("live_state.json"), have, load("review_skip.json"), now)
    say(f"CloudTalk webhook: {len(events)} event(s) in the last {LOOKBACK_H:g} h, {len(ids)} call(s) still to do, "
        f"{'working' if recent else 'nothing'} in the last {RECENT_MIN:g} min")
    print(f"HOOK_CALLS={','.join(ids)}\nHOOK_RECENT={1 if recent else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
