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
check("lead: source recorded", (b["phones"][0]["sources"][0]["src"], b["phones"][0]["sources"][0]["at"]), ("prospeo", "T"))
# Ife 30 Oct: reliability decides the primary. The lookup's verified number beats an imported / synced one ...
b, ch, why = PP.apply_lookup(vals(ph("+17135550142", "api-token")), "+18325550199", "Prospeo (verified)")
check("prospeo beats an imported list", PP.ordered(b), ["+18325550199", "+17135550142"])
check("why says older stays", "older number stays" in why, True)
# ... but a number typed by a person outranks every provider, and stays primary; the lookup's number is kept as an extra
b, ch, why = PP.apply_lookup(vals(ph("+17135550142", "workspace-member")), "+18325550199", "Prospeo (verified)")
check("typed number stays primary", (PP.ordered(b), ch), (["+17135550142", "+18325550199"], True))
check("typed number described as manual", [s["src"] for p in b["phones"] if p["n"] == "+17135550142" for s in p["sources"]], ["manual"])
check("why says more reliable stays", "more reliable" in why, True)
# their own signature outranks AI Ark (v1 book read: one source per entry)
sig = {"v": 1, "primary": "+19795550177", "phones": [{"n": "+19795550177", "src": "signature", "at": "2026-10-27T20:14:00Z"}]}
b, ch, _ = PP.apply_lookup(vals(ph("+19795550177"), book=sig), "+1337-555-0164", "AI Ark")
check("disagreeing providers: both kept, signature primary", (PP.ordered(b), [p["sources"][0]["src"] for p in b["phones"]]),
      (["+19795550177", "+13375550164"], ["signature", "aiark"]))
# disagreeing providers: AI Ark found one number, Prospeo verified another -> Prospeo primary, AI Ark's kept as an extra with its source
ark = {"v": 1, "primary": "+15145550111", "phones": [{"n": "+15145550111", "src": "aiark", "at": "2026-10-27T09:00:00Z"}]}
b, ch, why = PP.apply_lookup(vals(ph("+15145550111"), book=ark), "+15145550222", "Prospeo (verified)", at="2026-10-30T15:52:00Z")
check("disagree: prospeo primary, aiark extra", PP.ordered(b), ["+15145550222", "+15145550111"])
doc = json.loads(PP.to_values(b)["phone_sources"])
check("disagree: extra keeps its source", [(p["n"], [s["src"] for s in p["sources"]]) for p in doc["phones"]],
      [("+15145550222", ["prospeo"]), ("+15145550111", ["aiark"])])
# unverified Prospeo ranks below AI Ark
b, ch, _ = PP.apply_lookup(vals(ph("+15145550111"), book=ark), "+15145550222", "Prospeo")
check("unverified prospeo below aiark", PP.ordered(b), ["+15145550111", "+15145550222"])

# The incident (30 Oct 2026): AI Ark found the number for the call list, CloudTalk's Attio integration copied it onto the person
# (an "app" write, no book), then the positive-reply lookup verified the SAME number with Prospeo. It used to record nothing.
LEAD = "+35699000001"   # fake number
b, ch, why = PP.apply_lookup(vals(ph(LEAD, "app", "2026-10-27T10:00:00Z")), LEAD, "Prospeo (verified)", at="2026-10-30T15:52:00Z")
check("incident: changed, source added", (ch, "source added" in why), (True, True))
check("incident: CloudTalk placeholder replaced by the real source", [s["src"] for s in PP.sorted_sources(b["phones"][0])], ["prospeo"])
check("incident: primary unchanged", b["primary"], LEAD)
check("incident: book best src is prospeo (v1 readers)", json.loads(PP.to_values(b)["phone_sources"])["phones"][0]["src"], "prospeo")
PP.add(b, LEAD, "aiark", at="2026-10-27T09:00:00Z", by="call list")
check("incident: both providers", [s["src"] for s in PP.sorted_sources(b["phones"][0])], ["prospeo", "aiark"])
tv = PP.to_values(b)
b2, ch2, why2 = PP.apply_lookup({"phone_numbers": [ph(LEAD)], "phone_sources": [{"value": tv["phone_sources"]}]}, LEAD, "Prospeo (verified)")
check("incident: second run writes nothing", (ch2, why2), (False, "already on the person with this source"))
# once: a later run never re-promotes the lookup number over a primary someone chose
chosen = {"v": 1, "primary": "+19795550177", "phones": [{"n": "+19795550177", "src": "signature"}, {"n": "+13375550164", "src": "aiark"}]}
b, ch, why = PP.apply_lookup(vals(ph("+19795550177"), ph("+13375550164"), book=chosen), "+13375550164", "AI Ark")
check("lookup number already there: no change", (ch, b["primary"]), (False, "+19795550177"))
check("same number other format: one number", len(PP.apply_lookup(vals(ph("+16135550123")), "+1613-555-0123", "Prospeo (verified)")[0]["phones"]), 1)
# a person chose the primary in Koya HQ: never overridden, even by a better source
chose = {"v": 2, "primary": "+14155552671", "chosen": {"by": "An Owner", "at": "2026-10-29T10:00:00Z", "n": "+14155552671"},
         "phones": [{"n": "+14155552671", "sources": [{"src": "website", "at": "2026-10-20T00:00:00Z"}]}]}
b, ch, why = PP.apply_lookup(vals(ph("+14155552671"), book=chose), "+16135550123", "Prospeo (verified)")
check("person-chosen primary kept", (PP.ordered(b), ch, "chose" in why), (["+14155552671", "+16135550123"], True, True))
check("chosen survives the write", json.loads(PP.to_values(b)["phone_sources"]).get("chosen", {}).get("by"), "An Owner")
PP.add(b, "+16135550123", "manual", by="Someone")
check("person-chosen kept even over a typed number", b["primary"], "+14155552671")
check("choose() marks a person's pick", (PP.choose(b, "+16135550123", by="Owner"), b["primary"], b["chosen"]["by"]), (True, "+16135550123", "Owner"))
check("chosen for a removed number is dropped", "chosen" in PP.read_book(vals(ph("+16135550123"), book=chose)), False)
# v1 book read: one source per entry, its by/url kept
v1 = {"v": 1, "primary": "+442079460958", "phones": [{"n": "+442079460958", "src": "website", "at": "2026-10-28T00:00:00Z", "url": "https://acme.co/contact"}]}
b = PP.read_book(vals(ph("+442079460958"), book=v1))
check("v1 read", (b["v"], b["phones"][0]["sources"]), (2, [{"src": "website", "at": "2026-10-28T00:00:00Z", "url": "https://acme.co/contact"}]))
check("no verified number: nothing", PP.apply_lookup(vals(ph("+14155552671")), "", "")[1], False)
check("junk never added", PP.add(PP.read_book({}), "+1 582 553 9942", "website"), False)
b = PP.read_book(vals(ph("+15825539942"), ph("+14155552671")))
check("drop_junk returns removed and re-picks primary", (PP.drop_junk(b), b["primary"]), (["+15825539942"], "+14155552671"))

# to_values: primary first in phone_numbers, the book as JSON text
b = PP.read_book(vals(ph("+14155552671")))
PP.add(b, "+442079460958", "website", url="https://acme.co/contact"); PP.choose(b, "+442079460958", by="Owner")
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
