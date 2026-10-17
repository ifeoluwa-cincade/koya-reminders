"""
classify_calls.py — read each call transcript and decide what actually happened.

This is the piece that replaces the disposition button nobody presses. CloudTalk offers six
meaningful dispositions and on 25 recent calls checked, zero had one set. HubSpot had the same
gap. The transcripts are the only record of what was said, so the verdict comes from them.

The six dispositions are Ife's own, taken verbatim from the CloudTalk campaign config so the
CRM and the dialer speak the same language:
    MEETING_BOOKED / SEND_FOLLOW_UP / NOT_INTERESTED / DO_NOT_CALL / CALLBACK / WRONG_PERSON
plus NO_CONVERSATION for the 36 transcripts that are just a greeting and a refusal.

Uses the local `claude` CLI (Max plan, no API key). Cached per call id, so a re-run is free.

    python crm/scripts/classify_calls.py              # dry run, shows the spread
    python crm/scripts/classify_calls.py --commit
"""
from __future__ import annotations
import argparse, collections, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
IN = ROOT / "data/working/call_transcripts.json"
OUT = ROOT / "data/working/call_verdicts.json"

PROMPT = """You are reading a transcript of a COLD SALES CALL made by Koya, which places offshore
AI-automation developers with AI agency founders. The caller is Temi or Chris. The other person
is the prospect.

Decide what happened, using EXACTLY one of these labels:

MEETING_BOOKED   - a meeting, demo or follow-up call was actually agreed

SEND_FOLLOW_UP   - they have a CURRENT NEED and want to take it further. There must be evidence
                   of real intent: they describe a gap they are trying to fill, ask for specific
                   profiles or pricing, compare you against what they use now, or ask a question
                   that only matters if they are considering it.

NOT_NOW          - they agreed to receive an email but showed NO current need. This is the polite
                   way people end a cold call and it is NOT interest. Use this whenever they say
                   any of: not a priority right now, already have someone, happy with my current
                   developers, solving it in-house, I build it myself, maybe later, send it and
                   I'll look when I need it. **If the only positive thing is that they accepted
                   an email, it is NOT_NOW.** Be strict — a pipeline full of people who were
                   being polite is worse than an empty one.
CALLBACK         - they asked to be called back another time
NOT_INTERESTED   - they declined
DO_NOT_CALL      - they asked not to be contacted again, or were hostile about it
WRONG_PERSON     - wrong number, not a decision maker, or not an AI agency at all
NO_CONVERSATION  - gatekeeper, voicemail, immediate hang-up, a dropped or restarted call, a
                   language barrier, or anything too short to judge. Logistics (a text that never
                   arrived, swapping to WhatsApp) is NOT interest.

Then in ONE sentence say what was actually discussed, in plain English. If they raised an
objection, say what it was.

Reply as JSON only:
{"verdict": "...", "summary": "...", "objection": "... or null"}

TRANSCRIPT:
"""


def who_line(name: str | None, company: str | None) -> str:
    """The prospect's real first name and company (from the contact record) for the prompt. Speech-to-text
    mishears names ("Jada" for "Jader"), so the model is told to spell them exactly as given."""
    fn = (str(name or "").strip().split() or [""])[0]
    co = str(company or "").strip()
    if not fn and not co:
        return ""
    who = " at ".join(x for x in (fn, co) if x)
    names = " and ".join(f'"{x}"' for x in (fn, co) if x)
    return (f"The prospect is {who} (from our contact record). The transcript is speech-to-text and often "
            f"mishears names: whenever you mention the prospect or their company, write {names} exactly, "
            f"never a spelling heard on the call.\n\n")


def prompt_for(name: str | None = None, company: str | None = None) -> str:
    """PROMPT with the prospect's real name and company just above the transcript."""
    return PROMPT.replace("TRANSCRIPT:\n", who_line(name, company) + "TRANSCRIPT:\n")


def classify(text: str, name: str | None = None, company: str | None = None) -> dict:
    p = subprocess.run(["claude", "-p", prompt_for(name, company) + text[:12000]],
                       capture_output=True, text=True, timeout=240)
    out = (p.stdout or "").strip()
    i, j = out.find("{"), out.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(out[i:j + 1])
        except Exception:
            pass
    return {"verdict": "UNKNOWN", "summary": out[:200], "objection": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    calls = json.loads(IN.read_text())
    done = json.loads(OUT.read_text()) if OUT.exists() else {}
    todo = [(k, v) for k, v in calls.items() if k not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(calls)} transcripts, {len(done)} already classified, {len(todo)} to do")
    if not a.commit:
        print("\nDRY RUN — nothing sent.")
        return 0
    for n, (cid, c) in enumerate(todo, 1):
        v = classify(c["text"], c.get("name"), c.get("company"))
        done[cid] = {**{k: c[k] for k in ("name", "company", "talk", "started", "emails")}, **v}
        OUT.write_text(json.dumps(done, indent=1))     # save every one
        print(f"   [{n}/{len(todo)}] {str(c['name'])[:18]:<18} {v.get('verdict','?'):<16} "
              f"{str(v.get('summary'))[:70]}", flush=True)
    print("\n" + str(dict(collections.Counter(v["verdict"] for v in done.values()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
