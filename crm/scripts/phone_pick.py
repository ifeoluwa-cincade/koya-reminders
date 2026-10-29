"""phone_pick.py: a lead's phone numbers, where each came from, and which one is the main (primary) number.

Why (Ife, 29 Oct 2026, a positive-reply lead): the positive-reply lookup found the verified mobile (Prospeo, matched by their
LinkedIn URL), but the Attio person already had +1 582-553-9942 from the cold-call list. That number was never theirs: the free
website scrape (tools/cloud_phones.py) had read an SVG icon's path data as a US number, on four agencies' sites. The lookup
only wrote "when the person has no phone yet", so the junk number won everywhere and a caller dialled a stranger.

Ife's rule: "the number we got from the lookup should replace what we find [as the main number] ... keep more numbers, say
this number came from Prospeo, this one from the website, the source of each number, so we don't limit ourselves to one."

Where it lives (both on the Attio person, so every job and Koya HQ read the same thing):
  * phone_numbers  (Attio's own multi-value phone attribute): every number, the primary FIRST (written with PUT, which
                   overwrites the list in the order given; PATCH would only prepend);
  * phone_sources  (a text attribute holding JSON, the "book"):
        {"v": 1, "primary": "+16135550123",
         "phones": [{"n": "+16135550123", "src": "prospeo", "at": "2026-10-29T12:24:08Z", "by": "phone lookup"},
                    {"n": "+15551234567", "src": "website", "at": "...", "url": "https://acme.com/contact"}]}
    src is one of SOURCES. A number on the person with no entry in the book is described from who wrote it in Attio
    (a workspace member -> manual, an installed app -> integration, an API token -> import).

Rules:
  * the lookup's verified number becomes the primary ONCE (when the book does not have it yet); every older number stays as an
    extra number, a typed one and the lead's own signature number included. After that, people choose (Koya HQ "make primary").
  * known junk (JUNK: numbers proved not to be anyone's, e.g. SVG path data) is never kept, wherever it came from.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

HUMAN = "workspace-member"
VERIFIED_SOURCES = ("prospeo (verified)", "ai ark")
SOURCES = ("prospeo", "aiark", "website", "signature", "call", "manual", "integration", "import", "salesql", "contactout", "other")
# Proved not to be anyone's number (29 Oct 2026): SVG path data read as numbers by the old website scrape, and one number the
# old scrape cut short (the site publishes one more digit).
JUNK = {"+15825539942", "+12284085608", "+24742475", "+498927788519"}


def digits(p) -> str:
    return re.sub(r"\D", "", str(p or ""))


def e164(p) -> str:
    d = re.sub(r"[^\d+]", "", str(p or ""))
    if not d:
        return ""
    return d if d.startswith("+") else "+" + d.lstrip("0")


def same(a, b) -> bool:
    """Same number, whatever the formatting ('+1613-555-0123' == '+16135550123')."""
    da, db = digits(a), digits(b)
    return bool(da) and da == db


def is_junk(n) -> bool:
    return any(same(n, j) for j in JUNK)


def verified_source(source) -> bool:
    s = str(source or "").strip().lower()
    return any(s.startswith(v) for v in VERIFIED_SOURCES)


def src_code(source) -> str:
    """The lookup sheet's Source text ('Prospeo (verified)', 'AI Ark', ...) as a book source code."""
    s = str(source or "").strip().lower()
    return "prospeo" if s.startswith("prospeo") else "aiark" if s.startswith("ai ark") else "salesql" if s.startswith("salesql") else "other"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def person_phone(values: dict):
    """(number, who wrote it) for the number Koya HQ shows: the first phone_numbers value of an Attio person's values."""
    v = (values or {}).get("phone_numbers") or []
    if not v:
        return "", ""
    x = v[0]
    return (x.get("original_phone_number") or x.get("phone_number") or ""), ((x.get("created_by_actor") or {}).get("type") or "")


def _actor_src(t) -> str:
    return "manual" if t == HUMAN else "integration" if t == "app" else "import"


def read_book(values: dict) -> dict:
    """The person's numbers as a book: phone_sources JSON merged with phone_numbers (Attio's list is the truth for WHICH numbers
    exist; the JSON says where each came from). Primary = the book's primary if still on the person, else phone_numbers[0]."""
    values = values or {}
    raw = ""
    ps = values.get("phone_sources") or []
    if ps:
        raw = ps[0].get("value") if isinstance(ps[0], dict) else ps[0]
    try:
        saved = json.loads(raw) if raw else {}
    except Exception:
        saved = {}
    saved_by = {digits(x.get("n")): x for x in (saved.get("phones") or []) if isinstance(x, dict) and x.get("n")}
    phones = []
    for x in values.get("phone_numbers") or []:
        n = e164(x.get("original_phone_number") or x.get("phone_number") or "")
        if not n or any(same(n, p["n"]) for p in phones):
            continue
        rec = dict(saved_by.get(digits(n)) or {})
        if not rec:
            rec = {"src": _actor_src((x.get("created_by_actor") or {}).get("type")), "at": str(x.get("active_from") or "")[:19] + "Z"}
        rec["n"] = n
        phones.append(rec)
    prim = saved.get("primary")
    primary = next((p["n"] for p in phones if same(p["n"], prim)), phones[0]["n"] if phones else "")
    return {"v": 1, "primary": primary, "phones": phones}


def has(book, n) -> bool:
    return any(same(p["n"], n) for p in book["phones"])


def add(book, n, src, at=None, by="", url="", primary=False) -> bool:
    """Add a number (junk is refused). primary=True makes it the main number. Returns True when the book changed."""
    n = e164(n)
    if not n or is_junk(n):
        return False
    changed = False
    if not has(book, n):
        rec = {"n": n, "src": src if src in SOURCES else "other", "at": at or now_iso()}
        if by:
            rec["by"] = by
        if url:
            rec["url"] = url
        book["phones"].append(rec); changed = True
    if (primary or not book["primary"]) and not same(book["primary"], n):
        book["primary"] = n; changed = True
    return changed


def drop_junk(book) -> list:
    """Remove known junk numbers; returns the ones removed."""
    gone = [p["n"] for p in book["phones"] if is_junk(p["n"])]
    if gone:
        book["phones"] = [p for p in book["phones"] if not is_junk(p["n"])]
        if not book["phones"]:
            book["primary"] = ""
        elif not has(book, book["primary"]) or is_junk(book["primary"]):
            book["primary"] = book["phones"][0]["n"]
    return gone


def ordered(book) -> list:
    """Every number, the primary first."""
    return sorted((p["n"] for p in book["phones"]), key=lambda n: 0 if same(n, book["primary"]) else 1)


def to_values(book) -> dict:
    """Attio values for a PUT on the person: phone_numbers (primary first, overwriting) + phone_sources (the JSON book)."""
    phones = sorted(book["phones"], key=lambda p: 0 if same(p["n"], book["primary"]) else 1)
    doc = {"v": 1, "primary": book["primary"], "phones": phones}
    return {"phone_numbers": [{"original_phone_number": n} for n in ordered(book)],
            "phone_sources": json.dumps(doc, separators=(",", ":"))}


def apply_lookup(values: dict, verified: str, source: str, at=None, by="phone lookup"):
    """The positive-reply lookup's rule. -> (book, changed, why). The verified number becomes primary the FIRST time it is seen
    (not yet in the book); every older number stays as an extra number; known junk is dropped."""
    book = read_book(values)
    gone = drop_junk(book)
    v = e164(verified)
    if not v or is_junk(v):
        return book, bool(gone), ("junk removed" if gone else "no verified lookup number")
    if has(book, v):
        return book, bool(gone), ("junk removed" if gone else "already on the person (people choose the primary after that)")
    was = book["primary"]
    add(book, v, src_code(source), at=at, by=by, primary=True)
    return book, True, ("verified lookup number is now primary; the older number stays as an extra" if was
                        else "verified lookup number, the person had none")
