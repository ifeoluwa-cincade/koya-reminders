"""Offline checks for reply_scope.py and the fast path's choice of reply row (no network). Run: python crm/scripts/test_reply_scope.py"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reply_scope as RS  # noqa: E402

bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    bad += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


AI, US = "updated by AI reply detection", "updated by User API"
# scope: the two 26 Oct cases replied in TAM re-outreach
check("TAM original copy in scope", RS.in_scope("Q4 TAM Re-outreach (Original Copy)"), True)
check("TAM cycle 1 in scope", RS.in_scope("Q4 TAM Re-outreach | Cycle 1"), True)
check("AI agency in scope", RS.in_scope("Q4 AI Agency Owners (Rejoice) | Cycle 2"), True)
check("nurture in scope", RS.in_scope("Q4 Nurture"), True)
check("marketing out of scope", RS.in_scope("Marketing Agencies | Cycle 1"), False)
check("freight out of scope", RS.in_scope("Freight and Logistics | Free Build"), False)

ramsey = {"campaign_name": "Q4 TAM Re-outreach (Original Copy)", "status": "REPLIED", "lead_data": {"label": "LATER", "label_history": [
    {"ts": "2026-10-26T17:50:46.305Z", "action": AI, "label": "INTERESTED"}, {"ts": "2026-10-26T17:51:26.387Z", "action": US, "label": "LATER"}]}}
rachael = {"campaign_name": "Q4 TAM Re-outreach (Original Copy)", "status": "REPLIED", "lead_data": {"label": "CYCLE_1", "label_history": [
    {"ts": "2026-10-26T19:01:03.322Z", "action": AI, "label": "INTERESTED"}, {"ts": "2026-10-26T19:02:01.508Z", "action": US, "label": "CYCLE_1"}]}}
check("final label: ours beats the AI's", RS.final_label(ramsey)[:2], ("LATER", "us"))
check("final label: cycle 1", RS.final_label(rachael)[0], "CYCLE_1")
check("final label: AI only", RS.final_label({"lead_data": {"label_history": [{"ts": "1", "action": AI, "label": "interested"}]}})[:2], ("INTERESTED", "ai"))
check("final label: no history", RS.final_label({"label": "LATER"})[:2], ("LATER", ""))
check("reply time = first history entry", RS.first_reply_at(rachael), "2026-10-26T19:01:03.322Z")

check("stage Interested", RS.stage_for_label("INTERESTED"), "Interested")
check("stage Later", RS.stage_for_label("LATER"), "Later")
check("stage cycle 1", RS.stage_for_label("CYCLE_1"), "Interested")
check("stage meeting", RS.stage_for_label("MEETING_BOOKED"), "Meeting Booked")
check("no stage for OOO", RS.stage_for_label("OUT_OF_OFFICE"), None)
check("hard no", RS.is_hard_no("NOT_INTERESTED") and RS.is_hard_no("wrong person"), True)
check("later is not a hard no", RS.is_hard_no("LATER"), False)
now = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
check("Later follow-up 30 days out at 08:00 UTC", RS.follow_up_at("Later", now), "2026-11-25T08:00:00Z")
check("no follow-up for Interested", RS.follow_up_at("Interested", now), None)

# phones from the signature
sig = "Yes, I would be interested , but not at this time<br><br>With service in mind,<br>Ramsey<br>Phone:  (248) 275-1222<br>Email: rs@example.com"
check("US number in a signature", RS.phones_in(RS.own_words(sig), "US"), ["+12482751222"])
check("UK number with +44", RS.phones_in("call me on +44 20 7946 0958 tomorrow", "US"), ["+442079460958"])
check("UK number without + read in GB", RS.phones_in("mobile 07911 123456", "GB"), ["+447911123456"])
check("no invalid numbers", RS.phones_in("order 12345, call 555-0000, year 2026", "US"), [])
quoted = "Sounds good<br><div class=\"gmail_quote\">On Mon, Oct 26 Chris wrote: call +1 (248) 275-1222</div>"
check("the quoted thread is cut off", RS.phones_in(RS.own_words(quoted), "US"), [])
check("plain-text quote cut", RS.phones_in(RS.own_words("thanks\nOn Mon, 26 Oct 2026 at 10:00, X <x@y.com> wrote:\n> 248 275 1222"), "US"), [])

# the fast path's row choice (positive_replies_to_deals.pick_row)
import positive_replies_to_deals as PRD  # noqa: E402
check("pick: Ramsey -> LATER", (PRD.pick_row([ramsey]) or [None, None])[1], "LATER")
check("pick: Rachael -> CYCLE_1", (PRD.pick_row([rachael]) or [None, None])[1], "CYCLE_1")
no = {"campaign_name": "Q4 AI Agency Owners (Rejoice)", "status": "REPLIED", "lead_data": {"label": "NOT_INTERESTED"}}
check("pick: a hard no anywhere in scope -> no deal", PRD.pick_row([ramsey, no]), None)
mkt = dict(ramsey, campaign_name="Marketing Agencies | Cycle 1")
check("pick: out-of-scope campaign -> no deal", PRD.pick_row([mkt]), None)
ooo = {"campaign_name": "Q4 TAM Re-outreach", "status": "REPLIED", "lead_data": {"label": "OUT_OF_OFFICE"}}
check("pick: out of office -> no deal", PRD.pick_row([ooo]), None)
notrep = dict(ramsey, status="ACTIVE")
check("pick: not replied -> no deal", PRD.pick_row([notrep]), None)


# fast path waits for our correction when the label is still the AI's guess
class R:
    def __init__(self, j): self.status_code, self._j = 200, j
    def json(self): return self._j


calls = {"n": 0, "slept": 0}
ai_only = {"campaign_name": "Q4 TAM Re-outreach (Original Copy)", "status": "REPLIED", "lead_data": {"label": "INTERESTED", "label_history": [
    {"ts": "2026-10-26T19:01:03.322Z", "action": AI, "label": "INTERESTED"}]}}


def fake_get(path, params, H, tries=6):
    calls["n"] += 1
    return R([ai_only] if calls["n"] == 1 else [rachael])


class StopHere(Exception):
    pass


class FakeAttio:
    def __init__(self, *a, **k): raise StopHere()


PRD.pv_get, PRD.AttioClient, PRD.env = fake_get, FakeAttio, (lambda: {"PLUS_VIBE_API_KEY": "x"})
try:
    PRD.fast("rachael@example.com", False, settle=100, sleep=lambda s: calls.__setitem__("slept", s),
             now=lambda: datetime(2026, 10, 26, 19, 1, 23, tzinfo=timezone.utc))
except StopHere:
    pass
check("fast path waited for the correction (~80 s)", 79 <= calls["slept"] <= 81, True)
check("fast path read PlusVibe again after waiting", calls["n"], 2)

print("FAILED" if bad else "all passed")
sys.exit(1 if bad else 0)
