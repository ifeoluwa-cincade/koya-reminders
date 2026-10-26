"""
reply_scope.py — the shared rules for "a reply becomes a deal" (Ife, 26 Oct 2026). Pure functions plus one PlusVibe read helper.

Why it exists: Ramsey Sweis ("interested, but not at this time") and Rachael Quisel ("Sure, send more info") replied in
"Q4 TAM Re-outreach (Original Copy)". The deal jobs only looked at campaigns whose name contains "ai agency", so TAM re-outreach
(AI agencies only, project_tam_reoutreach) and its Cycle 1 / Cycle 2 subsequences never made a deal, whatever the label said.

    in_scope(campaign name)       AI-agency outreach: "AI Agency ..." campaigns, TAM re-outreach (and its cycles), the nurture campaign
    final_label(lead row)         the LAST label_history entry (ours, "updated by User API", beats the AI's first guess), else `label`
    stage_for_label(label)        the deal stage a NEW deal starts at, or None (no deal)
    follow_up_at(stage, now)      Later -> about 30 days out (08:00 UTC, like the hand-made Ramsey deal); otherwise None
    phones_in(text, region)       valid E.164 numbers in a reply's own words (signature included, the quoted thread left out)

Public repo: nothing here prints names, emails or numbers.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone

# a campaign belongs to the AI-agency funnel when its name contains one of these (lower case)
SCOPE_MARKERS = ("ai agency", "tam re-outreach", "q4 nurture")
# labels that make a deal, and the stage a NEW deal starts at.
# CYCLE_1 / CYCLE_2: our reply check moved them to the "send info" subsequence; the first touch beyond that email (a call, a LinkedIn
# message) is what makes Multi-touch (26 Oct stage decision), so the deal starts at Interested.
STAGE_OF = {"INTERESTED": "Interested", "CYCLE_1": "Interested", "CYCLE_2": "Interested",
            "LATER": "Later", "MEETING_BOOKED": "Meeting Booked"}
DEAL_LABELS = set(STAGE_OF)
# a hard no: never a deal
HARD_NO = {"NOT_INTERESTED", "WRONG_PERSON", "UNSUBSCRIBED", "UNSUBSCRIBE", "DO_NOT_CONTACT", "DNC", "BOUNCED"}
FOLLOW_UP_DAYS = 30
AI_ACTION = "updated by AI reply detection"


def in_scope(campaign_name: str) -> bool:
    n = (campaign_name or "").lower()
    return any(m in n for m in SCOPE_MARKERS)


def norm_label(x) -> str:
    return re.sub(r"[\s-]+", "_", str(x or "").strip().upper())


def history(row: dict) -> list:
    ld = (row or {}).get("lead_data") or {}
    h = ld.get("label_history") or (row or {}).get("label_history") or []
    return [x for x in h if isinstance(x, dict) and x.get("label")]


def final_label(row: dict) -> tuple[str, str, str]:
    """(label, set_by, at). set_by: "us" (User API), "ai" (PlusVibe's reply detection) or "" (no history: the plain label)."""
    h = sorted(history(row), key=lambda x: str(x.get("ts") or ""))
    if h:
        last = h[-1]
        by = "ai" if (last.get("action") or "") == AI_ACTION else "us"
        return norm_label(last.get("label")), by, str(last.get("ts") or "")
    ld = (row or {}).get("lead_data") or {}
    return norm_label(ld.get("label") or (row or {}).get("label")), "", str(ld.get("modified_at") or (row or {}).get("modified_at") or "")


def first_reply_at(row: dict) -> str:
    """When the reply came in: the first label_history entry (PlusVibe labels a reply the moment it lands), else modified_at."""
    h = sorted(history(row), key=lambda x: str(x.get("ts") or ""))
    if h:
        return str(h[0].get("ts") or "")
    ld = (row or {}).get("lead_data") or {}
    return str(ld.get("modified_at") or (row or {}).get("modified_at") or "")


def stage_for_label(label: str):
    return STAGE_OF.get(norm_label(label))


def is_hard_no(label: str) -> bool:
    return norm_label(label) in HARD_NO


def follow_up_at(stage: str, now: datetime | None = None):
    """ISO timestamp for the deal's next_action_at, or None."""
    if stage != "Later":
        return None
    now = now or datetime.now(timezone.utc)
    d = (now + timedelta(days=FOLLOW_UP_DAYS)).astimezone(timezone.utc)
    return d.replace(hour=8, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(x):
    s = str(x or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    if "." in s:
        head, rest = s.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        s = head + "." + digits[:6].ljust(6, "0") + rest[len(digits):]
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ phones from the reply's signature --
_QUOTE = [re.compile(p, re.I | re.S) for p in (
    r"(^|\n|\s)(On|Le|Am|El|Il|Op)\s[^\n]{4,240}?(wrote|a écrit|schrieb|escribió|ha scritto|schreef)\s*:",
    r"(^|\n)\s*-{2,}\s*(Original|Forwarded) Message",
    r"(^|\n)\s*From:\s[^\n]{1,200}\n\s*(Sent|Date):",
    r"(^|\n)\s*_{8,}",
    r"(^|\n)\s*>")]


def own_words(raw: str) -> str:
    """The lead's own message as plain text, with the quoted earlier thread cut off (our emails carry no number, but a forwarded
    thread could carry someone else's)."""
    s = str(raw or "")
    if re.search(r"<[a-z][\s\S]*>", s, re.I):
        s = re.sub(r"<(blockquote|div[^>]*class=\"?[^\">]*gmail_quote)[\s\S]*$", "", s, flags=re.I)
        s = re.sub(r"<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&#43;", "+").replace("&#x2B;", "+").replace("\r\n", "\n").replace("\r", "\n"))
    cut = len(s)
    for rx in _QUOTE:
        m = rx.search(s)
        if m and m.start() < cut:
            cut = m.start()
    return s[:cut]


def phones_in(text: str, region: str = "") -> list[str]:
    """Every valid number (phonenumbers.is_valid_number) in the text, as E.164, in order of appearance, no repeats.
    A number written without + is read in the lead's country (region, ISO alpha-2), then as a US number."""
    import phonenumbers
    out = []
    regions = [r for r in ((region or "").upper(), "US") if r]
    for reg in regions or ["US"]:
        try:
            for m in phonenumbers.PhoneNumberMatcher(text or "", reg, leniency=phonenumbers.Leniency.VALID):
                n = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
                if n not in out:
                    out.append(n)
        except Exception:
            continue
        if out:
            break
    return out


def signature_phone(pv_get, H, ws: str, email: str, region: str = "") -> str:
    """The first valid number in the lead's own newest replies (newest first), or "". pv_get(path, params, H) -> response."""
    try:
        r = pv_get("/unibox/emails", {"workspace_id": ws, "lead": email, "email_type": "received", "preview_only": "false"}, H)
        items = (r.json().get("data") or []) if r.status_code == 200 else []
    except Exception:
        return ""
    if not region:                                   # the lead's country, for numbers written without +
        try:
            r2 = pv_get("/lead/get", {"workspace_id": ws, "email": email}, H)
            rows = r2.json() if r2.status_code == 200 else []
            region = next((((x.get("lead_data") or {}).get("country_code") or x.get("country_code") or "") for x in rows
                           if ((x.get("lead_data") or {}).get("country_code") or x.get("country_code"))), "") if isinstance(rows, list) else ""
        except Exception:
            region = ""
    mine = [x for x in items if (x.get("from_address_email") or "").lower() == email.lower()]
    mine.sort(key=lambda x: str(x.get("timestamp_created") or ""), reverse=True)
    for x in mine[:5]:
        body = x.get("body")
        if isinstance(body, dict):
            body = body.get("html") or body.get("text") or ""
        got = phones_in(own_words(body or x.get("content_preview") or ""), region)
        if got:
            return got[0]
    return ""
