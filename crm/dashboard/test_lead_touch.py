"""Offline checks for lead_touch.py. Run: python crm/dashboard/test_lead_touch.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lead_touch as LT  # noqa: E402

ok = fails = 0


def check(name, cond, got=None):
    global ok, fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  got {got!r}"))
    ok += bool(cond); fails += (not cond)


API = {"type": "api-token"}; MEM = {"type": "workspace-member"}
N = lambda title, at, actor=API: {"title": title, "created_at": at, "created_by_actor": actor}
# 18 Nov 2026 production: Dennis at B3 Intelligence, Emeka's HQ note "Sent Linkedin message 1"
check("an HQ lead-window note counts", LT.is_touch(N("Note · 2026-11-18 21:39", "2026-11-18T20:39:26.092000000Z")))
check("an HQ follow-up plan note counts", LT.is_touch(N("Follow-up note · 2026-11-20 10:00", "x")))
check("Follow-up done counts", LT.is_touch(N("Follow-up done", "x")))
check("a note written in Attio by a person counts", LT.is_touch(N("Spoke on WhatsApp", "x", MEM)))
check("a call note does not (calls count on their own)", not LT.is_touch(N("Call · 2026-10-14 16:16 · Emeka", "x")))
check("a call note by a person does not either", not LT.is_touch(N("Call history — 3 calls", "x", MEM)))
check("a script note does not", not LT.is_touch(N("Stage moved to Interested", "x")))
check("an automatic note does not", not LT.is_touch(N("Deal follows label (automatic)", "x", MEM)))
check("Not a real opportunity does not", not LT.is_touch(N("Not a real opportunity", "x")))
check("Lost: no response does not", not LT.is_touch(N("Lost: no response", "x")))
g = LT.newest([N("Note · 2026-10-12 14:46", "2026-10-12T13:46:01.835000000Z"), N("Call · 2026-11-19 10:00", "2026-11-19T09:00:00.000000000Z"),
               N("Note · 2026-11-18 21:19", "2026-11-18T20:19:58.977000000Z")])
check("newest: the latest note that counts, never a call note", g == "2026-11-18T20:19:58.977000000Z", g)
check("newest: nothing that counts -> None", LT.newest([N("Call · 2026-11-19", "2026-11-19T09:00:00Z")]) is None)
check("newest: empty -> None", LT.newest([]) is None and LT.newest(None) is None)

print(f"\n{ok} passed, {fails} failed")
sys.exit(1 if fails else 0)
