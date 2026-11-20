"""Which Attio notes count as a touch for Opportunities' Last touch (Ife, 20 Nov 2026: "we need both tools to talk to each other").

Last touch came from deal activity, calls, last_reply_at and last_contacted_at only. A note was never a touch, so a lead worked in Koya HQ
(Emeka's cold-call trial leads: Dennis at B3 Intelligence, Mihaly Babicz at Nexly, notes saved 17-18 Nov) stayed "36 days ago" in the build.
A note counts when a PERSON wrote it about their work on the lead:
  - written by a workspace member in Attio itself, or
  - written by Koya HQ for a person (HQ writes with the API key, so its titles say who it is): the lead window's "Note · <date>",
    a follow-up plan "Follow-up note · <date>", and "Follow-up done".
Not a touch: call notes (the calls already count, with their own time), script notes (stage sync, verdicts, "(automatic)"), and decisions
that are not contact ("Not a real opportunity", "Lost: no response"). The note's created_at is the time, never a date parsed from the title."""
import re

HQ_TITLE = re.compile(r"^(Note|Follow-up note) · \d{4}-\d\d-\d\d|^Follow-up done$")


def is_touch(note: dict) -> bool:
    title = str(note.get("title") or "")
    if title.startswith("Call") or "(automatic)" in title:
        return False
    actor = (note.get("created_by_actor") or {}).get("type")
    return actor == "workspace-member" or bool(HQ_TITLE.match(title))


def newest(notes) -> str | None:
    """the newest created_at (ISO text, as Attio gives it) of the notes that count, or None"""
    ts = [str(n.get("created_at")) for n in (notes or []) if n and n.get("created_at") and is_touch(n)]
    return max(ts, key=lambda s: s[:26]) if ts else None
