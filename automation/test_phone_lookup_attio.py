"""Offline checks for plusvibe_auto_phone_lookup.attio_phone_first (no network: requests is faked).
Run: python automation/test_phone_lookup_attio.py"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["ATTIO_API_KEY_2"] = "test"
import plusvibe_auto_phone_lookup as L  # noqa: E402

bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    bad += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


class R:
    def __init__(self, j, ok=True): self._j, self.ok, self.status_code = j, ok, 200 if ok else 500
    def json(self): return self._j


def run(person_phones, book=None):
    calls = []
    vals = {"phone_numbers": person_phones}
    if book:
        vals["phone_sources"] = [{"value": json.dumps(book)}]
    rec = {"id": {"record_id": "p1"}, "values": vals}
    L.requests.post = lambda *a, **k: R({"data": [rec]})
    L.requests.put = lambda url, **k: calls.append(k["json"]["data"]["values"]) or R({})
    L.requests.patch = lambda url, **k: calls.append(("PATCH", k["json"])) or R({})
    L.attio_phone_first("lead@example.com", "+16135550123", "Prospeo (verified)")
    return [([x["original_phone_number"] for x in c["phone_numbers"]], [(p["n"], p["src"]) for p in json.loads(c["phone_sources"])["phones"]])
            if isinstance(c, dict) else c for c in calls]


app = lambda n, t="app": [{"original_phone_number": n, "created_by_actor": {"type": t}, "active_from": "2026-10-27T09:11:39Z"}]
# 29 Oct 2026: a lead's junk scrape number is dropped; Prospeo's is written with PUT (overwrite), never PATCH (prepend)
check("junk dropped, prospeo primary", run(app("+15825539942")), [(["+16135550123"], [("+16135550123", "prospeo")])])
check("no phone: written", run([]), [(["+16135550123"], [("+16135550123", "prospeo")])])
check("same number: nothing written", run(app("+1 613-555-0123")), [])
check("typed by a person: kept as extra", run(app("+14155552671", "workspace-member")),
      [(["+16135550123", "+14155552671"], [("+16135550123", "prospeo"), ("+14155552671", "manual")])])
check("already in the book (someone chose another primary): nothing written",
      run(app("+14155552671") + app("+16135550123"), book={"primary": "+14155552671", "phones": [{"n": "+16135550123", "src": "prospeo"}]}), [])

print("all ok" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
