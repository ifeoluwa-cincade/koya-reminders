"""Which Opportunities tab a lead is in (the build's half of ONE rule; the page's half is koya-app public/look/leadkey.js bucketOf /
closedBy, and koya-app test/leadkey.test.js checks that STAGE_BUCKET below matches it).

Ife, 17 Oct 2026: the stage tabs changed three times after load, and some leads sat in Open although their deal was at Later. The
newest decision wins: a deal's stage decides, and a call outcome only outranks it when the call came AFTER the stage was set (a deal
moved to Later or Multi-touch after a "send follow-up" call is Later / Multi-touch, not Follow up).

Ife, 29 Oct 2026: Profiles Sent and Trial are their own bucket, "aftercall" (the Opportunities stage "After the call", after Meeting
booked), not Interested: Greyson Jones (Krastor) showed as Interested after his profiles were sent. A follow-up call after the stage was
set never pulls such a lead back to Interested (ORDER: only a newer booked meeting outranks it)."""
from datetime import datetime, timezone

STAGE_BUCKET = {"Meeting Booked": "meeting", "Interested": "followup", "Multi-touch": "chasing", "Later": "later",
                "Profiles Sent": "aftercall", "Trial": "aftercall", "Lost": "lost", "Won 🎉": "won"}
# A call is an opportunity only when something was agreed or asked for ("Not now" is not one, Ife 2026-10-12)
# the word a lead in the aftercall bucket shows: its real stage (Ife 29 Oct 2026), never the bucket's name
STAGE_LABEL = {"Profiles Sent": "Profiles sent", "Trial": "Trial"}
REAL_CALL = {"MEETING_BOOKED": "meeting", "SEND_FOLLOW_UP": "followup", "CALLBACK": "callback"}
ORDER = {"meeting": 0, "aftercall": 0.5, "followup": 1, "chasing": 1.5, "callback": 2, "later": 3}


def bucket_of(stage):
    s = str(stage or "")
    if s.startswith("Won"):
        return "won"
    if s.startswith("Lost"):
        return "lost"
    return STAGE_BUCKET.get(s)


def _t(x):
    if not x:
        return None
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    s = str(x).strip().replace("Z", "+00:00")
    if "." in s:                                   # Attio gives nanoseconds; fromisoformat wants at most 6 digits
        head, _, rest = s.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        s = head + "." + digits[:6] + rest[len(digits):]
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pick(deals, last_call=None):
    """deals: [{"stage", "since"}] with open deals first; last_call: {"outcome", "at"} or None. -> bucket or None."""
    bucket, since = None, None
    for dl in deals:
        b = bucket_of(dl.get("stage"))
        if b and (bucket is None or bucket in ("lost", "won")):
            bucket, since = b, dl.get("since")
    if last_call and bucket not in ("lost", "won") and last_call.get("outcome") in REAL_CALL:
        cb = REAL_CALL[last_call["outcome"]]
        ca, sa = _t(last_call.get("at")), _t(since)
        newer = bucket is None or not sa or not ca or ca > sa      # unknown times keep the old behaviour (the call may outrank)
        if newer and (bucket is None or ORDER[cb] < ORDER.get(bucket, 9)):
            bucket = cb
    return bucket
