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
  * phone_sources  (a text attribute holding JSON, the "book"). Version 2 (30 Oct 2026) lists EVERY source that found a number:
        {"v": 2, "primary": "+16135550123", "chosen": {"by": "<HQ user>", "at": "..."},      # chosen only when a person picked it
         "phones": [{"n": "+16135550123", "src": "prospeo", "at": "...",                   # src/at = the best source (v1 readers)
                     "sources": [{"src": "prospeo", "at": "2026-10-30T15:52:00Z", "by": "phone lookup"},
                                 {"src": "aiark", "at": "2026-10-27T09:00:00Z", "by": "call list"},
                                 {"src": "integration", "at": "2026-10-27T09:11:39Z"}]}]}
    A version 1 entry {"n", "src", "at", "by", "url"} reads as a number with that one source. A number on the person with no
    entry in the book is described from who wrote it in Attio (a workspace member -> manual, an installed app such as
    CloudTalk's Attio integration -> integration, shown "Unknown"; an API token -> import, shown "Imported list").

Ranking (Ife, 30 Oct 2026; the SAME list is in koya-app lib/phones.js RANK, change both together). The most reliable source
of a number is its rank; the best-ranked number is the primary, UNLESS a person chose the primary in Koya HQ ("Make primary"
writes "chosen"), which is never overridden automatically. When two sources give different numbers both are kept; the
lower one is an extra number with its own sources shown.
    manual / call      typed or chosen by a person
    prospeo            Prospeo (verified)     ("verified": false on a source drops it to prospeo_unverified, below SalesQL)
    signature          the lead's own email signature
    contactout, aiark, salesql, prospeo_unverified, website
    import             an imported list (an API-token write with no known origin)
    integration        unknown: an installed app (CloudTalk's Attio integration) copied it and we have no record of who found it.
                       CloudTalk is NOT a provider and is never a label; this placeholder is dropped as soon as a real source is added.
Labels (Koya HQ, Ife 30 Oct): the provider name only, no extra words: "Prospeo", "AI Ark", "ContactOut", "SalesQL",
"Website", "Signature", "Imported list", "Typed by <name>", and "Unknown" for integration. The verified flag is used for ranking only.
    other
Ties keep the current primary (nothing moves for no reason).

Rules:
  * every writer ADDS its source to the number (a number already there gets one more source), then the primary is
    recomputed by the ranking unless a person chose it.
  * known junk (JUNK: numbers proved not to be anyone's, e.g. SVG path data) is never kept, wherever it came from.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

HUMAN = "workspace-member"
VERIFIED_SOURCES = ("prospeo (verified)", "ai ark")
SOURCES = ("prospeo", "aiark", "website", "signature", "call", "manual", "integration", "import", "salesql", "contactout", "other")
RANK = ("manual", "call", "prospeo", "signature", "contactout", "aiark", "salesql", "prospeo_unverified", "website",
        "import", "integration", "other")
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
    return ("prospeo" if s.startswith("prospeo") else "aiark" if s.startswith("ai ark") else "salesql" if s.startswith("salesql")
            else "contactout" if s.startswith("contactout") else "other")


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


def _clean_src(src) -> str:
    return src if src in SOURCES else "other"


def rank_key(source: dict) -> str:
    """A source's place in RANK: prospeo is verified unless the source says "verified": false."""
    s = _clean_src((source or {}).get("src"))
    return "prospeo_unverified" if s == "prospeo" and (source or {}).get("verified") is False else s


def rank_of(source: dict) -> int:
    k = rank_key(source)
    return RANK.index(k) if k in RANK else len(RANK)


def best(phone: dict) -> dict:
    """The phone's most reliable source (newest first on a tie)."""
    return sorted_sources(phone)[0] if phone.get("sources") else {}


def sorted_sources(phone: dict) -> list:
    """Most reliable first, then newest."""
    srcs = list(phone.get("sources") or [])
    srcs.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
    return sorted(srcs, key=rank_of)


def _sources_of(rec: dict) -> list:
    """A saved book entry's sources: v2 has a list, a v1 entry {n, src, at, by, url} is one source."""
    out = []
    raw = rec.get("sources") if isinstance(rec.get("sources"), list) else [rec]
    for x in raw:
        if not isinstance(x, dict) or not x.get("src"):
            continue
        s = {"src": _clean_src(x.get("src")), "at": str(x.get("at") or "")}
        for k in ("by", "url"):
            if x.get(k):
                s[k] = x[k]
        if x.get("verified") is False:
            s["verified"] = False
        if not any(rank_key(o) == rank_key(s) for o in out):
            out.append(s)
    return out


def _phone_rank(p) -> int:
    return min((rank_of(s) for s in p.get("sources") or []), default=len(RANK))


def read_book(values: dict) -> dict:
    """The person's numbers as a book: phone_sources JSON (v1 or v2) merged with phone_numbers (Attio's list is the truth for
    WHICH numbers exist; the JSON says where each came from). Primary = the book's primary if still on the person, else
    phone_numbers[0]. Reading never re-ranks; writers call rerank (add / apply_lookup do)."""
    values = values or {}
    raw = ""
    ps = [x for x in (values.get("phone_sources") or []) if not (isinstance(x, dict) and x.get("active_until"))]
    if ps:
        raw = ps[0].get("value") if isinstance(ps[0], dict) else ps[0]
    try:
        saved = json.loads(raw) if raw else {}
    except Exception:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    saved_by = {}
    for x in saved.get("phones") or []:
        if isinstance(x, dict) and x.get("n") and digits(x.get("n")) not in saved_by:
            saved_by[digits(x.get("n"))] = x
    phones = []
    for x in values.get("phone_numbers") or []:
        if x.get("active_until"):
            continue
        n = e164(x.get("original_phone_number") or x.get("phone_number") or "")
        if not n or any(same(n, p["n"]) for p in phones):
            continue
        srcs = _sources_of(saved_by.get(digits(n)) or {})
        if not srcs:
            srcs = [{"src": _actor_src((x.get("created_by_actor") or {}).get("type")), "at": str(x.get("active_from") or "")[:19] + "Z"}]
        phones.append({"n": n, "sources": srcs})
    prim = saved.get("primary")
    primary = next((p["n"] for p in phones if same(p["n"], prim)), phones[0]["n"] if phones else "")
    book = {"v": 2, "primary": primary, "phones": phones}
    ch = saved.get("chosen")
    if isinstance(ch, dict) and same(ch.get("n") or prim, primary) and primary:
        book["chosen"] = {"by": ch.get("by") or "", "at": ch.get("at") or ""}
    return book


def has(book, n) -> bool:
    return any(same(p["n"], n) for p in book["phones"])


def rerank(book) -> bool:
    """Primary = the best-ranked number, unless a person chose it (book["chosen"]). Ties keep the current primary.
    Returns True when the primary changed."""
    if not book["phones"]:
        changed = bool(book["primary"])
        book["primary"] = ""
        book.pop("chosen", None)
        return changed
    if book.get("chosen") and has(book, book["primary"]):
        return False
    book.pop("chosen", None)
    top = min(_phone_rank(p) for p in book["phones"])
    cur = next((p for p in book["phones"] if same(p["n"], book["primary"])), None)
    if cur is not None and _phone_rank(cur) == top:
        return False
    new = next(p["n"] for p in book["phones"] if _phone_rank(p) == top)
    changed = not same(new, book["primary"])
    book["primary"] = new
    return changed


def add(book, n, src, at=None, by="", url="", verified=None) -> bool:
    """Add a number, or one more source for a number already there (junk is refused), then re-rank the primary (a person's
    choice is kept). verified=False marks an unverified Prospeo hit. Returns True when the book changed."""
    n = e164(n)
    if not n or is_junk(n):
        return False
    s = {"src": _clean_src(src), "at": at or now_iso()}
    if by:
        s["by"] = by
    if url:
        s["url"] = url
    if verified is False:
        s["verified"] = False
    changed = False
    p = next((p for p in book["phones"] if same(p["n"], n)), None)
    if p is None:
        book["phones"].append({"n": n, "sources": [s]}); changed = True
    elif not any(rank_key(o) == rank_key(s) for o in p["sources"]):
        p["sources"].append(s); changed = True
    if s["src"] != "integration":   # CloudTalk is not a provider: "unknown" goes once a real source is known (Ife 30 Oct)
        p = next(p for p in book["phones"] if same(p["n"], n))
        if any(x["src"] == "integration" for x in p["sources"]):
            p["sources"] = [x for x in p["sources"] if x["src"] != "integration"]; changed = True
    if not book["primary"]:
        book["primary"] = n; changed = True
    if changed:                    # nothing new: nothing moves (a v1 book's primary may have been picked by a person in HQ)
        rerank(book)
    return changed


def choose(book, n, by="", at=None) -> bool:
    """A person picked the primary (Koya HQ "Make primary"): it stays primary until a person changes it."""
    p = next((p for p in book["phones"] if same(p["n"], n)), None)
    if p is None:
        return False
    book["primary"] = p["n"]
    book["chosen"] = {"by": by, "at": at or now_iso()}
    return True


def drop_junk(book) -> list:
    """Remove known junk numbers; returns the ones removed."""
    gone = [p["n"] for p in book["phones"] if is_junk(p["n"])]
    if gone:
        book["phones"] = [p for p in book["phones"] if not is_junk(p["n"])]
        if not has(book, book["primary"]) or is_junk(book["primary"]):
            book["primary"] = ""
            book.pop("chosen", None)
        rerank(book)
    return gone


def ordered(book) -> list:
    """Every number, the primary first."""
    return sorted((p["n"] for p in book["phones"]), key=lambda n: 0 if same(n, book["primary"]) else 1)


def to_doc(book) -> dict:
    """The v2 JSON book: primary first; each number carries src/at of its best source (what a v1 reader shows)."""
    out = []
    for p in sorted(book["phones"], key=lambda p: 0 if same(p["n"], book["primary"]) else 1):
        srcs = sorted_sources(p)
        top = srcs[0] if srcs else {"src": "other", "at": ""}
        e = {"n": p["n"], "src": top["src"], "at": top.get("at") or ""}
        if top.get("by"):
            e["by"] = top["by"]
        if top.get("url"):
            e["url"] = top["url"]
        e["sources"] = srcs
        out.append(e)
    doc = {"v": 2, "primary": book["primary"], "phones": out}
    if book.get("chosen"):
        doc["chosen"] = dict(book["chosen"], n=book["primary"])
    return doc


def to_values(book) -> dict:
    """Attio values for a PUT on the person: phone_numbers (primary first, overwriting) + phone_sources (the JSON book)."""
    return {"phone_numbers": [{"original_phone_number": n} for n in ordered(book)],
            "phone_sources": json.dumps(to_doc(book), separators=(",", ":"))}


def apply_lookup(values: dict, verified: str, source: str, at=None, by="phone lookup"):
    """The positive-reply lookup's rule. -> (book, changed, why). The lookup's number is added, or its source is added to the
    same number already on the person (30 Oct 2026: it used to record nothing, so a number the lookup verified kept showing
    only "CloudTalk"). Then the primary follows the ranking, unless a person chose it. Known junk is dropped."""
    book = read_book(values)
    gone = drop_junk(book)
    v = e164(verified)
    if not v or is_junk(v):
        return book, bool(gone), ("junk removed" if gone else "no verified lookup number")
    was, existed = book["primary"], has(book, v)
    s = str(source or "").strip().lower()
    changed = add(book, v, src_code(source), at=at, by=by, verified=(False if s.startswith("prospeo") and "verified" not in s else None))
    if not changed:
        return book, bool(gone), ("junk removed" if gone else "already on the person with this source")
    now_primary = same(book["primary"], v)
    if existed:
        why = "lookup source added to the number already on the person" + ("; it is now primary" if now_primary and not same(was, v) else "")
    elif not was:
        why = "verified lookup number, the person had none"
    elif now_primary:
        why = "verified lookup number is now primary; the older number stays as an extra"
    elif book.get("chosen"):
        why = "lookup number kept as an extra; a person chose the primary"
    else:
        why = "lookup number kept as an extra; a more reliable number stays primary"
    return book, True, why
