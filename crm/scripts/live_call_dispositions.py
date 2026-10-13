"""
live_call_dispositions.py — give every new CloudTalk call a disposition, with nobody clicking anything.

WHY: agents do not set dispositions in CloudTalk, and the built-in CloudTalk -> Attio link has
written nothing since it was connected (checked 2026-10-07). So we work out the outcome ourselves,
from the call itself, and bucket it into the dispositions CloudTalk already uses.

HOW, per new call (CloudTalk API -> this script -> files in CALL_DATA_DIR):
  1. no talk time                      -> NO_ANSWER      (rang out, cancelled, busy)
  2. a few seconds of talk             -> VOICEMAIL      (hung up / machine picked up)
  3. longer, has a recording           -> Groq Whisper transcript, then
       a. voicemail phrases ("leave a message", "after the tone")  -> VOICEMAIL
       b. otherwise Claude reads it and picks one of the real dispositions
          MEETING_BOOKED / SEND_FOLLOW_UP / NOT_NOW / CALLBACK / NOT_INTERESTED / DO_NOT_CALL /
          WRONG_PERSON / NO_CONVERSATION  (same prompt as classify_calls.py, so Ife's rules apply)
It then writes the two files apply_call_verdicts.py and calls_to_call_lists.py already read, so the
Attio notes, person state and call-list entries follow without new Attio code.

State is kept in CALL_DATA_DIR/live_state.json, written after EVERY call. Re-running costs nothing
for calls already done. Dry-run by default.

    CALL_DATA_DIR=data/working/live python crm/scripts/live_call_dispositions.py            # dry run
    CALL_DATA_DIR=data/working/live python crm/scripts/live_call_dispositions.py --commit

Needs: CLOUD_TALK_API_KEY_ID, CLOUD_TALK_API_KEY, GROQ_API_KEY, and the `claude` CLI logged in
(CLAUDE_CODE_OAUTH_TOKEN in the cloud).
"""
from __future__ import annotations
import argparse, base64, collections, json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from classify_calls import PROMPT  # noqa: E402  (the prompt with Ife's strict rules)
sys.path.insert(0, str(ROOT / "pipeline"))
from lite_claude import call_claude_lite  # noqa: E402  (no CLAUDE.md auto-load: ~65% cheaper per call)


def classify(text: str) -> dict:
    """Same verdicts as classify_calls.py, through the lightweight wrapper so hundreds of calls a day
    do not each drag in the project's instruction files."""
    words = len(text.split())
    if words < 3:
        return {"verdict": "NO_CONVERSATION", "summary": "Too little said to judge.", "objection": None}
    err = ""
    try:
        res = call_claude_lite(PROMPT + text[:12000], model="sonnet", timeout_sec=240)   # returns the parsed JSON
        if isinstance(res, dict) and res.get("verdict"):
            return res
        err = f"unexpected reply {str(res)[:80]}"
    except Exception as ex:
        err = f"{type(ex).__name__}: {str(ex)[:100]}"
    # Claude failed (token expired, quota, timeout): fall back to Groq's chat model so the day is not lost.
    # Free tier for openai/gpt-oss-120b is 30 requests/min, 8,000 tokens/min, 200,000 tokens/day, shared across all keys.
    for key in groq_keys():
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=120,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": "openai/gpt-oss-120b", "temperature": 0,
                                    "messages": [{"role": "user", "content": PROMPT + text[:9000]}]})
            if r.status_code == 429:
                time.sleep(8)
                continue
            if r.status_code < 300:
                out = r.json()["choices"][0]["message"]["content"]
                i, j = out.find("{"), out.rfind("}")
                got = json.loads(out[i:j + 1]) if i >= 0 and j > i else {}
                if got.get("verdict"):
                    got["judged_by"] = "groq"
                    return got
        except Exception as ex:
            err += f" | groq {type(ex).__name__}"
    return {"verdict": "UNKNOWN", "summary": f"classifier failed: {err}"[:200], "objection": None}

CT = "https://my.cloudtalk.io/api"
WORK = Path(os.environ.get("CALL_DATA_DIR") or ROOT / "data/working/live")
STATE = WORK / "live_state.json"
AUDIO = WORK / "audio"
MODEL = "whisper-large-v3-turbo"
REAL_VERDICTS = {"MEETING_BOOKED", "SEND_FOLLOW_UP", "CALLBACK", "NOT_NOW", "NOT_INTERESTED", "DO_NOT_CALL", "WRONG_PERSON"}
MIN_TALK = 3            # seconds; below this there is nothing to transcribe
# Groq free tier: 28,800 audio seconds a day and 7,200 an hour. Stop at about 80% and carry on next run.
DAILY_BUDGET = int(os.environ.get("GROQ_DAILY_AUDIO_BUDGET") or 23000)
HOURLY_BUDGET = int(os.environ.get("GROQ_HOURLY_AUDIO_BUDGET") or 5800)
# --- rules that settle a call WITHOUT asking an AI. Tested on every 8 Oct transcript: they settle 291 of 361 and
# wrongly caught 0 of the 17 real conversations. A greeting is the first thing said, so only the opening is read.
VM = re.compile(r"leave (a|your|me a|us a|the)?\s*(short |brief )?(message|name|number)|(after|at) the (tone|beep)|voice ?mail|mailbox|record your message|"
    r"can'?t (come|get) to the phone|can'?t take your call|unable to (take|answer)|not (available|able to (take|answer))|isn'?t available|unavailable|"
    r"(you'?ve|you have) reached|your call (has been|is being) forwarded|(the )?person you('re| are) trying to reach|"
    r"get back to you as soon|call you back as soon|please leave|away from (my|the) (phone|desk)|after leaving a message|press (the )?pound|"
    r"buz[o\u00f3]n de voz|bo[i\u00ee]te vocale|r[\u00e9e]pondeur|anrufbeantworter|caixa postal|segreteria|dejar un mensaje|laissez un message|hinterlassen|lascia un messaggio|mensaje despu", re.I)
# Screening and phone menus are only decisive while nothing else is said: a real person can pick up afterwards.
SCREEN = re.compile(r"record your name|reason for calling|call assist|recording this call|ai assistant|call screen|call control|to get through|please stay on the line|"
    r"press (one|two|three|zero|1|2|3|0)\b|please hold|how can i help you today|thank you for calling|office hours|business hours|dial by name|extension|"
    r"if you know your party|the number you (have )?dialed|cannot be completed|not in service|receptionist", re.I)
REFUSAL = re.compile(r"not interested|no thanks|no thank you|don'?t call|do not call|stop calling|remove me|wrong number|not (the|him|her)|who is this", re.I)


# The CALLER'S OWN NOTE (CloudTalk "Notes" on the call) is the first-class disposition (Ife, 2026-10-12): a person who just spoke to the lead wrote it,
# it exists the moment the call ends, and it needs no recording or AI. Only when there is no usable note does the recording decide.
_DNC = re.compile(r"\b(do not call|don'?t call|dnc|stop calling|never call|remove (me|him|her|them) from)\b", re.I)
_WRONG = re.compile(r"\b(wrong (number|person|contact)|left the company|no longer (works|there|with))\b", re.I)
_CALLBACK = re.compile(r"\b(call (him |her |them )?(back|again)|callback|ring (him |her |them )?(back|again)|try (him |her |them )?again|phone (him |her |them )?(back|again))\b", re.I)
_FOLLOW = re.compile(r"\b(follow[- ]?up|send (an? |the |some )?(email|e-mail|mail|profile|profiles|info|information|details|deck|pitch|link|samples?)|email (him|her|them|me)|to send em|will send)\b", re.I)
_NOFOLLOW = re.compile(r"\b(no|don'?t|do not|no need to|not worth)\s+(follow|send|call|email)", re.I)
_NOTINT = re.compile(r"\b(not interested|no interest|declined|said no|hard no|uninterested)\b", re.I)
_MEET = re.compile(r"\b(booked|scheduled|meeting (set|booked)|demo (set|booked)|call (set|booked) for)\b", re.I)


def agent_verdict(note: str) -> dict | None:
    """What the caller said to do next, in their own words. None = no clear instruction (the recording decides)."""
    n = (note or "").strip()
    if len(n) < 3:
        return None
    if _DNC.search(n):
        v = "DO_NOT_CALL"
    elif _WRONG.search(n):
        v = "WRONG_PERSON"
    elif _NOFOLLOW.search(n) or (_NOTINT.search(n) and not (_FOLLOW.search(n) or _CALLBACK.search(n))):
        v = "NOT_INTERESTED"
    elif _MEET.search(n):
        v = "MEETING_BOOKED"
    elif _CALLBACK.search(n):
        v = "CALLBACK"
    elif _FOLLOW.search(n):
        v = "SEND_FOLLOW_UP"
    else:
        return None
    return {"verdict": v, "summary": "Caller's note: " + n[:300], "objection": None, "judged_by": "agent"}


def call_note(row: dict) -> str:
    return " ".join((x.get("note") or "").strip() for x in (row.get("Notes") or []) if isinstance(x, dict)).strip()


def rule_verdict(text: str) -> dict | None:
    words = text.split()
    opening = " ".join(words[:35])
    if VM.search(opening):
        return {"verdict": "VOICEMAIL", "summary": "Voicemail greeting on the line.", "objection": None}
    if SCREEN.search(opening) and len(words) <= 40:
        return {"verdict": "NO_CONVERSATION", "summary": "Call screening or a phone menu only.", "objection": None}
    if len(words) <= 6 and not REFUSAL.search(text):      # "Hello?", "Thank you." — nobody to judge
        return {"verdict": "NO_CONVERSATION", "summary": "A greeting only.", "objection": None}
    return None


def env(k: str) -> str:
    if os.environ.get(k):
        return os.environ[k]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip()
    sys.exit(f"{k} is not set")


def ct_get(path: str, H: dict, **kw):
    """CloudTalk 429s under load and stalls; back off and retry, never treat a 429 as 'no data'."""
    delay, r = 2.0, None
    for _ in range(6):
        try:
            r = requests.get(CT + path, headers=H, timeout=120, **kw)
        except requests.exceptions.RequestException:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code != 429:
            return r
        time.sleep(delay); delay *= 1.8
    return r


def fetch_calls(H: dict, since: str) -> list[dict]:
    out, page = [], 1
    while True:
        r = ct_get("/calls/index.json", H, params={"date_from": since, "limit": 100, "page": page})
        if r is None or r.status_code >= 300:
            sys.exit(f"CloudTalk calls list failed: {getattr(r, 'status_code', '?')}")
        rows = (r.json().get("responseData", {}) or {}).get("data") or []
        out += rows
        if len(rows) < 100:
            return out
        page += 1


def contact_bits(row: dict, H: dict) -> tuple[list[str], list[str]]:
    """Emails and numbers for the person on the call. The call row sometimes omits them."""
    ctc = row.get("Contact") or {}
    emails = [e.strip().lower() for e in (ctc.get("contact_emails") or []) if e]
    nums = list(ctc.get("contact_numbers") or [])
    if not emails and ctc.get("id"):
        d = (ct_get(f"/contacts/show/{ctc['id']}.json", H).json().get("responseData") or {})
        emails = [x.get("email", "").lower() for x in (d.get("ContactEmail") or []) if x.get("email")]
        nums = nums or [x.get("public_number") for x in (d.get("ContactNumber") or [])]
    return emails, nums


def groq_keys() -> list[str]:
    """GROQ_API_KEY, then _2 and _3 if set. Groq limits are per ORGANIZATION, so extra keys only help
    when they belong to a different Groq account; keys inside one org share a single limit."""
    out = []
    for k in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"):
        try:
            out.append(env(k))
        except SystemExit:
            pass
    return out


def transcribe(cid: str, H: dict, G: dict) -> tuple[str | None, str | None]:
    AUDIO.mkdir(parents=True, exist_ok=True)
    wav = AUDIO / f"{cid}.wav"
    if not wav.exists():
        r = ct_get(f"/calls/recording/{cid}.json", H)
        if r is None or r.status_code >= 300 or len(r.content) < 1000:
            return None, f"no recording ({getattr(r, 'status_code', '?')})"
        wav.write_bytes(r.content)
    delay, g, keys = 20.0, None, groq_keys()
    for attempt in range(8):      # Groq caps audio seconds per hour; a 429 means switch key, then wait
        hdr = {"Authorization": f"Bearer {keys[attempt % len(keys)]}"}
        with open(wav, "rb") as fh:
            g = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=hdr, timeout=300,
                              files={"file": (f"{cid}.wav", fh, "audio/wav")},
                              data={"model": MODEL, "response_format": "text", "language": "en"})
        if g.status_code != 429:
            break
        if (attempt + 1) % len(keys) == 0:      # every key was limited: wait before the next round
            time.sleep(delay); delay = min(delay * 1.6, 240)
    if g is None or g.status_code >= 300:
        return None, f"groq {getattr(g, 'status_code', '?')}: {getattr(g, 'text', '')[:120]}"
    return g.text.strip(), None


def decide(cdr: dict, cid: str, H: dict, G: dict) -> dict:
    talk = int(cdr.get("talking_time") or 0)
    if talk == 0:
        return {"verdict": "NO_ANSWER", "summary": "Not answered (rang out, cancelled or busy).", "objection": None}
    if talk < MIN_TALK:
        return {"verdict": "VOICEMAIL", "summary": f"Only {talk}s connected: hung up or a machine picked up.",
                "objection": None}
    if not cdr.get("recorded"):
        return {"verdict": "NO_CONVERSATION", "summary": f"{talk}s connected, no recording to judge from.",
                "objection": None}
    text, err = transcribe(cid, H, G)
    if err:
        return {"verdict": "UNKNOWN", "summary": f"Could not transcribe: {err}", "objection": None, "retry": True}
    v = rule_verdict(text)
    if v:
        return {**v, "text": text}
    v = classify(text)
    v["text"] = text
    if v.get("verdict") == "UNKNOWN":
        v["retry"] = True
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="YYYY-MM-DD; default 3 days ago")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    since = a.since or (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")

    tok = base64.b64encode(f"{env('CLOUD_TALK_API_KEY_ID')}:{env('CLOUD_TALK_API_KEY')}".encode()).decode()
    H = {"Authorization": f"Basic {tok}"}
    G = {}                                   # keys are read per request in groq_keys()

    WORK.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    rows = fetch_calls(H, since)
    byid = {str((r.get("Cdr") or {}).get("id")): r for r in rows}
    agent = {cid: av for cid, r in byid.items() for av in [agent_verdict(call_note(r))] if av}
    todo = [cid for cid in byid if cid not in state or state[cid].get("retry")
            or (cid in agent and state[cid].get("agent_note") != call_note(byid[cid]))]      # a note added or changed after the call is picked up too
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(rows)} calls since {since}  |  already done {len(byid) - len(todo)}  |  to do {len(todo)}")
    mech = sum(1 for cid in todo if cid in agent or int(byid[cid]["Cdr"].get("talking_time") or 0) < MIN_TALK)
    print(f"   {sum(1 for cid in todo if cid in agent)} decided by the caller's own note, {mech - sum(1 for cid in todo if cid in agent)} need no AI (no answer / under {MIN_TALK}s), {len(todo) - mech} get a transcript")
    if not a.commit:
        print("\nDRY RUN — nothing transcribed or written.")
        return 0

    def used(prefix: str) -> int:
        return sum(r.get("talk", 0) for r in state.values() if r.get("transcribed_at", "").startswith(prefix))

    # 1. decide up front which calls fit inside Groq's audio budget; the rest wait for the next run
    now = datetime.now(timezone.utc)
    day_used, hour_used = used(now.strftime("%Y-%m-%d")), used(now.strftime("%Y-%m-%dT%H"))
    work, held = [], 0
    for cid in todo:
        cdr = byid[cid].get("Cdr") or {}
        talk = int(cdr.get("talking_time") or 0)
        if cid not in agent and talk >= MIN_TALK and cdr.get("recorded"):
            if day_used + talk > DAILY_BUDGET or hour_used + talk > HOURLY_BUDGET:
                held += 1            # not lost: it stays out of state and is picked up next run
                continue
            day_used += talk; hour_used += talk
        work.append(cid)

    # 2. do them in parallel (CloudTalk 429s above ~3 downloads at once), saving after every call
    def one(cid):
        row = byid[cid]; cdr = row.get("Cdr") or {}
        if cid in agent:
            return cid, dict(agent[cid])
        try:
            return cid, decide(cdr, cid, H, G)
        except Exception as ex:      # a dropped connection or timeout: leave it for the next run, never crash the batch
            return cid, {"verdict": "UNKNOWN", "summary": f"{type(ex).__name__}: {str(ex)[:100]}", "objection": None, "retry": True}

    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for cid, v in ex.map(one, work):
            row = byid[cid]; cdr = row.get("Cdr") or {}
            try:
                emails, nums = contact_bits(row, H)
            except Exception:
                emails = []
            ctc = row.get("Contact") or {}
            if int(cdr.get("talking_time") or 0) >= MIN_TALK and cdr.get("recorded") and v.get("text"):
                v["transcribed_at"] = datetime.now(timezone.utc).isoformat()
            state[cid] = {"name": ctc.get("name"), "company": ctc.get("company"),
                          "talk": int(cdr.get("talking_time") or 0), "started": cdr.get("started_at"),
                          "agent": (row.get("Agent") or {}).get("fullname"), "emails": emails,
                          "type": cdr.get("type"), "agent_note": call_note(row), "applied_a": False, "applied_l": False, **v}      # the two Attio writers look up only calls still marked False (attio_fast.py)
            STATE.write_text(json.dumps(state, indent=1))        # save after every call
            done += 1
            if v["verdict"] not in ("VOICEMAIL", "NO_ANSWER", "NO_CONVERSATION"):
                print(f"   -> {v['verdict']} (judged by {v.get('judged_by', 'claude')})", flush=True)
            if done % 25 == 0 or done == len(work):
                print(f"   [{done}/{len(work)}] last: {v['verdict']}", flush=True)

    if not STATE.exists():
        STATE.write_text(json.dumps(state, indent=1))        # an empty day still leaves a state file for the next steps
    if held:
        print(f"   {held} calls held for the next run to stay inside Groq's audio limits")
    # the two files the existing Attio writers read: only the calls in the window, never the Mac's history
    window = {cid: state[cid] for cid in byid if cid in state}
    (WORK / "call_verdicts.json").write_text(json.dumps(
        {cid: {**{k: r.get(k) for k in ("name", "company", "talk", "started", "agent", "emails",
                                        "verdict", "summary", "objection", "judged_by")},
               # the full transcript only for real conversations: a voicemail greeting is not worth storing
               **({"text": r.get("text")} if r["verdict"] in REAL_VERDICTS else {})}
         for cid, r in window.items()}, indent=1))
    (WORK / "cloudtalk_calls.json").write_text(json.dumps([byid[cid] for cid in window], indent=1))
    print("\n" + str(dict(collections.Counter(r["verdict"] for r in window.values()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
