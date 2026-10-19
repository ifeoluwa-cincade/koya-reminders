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
print("ok")
