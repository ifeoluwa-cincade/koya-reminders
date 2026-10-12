"""Keeps the hourly health-check history behind the Systems & operations page.

Each run of collect_ops.py appends one record: {"t": time, "s": {check_id: o|w|b|i}, "d": {check_id: detail}}
(details only for checks that were not ok). The file lives on the `dashboard-build` branch next to the page, so the
history survives between hourly builds; the workflow restores it before collecting and saves it after building.
`backfill` seeds it once from GitHub's own run history for the workflows that have run for weeks.

  python crm/dashboard/check_history.py backfill     # one-off seed from GitHub run history
"""
from __future__ import annotations
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FILE = ROOT / "data/working/check_history.json"
SEED = ROOT / "crm/dashboard/check_history_seed.json"
KEEP_DAYS = 120
CODE = {"ok": "o", "warn": "w", "bad": "b", "info": "i"}
RETIRED = ("followup_digest.yml",)      # the daily follow-up digest was killed on 2026-10-11 (Ife)


def load() -> dict:
    for p in (FILE, SEED):
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    return {"runs": [], "names": {}}


def clean(h: dict) -> int:
    """Drop records that were false alarms of our own making: a job skipped on purpose (2026-10-11 minute saving) counted as 'failing',
    and the reminder check used to mention an email backup that does not exist."""
    n = 0
    for cid in RETIRED:                                   # automations that were switched off on purpose must not show as dead on the status page
        h.get("names", {}).pop(cid, None)
    for r in h["runs"]:
        for cid in RETIRED:
            if cid in r.get("s", {}):
                r["s"].pop(cid, None); (r.get("d") or {}).pop(cid, None); n += 1
        for cid in list(r.get("s", {})):
            d = (r.get("d") or {}).get(cid, "")
            if (cid in ("crm_sync.yml", "plusvibe_phone_lookup.yml", "crm_dashboard.yml", "call_dispositions.yml") and "skipped" in d) or (cid == "reminders" and "email" in d.lower()):
                r["s"][cid] = "o"; (r.get("d") or {}).pop(cid, None); n += 1
    return n


def save(h: dict) -> None:
    clean(h)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    h["runs"] = [r for r in h["runs"] if r["t"] >= cutoff]
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(h, separators=(",", ":")))


def record(checks: list[dict], when: str | None = None) -> dict:
    h = load()
    t = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    s, d = {}, {}
    for c in checks:
        h["names"][c["id"]] = {"node": c["node"], "name": c["name"]}
        s[c["id"]] = CODE.get(c["status"], "i")
        if c["status"] in ("warn", "bad"):
            d[c["id"]] = str(c.get("detail") or "")[:160]
    h["runs"].append({"t": t, "s": s, "d": d})
    h["runs"].sort(key=lambda r: r["t"])
    save(h)
    return h


def backfill() -> None:
    """GitHub keeps ~100 runs per workflow: turn each completed run into a record (success = ok, anything else = bad)."""
    repo = os.environ.get("GITHUB_REPOSITORY") or "ifeoluwa-cincade/AAT-Sales-Nav-Pipeline"
    flows = [("crm_dashboard.yml", "github", "Hourly dashboard build"), ("crm_sync.yml", "github", "PlusVibe replies into Attio deals"),
             ("plusvibe_phone_lookup.yml", "github", "Phone lookup for interested leads"),
             ("crm_daily.yml", "cloudtalk", "Daily check of dialler tags"), ("followup_digest.yml", "discord", "Follow-up digest to Discord")]
    h = {"runs": [], "names": {}}
    runs: dict[str, dict] = {}
    for fname, node, name in flows:
        h["names"][fname] = {"node": node, "name": name}
        for page in (1, 2):
            out = subprocess.run(["gh", "api", f"repos/{repo}/actions/workflows/{fname}/runs?per_page=100&page={page}&status=completed"],
                                 capture_output=True, text=True).stdout
            try:
                wr = json.loads(out).get("workflow_runs") or []
            except Exception:
                wr = []
            for r in wr:
                if r.get("conclusion") in ("skipped", "cancelled"):
                    continue
                t = (r.get("run_started_at") or r["created_at"]).replace("Z", "+00:00")
                ok = r.get("conclusion") == "success"
                rec = runs.setdefault(t, {"t": t, "s": {}, "d": {}, "bf": 1})
                rec["s"][fname] = "o" if ok else "b"
                if not ok:
                    rec["d"][fname] = f"run {r.get('conclusion')} (from GitHub run history)"
            if len(wr) < 100:
                break
    h["runs"] = sorted(runs.values(), key=lambda r: r["t"])
    SEED.write_text(json.dumps(h, separators=(",", ":")))
    bad = sum(1 for r in h["runs"] for v in r["s"].values() if v == "b")
    print(f"seeded {len(h['runs'])} records from {h['runs'][0]['t'][:10] if h['runs'] else '-'}; {bad} failed runs")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill()
