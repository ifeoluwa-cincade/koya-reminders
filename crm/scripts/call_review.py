"""
call_review.py - turn each real cold-call conversation into a "call review" the team app can play.

WHY: the team wants to learn from its own calls (Ife, 16 Oct 2026). The disposition job already works
out an outcome, but it keeps a plain transcript inside an encrypted cache and throws the audio away.
This step keeps what a review needs, in our own Supabase:

  1. the audio, copied ONCE from CloudTalk, compressed (AAC in .m4a, about 0.18 MB a minute, plays on
     every browser and phone) into the PRIVATE bucket `call-audio`. The app hands out one-hour signed links.
  2. a transcript with times, split into turns, each turn labelled us / them
  3. a short AI breakdown (2-3 went well, 1-2 to work on, each with a time) and stats worked out in code
  4. waveform peaks, so the page draws the waveform without decoding audio

It writes one row per call to the shared `docs` table: `callreview/<CloudTalk call id>`.

Which calls: at least MIN_TALK seconds of talk, recorded, and not a voicemail or call screening.

Transcriber is pluggable (TRANSCRIBER env):
  groq      (default) Groq Whisper with segment times. Whisper does not say who spoke, so Claude
            labels each segment from the words; the page says the labels are estimated.
  deepgram  Deepgram Nova-3 with diarize=true: real speaker turns. Needs DEEPGRAM_API_KEY. Not switched on
            until Ife says yes to a new vendor.

PUBLIC REPO RULE: this prints counts and error TYPES only. Never transcript text, names, emails,
phone numbers or audio links.

    python crm/scripts/call_review.py --since 2026-10-09 --limit 15            # dry run: counts only
    python crm/scripts/call_review.py --since 2026-10-09 --limit 15 --commit   # do it
    python crm/scripts/call_review.py --delete <call id> --commit              # remove one call on request

Kept indefinitely (Ife, 16 Oct 2026): there is no retention timer. One call can be deleted on request.

Needs: CLOUD_TALK_API_KEY_ID, CLOUD_TALK_API_KEY, GROQ_API_KEY (or DEEPGRAM_API_KEY), SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, ffmpeg, and the `claude` CLI logged in (CLAUDE_CODE_OAUTH_TOKEN in the cloud).
"""
from __future__ import annotations
import argparse, array, base64, json, os, re, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from live_call_dispositions import (env, ct_get, fetch_calls, contact_bits, groq_keys, rule_verdict,  # noqa: E402
                                    REAL_VERDICTS, WORK)
sys.path.insert(0, str(ROOT / "pipeline"))
from lite_claude import call_claude_lite  # noqa: E402

MIN_TALK = int(os.environ.get("REVIEW_MIN_TALK") or 45)        # seconds of talk before a call is worth reviewing
TRANSCRIBER = (os.environ.get("TRANSCRIBER") or "groq").lower()
BUCKET = "call-audio"
PEAKS = 600
NOT_TALK = {"VOICEMAIL", "NO_ANSWER", "NO_CONVERSATION"}
AUDIO = WORK / "audio"


# ---------------------------------------------------------------- Supabase (service key, server side only)
def sb():
    return env("SUPABASE_URL").rstrip("/"), env("SUPABASE_SERVICE_ROLE_KEY")


def sb_h(extra: dict | None = None) -> dict:
    _, k = sb()
    h = {"apikey": k, "Authorization": f"Bearer {k}"}
    h.update(extra or {})
    return h


def ensure_bucket() -> None:
    url, _ = sb()
    r = requests.get(f"{url}/storage/v1/bucket/{BUCKET}", headers=sb_h(), timeout=30)
    if r.status_code == 200:
        if r.json().get("public"):
            sys.exit("the call-audio bucket is PUBLIC; refusing to upload call audio there")
        return
    r = requests.post(f"{url}/storage/v1/bucket", headers=sb_h({"Content-Type": "application/json"}), timeout=30,
                      json={"id": BUCKET, "name": BUCKET, "public": False, "file_size_limit": 50 * 1024 * 1024,
                            "allowed_mime_types": ["audio/mp4", "audio/m4a", "audio/aac"]})
    if r.status_code >= 300 and "already exists" not in r.text:
        sys.exit(f"could not create the private bucket ({r.status_code})")
    print("created the private bucket call-audio")


def done_ids() -> set[str]:
    url, _ = sb()
    out, start = set(), 0
    while True:
        r = requests.get(f"{url}/rest/v1/docs?coll=eq.callreview&select=id", timeout=30,
                         headers=sb_h({"Range": f"{start}-{start + 999}"}))
        if r.status_code >= 300:
            sys.exit(f"could not read existing reviews ({r.status_code})")
        rows = r.json()
        out |= {x["id"] for x in rows}
        if len(rows) < 1000:
            return out
        start += 1000


def put_doc(coll: str, did: str, data: dict) -> None:
    url, _ = sb()
    r = requests.post(f"{url}/rest/v1/docs?on_conflict=path", timeout=60,
                      headers=sb_h({"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}),
                      json=[{"path": f"{coll}/{did}", "coll": coll, "id": did, "data": data,
                             "updated_at": datetime.now(timezone.utc).isoformat(), "updated_by": "call-review-job"}])
    if r.status_code >= 300:
        raise RuntimeError(f"docs write {r.status_code}")


def upload(path: str, blob: bytes) -> None:
    url, _ = sb()
    r = requests.post(f"{url}/storage/v1/object/{BUCKET}/{path}", data=blob, timeout=120,
                      headers=sb_h({"Content-Type": "audio/mp4", "x-upsert": "true", "Cache-Control": "3600"}))
    if r.status_code >= 300:
        raise RuntimeError(f"audio upload {r.status_code}")


# ---------------------------------------------------------------- audio
def get_wav(cid: str, H: dict) -> Path | None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    wav = AUDIO / f"{cid}.wav"                      # the disposition job may have downloaded it in this same run
    if wav.exists() and wav.stat().st_size > 1000:
        return wav
    r = ct_get(f"/calls/recording/{cid}.json", H)
    if r is None or r.status_code >= 300 or len(r.content) < 1000:
        return None
    wav.write_bytes(r.content)
    return wav


def pcm(wav: Path) -> tuple[array.array, int]:
    """Mono 16-bit samples at 8 kHz, whatever the file was."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                       capture_output=True, timeout=120)
    if p.returncode:
        raise RuntimeError("ffmpeg decode failed")
    a = array.array("h"); a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    return a, 8000


def peaks(a: array.array, n: int = PEAKS) -> list[int]:
    if not a:
        return []
    step = max(1, len(a) // n)
    raw = []
    for i in range(0, step * n, step):
        chunk = a[i:i + step]
        if not chunk:
            break
        # loudness of the bucket: mean absolute value (steadier than the single max on phone audio)
        raw.append(sum(abs(x) for x in chunk[::4]) / max(1, len(chunk[::4])))
    top = sorted(raw)[int(len(raw) * 0.98)] or 1
    return [min(100, round(100 * (v / top) ** 0.7)) for v in raw]


def compress(wav: Path) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "a.m4a"
        p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "24k",
                            "-movflags", "+faststart", str(out)], capture_output=True, timeout=180)
        if p.returncode or not out.exists():
            raise RuntimeError("ffmpeg encode failed")
        return out.read_bytes()


# ---------------------------------------------------------------- transcribers (same output shape: list of {s, e, text, spk?})
def transcribe_groq(wav: Path) -> list[dict]:
    keys, delay, g = groq_keys(), 20.0, None
    for attempt in range(8):
        with open(wav, "rb") as fh:
            g = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", timeout=300,
                              headers={"Authorization": f"Bearer {keys[attempt % len(keys)]}"},
                              files={"file": (wav.name, fh, "audio/wav")},
                              data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json", "language": "en",
                                    "timestamp_granularities[]": "segment"})
        if g.status_code != 429:
            break
        if (attempt + 1) % len(keys) == 0:
            time.sleep(delay); delay = min(delay * 1.6, 240)
    if g is None or g.status_code >= 300:
        raise RuntimeError(f"groq {getattr(g, 'status_code', '?')}")
    segs = []
    for s in g.json().get("segments") or []:
        t = (s.get("text") or "").strip()
        # Whisper invents "Thank you." on silence: drop segments it was itself unsure held speech
        if not t or (s.get("no_speech_prob") or 0) > 0.8:
            continue
        segs.append({"s": round(float(s["start"]), 2), "e": round(float(s["end"]), 2), "text": t})
    return segs


def transcribe_deepgram(wav: Path) -> list[dict]:
    key = env("DEEPGRAM_API_KEY")
    r = requests.post("https://api.deepgram.com/v1/listen", timeout=300, data=wav.read_bytes(),
                      params={"model": "nova-3", "diarize": "true", "utterances": "true", "punctuate": "true",
                              "smart_format": "true", "language": "en"},
                      headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"})
    if r.status_code >= 300:
        raise RuntimeError(f"deepgram {r.status_code}")
    return [{"s": round(u["start"], 2), "e": round(u["end"], 2), "text": u["transcript"].strip(), "spk": int(u.get("speaker", 0))}
            for u in (r.json().get("results") or {}).get("utterances") or [] if u.get("transcript", "").strip()]


TRANSCRIBERS = {"groq": transcribe_groq, "deepgram": transcribe_deepgram}


# ---------------------------------------------------------------- the AI pass: who said each line + a short breakdown
SCHEMA = {
    "type": "object",
    "properties": {
        "is_conversation": {"type": "boolean"},
        "outcome": {"type": "string", "enum": ["MEETING_BOOKED", "SEND_FOLLOW_UP", "NOT_NOW", "CALLBACK", "NOT_INTERESTED",
                                               "DO_NOT_CALL", "WRONG_PERSON", "NO_CONVERSATION"]},
        "speakers": {"type": "array", "items": {"type": "string", "enum": ["us", "them"]}},
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "objection": {"type": ["string", "null"]},
        "reason_at": {"type": ["number", "null"]},
        "went_well": {"type": "array", "items": {"type": "object", "properties": {"t": {"type": "number"}, "title": {"type": "string"}, "text": {"type": "string"}}, "required": ["t", "title", "text"]}},
        "work_on": {"type": "array", "items": {"type": "object", "properties": {"t": {"type": "number"}, "title": {"type": "string"}, "text": {"type": "string"}}, "required": ["t", "title", "text"]}},
        "moments": {"type": "array", "items": {"type": "object", "properties": {"t": {"type": "number"}, "text": {"type": "string"}}, "required": ["t", "text"]}},
    },
    "required": ["is_conversation", "outcome", "speakers", "headline", "summary", "objection", "reason_at", "went_well", "work_on", "moments"],
}

PROMPT = """You are helping a small sales team review one of its own COLD CALLS. Koya places full-time
AI-automation developers inside agencies (an embedded developer for a monthly fee, far cheaper than a local hire).
The caller ("us") is {agent}. The other person ("them") is the prospect{lead}.

Below is the call as numbered segments with their start time in seconds.{diar}

Return JSON:
- is_conversation: false if this is a voicemail, an answering service, call screening or a phone menu with no real person talking back.
- outcome: what happened, one label.
- speakers: one entry per segment, in order, "us" or "them". {spk_rule}
- headline: outcome in a few plain words, then a colon and the single most important reason (max 8 words), e.g. "Send follow-up: real need".
- summary: one or two short plain sentences on what happened. No hype.
- objection: the main objection in a few words, or null.
- reason_at: the second at which the caller first says WHY they are calling (what Koya does), or null if never.
- went_well: 2 or 3 specific things the caller did well, each {{t: second it happens, title: 3-6 words, text: one short sentence quoting or pointing at what was said}}.
- work_on: 1 or 2 specific things to do better next time, same shape. Concrete, never vague ("improve discovery" is not allowed).
- moments: up to 3 key moments in the call (a pain stated, an objection, a next step), each {{t, text}} in a few words.
Every t must be the start second of a real segment. Plain English, no em dashes, no emoji, no scores.

SEGMENTS:
{segs}"""


def first(n: str | None) -> str:
    return (str(n or "").strip().split() or [""])[0]


def ai_pass(segs: list[dict], agent: str, lead: str, diarized: bool) -> dict:
    lines = "\n".join(f"[{i}] {s['s']:.1f}s" + (f" speaker{s['spk']}" if diarized else "") + f": {s['text']}" for i, s in enumerate(segs))
    p = PROMPT.format(agent=first(agent) or "our caller", lead=f" ({first(lead)})" if first(lead) else "",
                      diar=" Speaker numbers come from the recording itself." if diarized else
                      " The recording is mono, so nobody marked who is speaking.",
                      spk_rule="Keep each speaker number on one side." if diarized else
                      "Work it out from the words: the caller introduces Koya, asks the questions about their agency and pitches; the prospect answers about their own business. A greeting like 'Hello?' at the very start is usually them picking up.",
                      segs=lines[:30000])
    out = call_claude_lite(p, json_schema=SCHEMA, model="sonnet", timeout_sec=300, max_retries=1)
    sp = list(out.get("speakers") or [])
    if len(sp) != len(segs):                      # never trust a misaligned list: pad with the last label
        sp = (sp + [sp[-1] if sp else "them"] * len(segs))[:len(segs)]
    out["speakers"] = sp
    return out


# ---------------------------------------------------------------- turns and stats (code, not AI)
OPEN_Q = re.compile(r"^\s*(?:(?:so|and|but|okay|ok|um|uh|well|then|right|yeah|alright|now)[,\s]+)*(what|how|why|which|where|who|tell me|walk me|talk me|describe|when)\b", re.I)


def turns_of(segs: list[dict], who: list[str]) -> list[dict]:
    out = []
    for s, w in zip(segs, who):
        if out and out[-1]["who"] == w and s["s"] - out[-1]["e"] < 2.0 and len(out[-1]["text"]) < 600:
            out[-1]["e"] = s["e"]; out[-1]["text"] += " " + s["text"]
        else:
            out.append({"s": s["s"], "e": s["e"], "who": w, "text": s["text"]})
    return out


def stats_of(turns: list[dict], reason_at) -> dict:
    us = sum(t["e"] - t["s"] for t in turns if t["who"] == "us")
    them = sum(t["e"] - t["s"] for t in turns if t["who"] == "them")
    tot = (us + them) or 1
    # a real question has a few words: "Dennis?" or "Hello?" is a name check, not a question
    qs = [q for t in turns if t["who"] == "us" for q in re.findall(r"[^.?!]*\?", t["text"]) if len(q.split()) >= 3]
    mono = max(((t["e"] - t["s"], t["s"]) for t in turns if t["who"] == "us"), default=(0, 0))
    pick = turns[0]["s"] if turns else 0
    return {"ratio": [round(100 * us / tot), 100 - round(100 * us / tot)], "questions": len(qs),
            "open": sum(1 for q in qs if OPEN_Q.search(q)), "mono": {"len": round(mono[0]), "at": round(mono[1], 1)},
            "reason": None if reason_at is None else {"at": round(float(reason_at), 1), "after": round(max(0.0, float(reason_at) - pick), 1)}}


# ---------------------------------------------------------------- one call
def review(cid: str, row: dict, H: dict, verdict: str | None) -> tuple[str, str]:
    cdr = row.get("Cdr") or {}
    wav = get_wav(cid, H)
    if not wav:
        return cid, "no-recording"
    a, sr = pcm(wav)
    segs = TRANSCRIBERS[TRANSCRIBER](wav)
    if not segs:
        return cid, "empty"
    text = " ".join(s["text"] for s in segs)
    if not verdict and rule_verdict(text):
        return cid, "voicemail"
    ctc = row.get("Contact") or {}
    agent = (row.get("Agent") or {}).get("fullname") or ""
    ai = ai_pass(segs, agent, ctc.get("name") or "", TRANSCRIBER == "deepgram")
    if not ai.get("is_conversation"):
        return cid, "not-a-conversation"
    if TRANSCRIBER == "deepgram":      # map each diarized speaker to the side most of its segments were given
        side = {}
        for s, w in zip(segs, ai["speakers"]):
            side.setdefault(s.get("spk", 0), []).append(w)
        who = [max(set(side[s.get("spk", 0)]), key=side[s.get("spk", 0)].count) for s in segs]
    else:
        who = ai["speakers"]
    turns = turns_of(segs, who)
    blob = compress(wav)
    upload(f"{cid}.m4a", blob)
    try:
        emails, nums = contact_bits(row, H)
    except Exception:
        emails, nums = [], []
    nums = [str(n) for n in (nums or []) if n] + [str(cdr.get("public_external") or "")]
    phone9 = next((re.sub(r"\D", "", n)[-9:] for n in nums if len(re.sub(r"\D", "", n)) >= 9), "")
    doc = {
        "v": 1, "id": cid, "at": cdr.get("started_at"), "dur": round(len(a) / sr, 1), "talk": int(cdr.get("talking_time") or 0),
        "agent": agent, "lead": {"name": ctc.get("name") or "", "company": ctc.get("company") or "", "emails": emails, "phone9": phone9},
        "outcome": verdict if verdict in REAL_VERDICTS else ai.get("outcome"), "outcomeBy": "dispositions" if verdict in REAL_VERDICTS else "review",
        "audio": {"path": f"{cid}.m4a", "bytes": len(blob), "type": "audio/mp4"},
        "peaks": peaks(a),
        "turns": turns,
        "speakers": {"method": "diarized" if TRANSCRIBER == "deepgram" else "estimated"},
        "breakdown": {k: ai.get(k) for k in ("headline", "summary", "objection", "went_well", "work_on", "moments")},
        "stats": stats_of(turns, ai.get("reason_at")),
        "made": {"at": datetime.now(timezone.utc).isoformat(), "by": TRANSCRIBER, "ai": "claude-sonnet"},
    }
    put_doc("callreview", cid, doc)
    return cid, "ok"


# ---------------------------------------------------------------- delete one call on request (Ife 16 Oct: keep calls indefinitely, no timer)
def delete_call(cid: str, commit: bool) -> None:
    """Removes the audio, the review row, its comments and any Game tape clips of it."""
    if not re.fullmatch(r"\d{4,20}", cid):
        sys.exit("a call id is digits only")
    url, _ = sb()
    print(f"delete one call: audio, review, comments and Game tape clips{'' if commit else ' (dry run)'}")
    if not commit:
        return
    requests.delete(f"{url}/storage/v1/object/{BUCKET}", headers=sb_h({"Content-Type": "application/json"}), json={"prefixes": [f"{cid}.m4a"]}, timeout=30)
    requests.delete(f"{url}/rest/v1/docs?path=eq.callreview/{cid}", headers=sb_h(), timeout=30)
    for coll in ("callcomment", "gametape"):
        requests.delete(f"{url}/rest/v1/docs?coll=eq.{coll}&data->>callId=eq.{cid}", headers=sb_h(), timeout=30)
    print("deleted")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="YYYY-MM-DD; default 3 days ago")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--max-ok", type=int, default=0, help="stop once this many reviews are written (0 = no cap)")
    ap.add_argument("--delete", metavar="CALL_ID", help="remove one call's audio, review, comments and clips (on request)")
    a = ap.parse_args()
    if a.delete:
        delete_call(a.delete, a.commit)
        return 0
    if TRANSCRIBER not in TRANSCRIBERS:
        sys.exit(f"TRANSCRIBER must be one of {sorted(TRANSCRIBERS)}")
    since = a.since or (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    tok = base64.b64encode(f"{env('CLOUD_TALK_API_KEY_ID')}:{env('CLOUD_TALK_API_KEY')}".encode()).decode()
    H = {"Authorization": f"Basic {tok}"}

    state_f = WORK / "live_state.json"              # the disposition job's verdicts, when it ran first in the same job
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    rows = fetch_calls(H, since)
    byid = {str((r.get("Cdr") or {}).get("id")): r for r in rows}
    have = done_ids()
    skip_f = WORK / "review_skip.json"              # calls already found to be a voicemail or screening: never transcribed twice
    skipped = json.loads(skip_f.read_text()) if skip_f.exists() else {}

    def worth(cid: str) -> bool:
        cdr = byid[cid].get("Cdr") or {}
        if int(cdr.get("talking_time") or 0) < MIN_TALK or not cdr.get("recorded"):
            return False
        v = (state.get(cid) or {}).get("verdict")
        return v not in NOT_TALK
    cand = sorted([c for c in byid if c not in have and c not in skipped and worth(c)], key=lambda c: (byid[c].get("Cdr") or {}).get("started_at") or "", reverse=True)
    todo = cand[: a.limit]
    print(f"{len(rows)} calls since {since} | {len(have)} already reviewed | {len(cand)} new with {MIN_TALK}s+ of talk | doing {len(todo)} | transcriber {TRANSCRIBER}")
    if not a.commit:
        print("DRY RUN - nothing downloaded, transcribed or written.")
        return 0
    ensure_bucket()

    wrote = [0]

    def one(cid):
        if a.max_ok and wrote[0] >= a.max_ok:
            return cid, "held"
        try:
            out = review(cid, byid[cid], H, (state.get(cid) or {}).get("verdict"))
            if out[1] == "ok":
                wrote[0] += 1
            return out
        except Exception as ex:                      # type only: messages can carry what was said
            return cid, f"error {type(ex).__name__}"

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=3) as ex:    # CloudTalk 429s above ~3 downloads at once
        for i, (cid, res) in enumerate(ex.map(one, todo), 1):
            counts[res] = counts.get(res, 0) + 1
            if res in ("voicemail", "not-a-conversation", "empty"):
                skipped[cid] = res
                skip_f.write_text(json.dumps(skipped))
            print(f"   [{i}/{len(todo)}] {res}", flush=True)
    print("result: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
