"""Offline checks for phone_pick.py (no network). Run: python crm/scripts/test_phone_pick.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phone_pick as PP  # noqa: E402

bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    bad += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


def ph(n, t="api-token", at="2026-10-27T09:11:39.7Z"):
    return {"original_phone_number": n, "created_by_actor": {"type": t}, "active_from": at}


def vals(*phones, book=None):
    v = {"phone_numbers": list(phones)}
    if book is not None:
        v["phone_sources"] = [{"value": json.dumps(book)}]
    return v


# 29 Oct 2026: the junk scrape number is dropped, Prospeo's becomes primary
b, ch, why = PP.apply_lookup(vals(ph("+15825539942", "app")), "+1613-555-0123", "Prospeo (verified)", at="T")
check("lead: junk dropped, prospeo primary", (PP.ordered(b), ch), (["+16135550123"], True))
check("lead: source recorded", (b["phones"][0]["src"], b["phones"][0]["at"]), ("prospeo", "T"))
# Ife: the lookup replaces as MAIN number, older numbers are kept as extras (typed, signature, anything)
b, ch, why = PP.apply_lookup(vals(ph("+17135550142", "workspace-member")), "+18325550199", "Prospeo (verified)")
check("typed number kept as extra", PP.ordered(b), ["+18325550199", "+17135550142"])
check("typed number described as manual", [p["src"] for p in b["phones"] if p["n"] == "+17135550142"], ["manual"])
check("why says older stays", "older number stays" in why, True)
sig = {"v": 1, "primary": "+19795550177", "phones": [{"n": "+19795550177", "src": "signature", "at": "2026-10-27T20:14:00Z"}]}
b, ch, _ = PP.apply_lookup(vals(ph("+19795550177"), book=sig), "+1337-555-0164", "AI Ark")
check("signature kept as extra, aiark primary", (PP.ordered(b), [p["src"] for p in b["phones"]]), (["+13375550164", "+19795550177"], ["signature", "aiark"]))
# once: a later run never re-promotes the lookup number over a primary someone chose
chosen = {"v": 1, "primary": "+19795550177", "phones": [{"n": "+19795550177", "src": "signature"}, {"n": "+13375550164", "src": "aiark"}]}
b, ch, why = PP.apply_lookup(vals(ph("+19795550177"), ph("+13375550164"), book=chosen), "+13375550164", "AI Ark")
check("lookup number already there: no change", (ch, b["primary"]), (False, "+19795550177"))
check("same number other format: no change", PP.apply_lookup(vals(ph("+16135550123")), "+1613-555-0123", "Prospeo (verified)")[1], False)
check("no verified number: nothing", PP.apply_lookup(vals(ph("+14155552671")), "", "")[1], False)
check("junk never added", PP.add(PP.read_book({}), "+1 582 553 9942", "website"), False)
b = PP.read_book(vals(ph("+15825539942"), ph("+14155552671")))
check("drop_junk returns removed and re-picks primary", (PP.drop_junk(b), b["primary"]), (["+15825539942"], "+14155552671"))

# to_values: primary first in phone_numbers, the book as JSON text
b = PP.read_book(vals(ph("+14155552671")))
PP.add(b, "+442079460958", "website", url="https://acme.co/contact", primary=True)
tv = PP.to_values(b)
check("to_values order", [x["original_phone_number"] for x in tv["phone_numbers"]], ["+442079460958", "+14155552671"])
doc = json.loads(tv["phone_sources"])
check("to_values book", (doc["primary"], doc["phones"][0]["src"], doc["phones"][0]["url"]), ("+442079460958", "website", "https://acme.co/contact"))
check("read back is the same book", PP.read_book({"phone_numbers": [ph(x["original_phone_number"]) for x in tv["phone_numbers"]],
                                                   "phone_sources": [{"value": tv["phone_sources"]}]})["primary"], "+442079460958")
check("book primary wins over Attio order", PP.read_book(vals(ph("+1"), ph("+14155552671"), book={"primary": "+14155552671", "phones": []}))["primary"], "+14155552671")
check("a removed number is not in the book", PP.ordered(PP.read_book(vals(ph("+14155552671"), book={"primary": "+19999999999", "phones": [{"n": "+19999999999", "src": "website"}]}))), ["+14155552671"])
check("prospeo verified source", PP.verified_source("Prospeo (verified)"), True)
check("ai ark source", PP.verified_source("AI Ark"), True)
check("salesql is not verified", PP.verified_source("SalesQL"), False)
check("person_phone reads the first value", PP.person_phone(vals(ph("+15825539942", "app"))), ("+15825539942", "app"))

# the CloudTalk contact for a person is the one with their email, never just the first hit for the number
import positive_replies_to_call_queue as Q  # noqa: E402
hits = [{"Contact": {"id": "c1", "name": "Another Lead"}, "ContactEmail": {"email": "other@example.org"}},
        {"Contact": {"id": "c2", "name": "The Lead"}, "ContactEmail": {"email": "Lead@Example.com"}}]
check("contact chosen by email, not the first hit", Q.ct_contact_for(hits, ["lead@example.com"]), "c2")
check("no email match -> none (create one)", Q.ct_contact_for(hits[:1], ["lead@example.com"]), None)
check("list-shaped emails", Q.ct_contact_for([{"Contact": {"id": "9"}, "ContactEmail": [{"email": "a@b.co"}]}], ["a@b.co"]), "9")

print("all ok" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
