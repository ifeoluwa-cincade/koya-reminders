"""Where a lead came from: the channel of the FIRST touch that started the relationship (the build's half of ONE rule; the page's half
is koya-app public/look/leadkey.js sourceOf, and both sides run lead_source_fixtures.json).

Ife, 18 Oct 2026: having called someone does not make them a cold-call lead. If our first outreach was a cold email the lead is
Cold email, even when we called later; if the first touch was a cold call it is Cold call; anything else (a referral, inbound, an
imported deal, nothing known) is Other.

Touches, from real timestamps:
  email  the first email a PlusVibe sequence sent the lead
  call   the first outgoing CloudTalk call to the lead
  deal   a deal whose channel is not cold email or cold call (Referral, Inbound, ...): the relationship started there, at its creation
The earliest one wins. A deal whose channel is Cold email but with no PlusVibe send we can find (emails sent before PlusVibe, from
the 1 Oct import) names the first touch itself; a Cold call deal with no call found counts as a call at its creation. Nothing: Other.
"also": the lead was ALSO touched by the other cold channel later ("call" for a Cold email lead we called, "email" for a Cold call lead
we emailed). Values: source "Email" | "Call" | "Other" (the page's own words), also "call" | "email" | None."""
from datetime import datetime, timezone

CHANNEL = {"cold email": "Email", "email": "Email", "cold call": "Call", "call": "Call"}
LABEL = {"Email": "Cold email", "Call": "Cold call", "Other": "Other"}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _t(x):
    if not x:
        return None
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    s = str(x).strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if "." in s:                                   # Attio gives nanoseconds; fromisoformat wants at most 6 digits
        head, _, rest = s.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        s = head + "." + digits[:6] + rest[len(digits):]
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d):
    return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if d else None      # as the page's toISOString()


def pick(email_at=None, call_at=None, deals=None) -> dict:
    """email_at / call_at: the first sequence email / first outgoing call (any time format), deals: [{"channel", "created"}].
    -> {"source": "Email" | "Call" | "Other", "also": "call" | "email" | None, "first": "email" | "call" | "deal" | None, "at": iso | None}"""
    e, c = _t(email_at), _t(call_at)
    # (rank, time, kind): rank 1 = a timed touch. rank 0 = a deal NAMES Cold email but no PlusVibe send was found (emails sent before
    # PlusVibe, deals from the 1 Oct import): that email counts as the first touch. A Cold call deal with no call found: its creation
    # stands in for the call (CloudTalk's history is complete, so the number simply did not match).
    cands = [(1, e, "email")] if e else []
    if c:
        cands.append((1, c, "call"))
    for dl in deals or []:
        ch = str((dl or {}).get("channel") or "").strip().lower()
        if not ch:
            continue
        kind = {"Email": "email", "Call": "call"}.get(CHANNEL.get(ch))
        if kind == "call" and not c and _t(dl.get("created")):
            cands.append((1, _t(dl.get("created")), "call"))      # CloudTalk's history is complete: the deal was made at the call
        elif kind and not (e if kind == "email" else c):
            cands.append((0, _t(dl.get("created")) or EPOCH, kind))
        elif not kind and _t(dl.get("created")):
            cands.append((1, _t(dl.get("created")), "deal"))
    if cands:
        rank, at, first = min(cands, key=lambda x: (x[0], x[1], ("email", "call", "deal").index(x[2])))      # a tie goes to email, then call
        source = {"email": "Email", "call": "Call", "deal": "Other"}[first]
        at = at if rank else None
    else:
        at, first, source = None, None, "Other"
    kinds = {x[2] for x in cands}
    also = "call" if source == "Email" and "call" in kinds else "email" if source == "Call" and "email" in kinds else None
    return {"source": source, "also": also, "first": first, "at": _iso(at)}
