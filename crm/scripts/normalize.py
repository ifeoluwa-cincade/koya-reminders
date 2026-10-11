"""
normalize.py — turn a messy legacy row into canonical values, and decide identity.

Pure functions, no network, so the rules can be tested in milliseconds. Every rule here
exists because a real record in our data broke something. Attio cleans nothing, so if it
is not done here it is not done at all.
"""
from __future__ import annotations
import re, unicodedata
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------- names --
CREDENTIALS = r"(?:PhD|Ph\.D|MBA|MD|MSc|MS|MA|BA|BSc|CFA|CPA|PMP|CSM|MSPH|MSHRM|Ed\.?D|DDS|JD|RN|LLM|SA|SD|SSM|MPH|MFA|DVM|PE)"
HONORIFICS = r"(?:Dr|Mr|Mrs|Ms|Miss|Prof|Sir|Rev|Fr|Hon)"
EMOJI = re.compile(
    "[" "\U0001F000-\U0001FAFF" "←-⇿" "⌀-⏿" "①-⓿"
    "■-➿" "⬀-⯿" "️" "‍" "]+")

def strip_emoji(s: str) -> str:
    return EMOJI.sub("", s or "").strip()

def clean_name(raw: str) -> str:
    """'Jesika Keener, Ed.D.' -> 'Jesika Keener'.  'David GUT 🚀' -> 'David GUT'."""
    s = unicodedata.normalize("NFC", raw or "")
    s = strip_emoji(s)
    s = re.sub(rf"^\s*{HONORIFICS}\.?\s+", "", s, flags=re.I)
    # credentials after a comma, possibly several
    s = re.sub(rf",\s*{CREDENTIALS}\b\.?", "", s, flags=re.I)
    s = re.sub(rf",\s*(?:{CREDENTIALS}\b\.?[,\s]*)+$", "", s, flags=re.I)
    s = re.sub(r"\s*[~|–—-]\s*(?:Founder|CEO|Helping|I help|We help).*$", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.-|")
    return s

def first_name(raw: str) -> str:
    """Return the given name WITHOUT splitting multi-word given names wrongly.
    'Baha Eddine' stays whole only if the source treated it as the first name."""
    n = clean_name(raw)
    return n.split(" ")[0] if n else ""

# -------------------------------------------------------------------- emails --
JUNK = {"no email found", "email 1", "n/a", "na", "none", "null", "-", "unknown"}
ROLE = re.compile(r"^(info|sales|hello|contact|admin|team|support|office|mail|enquiries|hi)@", re.I)
# Free/personal mailbox providers. Matched on the SECOND-LEVEL LABEL so every country variant is
# caught: hotmail.co.uk, yahoo.co.uk, googlemail.com, hotmail.fr all resolve to a known provider.
# Found the hard way — a .com-only list treated 21 people at hotmail.co.uk as one company.
PERSONAL_LABELS = {
    "gmail","googlemail","outlook","hotmail","live","msn","yahoo","ymail","rocketmail",
    "aol","icloud","me","mac","gmx","web","freenet","t-online","proton","protonmail","pm",
    "yandex","mail","inbox","list","zoho","fastmail","hushmail","tutanota","tuta",
    "btinternet","virginmedia","sky","talktalk","orange","wanadoo","free","sfr","laposte",
    "libero","alice","tiscali","bluewin","comcast","verizon","att","sbcglobal","bellsouth",
    "cox","charter","optonline","earthlink","juno","rediffmail","sina","qq","163","126",
    "naver","daum","seznam","onet","wp","interia","abv","mynet",
    # US ISP mailboxes — old but still in use, and never a company's own domain
    "sbcglobal","bellsouth","earthlink","worldnet","ameritech","pacbell","swbell","prodigy",
    "netzero","gmx","mailfence","gmail","hushmail","lycos","excite","netscape","compuserve",
    "blueyonder","ntlworld","tiscali","bigpond","xtra","shaw","telus","sympatico","rogers",
    "videotron","bt","aliceadsl","laposte","voila","neuf","cegetel","club-internet",
}

def is_personal_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip(".")
    if not d or "." not in d:
        return False
    parts = d.split(".")
    # second-level label, allowing for co.uk / com.au style suffixes
    label = parts[-3] if len(parts) >= 3 and parts[-2] in {"co","com","net","org","ac","gov"} else parts[-2]
    return label in PERSONAL_LABELS

PERSONAL = {"gmail.com","outlook.com","hotmail.com","yahoo.com","icloud.com","me.com",
            "gmx.de","gmx.net","live.com","proton.me","protonmail.com","aol.com","yandex.ru"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)

def clean_email(raw: str) -> tuple[str, str]:
    """Return (email_or_empty, status). Junk strings are REJECTED, never stored."""
    e = (raw or "").strip().lower()
    if not e or e in JUNK:
        return "", "NOT_FOUND"
    if re.match(r"^\d{4}$", e):          # a year stored in the email column
        return "", "INVALID"
    if not EMAIL_RE.match(e):
        return "", "INVALID"
    if ROLE.match(e):
        return e, "ROLE_BASED"
    return e, "VALID"

def email_domain_match(email: str, company_domain: str) -> str:
    if not email or "@" not in email:
        return "UNKNOWN"
    dom = email.split("@")[-1]
    if is_personal_domain(dom):
        return "PERSONAL_PROVIDER"
    if not company_domain:
        return "UNKNOWN"
    cd = re.sub(r"^www\.", "", company_domain.lower())
    return "MATCH" if dom == cd else "MISMATCH"

# ------------------------------------------------------------------ linkedin --
def linkedin_slug(url: str) -> tuple[str, str]:
    """Return (slug, status). A /sales/lead/ id is NOT a slug and must never be a key."""
    u = (url or "").strip()
    if not u:
        return "", "MISSING"
    low = u.lower()
    if "/sales/lead/" in low or "/sales/people/" in low:
        return "", "SALES_NAV"
    m = re.search(r"linkedin\.com/in/([^/?#]+)", low)
    if not m:
        return "", "INVALID"
    from urllib.parse import unquote
    slug = unquote(m.group(1)).rstrip("/").strip()
    slug = unicodedata.normalize("NFC", slug)
    return slug, "VALID_PROFILE"

# --------------------------------------------------------------------- dates --
SHEETS_EPOCH = date(1899, 12, 30)   # Google Sheets / Excel serial origin

def parse_legacy_date(raw: str):
    """Handles ISO date, ISO datetime, and raw spreadsheet serial numbers."""
    s = (raw or "").strip()
    if not s:
        return None
    if re.match(r"^\d{5}(\.\d+)?$", s):
        return SHEETS_EPOCH + timedelta(days=float(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    return None

# ------------------------------------------------------------------ identity --
def identity_keys(email: str, slug: str) -> list[str]:
    """Ordered identity keys. first_name+company is DELIBERATELY ABSENT — it would
    false-merge 340 key groups in our data (e.g. two different Thomases at CodeHive)."""
    keys = []
    if email:
        keys.append("e:" + email)
    if slug:
        keys.append("s:" + slug)
    return keys

def domain_from_url(url: str) -> str:
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0].strip()
