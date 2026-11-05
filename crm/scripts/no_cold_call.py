"""The one "never cold-call this person" rule. Every job that puts people in, or keeps them in, a cold-call campaign imports it
(calllist_to_cloudtalk.py, verify_cloudtalk_tags.py, call_recall.py, and sales-nav-pipeline seed_call_lists.py), so the jobs
cannot drift apart again (30-31 Oct: the daily checker's own copy lacked two outcomes and kept re-adding finished people).

Not for the Positive Replies list: people with an open deal belong there.

Never cold-call someone who (Ife 5 Nov 2026, after Temi asked about email "not interested" and Lost people):
  * had a finished call result on the entry (booked, not interested, do not call, wrong person, follow-up, not now),
  * is LOST, a CUSTOMER, or already in a conversation with us (INTERESTED, ENGAGED, MEETING),
  * is do-not-contact (dnc_status or outreach_status),
  * has any deal, open or lost: the deal owns the conversation.
On 5 Nov this kept 54 people out: 17 who said no by email, 4 whose call result never reached their entry, 1 marked lost by hand,
33 with a deal.
"""

FINISHED = {"MEETING_BOOKED", "NOT_INTERESTED", "DO_NOT_CALL", "WRONG_PERSON", "SEND_FOLLOW_UP", "NOT_NOW"}
BLOCKED_STAGES = {"LOST", "CUSTOMER", "INTERESTED", "ENGAGED", "MEETING"}


def _title(values: dict, attr: str):
    v = (values.get(attr) or [{}])[0]
    return ((v.get("status") or v.get("option") or {}).get("title")) or None


def deal_people(c) -> set:
    """Record ids of every person linked to any deal (open, won or lost)."""
    out, off = set(), 0
    while True:
        rows = c.req("POST", "/objects/deals/records/query", json={"limit": 500, "offset": off}).json()["data"]
        for d in rows:
            out |= {q["target_record_id"] for q in (d["values"].get("associated_people") or [])}
        if len(rows) < 500:
            return out
        off += 500


def reason(entry_values: dict | None, person: dict | None, with_deal: set) -> str | None:
    """Why this person must not be cold-called, or None. person is an Attio person record (or None if unknown)."""
    outcome = _title(entry_values or {}, "call_outcome")
    if outcome in FINISHED:
        return f"finished: {outcome}"
    if not person:
        return None
    pv = person.get("values") or {}
    stage = _title(pv, "relationship_stage")
    if stage in BLOCKED_STAGES:
        return f"person {stage}"
    if "DO_NOT_CONTACT" in (_title(pv, "dnc_status"), _title(pv, "outreach_status")):
        return "do not contact"
    if person["id"]["record_id"] in with_deal:
        return "has a deal"
    return None
