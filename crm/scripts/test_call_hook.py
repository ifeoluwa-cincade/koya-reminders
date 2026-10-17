"""Offline checks for the CloudTalk-webhook fast path (no network, made-up ids, no lead data).
Run: python crm/scripts/test_call_hook.py"""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = tempfile.mkdtemp()
os.environ["CALL_DATA_DIR"] = TMP
import ct_hook_calls as HK          # noqa: E402
import live_call_dispositions as L  # noqa: E402
import attio_fast as AF             # noqa: E402

ok = fails = 0


def check(name, cond):
    global ok, fails
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond); fails += (not cond)


now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
iso = lambda m: (now - timedelta(minutes=m)).isoformat()

# 1. call_ids: digits only, order kept, no repeats
check("call_ids parses a comma list", L.call_ids("11, 22,11 x 33") == ["11", "22", "33"])
check("call_ids of nothing is empty", L.call_ids(None) == [] and L.call_ids("") == [])

# 2. pick: which reported calls still need work
ev = [
    {"kind": "call_ended", "call_id": "100", "talk": "0", "recorded": "false", "received_at": iso(1)},        # new, not in memory
    {"kind": "call_ended", "call_id": "101", "talk": "120", "recorded": "true", "received_at": iso(3)},      # done, real, no review yet
    {"kind": "recording_ready", "call_id": "102", "received_at": iso(4)},                                     # waiting for the recording (retry)
    {"kind": "call_ended", "call_id": "103", "talk": "5", "recorded": "true", "received_at": iso(5)},        # done, voicemail: nothing to do
    {"kind": "call_ended", "call_id": "104", "talk": "200", "recorded": "true", "received_at": iso(6)},      # done and reviewed
    {"kind": "tags_modified", "call_id": "105", "received_at": iso(7)},                                       # tags changed after it was done
    {"kind": "call_ended", "call_id": "106", "talk": "90", "recorded": "true", "received_at": iso(8)},       # real, but on the review skip list
]
state = {"101": {"verdict": "NOT_NOW", "talk": 120}, "102": {"verdict": "UNKNOWN", "retry": True, "talk": 60},
         "103": {"verdict": "VOICEMAIL", "talk": 5}, "104": {"verdict": "CALLBACK", "talk": 200}, "105": {"verdict": "NO_ANSWER", "talk": 0},
         "106": {"verdict": "SEND_FOLLOW_UP", "talk": 90}}
ids, recent = HK.pick(ev, state, {"104"}, {"106": "voicemail"}, now)
check("pick: new, review-missing, retry and re-tagged calls, newest first", ids == ["100", "101", "102", "105"])
check("pick: the webhook counts as working", recent is True)
ids2, recent2 = HK.pick([{"kind": "call_ended", "call_id": "100", "received_at": iso(120)}], {"100": {"verdict": "NO_ANSWER"}}, set(), {}, now)
check("pick: an old, finished call -> nothing to do and not recent", ids2 == [] and recent2 is False)
check("pick: a junk call id is ignored", HK.pick([{"kind": "call_ended", "call_id": "x;1", "received_at": iso(1)}], {}, set(), {}, now)[0] == [])

check("pick: CloudTalk AI results are not work", HK.pick([{"kind": "summary_ready", "call_id": "107", "received_at": iso(1)}], {}, set(), {}, now)[0] == [])

# 3. fetch_call_rows: one read per call with call_id, a call CloudTalk does not list yet is left out
asked = []


class R:
    status_code = 200

    def __init__(self, rows): self.rows = rows

    def json(self): return {"responseData": {"data": self.rows}}


def fake_get(path, H, **kw):
    cid = kw["params"]["call_id"]; asked.append((path, cid))
    return R([] if cid == "999" else [{"Cdr": {"id": int(cid), "talking_time": 0}}])


L.ct_get = fake_get
rows = L.fetch_call_rows({}, ["100", "999"])
check("fetch_call_rows reads each call once by call_id", asked == [("/calls/index.json", "100"), ("/calls/index.json", "999")])
check("fetch_call_rows keeps only the calls CloudTalk returned", [r["Cdr"]["id"] for r in rows] == [100])

# 4. pending_cids: a short run never marks calls done that are not in its input files
st = {"1": {"applied_a": False}, "2": {"applied_a": False}, "3": {"applied_a": True}}
Path(TMP, "live_state.json").write_text(json.dumps(st))
Path(TMP, "call_verdicts.json").write_text(json.dumps({"2": {}, "3": {}}))
check("pending_cids is limited to this run's calls", AF.pending_cids(Path(TMP), "applied_a") == {"2"})
os.remove(Path(TMP, "call_verdicts.json"))
check("pending_cids without a window file is unchanged", AF.pending_cids(Path(TMP), "applied_a") == {"1", "2"})

print(f"\n{ok} passed, {fails} failed")
sys.exit(1 if fails else 0)
