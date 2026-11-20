"""Run: python crm/scripts/test_stage_to_plusvibe.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage_to_plusvibe as S

class R:
    def __init__(s, code, text=""): s.status_code, s.text = code, text

NF = R(400, '{"message":"Lead not found"}')
assert S.not_in_seq(NF, True)
assert not S.not_in_seq(NF, False)                       # main campaign: still a problem
assert not S.not_in_seq(R(400, "bad request"), True)     # other 400 still fails
assert not S.not_in_seq(R(500, "Lead not found"), True)

rows = [{"id": "m", "sub": False, "status": "ACTIVE", "label": "CYCLE_1"}, {"id": "s", "sub": True, "status": "ACTIVE", "label": "CYCLE_1"}]
state = {"after": False}
def seqs(*a, **k):
    if state["after"]:
        return [{"id": "m", "sub": False, "status": "COMPLETED", "label": "LATER"}, {"id": "s", "sub": True, "status": "ACTIVE", "label": "CYCLE_1"}]
    state["after"] = True; return rows
def post(path, body, hdr):
    return NF if body["campaign_id"] == "s" else R(200)
S.CO.pv_sequence = seqs; S.pv_post = post; S.time.sleep = lambda *_: None
t, b = S.apply("Later", "", "x@y.z", {}, {}, True)
assert b == 0, b

# ---- the hourly sweep and label precedence (20 Nov 2026) ----
class PV:
    """A fake PlusVibe: rows per campaign; posts change them, so the readback sees the writes."""
    def __init__(s, rows): s.rows = {r["id"]: dict(r) for r in rows}; s.posts = []
    def seq(s, *a, **k): return [dict(r) for r in s.rows.values()]
    def post(s, path, body, hdr):
        s.posts.append((path, body["campaign_id"], body.get("new_status") or body["variables"]["label"]))
        r = s.rows[body["campaign_id"]]
        if path == "/lead/update/status": r["status"] = body["new_status"]
        else: r["label"] = body["variables"]["label"]
        return R(200)
def run(rows, stage="Later", sweep=True, chosen="", camps=None, commit=True):
    pv = PV(rows); S.CO.pv_sequence = pv.seq; S.pv_post = pv.post
    t, b = S.apply(stage, "", "x@y.z", {}, camps or {}, commit, chosen, False, sweep=sweep)
    return pv, t, b
CAMPS = {"tam": {"name": "Q4 TAM Re-outreach"}, "main": {"name": "AI Agency Owners"}, "nur": {"name": "Q4 Nurture"}, "nsub": {"name": "Cycle 1", "parent": "nur"}}

# mismatch found -> fixed: a Later lead still CONTACTED in a second campaign with no label (the 20 Nov case)
pv, t, b = run([{"id": "tam", "sub": False, "status": "REPLIED", "label": "LATER"}, {"id": "main", "sub": False, "status": "CONTACTED", "label": ""}], camps=CAMPS)
assert b == 0 and t == 2, (t, b)
assert pv.rows["main"]["status"] == "COMPLETED" and pv.rows["main"]["label"] == "LATER", pv.rows
assert all(c == "main" for _, c, _ in pv.posts), pv.posts

# already matching -> no writes at all
pv, t, b = run([{"id": "tam", "sub": False, "status": "REPLIED", "label": "LATER"}, {"id": "main", "sub": False, "status": "COMPLETED", "label": "LATER"}], camps=CAMPS)
assert (t, b) == (0, 0) and pv.posts == [], pv.posts

# dry run plans but writes nothing
pv, t, b = run([{"id": "main", "sub": False, "status": "CONTACTED", "label": ""}], commit=False, camps=CAMPS)
assert t == 2 and pv.posts == []

# precedence: the sweep never replaces a label (INTERESTED stays; a lead warming up again is the label sync's call), only stops
pv, t, b = run([{"id": "main", "sub": False, "status": "CONTACTED", "label": "INTERESTED"}], camps=CAMPS)
assert b == 0 and pv.rows["main"] == {"id": "main", "sub": False, "status": "COMPLETED", "label": "INTERESTED"}, pv.rows

# precedence: LATER never replaces MEETING_BOOKED / CLOSED, in the sweep AND the webhook job (the readback does not call it a problem)
for sw in (True, False):
    for lab in ("MEETING_BOOKED", "CLOSED"):
        pv, t, b = run([{"id": "tam", "sub": False, "status": "REPLIED", "label": lab}], sweep=sw, camps=CAMPS)
        assert (t, b) == (0, 0) and pv.posts == [], (sw, lab, pv.posts)
# ...unless the tag was picked on the dashboard
pv, t, b = run([{"id": "tam", "sub": False, "status": "REPLIED", "label": "MEETING_BOOKED"}], sweep=False, chosen="LATER", camps=CAMPS)
assert b == 0 and pv.rows["tam"]["label"] == "LATER"
# KEEP on the dashboard: no tag even on an empty label
pv, t, b = run([{"id": "main", "sub": False, "status": "CONTACTED", "label": ""}], sweep=False, chosen="KEEP", camps=CAMPS)
assert pv.rows["main"]["label"] == "" and pv.rows["main"]["status"] == "COMPLETED"
# the webhook job still replaces an ordinary label (CYCLE_1 -> LATER), as before
pv, t, b = run([{"id": "main", "sub": False, "status": "CONTACTED", "label": "CYCLE_1"}], sweep=False, camps=CAMPS)
assert b == 0 and pv.rows["main"]["label"] == "LATER"
# Lost still sets NOT_INTERESTED over an empty label in the sweep, and Meeting Booked fills MEETING_BOOKED
pv, t, b = run([{"id": "main", "sub": False, "status": "CONTACTED", "label": ""}], stage="Lost", camps=CAMPS)
assert pv.rows["main"]["label"] == "NOT_INTERESTED" and pv.rows["main"]["status"] == "COMPLETED"

# the sweep leaves a Later lead's nurture campaign (and its subsequence) running; other stages stop it
pv, t, b = run([{"id": "nur", "sub": False, "status": "CONTACTED", "label": ""}, {"id": "nsub", "sub": True, "status": "ACTIVE", "label": ""}], camps=CAMPS)
assert (t, b) == (0, 0) and pv.posts == [], pv.posts           # not stopped, not retagged
pv, t, b = run([{"id": "nur", "sub": False, "status": "CONTACTED", "label": ""}], stage="Lost", camps=CAMPS)
assert pv.rows["nur"]["status"] == "COMPLETED"

assert S.row_tag("LATER", "", sweep=True) == "LATER" and S.row_tag("LATER", "LATER") is None and S.row_tag(None, "") is None
assert "Later" in S.SWEEP_STAGES and set(S.CLOSED_STAGES) < set(S.SWEEP_STAGES)

# a script that creates a deal at Later runs the stage job (run_for_deal) and never raises
import os
os.environ["PLUS_VIBE_API_KEY"] = "k"
class C:
    def req(s, m, path, **k):
        if "/people/" in path:
            return type("X", (), {"status_code": 200, "json": lambda self: {"data": {"values": {"email_addresses": [{"email_address": "x@y.z"}]}}}})()
        raise RuntimeError("no")
S._CAMPS.update(CAMPS)
deal = {"id": {"record_id": "d1"}, "values": {"stage": [{"status": {"title": "Later"}}], "associated_people": [{"target_record_id": "p1"}]}}
pv = PV([{"id": "main", "sub": False, "status": "CONTACTED", "label": ""}]); S.CO.pv_sequence = pv.seq; S.pv_post = pv.post
t, b = S.run_for_deal(C(), deal)
assert b == 0 and pv.rows["main"] == {"id": "main", "sub": False, "status": "COMPLETED", "label": "LATER"}, pv.rows
assert S.run_for_deal(C(), "d-unknown") == (0, 1)                                    # lookup error: printed, not raised
deal["values"]["stage"] = [{"status": {"title": "Interested"}}]
assert S.run_for_deal(C(), deal) == (0, 0)                                           # not a stopping stage: nothing
print("ok")
