"""Offline checks for call_review.py's objections and warm-call parts (no network, made-up calls). Run: python crm/scripts/test_call_review_extras.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import call_review as CR  # noqa: E402

bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    bad += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


# the old-offer flag: today's offer is about $1,500 a month with a 30-day trial
check("current price is fine", CR.offer_flag("It's about $1,500 a month per developer."), False)
check("1.5k is fine", CR.offer_flag("roughly $1.5k a month"), False)
check("30-day trial is fine", CR.offer_flag("You get a 30-day trial first."), False)
check("one month trial is fine", CR.offer_flag("there is a one month trial"), False)
check("$500 is an old price", CR.offer_flag("It's $500 a month, we just pass it through."), True)
check("2,000 dollars flagged", CR.offer_flag("about 2,000 dollars"), True)
check("hourly rate flagged", CR.offer_flag("we charge $25 an hour"), True)
check("two week trial flagged", CR.offer_flag("we do a two-week trial"), True)
check("retired offer flagged", CR.offer_flag("we used to offer a build service"), True)
check("no price, no flag", CR.offer_flag("They work your hours and join your Slack."), False)

# warm openings
T = lambda *xs: [{"s": s, "e": s + 3, "who": w, "text": t} for s, w, t in xs]
check("you replied to my email", CR.opening_warm(T((2, "us", "Hi Sam, it's Alex from Koya, you replied to my email last week."))), True)
check("he just replied (screener)", CR.opening_warm(T((1, "them", "State your name."), (5, "us", "I'm calling because he just replied to the email about a developer."))), True)
check("got your email", CR.opening_warm(T((3, "them", "Oh yes, got your email."))), True)
check("following up on your reply", CR.opening_warm(T((4, "us", "Following up on your reply about staffing."))), True)
check("cold opening", CR.opening_warm(T((2, "us", "Hi, this is Alex from Koya, do you have a minute?"))), False)
check("reply mentioned late is not the opening", CR.opening_warm(T((2, "us", "Hi, do you have a minute?"), (300, "us", "you replied to my email"))), False)

# objections are cleaned: times snap to real turns, unknown topics become other, the flag is set by code too
turns = T((0, "us", "Hi"), (10.4, "them", "How much is it?"), (14.2, "us", "It's $500 a month."), (30, "them", "What hours?"), (33, "us", "Your hours."))
got = CR.norm_objections([{"q": "  How much  is it? ", "q_t": 11, "answer": "It's $500 a month.", "a_t": 14, "topic": "pricing", "old_offer": False},
                          {"q": "What hours?", "q_t": 29, "answer": "Your hours.", "a_t": 33.4, "topic": "weird", "old_offer": False},
                          {"q": "", "q_t": 1, "answer": "x", "a_t": 1, "topic": "other", "old_offer": False}], turns)
check("empty question dropped", len(got), 2)
check("question tidied", got[0]["q"], "How much is it?")
check("q_t snapped", got[0]["q_t"], 10.4)
check("a_t snapped", got[0]["a_t"], 14.2)
check("old price flagged by code", got[0]["old"], True)
check("unknown topic -> other", got[1]["topic"], "other")
check("current answer not flagged", got[1]["old"], False)
check("unanswered has no a_t", CR.norm_objections([{"q": "Who are you?", "q_t": 0, "answer": "", "a_t": 5, "topic": "other", "old_offer": False}], turns)[0]["a_t"], None)

# warm_of without a PlusVibe key: the reply check is unknown (None), the opening still counts
import os
os.environ.pop("PLUS_VIBE_API_KEY", None)
CR.opt_env = lambda k: ""
w = CR.warm_of(T((2, "us", "you replied to my email")), False, ["a@b.co"], "2026-09-04T17:00:00+02:00")
check("warm by opening", (w["yes"], w["by"], w["reply"]), (True, ["opening"], None))
w = CR.warm_of(T((2, "us", "Hi there")), True, [], None)
check("warm by the model's flag", (w["yes"], w["opening"]), (True, True))
w = CR.warm_of(T((2, "us", "Hi there")), False, [], None)
check("cold call", w["yes"], False)

# the prompts still format (the objection rules are a value, so their braces stay single)
p = CR.PROMPT.format(agent="Alex", lead=" (Sam at Acme)", names="", diar="", spk_rule="", obj_rules=CR.OBJ_RULES, segs="[0] 0.0s: hi")
check("review prompt has the rules", "objections: EVERY question" in p and "{q:" in p and "{{" not in p, True)
p = CR.EXTRAS_PROMPT.format(agent="Alex", lead="", names="", obj_rules=CR.OBJ_RULES, turns="[0] 0.0s us: hi")
check("extras prompt formats", "warm_opening" in p, True)
check("schema requires objections", "objections" in CR.SCHEMA["required"] and CR.SCHEMA["properties"]["objections"]["items"] is CR.OBJ_ITEM, True)
check("old objection field kept", "objection" in CR.SCHEMA["required"], True)

print("all ok" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
