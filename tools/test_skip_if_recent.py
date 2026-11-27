"""python tools/test_skip_if_recent.py"""
import sys, os
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
import skip_if_recent as S

NOW = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
r = lambda i, t, ev="workflow_dispatch", c="success": {"id": i, "run_started_at": t, "event": ev, "conclusion": c}
assert S.decide([r(1, "2026-11-27T09:40:00Z")], "9", NOW, 45)[0] is False, "HQ started it 20 min ago: skip"
assert S.decide([r(1, "2026-11-27T09:00:00Z")], "9", NOW, 45)[0] is True, "65 min ago: run"
assert S.decide([r(9, "2026-11-27T09:59:00Z", "schedule")], "9", NOW, 45)[0] is True, "itself does not count"
assert S.decide([r(1, "2026-11-27T09:50:00Z", c="cancelled")], "9", NOW, 45)[0] is True, "a cancelled run does not count"
assert S.decide([r(1, "2026-11-27T09:50:00Z", c=None)], "9", NOW, 45)[0] is False, "one still running counts"
assert S.decide([r(1, "2026-11-26T18:00:00Z")], "9", NOW, 20 * 60)[0] is False, "a daily job: HQ ran it this morning"
assert S.decide([], "9", NOW, 45)[0] is True
assert S.decide([r(1, "2026-11-27T09:50:00Z", "repository_dispatch")], "9", NOW, 45, ["schedule", "workflow_dispatch"])[0] is True, "a single-deal stage run is not the sweep"
print("ok skip_if_recent: 8 checks")
