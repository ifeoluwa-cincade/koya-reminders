"""
Runs on a GitHub Actions schedule (see .github/workflows/plusvibe_phone_lookup.yml).
No Mac, no local session needed.

What it does, every run:
  1. Reads every PlusVibe campaign that has leads, pulls REPLIED leads.
  2. Flags a lead "positive" if its current label OR any past label in its
     history is CYCLE_1 / CYCLE_2 / MEETING_BOOKED / INTERESTED — PlusVibe's
     live `label` field alone is unreliable (an AI-tagged INTERESTED lead can
     get manually downgraded later; still wanted on the call list).
  3. Skips anyone whose email is already in the "Positive Reply Call List"
     tab — so re-runs only ever process genuinely new leads.
  4. For each new positive lead: Prospeo first (verified mobile), AI Ark
     second, SalesQL third as an unverified hint only (never written to the
     sheet on its own — SalesQL numbers only matched already-verified ones
     1/8 times in testing 2026-09-09, see feedback_salesql_unverified.md).
  5. Appends one row to "Positive Reply Call List" (phone or not — this is
     the single destination for everything this automation finds; it does
     NOT touch "PlusVibe Positive Replies" or "Call List - All Enriched",
     which stay as the manually-built historical record).
  6. Posts one Discord message per new lead, into the same channel the
     PlusVibe reply-agent already posts to, with what it found.

All secrets come from environment variables (GitHub Actions secrets) — never
hardcoded, never read from a local .env.
"""
from __future__ import annotations

import argparse, json, os, re, sys, time
from urllib.parse import urlparse

import gspread
import requests
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crm", "scripts"))
import phone_pick as PP  # noqa: E402  (shared rule with positive_replies_to_call_queue.py)

SHEET_ID = os.environ.get("CALLLIST_SHEET_ID", "")  # the call-list Google Sheet; set as a repository secret
OUT_TAB = "Positive Reply Call List"
WORKSPACE_ID = "6a6e634971ad0f0e174a5471"
PV_BASE = "https://api.plusvibe.ai/api/v1"
POSITIVE_LABELS = {"CYCLE_1", "CYCLE_2", "MEETING_BOOKED", "INTERESTED"}

ARK_URL = "https://api.ai-ark.com/api/developer-portal/v2/people/mobile-phone-finder"
PROSPEO_URL = "https://api.prospeo.io/enrich-person"
SALESQL_URL = "https://api-public.salesql.com/v1/persons/enrich"

# What was paid for, written BEFORE the sheet row (2026-10-17). The sheet is this job's main memory, but a run that died between
# the lookups and the sheet write paid again next time. The GitHub job keeps this file encrypted between runs (main only).
MEMORY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "caches", "pv_phone_lookup_memory.json")
MEMORY_DAYS = 30


def load_memory() -> dict:
    try:
        mem = json.load(open(MEMORY))
    except Exception:
        return {}
    cutoff = time.time() - MEMORY_DAYS * 86400
    return {k: v for k, v in mem.items() if isinstance(v, dict) and v.get("at", 0) >= cutoff}


def save_memory(mem: dict) -> None:
    os.makedirs(os.path.dirname(MEMORY), exist_ok=True)
    tmp = MEMORY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(mem, f)
    os.replace(tmp, MEMORY)


def env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"Missing required env var {name}")
    return v


def get_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(env("GOOGLE_SERVICE_ACCOUNT_JSON")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def raw_values_update(worksheet, cell_range: str, values: list):
    """Direct Sheets API call, bypassing gspread's Worksheet.update() — that
    path silently converts a Python bool into the literal text "FALSE"/"TRUE"
    instead of preserving it as a real boolean, which breaks checkbox cells."""
    from urllib.parse import quote
    full_range = quote(f"{worksheet.title}!{cell_range}", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{worksheet.spreadsheet_id}/values/{full_range}"
    worksheet.client.session.put(url, params={"valueInputOption": "RAW"}, json={"values": values}).raise_for_status()



def _get(url, **kw):
    """requests.get that retries a dropped connection or a 5xx (a transient network blip used to fail the whole run)."""
    import time
    last = None
    for i in range(4):
        try:
            r = requests.get(url, **kw)
            if r.status_code < 500 and r.status_code != 429:
                return r
            last = r
        except requests.exceptions.RequestException as e:
            last = e
        time.sleep(2 * (i + 1))
    if isinstance(last, Exception):
        raise last
    return last

def pv_headers():
    return {"x-api-key": env("PLUS_VIBE_API_KEY")}


def fetch_new_positive_leads(already_seen: set[str]) -> list[dict]:
    r = _get(f"{PV_BASE}/campaign/list-all", headers=pv_headers(),
                      params={"workspace_id": WORKSPACE_ID}, timeout=30)
    r.raise_for_status()
    camps = r.json()

    found = []
    for c in camps:
        if not c.get("lead_count"):
            continue
        cid, cname = c["id"], c["camp_name"]
        r2 = _get(f"{PV_BASE}/lead/workspace-leads", headers=pv_headers(),
                           params={"workspace_id": WORKSPACE_ID, "campaign_id": cid,
                                   "limit": 1000, "status": "REPLIED"}, timeout=30)
        if r2.status_code != 200:
            print(f"  WARN: {cname} REPLIED fetch failed ({r2.status_code})", file=sys.stderr)
            continue
        for lead in r2.json():
            email = lead.get("email", "")
            if not email or email in already_seen:
                continue
            is_positive = lead.get("label") in POSITIVE_LABELS
            if not is_positive:
                r3 = _get(f"{PV_BASE}/lead/get", headers=pv_headers(),
                                   params={"workspace_id": WORKSPACE_ID, "email": email}, timeout=30)
                time.sleep(0.15)
                if r3.status_code == 200:
                    detail = (r3.json() or [{}])[0]
                    history = (detail.get("lead_data") or {}).get("label_history") or []
                    is_positive = any(h.get("label") in POSITIVE_LABELS for h in history)
            if is_positive:
                lead["_campaign_name"] = cname
                found.append(lead)
        time.sleep(0.2)
    return found


def extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc or url
        return re.sub(r"^www\.", "", netloc.lower().strip())
    except Exception:
        return ""


def prospeo_lookup(linkedin: str = "", full_name: str = "", company_name: str = "",
                    company_website: str = "") -> tuple[str, str]:
    """Returns (phone, resolved_website). Empty phone means no verified mobile."""
    data = {}
    if linkedin:
        data["linkedin_url"] = linkedin
    else:
        if not (full_name and (company_name or company_website)):
            return "", ""
        data["full_name"] = full_name
        if company_name:
            data["company_name"] = company_name
        if company_website:
            data["company_website"] = company_website
    try:
        r = requests.post(PROSPEO_URL, headers={"X-KEY": env("PROSPEO_API_KEY"), "Content-Type": "application/json"},
                           json={"enrich_mobile": True, "data": data}, timeout=30)
        d = r.json()
    except Exception:
        return "", ""
    if d.get("error"):
        return "", ""
    person = d.get("person") or {}
    mobile = person.get("mobile") or {}
    phone = mobile.get("mobile_international", "") if mobile.get("status") == "VERIFIED" else ""
    website = (d.get("company") or {}).get("website", "")
    return phone.replace(" ", "") if phone else "", website


def aiark_lookup(linkedin: str) -> str:
    if not linkedin:
        return ""
    try:
        r = requests.post(ARK_URL, headers={"Content-Type": "application/json", "X-TOKEN": env("AI_ARK_KEY_2")},
                           json={"linkedin": linkedin}, timeout=30)
        body = r.json()
    except Exception:
        return ""
    if body.get("status") != 200:
        return ""
    nested = (body.get("data") or {}).get("data") or []
    return nested[0][0] if nested and nested[0] else ""


def salesql_hint(linkedin: str) -> str:
    """Unverified-only signal — never written to the sheet directly."""
    if not linkedin:
        return ""
    try:
        r = _get(SALESQL_URL, headers={"Authorization": f"Bearer {env('SALES_QL_API_KEY')}"},
                          params={"linkedin_url": linkedin}, timeout=30)
        if r.status_code != 200:
            return ""
        phones = r.json().get("phones") or []
        return phones[0].get("phone", "") if phones else ""
    except Exception:
        return ""


def post_discord(text: str):
    try:
        requests.post(env("DISCORD_WEBHOOK_URL"), json={"content": text}, timeout=15)
    except Exception as e:
        print(f"  WARN: Discord post failed: {e}", file=sys.stderr)


def attio_phone_first(email: str, phone: str, source: str = "") -> str:
    """Koya HQ (19 Oct): the phone goes onto the Attio person the moment it is found, BEFORE the sheet row and the Discord post, so the
    lead window (which re-reads the person every 3 s after a positive reply) shows it no later than Discord. Never fails the run (the
    CRM sync still writes it from the sheet as before).
    29 Oct 2026 (a positive-reply lead; Ife: "the number we got from the lookup should replace what we find ... keep more
    numbers, with the source of each"): this VERIFIED number becomes the primary; every older number stays as an extra number,
    known junk is dropped, and phone_sources records where each number came from (crm/scripts/phone_pick.py).
    Returns why, for the log (no number printed)."""
    key = os.environ.get("ATTIO_API_KEY_2", "")
    if not (key and email and phone):
        return ""
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.attio.com/v2/objects/people/records/query", headers=h, timeout=15,
                          json={"filter": {"email_addresses": email.strip().lower()}, "limit": 2})
        recs = (r.json() or {}).get("data") or [] if r.ok else []
        if len(recs) != 1:
            return ""
        book, changed, why = PP.apply_lookup(recs[0].get("values") or {}, phone, source)
        if not changed:
            return why
        rid = recs[0]["id"]["record_id"]
        # PUT overwrites phone_numbers (a multiselect) in the order given, primary first; PATCH would only prepend
        w = requests.put(f"https://api.attio.com/v2/objects/people/records/{rid}", headers=h, timeout=15,
                         json={"data": {"values": PP.to_values(book)}})
        print(f"  Attio phone written first: {w.status_code} ({why}; {len(book['phones'])} number(s) on the person)")
        return why if w.ok else ""
    except Exception as e:
        print(f"  WARN: Attio phone not written first ({type(e).__name__}); the CRM sync writes it later", file=sys.stderr)
        return ""


REQUIRED_ENV_VARS = [
    "PLUS_VIBE_API_KEY", "PROSPEO_API_KEY", "AI_ARK_KEY_2", "SALES_QL_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_JSON", "DISCORD_WEBHOOK_URL",
    "CALLLIST_SHEET_ID",
]


def main(dry_run: bool = False, limit: int = 5, no_discord: bool = False):
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env var(s), refusing to start: {', '.join(missing)}")

    out_ws = None
    already_seen = set()
    next_row = 2  # first data row, right after the header
    if dry_run:
        print("[DRY RUN] Not touching the sheet or Discord — printing what would happen.")
    else:
        sh = get_sheet()
        out_ws = sh.worksheet(OUT_TAB)
        out_rows = out_ws.get_all_values()
        # Real data rows are the ones with a FirstName (col A) — never trust append_row's
        # own table-detection here: the checkbox-validated Outreached column (J) reads back
        # as "FALSE" for every row in its validated range even when nothing was ever
        # written there, which fooled append_row into inserting at the very bottom of the
        # sheet instead of right after the header (real incident, 2026-09-13).
        data_rows = [r for r in out_rows[1:] if len(r) > 0 and r[0]]
        already_seen = {row[6] for row in data_rows if len(row) > 6 and row[6]}  # Email is col G (index 6)
        next_row = len(data_rows) + 2
    print(f"Already tracked: {len(already_seen)} leads")

    new_leads = fetch_new_positive_leads(already_seen)
    print(f"New positive leads this run: {len(new_leads)}")
    if not new_leads:
        return
    if limit:
        # Process oldest-first so a big backlog drains in order across runs,
        # rather than the same handful of newest leads winning every time.
        new_leads.sort(key=lambda l: l.get("last_sent_at") or l.get("modified_at") or "")
        if len(new_leads) > limit:
            print(f"Capping this run to {limit} of {len(new_leads)} — the rest will be picked up on later runs.")
        new_leads = new_leads[:limit]

    memory = load_memory()
    reused = 0
    for lead in new_leads:
        first_name = lead.get("first_name", "")
        company = lead.get("company_name", "")
        email = lead.get("email", "")
        linkedin = lead.get("linkedin_person_url", "")
        website = lead.get("company_website", "")
        campaign = lead.get("_campaign_name", "")
        replied = (lead.get("last_sent_at") or lead.get("modified_at") or "")[:10]

        phone, source = "", ""
        if dry_run:
            # Prospeo, AI Ark and SalesQL cost credits, and a dry run writes nothing to the sheet (this job's only memory),
            # so every dry run used to pay for the same people again. A dry run now only says who would be looked up.
            print(f"  [DRY RUN] lead -> would look up a phone (Prospeo, then AI Ark{' (has LinkedIn)' if linkedin else ' (no LinkedIn: skipped)'}, then SalesQL)")
            print("  Would post one message to Discord")
            continue
        mkey = (email or linkedin).strip().lower()
        if mkey and mkey in memory:
            m = memory[mkey]                       # paid for by a run that stopped before the sheet row: reuse, never pay twice
            phone, source, hint = m.get("phone", ""), m.get("source", ""), m.get("hint", "")
            website = website or m.get("website", "")
            reused += 1
        else:
            p_phone, p_website = prospeo_lookup(linkedin=linkedin, full_name=first_name,
                                                 company_name=company, company_website=website)
            if p_phone:
                phone, source = p_phone, "Prospeo (verified)"
            if not website and p_website:
                website = p_website

            if not phone:
                a_phone = aiark_lookup(linkedin)
                if a_phone:
                    phone, source = a_phone, "AI Ark"

            hint = "" if phone else salesql_hint(linkedin)
            if mkey:
                memory[mkey] = {"phone": phone, "source": source, "hint": hint, "website": website, "at": time.time()}
                save_memory(memory)                # before the sheet write: a crash from here on costs nothing next run

        why = ""
        if phone and PP.verified_source(source) and not dry_run:
            why = attio_phone_first(email, phone, source)    # Koya HQ shows it before Discord does

        msg = f"**New positive reply** — {first_name} @ {company}\n{campaign}\n{email}"
        if phone:
            msg += f"\n📞 {phone} ({source}) — added to Positive Reply Call List"
            if "older number stays" in why:
                msg += "\nNow the main number in Attio; the older number is kept as an extra."
        elif hint:
            msg += f"\n⚠️ No verified number. SalesQL has an unconfirmed candidate: {hint} — verify before calling."
        else:
            msg += "\n No phone number found (Prospeo, AI Ark, SalesQL all checked)."

        if dry_run:
            print(f"  [DRY RUN] lead -> phone {'found' if phone else 'none'}, hint {'yes' if hint else 'no'}")
            print("  Would post one message to Discord")
        else:
            row_range = f"A{next_row}:I{next_row}"
            out_ws.update([[first_name, company, campaign, replied, phone, source, email, linkedin, website]],
                          row_range, value_input_option="RAW")
            # Outreached (col J) written via a raw API call, not gspread's
            # ws.update(): passing a Python False through ws.update() silently
            # stringifies it to the TEXT "FALSE" instead of a real boolean, which
            # breaks the checkbox — it shows literal text instead of an unchecked
            # box even with BOOLEAN validation applied (real incident, 2026-09-13).
            raw_values_update(out_ws, f"J{next_row}", [[False]])
            # Re-write just LinkedIn/Website with USER_ENTERED so they're actually
            # clickable (RAW never makes a URL clickable, even styled — see the
            # clay-style-sheet-formatting skill). Phone must stay RAW: USER_ENTERED
            # parses a leading "+" as a math sign and strips it from the number.
            out_ws.update([[linkedin]], f"H{next_row}", value_input_option="USER_ENTERED")
            out_ws.update([[website]], f"I{next_row}", value_input_option="USER_ENTERED")
            # Checkbox validation applied to exactly this one row, never a range
            # ahead of real data — Sheets silently writes a literal FALSE into
            # every cell a BOOLEAN validation rule touches, so pre-applying it to
            # hundreds of empty rows is what caused the "ghost rows" bug.
            sh.batch_update({"requests": [{"setDataValidation": {
                "range": {"sheetId": out_ws.id, "startRowIndex": next_row - 1, "endRowIndex": next_row,
                          "startColumnIndex": 9, "endColumnIndex": 10},
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
            }}]})
            next_row += 1
            if no_discord:
                print(f"  lead -> phone {'found' if phone else 'none'} (seeded, no Discord post)")
            else:
                post_discord(msg)
                print(f"  lead -> phone {'found' if phone else 'none'}")
        time.sleep(1)
    if reused:
        print(f"Reused {reused} earlier paid lookups from the lookup memory (no new credits spent on them)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Auto-lookup phones for new PlusVibe positive replies")
    p.add_argument("--dry-run", action="store_true", help="Don't write to the sheet or post to Discord, just print")
    p.add_argument("--limit", type=int, default=5, help="Max leads to process this run (default 5, keeps Discord from flooding)")
    p.add_argument("--no-discord", action="store_true",
                    help="Write to the sheet but skip Discord — for one-time backlog seeding so old replies don't trigger notifications")
    args = p.parse_args()
    main(dry_run=args.dry_run, limit=args.limit, no_discord=args.no_discord)
