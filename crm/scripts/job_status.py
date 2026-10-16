"""
job_status.py - a job says where it is, so the team app's Refresh button can show real progress.

Writes ONE row per job in the team app's Supabase `docs` table: `jobrun/<job>` (for example jobrun/dispositions,
jobrun/dashboard). Counts and phase names only: never names, numbers, emails or call text (this repository is public
and the row is read by the app's server). It never stops a job: if Supabase is not reachable it prints one short line.

The app writes `jobrun/refresh` when someone presses Refresh ({id, at}). The call-outcome job reads it at the end:
a refresh nobody has rebuilt the numbers for yet means "start the dashboard build" (see call_dispositions.yml).

    python crm/scripts/job_status.py set dispositions --phase reading --text "Reading new calls"
    python crm/scripts/job_status.py set dispositions --phase done --result ok
    python crm/scripts/job_status.py rebuild-needed          # prints true/false, and claims the request when true

    from job_status import JobStatus
    js = JobStatus("dispositions"); js.update(phase="outcomes", done=2, total=5)
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

TMP = Path(os.environ.get("RUNNER_TEMP") or "/tmp")
STALE_REQUEST_S = 40 * 60          # a refresh asked for more than 40 minutes ago is not rebuilt for any more


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sb():
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return (url, key) if url and key else (None, None)


def _h(key: str, extra: dict | None = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    h.update(extra or {})
    return h


def read_doc(did: str) -> dict | None:
    url, key = _sb()
    if not url:
        return None
    try:
        r = requests.get(f"{url}/rest/v1/docs?path=eq.jobrun/{did}&select=data", headers=_h(key), timeout=15)
        rows = r.json() if r.status_code < 300 else []
        return (rows[0] or {}).get("data") if rows else None
    except Exception:
        return None


def write_doc(did: str, data: dict) -> bool:
    url, key = _sb()
    if not url:
        return False
    try:
        r = requests.post(f"{url}/rest/v1/docs?on_conflict=path", timeout=15,
                          headers=_h(key, {"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}),
                          json=[{"path": f"jobrun/{did}", "coll": "jobrun", "id": did, "data": data,
                                 "updated_at": _now(), "updated_by": "job-status"}])
        if r.status_code >= 300:
            print(f"   (job status not saved: HTTP {r.status_code})", flush=True)
            return False
        return True
    except Exception as ex:
        print(f"   (job status not saved: {type(ex).__name__})", flush=True)
        return False


class JobStatus:
    """The current row for one job, kept in a temp file between the steps of a run (each step is a new process)."""

    def __init__(self, job: str):
        self.job = job
        self.file = TMP / f"jobstatus_{job}.json"
        rid = os.environ.get("GITHUB_RUN_ID", "local")
        try:
            cur = json.loads(self.file.read_text())
        except Exception:
            cur = {}
        if cur.get("run_id") != rid:                    # a new run starts a new row
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            cur = {"v": 1, "job": job, "run_id": rid, "event": os.environ.get("GITHUB_EVENT_NAME", "local"),
                   "run_url": f"https://github.com/{repo}/actions/runs/{rid}" if repo and rid != "local" else "",
                   "started_at": _now(), "phase": "starting", "result": None}
        self.d = cur
        self._last = 0.0

    def update(self, force: bool = False, **fields) -> None:
        self.d.update({k: v for k, v in fields.items() if v is not None})
        self.d["updated_at"] = _now()
        try:
            self.file.write_text(json.dumps(self.d))
        except Exception:
            pass
        if force or time.time() - self._last > 4 or fields.get("phase") or fields.get("result"):     # progress at most every 4 s
            self._last = time.time()
            write_doc(self.job, self.d)


def rebuild_needed(claim: bool = True) -> bool:
    """True when someone pressed Refresh and nothing has rebuilt the numbers for that press yet."""
    req = read_doc("refresh") or {}
    rid, at = req.get("id"), req.get("at")
    if not rid or not at:
        return False
    try:
        age = time.time() - datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
    except Exception:
        return False
    if age > STALE_REQUEST_S or req.get("rebuild_for") == rid:
        return False
    if claim:
        req["rebuild_for"] = rid
        req["rebuild_at"] = _now()
        req["rebuild_by_run"] = os.environ.get("GITHUB_RUN_ID", "local")
        write_doc("refresh", req)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("job")
    s.add_argument("--phase")
    s.add_argument("--text")
    s.add_argument("--result", choices=["ok", "partial", "failed"])
    s.add_argument("--finished", action="store_true")
    s.add_argument("--json", help="extra fields as a JSON object (counts only)")
    r = sub.add_parser("rebuild-needed")
    r.add_argument("--no-claim", action="store_true")
    a = ap.parse_args()
    if a.cmd == "set":
        js = JobStatus(a.job)
        extra = json.loads(a.json) if a.json else {}
        js.update(force=True, phase=a.phase, text=a.text, result=a.result,
                  finished_at=_now() if a.finished else None, **extra)
        return 0
    need = rebuild_needed(claim=not a.no_claim)
    print("true" if need else "false")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"rebuild={'true' if need else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
