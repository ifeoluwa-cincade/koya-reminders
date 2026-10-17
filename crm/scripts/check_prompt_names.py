"""Check: the call prompts carry the prospect's real first name and company, with the rule to spell them exactly.
Offline (the model is stubbed), made-up names only. Run: python crm/scripts/check_prompt_names.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classify_calls  # noqa: E402
import live_call_dispositions as lcd  # noqa: E402
import call_review  # noqa: E402

fails = []


def ok(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)


p = classify_calls.prompt_for("Ana Lima", "Example Labs")
ok('The prospect is Ana at Example Labs' in p and '"Ana" and "Example Labs" exactly' in p, "dispositions prompt names the prospect and company")
ok(p.index("The prospect is Ana") < p.index("TRANSCRIPT:"), "names sit just above the transcript")
ok(classify_calls.prompt_for() == classify_calls.PROMPT, "no contact record: prompt unchanged")
ok('"Example Labs" exactly' in classify_calls.prompt_for(None, "Example Labs"), "company alone still passed")

seen = {}
lcd.call_claude_lite = lambda prompt, **k: seen.setdefault("p", prompt) and {"verdict": "NOT_NOW", "summary": "x", "objection": None}
lcd.classify("hello this is a long enough transcript about the agency", "Ana Lima", "Example Labs")
ok('"Ana" and "Example Labs" exactly' in seen.get("p", ""), "live dispositions send the names to the model")

seen.clear()
call_review.call_claude_lite = lambda prompt, **k: seen.setdefault("p", prompt) and {"speakers": ["us"]}
call_review.ai_pass([{"s": 0.0, "e": 1.0, "text": "hi"}], "Temi Example", "Ana Lima", False, "Example Labs")
cp = seen.get("p", "")
ok("the prospect (Ana at Example Labs)." in cp and '"Ana" and "Example Labs" exactly' in cp, "call review breakdown sends the names to the model")
seen.clear()
call_review.ai_pass([{"s": 0.0, "e": 1.0, "text": "hi"}], "", "", False)
ok("the prospect." in seen.get("p", "") and "exactly" not in seen.get("p", ""), "call review without a contact: no empty names")

print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
