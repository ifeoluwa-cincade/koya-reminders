"""
call_stop_plusvibe.py — a cold call that ends "not interested" stops that lead's PlusVibe emails (Ife, 2026-10-16).

Someone who told us no on the phone must not keep getting the email sequence. apply_call_verdicts.py calls
stop_person() for every person whose newest real call this run is NOT_INTERESTED. The stop is the same one the
dashboard's "Stop emails" action and stage_to_plusvibe.py use: POST /lead/update/status, new_status COMPLETED,
for every campaign and subsequence the lead is still live in (PlusVibe docs: "COMPLETED - no further emails").

    verdict          emails
    NOT_INTERESTED   every live sequence set to COMPLETED
    WRONG_PERSON     untouched: the NUMBER reached the wrong person, the lead itself said nothing (saved rule)
    anything else    untouched (a booked meeting stops emails through its deal: stage_to_plusvibe.py)

Running it twice does nothing the second time: sequences already COMPLETED / REPLIED / BOUNCED / UNSUBSCRIBED /
SKIPPED are left alone. A lead that is not in PlusVibe is skipped quietly. A PlusVibe lookup that is refused
(429 after back-off) counts as a failure, so the call is tried again on the next run instead of being dropped.

Stand-alone, for a catch-up or a check (finds recent Not Interested calls from the call notes in Attio):
    python crm/scripts/call_stop_plusvibe.py --days 7              # dry run
    python crm/scripts/call_stop_plusvibe.py --days 7 --commit

Public repo: only counts, record ids and status codes are printed, never names or emails.
"""
from __future__ import annotations
import argparse, os, re, sys, time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
sys.path.insert(0, str(ROOT / "crm" / "dashboard"))
import collect_ops as CO  # noqa: E402
from stage_to_plusvibe import DONE, pv_post, _why  # noqa: E402  (same stop call, same back-off)

STOP_VERDICTS = {"NOT_INTERESTED"}


def pv_header() -> dict | None:
    key = os.environ.get("PLUS_VIBE_API_KEY", "").strip()
    return {"x-api-key": key} if key else None


def person_emails(p: dict) -> list[str]:
    return sorted({(e.get("email_address") or "").strip().lower()
                   for e in (p.get("values") or {}).get("email_addresses") or [] if "@" in (e.get("email_address") or "")})


def stop_person(emails: list[str], hdr: dict, camps: dict, commit: bool, tag: str = "") -> dict:
    """Stop every live PlusVibe sequence for these addresses. Returns counts:
    found (addresses PlusVibe knows), live (sequences that were still sending), stopped, bad (problems: retry next run)."""
    out = {"found": 0, "live": 0, "stopped": 0, "bad": 0}
    for em in emails:
        probe = CO._pv_get("/lead/get", hdr, {"workspace_id": CO.WS, "email": em})
        if probe is None:                                   # refused even after back-off: never read this as "not in PlusVibe"
            out["bad"] += 1
            print(f"   {tag} PlusVibe lookup refused, will retry")
            continue
        if not probe:                                       # not in PlusVibe at all: nothing to stop
            continue
        out["found"] += 1
        rows = [x for x in CO.pv_sequence(em, hdr, camps) if x.get("id") and x.get("status")]
        live = [x for x in rows if x["status"] not in DONE]
        out["live"] += len(live)
        for x in live:
            kind = "subsequence" if x["sub"] else "main"
            if not commit:
                print(f"   {tag} would stop {kind} {x['id']} (was {x['status']})")
                continue
            r = pv_post("/lead/update/status", {"workspace_id": CO.WS, "campaign_id": x["id"], "email": em,
                                                "new_status": "COMPLETED"}, hdr)
            ok = r.status_code < 300 or "already completed" in (r.text or "").lower()
            print(f"   {tag} stop {kind} {x['id']} -> {r.status_code} {'' if ok else _why(r)}")
            out["stopped" if ok else "bad"] += 1
        if commit and live:                                 # read back: nothing may still be sending
            time.sleep(3)
            still = [x for x in CO.pv_sequence(em, hdr, camps) if x.get("status") and x["status"] not in DONE]
            for x in still:
                out["bad"] += 1
                print(f"   {tag} READBACK: {x['id']} is still {x['status']}")
    return out


# ------------------------------------------------------------------ stand-alone catch-up / check
NOTE_RE = re.compile(r"^Call · (\d{4}-\d{2}-\d{2}) \d{2}:\d{2} · (.+)$")


def main() -> int:
    from attio_client import AttioClient, api_key
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="look at call notes from the last N days")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    hdr = pv_header()
    if not hdr:
        sys.exit("PLUS_VIBE_API_KEY is not set")
    c = AttioClient(workers=2); c._key = api_key(os.environ.get("ATTIO_KEY_VAR", "ATTIO_API_KEY_2")); c._local = type(c._local)()
    since = (date.today() - timedelta(days=a.days)).isoformat()

    # Every call note in the workspace (one per call, written by apply_call_verdicts.py). Read ALL notes, not a people filter:
    # filtering on last_outreach_result missed 15 of 26 people on 2026-10-16 (a lead already Lost keeps its old fields).
    calls_by_person, off = {}, 0
    while True:
        r = c.req("GET", "/notes", params={"limit": 50, "offset": off})
        if r.status_code >= 300:
            print(f"Attio notes read failed {r.status_code}"); return 1
        page = r.json().get("data", [])
        for n in page:
            m = NOTE_RE.match(n.get("title") or "")
            if m and n.get("parent_object") == "people":
                calls_by_person.setdefault(n["parent_record_id"], []).append((m.group(1), m.group(2)))
        off += len(page)
        if len(page) < 50:
            break
    print(f"{off} notes read, {len(calls_by_person)} people with call notes; picking those whose newest real call since {since} is Not Interested")

    picked = []
    for rid, calls in sorted(calls_by_person.items()):
        real = sorted(x for x in calls if x[1] not in ("No Conversation", "Voicemail", "No Answer"))
        if not (real and real[-1][1] == "Not Interested" and real[-1][0] >= since):     # the newest real call is the no, and it is recent
            continue
        rp = c.req("GET", f"/objects/people/records/{rid}")
        if rp.status_code >= 300:
            print(f"   {rid}: person lookup failed {rp.status_code}"); continue
        p = rp.json()["data"]
        stage = ((p["values"].get("relationship_stage") or [{}])[0].get("option") or {}).get("title")
        if stage == "CUSTOMER":
            print(f"   {rid}: customer, left alone"); continue
        picked.append((rid, real[-1][0], p))
    print(f"{len(picked)} people with a Not Interested call in the window\n")

    camps = CO.pv_campaigns(hdr)
    tot = {"people": 0, "no_email": 0, "not_in_pv": 0, "already_stopped": 0, "to_stop": 0, "sequences": 0, "stopped": 0, "bad": 0}
    for rid, day, p in picked:
        tot["people"] += 1
        ems = person_emails(p)
        if not ems:
            tot["no_email"] += 1; print(f"   {rid} ({day}): no email on the person"); continue
        res = stop_person(ems, hdr, camps, a.commit, tag=rid)
        if not res["found"] and not res["bad"]:
            tot["not_in_pv"] += 1; print(f"   {rid} ({day}): not in PlusVibe")
        elif not res["live"]:
            tot["already_stopped"] += 1; print(f"   {rid} ({day}): nothing still sending")
        else:
            tot["to_stop"] += 1; print(f"   {rid} ({day}): {res['live']} live sequence(s)")
        tot["sequences"] += res["live"]; tot["stopped"] += res["stopped"]; tot["bad"] += res["bad"]
    print(f"\n{'COMMIT' if a.commit else 'DRY RUN'}: " + ", ".join(f"{k} {v}" for k, v in tot.items()))
    return 1 if tot["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())
