"""Offline checks for lead_bucket.py. Run: python crm/dashboard/test_lead_bucket.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lead_bucket as LB  # noqa: E402

ok = fails = 0


def check(name, cond, got=None):
    global ok, fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  got {got!r}"))
    ok += bool(cond); fails += (not cond)


C = lambda o, at: {"outcome": o, "at": at}
# 17 Oct 2026 production: Hans Oh, call SEND_FOLLOW_UP 13 Oct 16:27, deal moved to Later 14 Oct 14:09 -> Later (was Follow up, in Open)
g = LB.pick([{"stage": "Later", "since": "2026-10-14T14:09:00.123456789Z"}], C("SEND_FOLLOW_UP", "2026-10-13T16:27:40.000000000Z"))
check("a deal moved to Later after the follow-up call is Later", g == "later", g)
g = LB.pick([{"stage": "Multi-touch", "since": "2026-10-14T19:36:00Z"}], C("SEND_FOLLOW_UP", "2026-10-14T15:02:16Z"))
check("a deal moved to Multi-touch after the call is Multi-touch", g == "chasing", g)
g = LB.pick([{"stage": "Later", "since": "2026-10-12T10:00:00Z"}], C("SEND_FOLLOW_UP", "2026-10-15T10:00:00Z"))
check("a follow-up call after the deal went Later outranks it", g == "followup", g)
g = LB.pick([{"stage": "Meeting Booked", "since": "2026-10-12T10:00:00Z"}], C("CALLBACK", "2026-10-15T10:00:00Z"))
check("a newer callback never outranks a booked meeting", g == "meeting", g)
g = LB.pick([], C("CALLBACK", "2026-10-15T10:00:00Z"))
check("no deal: the call decides", g == "callback", g)
g = LB.pick([{"stage": "Lost", "since": "2026-10-10T10:00:00Z"}], C("SEND_FOLLOW_UP", "2026-10-15T10:00:00Z"))
check("a closed deal stays closed", g == "lost", g)
g = LB.pick([{"stage": "Later", "since": "2026-10-12T13:46:00Z"}, {"stage": "Lost", "since": "2026-10-01T20:19:00Z"}], C("SEND_FOLLOW_UP", "2026-10-12T12:33:44Z"))
check("an open deal wins over an old closed one (Timeo Lejeune)", g == "later", g)
g = LB.pick([{"stage": "Profiles Sent", "since": None}])
check("Profiles Sent and Trial are Follow up", g == "followup" and LB.pick([{"stage": "Trial"}]) == "followup", g)
check("Won with the emoji and without", LB.bucket_of("Won 🎉") == "won" and LB.bucket_of("Won") == "won")
check("unknown stage has no bucket", LB.bucket_of("Something") is None and LB.pick([{"stage": "Something"}]) is None)
print(f"{ok} passed, {fails} failed")
sys.exit(1 if fails else 0)
