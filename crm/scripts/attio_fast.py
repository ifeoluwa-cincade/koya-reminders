"""attio_fast.py — find the few people a job needs, without reading all 16,000.

The call jobs used to load every person in Attio on every run (4 to 8 minutes) just to match a handful of new calls to their people.
These helpers look the people up one by one, in parallel, by email or by phone, which takes seconds. Used when a run knows which calls are new
(run_cids.json); every script keeps its old full read as the fallback (set FULL_INDEX=1 to force it, e.g. to compare the two).
"""
from __future__ import annotations
import re
from concurrent.futures import ThreadPoolExecutor


def dig10(x) -> str:
    d = re.sub(r"\D", "", str(x or ""))
    return d[-10:] if len(d) >= 10 else ""


def _first(c, flt, want=None, limit: int = 5):
    r = c.req("POST", "/objects/people/records/query", json={"filter": flt, "limit": limit})
    if r.status_code >= 300:
        return None
    rows = r.json().get("data", [])
    if want:
        rows = [p for p in rows if want(p)]
    # 2026-10-13: the CloudTalk link makes an EMPTY second person (no stage, company or email) for a number whose Attio person has no
    # email. When two people share a number the real one must win, so a call verdict never lands on the empty shell.
    def is_shell(p):
        v = p["values"]
        return not ((v.get("relationship_stage") or [{}])[0].get("option")) and not v.get("company") and not v.get("email_addresses")
    rows = sorted(rows, key=is_shell)
    return rows[0] if rows else None


def by_email(c, email: str):
    return _first(c, {"email_addresses": email.strip().lower()})


def by_phone10(c, d10: str):
    """2026-10-17: Attio's $contains on the last TEN digits misses most stored numbers (tested on people we created from calls:
    2 of 3 not found), so every targeted run printed '0 calls matched BY PHONE'. Ask with the exact NA E.164 number first, then
    $contains on the last SEVEN digits (which Attio does match), keeping only people whose number really ends in the 10 digits."""
    if not d10:
        return None
    def ok(p):
        return any(dig10(ph.get("original_phone_number") or ph.get("phone_number")) == d10 for ph in (p["values"].get("phone_numbers") or []))
    return _first(c, {"phone_numbers": "+1" + d10}, ok) or _first(c, {"phone_numbers": {"$contains": d10[-7:]}}, ok, limit=50)


def by_fullname(c, name: str):
    """The ONE person whose full name equals this name (case and spacing ignored), or None when there is none or more than one
    real candidate. Used only for an outbound call whose CloudTalk contact has no email and whose number is on no Attio person:
    the lead was dialled from a number found outside Attio (found 17 Oct 2026)."""
    nm = " ".join(str(name or "").split())
    if not nm or nm.lower().startswith("lead from") or len(nm.split()) < 2:
        return None
    r = c.req("POST", "/objects/people/records/query", json={"filter": {"name": {"full_name": {"$eq": nm}}}, "limit": 10})
    if r.status_code >= 300:
        return None
    rows = [p for p in r.json().get("data", [])
            if " ".join(((p["values"].get("name") or [{}])[0].get("full_name") or "").split()).lower() == nm.lower()]
    real = [p for p in rows if not is_shell(p)]
    if len(real) == 1:
        return real[0]
    return rows[0] if len(rows) == 1 else None


def is_shell(p) -> bool:
    """An EMPTY person the CloudTalk link made: no stage, company or email."""
    v = p["values"]
    return not ((v.get("relationship_stage") or [{}])[0].get("option")) and not v.get("company") and not v.get("email_addresses")


_COMPANY = {}


def company_name(c, cid: str) -> str:
    if cid not in _COMPANY:
        r = c.req("GET", f"/objects/companies/records/{cid}")
        _COMPANY[cid] = ((r.json()["data"]["values"].get("name") or [{}])[0].get("value") or "") if r.status_code < 300 else ""
    return _COMPANY[cid]


def by_namecomp(c, first: str, company: str, norm):
    """The person whose FIRST NAME and COMPANY match (the old code's second try when the email is not on file)."""
    if not first or not company:
        return None
    r = c.req("POST", "/objects/people/records/query", json={"filter": {"name": {"first_name": {"$contains": first}}}, "limit": 50})
    if r.status_code >= 300:
        return None
    for p in r.json().get("data", []):
        nm = ((p["values"].get("name") or [{}])[0].get("first_name") or "")
        cr = p["values"].get("company") or []
        if norm(nm) == norm(first) and cr and norm(company_name(c, cr[0]["target_record_id"])) == norm(company):
            return p
    return None


def index_people(c, emails=(), phones=(), workers: int = 6):
    """Returns (byem, byph): email -> person record (every email the person has), last-10-digits -> person record."""
    emails = sorted({e.strip().lower() for e in emails if e and "@" in e})
    phones = sorted({p for p in phones if p})
    byem, byph = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for e, p in zip(emails, ex.map(lambda e: by_email(c, e), emails)):
            if p:
                for x in p["values"].get("email_addresses", []):
                    byem[(x.get("email_address") or "").lower()] = p
                byem.setdefault(e, p)
        for d, p in zip(phones, ex.map(lambda d: by_phone10(c, d), phones)):
            if p:
                byph.setdefault(d, p)
    return byem, byph


# ---- which calls still need this script? Each call in live_state.json carries two flags set False when the call is (re)worked and True once the writer finished it.
import json, os, tempfile
from pathlib import Path


def pending_cids(work: Path, flag: str):
    """Set of call ids this writer still has to apply, or None to read everyone the old way (no state file, or FULL_INDEX=1).
    Calls recorded before the flags existed count as done."""
    if os.environ.get("FULL_INDEX"):
        return None
    f = Path(work) / "live_state.json"
    if not f.exists():
        return None
    try:
        st = json.loads(f.read_text())
    except Exception:
        return None
    return {cid for cid, r in st.items() if isinstance(r, dict) and r.get(flag) is False}


def mark_done(work: Path, flag: str, cids) -> int:
    f = Path(work) / "live_state.json"
    if not cids or not f.exists():
        return 0
    st = json.loads(f.read_text()); n = 0
    for cid in cids:
        if cid in st and isinstance(st[cid], dict) and st[cid].get(flag) is False:
            st[cid][flag] = True; n += 1
    tmp = tempfile.NamedTemporaryFile("w", delete=False, dir=str(f.parent)); tmp.write(json.dumps(st, indent=1)); tmp.close()
    os.replace(tmp.name, f)
    return n
