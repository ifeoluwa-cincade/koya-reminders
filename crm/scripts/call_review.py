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

Transcriber is pluggable (TRANSCRIBER env; the default stays groq until Ife decides on Gemini):
  gemini    Google Gemini 3.5 Transcribe. Two passes over the same file: one with speaker diarization and word
            times (who spoke, when), one with a short custom vocabulary (Koya, Temi, Emeka, Claude-certified)
            for the best words. The words of the second are laid onto the speakers and times of the first.
            Won the 16 Oct 2026 accuracy test on 4 real calls: nothing dropped, emails right, real speaker turns.
            Needs GEMINI_API_KEY. If Gemini fails on a call, that call falls back to groq (GEMINI_FALLBACK=0
            leaves it for the next run instead). Free-tier keys let Google use the audio for training: use a
            paid (billing on) key for real calls.
  groq      Groq Whisper large-v3 (full, not turbo: turbo dropped whole sentences and invented words in the
            test) with segment times. Whisper does not say who spoke, so Claude labels each segment from the
            words; the page says the labels are estimated. GROQ_PROMPT=1 adds a vocabulary prompt (it fixed
            our names but made Whisper skip whole stretches on 2 of 4 test calls, so it is off).
  deepgram  Deepgram Nova-3 with diarize=true. Needs DEEPGRAM_API_KEY (none exists; not signed up).
Every transcriber's text then goes through NAME_FIXES ("a maker" -> Emeka, "Korea" -> Koya, ...).

PUBLIC REPO RULE: this prints counts and error TYPES only. Never transcript text, names, emails,
phone numbers or audio links.

    python crm/scripts/call_review.py --since 2026-10-09 --limit 15            # dry run: counts only
    python crm/scripts/call_review.py --since 2026-10-09 --limit 15 --commit   # do it
    python crm/scripts/call_review.py --delete <call id> --commit              # remove one call on request
    python crm/scripts/call_review.py --fix-agents --commit                     # set the caller on docs already written

Backfill of the whole history (run locally, slowly: CloudTalk allows 60 requests a minute for the whole company
and the hourly jobs share it):
    python crm/scripts/call_review.py --since 2026-09-01 --calls-file <saved call log> --redo --limit 2000 \
        --pace 6 --quiet-top-of-hour --download-only --commit                       # 1. recordings to disk, slowly
    TRANSCRIBER=gemini GEMINI_FALLBACK=0 python crm/scripts/call_review.py --since 2026-09-01 \
        --calls-file <saved call log> --redo --limit 2000 --workers 2 --pace 6 --quiet-top-of-hour --commit  # 2. reviews (few CloudTalk calls
                                                                                    #    left except contact lookups)
    (AUDIO_CACHE=dir1:dir2 points at recordings already on disk, so nothing is downloaded twice.)

Kept indefinitely (Ife, 16 Oct 2026): there is no retention timer. One call can be deleted on request.

Needs: CLOUD_TALK_API_KEY_ID, CLOUD_TALK_API_KEY, GROQ_API_KEY (or DEEPGRAM_API_KEY), SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY (gemini), ffmpeg, and the `claude` CLI logged in (CLAUDE_CODE_OAUTH_TOKEN in the cloud).
"""
from __future__ import annotations
import argparse, array, base64, json, os, re, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from live_call_dispositions import (env, ct_get, fetch_calls, groq_keys, rule_verdict,  # noqa: E402
                                    REAL_VERDICTS, WORK)
sys.path.insert(0, str(ROOT / "pipeline"))
from lite_claude import call_claude_lite  # noqa: E402

# Seconds of talk before a call is worth a look. 20, not 45 (Ife 16 Oct: "we've had way more conversations than
# this"): a quick "not interested" is a real conversation too. Voicemails and screeners are still dropped, by the
# transcript (rule_verdict, then a short Haiku check on calls under GATE_BELOW seconds, then the Sonnet pass).
MIN_TALK = int(os.environ.get("REVIEW_MIN_TALK") or 20)
GATE_BELOW = 45
TRANSCRIBER = (os.environ.get("TRANSCRIBER") or "groq").lower()
TEAM_START = "2026-10-01"          # calls before this date with no caller on them were Temi's (Ife, 16 Oct 2026)
# Words the transcribers get wrong without help ("Korea", "Quora", "a maker", "cloth certified")
VOCAB = ["Koya", "Temi", "Emeka", "Claude-certified", "AI agency"]
VOCAB_PROMPT = "Cold call from Koya. Callers: Temi, Emeka. We have Claude-certified developers for AI agency owners."
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
        for attempt in range(4):                    # a dropped connection is not "no reviews"
            try:
                r = requests.get(f"{url}/rest/v1/docs?coll=eq.callreview&select=id", timeout=30,
                                 headers=sb_h({"Range": f"{start}-{start + 999}"}))
                break
            except requests.exceptions.RequestException:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
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


# ---------------------------------------------------------------- CloudTalk, gently
# CloudTalk allows 60 requests a minute for the WHOLE company and the hourly jobs share it. A backfill sets
# PACE (seconds between our requests) and QUIET_TOP (no requests in minutes :00-:15, when the hourly jobs run).
PACE = [0.0]
QUIET_TOP = [False]
_ct_lock = threading.Lock()
_ct_last = [0.0]


def ct(path: str, H: dict, **kw):
    with _ct_lock:
        while QUIET_TOP[0] and time.gmtime().tm_min < 15:
            time.sleep(20)
        wait = _ct_last[0] + PACE[0] - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            return ct_get(path, H, **kw)
        finally:
            _ct_last[0] = time.time()


# ---------------------------------------------------------------- audio
def local_audio_dirs() -> list[Path]:
    """Recordings already on disk (AUDIO_CACHE, colon separated): a backfill never downloads a call twice."""
    return [AUDIO] + [Path(p) for p in (os.environ.get("AUDIO_CACHE") or "").split(":") if p]


def get_wav(cid: str, H: dict) -> Path | None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    for d in local_audio_dirs():                    # the disposition job may have downloaded it in this same run
        wav = d / f"{cid}.wav"
        if wav.exists() and wav.stat().st_size > 1000:
            return wav
    wav = AUDIO / f"{cid}.wav"
    r = ct(f"/calls/recording/{cid}.json", H)
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
# Groq's free tier caps AUDIO SECONDS per hour (7,200) and per day (28,800) for the whole organisation, and the
# hourly disposition job shares it. A backfill sets AUDIO_PER_HOUR so it never takes the whole hour.
AUDIO_PER_HOUR = [0]
_au_lock = threading.Lock()
_au_log: list[tuple[float, float]] = []


def audio_budget(secs: float) -> None:
    if not AUDIO_PER_HOUR[0]:
        return
    while True:
        with _au_lock:
            now = time.time()
            _au_log[:] = [x for x in _au_log if now - x[0] < 3600]
            used = sum(x[1] for x in _au_log)
            if used + secs <= AUDIO_PER_HOUR[0] or not _au_log:
                _au_log.append((now, secs))
                return
            wait = 3600 - (now - _au_log[0][0]) + 1
        time.sleep(min(max(wait, 5), 300))


def wav_secs(wav: Path) -> float:
    return max(0.0, (wav.stat().st_size - 44) / 16000)        # CloudTalk recordings: 8 kHz, 16-bit, mono


def transcribe_groq(wav: Path) -> list[dict]:
    audio_budget(max(10.0, wav_secs(wav)))                    # Groq bills at least 10 s a request
    keys, delay, g = groq_keys(), 20.0, None
    model = os.environ.get("GROQ_REVIEW_MODEL") or "whisper-large-v3"
    for attempt in range(8):
        with open(wav, "rb") as fh:
            g = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", timeout=300,
                              headers={"Authorization": f"Bearer {keys[attempt % len(keys)]}"},
                              files={"file": (wav.name, fh, "audio/wav")},
                              data={"model": model, "response_format": "verbose_json", "language": "en",
                                    "timestamp_granularities[]": "segment",
                                    # the vocabulary prompt fixed our names but made Whisper skip whole stretches on 2 of 4
                                    # test calls, so it is off by default; NAME_FIXES repairs the names instead
                                    **({"prompt": VOCAB_PROMPT} if os.environ.get("GROQ_PROMPT") == "1" else {})})
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


GEM = "https://generativelanguage.googleapis.com"


def _gem_post(url: str, **kw) -> requests.Response:
    delay, r = 15.0, None
    for _ in range(6):
        try:
            r = requests.post(url, **kw)
        except requests.exceptions.RequestException:
            time.sleep(delay); delay = min(delay * 1.6, 180)
            continue
        if r.status_code not in (429, 500, 502, 503, 504):
            return r
        time.sleep(delay); delay = min(delay * 1.6, 180)
    if r is None:
        raise RuntimeError("gemini unreachable")
    return r


def _gem_upload(wav: Path, key: str) -> dict:
    data = wav.read_bytes()
    r = _gem_post(f"{GEM}/upload/v1beta/files", timeout=120, json={"file": {"display_name": "call"}},
                  headers={"x-goog-api-key": key, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                           "X-Goog-Upload-Header-Content-Length": str(len(data)), "X-Goog-Upload-Header-Content-Type": "audio/wav",
                           "Content-Type": "application/json"})
    if r.status_code >= 300 or "X-Goog-Upload-URL" not in r.headers:
        raise RuntimeError(f"gemini upload start {r.status_code}")
    r = _gem_post(r.headers["X-Goog-Upload-URL"], timeout=300, data=data,
                  headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize", "Content-Length": str(len(data))})
    if r.status_code >= 300:
        raise RuntimeError(f"gemini upload {r.status_code}")
    return r.json()["file"]


def _gem_run(f: dict, key: str, cfg: dict) -> dict:
    body = {"model": "gemini-3.5-transcribe", "input": [{"type": "audio", "uri": f["uri"], "mime_type": "audio/wav"}],
            "generation_config": {"transcription_config": {"language_codes": ["en-US"], **cfg}}}
    r = _gem_post(f"{GEM}/v1beta/interactions", headers={"x-goog-api-key": key}, json=body, timeout=600)
    if r.status_code >= 300:
        raise RuntimeError(f"gemini {r.status_code}")
    return r.json()


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9@]", "", w.lower())


def merge_words(timed: list[dict], text: str) -> list[dict]:
    """Lay the words of the vocabulary pass (better spelling) onto the speakers and times of the diarized pass.
    Each better word takes the speaker and time of the diarized word it lines up with; where the two passes
    disagree on a stretch, the stretch is mapped proportionally. Returns the better words with spk/s/e."""
    from difflib import SequenceMatcher
    good = text.split()
    if not timed or not good:
        return timed
    a, b = [_norm(w["text"]) for w in timed], [_norm(w) for w in good]
    out: list[dict] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "delete":                             # only the diarized pass heard these: keep 2+ word stretches
            near = "".join(b[max(0, j1 - 4):j1 + 4])  # ...unless it is the same thing spelled differently ("k o y a")
            if i2 - i1 >= 2 and "".join(a[i1:i2]) not in near:
                out += [dict(w) for w in timed[i1:i2]]
            continue
        for k, j in enumerate(range(j1, j2)):
            if op == "equal":
                src = timed[i1 + k]
            elif i2 > i1:                              # replace: spread the new words over the old stretch
                src = timed[i1 + min(i2 - i1 - 1, (k * (i2 - i1)) // max(1, j2 - j1))]
            else:                                      # insert: belongs with the word before it
                src = timed[max(0, i1 - 1)]
            out.append({"text": good[j], "spk": src["spk"], "s": src["s"], "e": src["e"]})
    return out


def transcribe_gemini(wav: Path) -> list[dict]:
    key = env("GEMINI_API_KEY")
    f = _gem_upload(wav, key)
    try:
        diar = _gem_run(f, key, {"mode": {"type": "verbatim", "diarization_mode": "speaker", "timestamp_granularities": ["word"]}})
        try:                                           # vocabulary pass: optional, the diarized words stand alone
            voc = _gem_run(f, key, {"mode": {"type": "verbatim"}, "custom_vocabulary": VOCAB})
            better = " ".join(c.get("text") or "" for st in voc.get("steps") or [] for c in st.get("content") or [] if c.get("type") == "text")
        except Exception:
            better = ""
    finally:
        try:
            requests.delete(f"{GEM}/v1beta/{f['name']}", headers={"x-goog-api-key": key}, timeout=30)
        except requests.exceptions.RequestException:
            pass
    sec = lambda v: float(str(v or "0").rstrip("s") or 0)
    timed = []
    for st in diar.get("steps") or []:
        for c in st.get("content") or []:
            for an in c.get("annotations") or []:
                if an.get("type") == "word_info" and (an.get("text") or "").strip():
                    timed.append({"text": an["text"].strip(), "spk": int(re.sub(r"\D", "", str(an.get("speaker") or "0")) or 0),
                                  "s": sec(an.get("start_offset")), "e": sec(an.get("end_offset"))})
    words = merge_words(timed, better) if better else timed
    # words -> segments: a new segment on a change of speaker, a pause over 1.2 s, or a long run
    segs: list[dict] = []
    for w in words:
        cur = segs[-1] if segs else None
        if cur and cur["spk"] == w["spk"] and w["s"] - cur["e"] < 1.2 and len(cur["text"]) < 400:
            cur["text"] += " " + w["text"]; cur["e"] = max(cur["e"], w["e"])
        else:
            segs.append({"s": round(w["s"], 2), "e": round(w["e"], 2), "text": w["text"], "spk": w["spk"]})
    for x in segs:
        x["e"] = round(x["e"], 2)
    return segs


TRANSCRIBERS = {"gemini": transcribe_gemini, "groq": transcribe_groq, "deepgram": transcribe_deepgram}
DIARIZED = {"gemini", "deepgram"}


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
def agent_of(row: dict) -> str:
    """The caller. CloudTalk's own agent first; only when it names nobody, a call before 1 Oct 2026 was Temi's
    (Ife, 16 Oct: "anyone before October is probably Temi"). After that date an unknown caller stays unknown."""
    ag = row.get("Agent") or {}
    name = (ag.get("fullname") or f"{ag.get('firstname') or ''} {ag.get('lastname') or ''}").strip()
    if name.strip("()").lower() == "unknown":       # CloudTalk writes "(unknown)" when no agent took the call
        name = ""
    if name:
        return name
    return "Temi Fadayomi" if ((row.get("Cdr") or {}).get("started_at") or "")[:10] < TEAM_START else ""


def contact_bits(row: dict, H: dict) -> tuple[list[str], list[str]]:
    """Emails and numbers for the person on the call (the call row sometimes omits them). Paced like every
    other CloudTalk request here."""
    ctc = row.get("Contact") or {}
    emails = [e.strip().lower() for e in (ctc.get("contact_emails") or []) if e]
    nums = list(ctc.get("contact_numbers") or [])
    if not emails and ctc.get("id"):
        r = ct(f"/contacts/show/{ctc['id']}.json", H)
        d = (r.json().get("responseData") or {}) if r is not None and r.status_code < 300 else {}
        emails = [x.get("email", "").lower() for x in (d.get("ContactEmail") or []) if x.get("email")]
        nums = nums or [x.get("public_number") for x in (d.get("ContactNumber") or [])]
    return emails, nums


GATE = {"type": "object", "properties": {"is_conversation": {"type": "boolean"}}, "required": ["is_conversation"]}


def is_talk(segs: list[dict]) -> bool:
    """Cheap check for short calls: did a real person talk back, or was it a voicemail, screener or menu?"""
    lines = "\n".join(s["text"] for s in segs)[:6000]
    out = call_claude_lite("A cold call transcript follows. is_conversation is true only if a real person on the other end "
                           "talked back to the caller (even just to say no). False for a voicemail greeting, an answering "
                           "service, call screening, an automated menu, or nobody there.\n\n" + lines,
                           json_schema=GATE, model="haiku", timeout_sec=120, max_retries=1)
    return bool(out.get("is_conversation"))


# Our own names, as the transcribers mishear them. Only in the phrases a caller says, so nothing else is touched.
NAME_FIXES = [
    (re.compile(r"\b(I'm|I am|it's|it is|this is|my name is|It's|This is|My name is)\s+a\s+maker\b"), r"\1 Emeka"),
    (re.compile(r"\b(?:Timmy|Jimmy|Tammy|Tami|Temmy|Tommy)(\s+(?:from|at)\s+(?:Koya|Korea|Kora|Quora|Coya|Koia|Koyah|Goya))\b"), r"Temi\1"),
    (re.compile(r"\b(from|at|with|called)\s+(?:Korea|Kora|Quora|Coya|Koia|Koyah|Goya|Quail|Kuya)\b"), r"\1 Koya"),
    (re.compile(r"\b(?:cloth|cloud|clawde|clawed|clod|clad)[\s-]+certified\b", re.I), "Claude-certified"),
]


def fix_names(segs: list[dict]) -> list[dict]:
    for x in segs:
        for rx, rep in NAME_FIXES:
            x["text"] = rx.sub(rep, x["text"])
    return segs


def transcribe(wav: Path) -> tuple[str, list[dict]]:
    """Returns (engine used, segments). A Gemini failure on one call falls back to groq, never to nothing."""
    if TRANSCRIBER == "gemini":
        try:
            return "gemini", fix_names(transcribe_gemini(wav))
        except Exception as ex:
            if os.environ.get("GEMINI_FALLBACK", "1") != "1":   # a backfill leaves the call for the next run instead
                raise
            print(f"   gemini failed ({type(ex).__name__}), using groq for this call", flush=True)
            return "groq", fix_names(transcribe_groq(wav))
    return TRANSCRIBER, fix_names(TRANSCRIBERS[TRANSCRIBER](wav))


def review(cid: str, row: dict, H: dict, verdict: str | None, audio_done: bool = False) -> tuple[str, str]:
    cdr = row.get("Cdr") or {}
    wav = get_wav(cid, H)
    if not wav:
        return cid, "no-recording"
    a, sr = pcm(wav)
    engine, segs = transcribe(wav)
    if not segs:
        return cid, "empty"
    text = " ".join(s["text"] for s in segs)
    if not verdict and rule_verdict(text):
        return cid, "voicemail"
    talk = int(cdr.get("talking_time") or 0)
    if not verdict and talk < GATE_BELOW and not is_talk(segs):
        return cid, "not-a-conversation"
    ctc = row.get("Contact") or {}
    agent = agent_of(row)
    diarized = engine in DIARIZED
    ai = ai_pass(segs, agent, ctc.get("name") or "", diarized)
    if not ai.get("is_conversation") and verdict not in REAL_VERDICTS:
        return cid, "not-a-conversation"
    if diarized:                       # map each diarized speaker to the side most of its segments were given
        side = {}
        for s, w in zip(segs, ai["speakers"]):
            side.setdefault(s.get("spk", 0), []).append(w)
        who = [max(set(side[s.get("spk", 0)]), key=side[s.get("spk", 0)].count) for s in segs]
        if len(set(who)) == 1 and len(set(ai["speakers"])) == 2:   # both voices put on one side: trust the words instead
            who, diarized = ai["speakers"], False
    else:
        who = ai["speakers"]
    turns = turns_of(segs, who)
    blob = compress(wav)
    if not audio_done:                 # a redo keeps the audio already in the bucket (same file, same path)
        upload(f"{cid}.m4a", blob)
    try:
        emails, nums = contact_bits(row, H)
    except Exception:
        emails, nums = [], []
    nums = [str(n) for n in (nums or []) if n] + [str(cdr.get("public_external") or "")]
    phone9 = next((re.sub(r"\D", "", n)[-9:] for n in nums if len(re.sub(r"\D", "", n)) >= 9), "")
    doc = {
        "v": 1, "id": cid, "at": cdr.get("started_at"), "dur": round(len(a) / sr, 1), "talk": talk,
        "agent": agent, "lead": {"name": ctc.get("name") or "", "company": ctc.get("company") or "", "emails": emails, "phone9": phone9},
        "outcome": verdict if verdict in REAL_VERDICTS else ai.get("outcome"), "outcomeBy": "dispositions" if verdict in REAL_VERDICTS else "review",
        "audio": {"path": f"{cid}.m4a", "bytes": len(blob), "type": "audio/mp4"},
        "peaks": peaks(a),
        "turns": turns,
        "speakers": {"method": "diarized" if diarized else "estimated"},
        "breakdown": {k: ai.get(k) for k in ("headline", "summary", "objection", "went_well", "work_on", "moments")},
        "stats": stats_of(turns, ai.get("reason_at")),
        "made": {"at": datetime.now(timezone.utc).isoformat(), "by": engine, "ai": "claude-sonnet"},
    }
    put_doc("callreview", cid, doc)
    return cid, "ok"


def fix_agents(rows: list[dict], commit: bool) -> None:
    """Set the caller on review docs already written (Temi before 1 Oct when CloudTalk names nobody)."""
    url, _ = sb()
    byid = {str((r.get("Cdr") or {}).get("id")): r for r in rows}
    r = requests.get(f"{url}/rest/v1/docs?coll=eq.callreview&select=id,data->agent,data->at", headers=sb_h(), timeout=30)
    if r.status_code >= 300:
        sys.exit(f"could not read reviews ({r.status_code})")
    changed = 0
    for d in r.json():
        row = byid.get(d["id"]) or {"Cdr": {"started_at": d.get("at") or ""}}
        want = agent_of(row)
        if want and want != (d.get("agent") or ""):
            changed += 1
            if commit:
                full = requests.get(f"{url}/rest/v1/docs?path=eq.callreview/{d['id']}&select=data", headers=sb_h(), timeout=30).json()[0]["data"]
                full["agent"] = want
                put_doc("callreview", d["id"], full)
    print(f"{len(r.json())} reviews | caller to set on {changed}{'' if commit else ' (dry run)'}")


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
    ap.add_argument("--redo", action="store_true", help="also redo calls already reviewed (same doc id: comments and clips stay)")
    ap.add_argument("--only", default="", help="comma-separated call ids to do (with --redo to redo them)")
    ap.add_argument("--calls-file", help="a saved CloudTalk call log (JSON list) instead of asking CloudTalk again")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pace", type=float, default=0.0, help="seconds between our CloudTalk requests (backfill: 6)")
    ap.add_argument("--quiet-top-of-hour", action="store_true", help="no CloudTalk requests in minutes :00-:15 (hourly jobs run then)")
    ap.add_argument("--fix-agents", action="store_true", help="set the caller on review docs already written")
    ap.add_argument("--download-only", action="store_true", help="only fetch the recordings to disk (slowly), nothing else")
    ap.add_argument("--audio-per-hour", type=int, default=0, help="cap on Groq audio seconds an hour (backfill: 4500; free tier is 7200 shared)")
    a = ap.parse_args()
    if a.delete:
        delete_call(a.delete, a.commit)
        return 0
    if TRANSCRIBER not in TRANSCRIBERS:
        sys.exit(f"TRANSCRIBER must be one of {sorted(TRANSCRIBERS)}")
    PACE[0], QUIET_TOP[0], AUDIO_PER_HOUR[0] = a.pace, a.quiet_top_of_hour, a.audio_per_hour
    since = a.since or (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    tok = base64.b64encode(f"{env('CLOUD_TALK_API_KEY_ID')}:{env('CLOUD_TALK_API_KEY')}".encode()).decode()
    H = {"Authorization": f"Basic {tok}"}

    state_f = WORK / "live_state.json"              # the disposition job's verdicts, when it ran first in the same job
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    if a.calls_file:
        rows = [r for r in json.loads(Path(a.calls_file).read_text()) if ((r.get("Cdr") or {}).get("started_at") or "")[:10] >= since]
    else:
        rows = fetch_calls(H, since)
    if a.fix_agents:
        fix_agents(rows, a.commit)
        return 0
    byid = {str((r.get("Cdr") or {}).get("id")): r for r in rows}
    have = done_ids()
    skip_f = WORK / "review_skip.json"              # calls already found to be a voicemail or screening: never transcribed twice
    skipped = json.loads(skip_f.read_text()) if skip_f.exists() else {}
    only = {x.strip() for x in a.only.split(",") if x.strip()}

    def worth(cid: str) -> bool:
        cdr = byid[cid].get("Cdr") or {}
        if int(cdr.get("talking_time") or 0) < MIN_TALK or not cdr.get("recorded"):
            return False
        v = (state.get(cid) or {}).get("verdict")
        return v not in NOT_TALK

    def wanted(c: str) -> bool:
        if only:
            return c in only and (a.redo or c not in have)
        return (a.redo or c not in have) and c not in skipped and worth(c)
    cand = sorted([c for c in byid if wanted(c)], key=lambda c: (byid[c].get("Cdr") or {}).get("started_at") or "", reverse=True)
    todo = cand[: a.limit]
    local = sum(1 for c in todo if any((d / f"{c}.wav").exists() for d in local_audio_dirs()))
    print(f"{len(rows)} calls since {since} | {len(have)} already reviewed | {len(cand)} to do with {MIN_TALK}s+ of talk"
          f"{' (incl. redo)' if a.redo else ''} | doing {len(todo)} | {local} recordings already on disk | transcriber {TRANSCRIBER}")
    if not a.commit:
        print("DRY RUN - nothing downloaded, transcribed or written.")
        return 0
    if a.download_only:
        got = 0
        for i, c in enumerate(todo, 1):
            got += 1 if get_wav(c, H) else 0
            if i % 25 == 0 or i == len(todo):
                print(f"   downloaded {i}/{len(todo)} ({got} with audio)", flush=True)
        return 0
    ensure_bucket()

    wrote = [0]

    def one(cid):
        if a.max_ok and wrote[0] >= a.max_ok:
            return cid, "held"
        try:
            out = review(cid, byid[cid], H, (state.get(cid) or {}).get("verdict"), audio_done=cid in have)
            if out[1] == "ok":
                wrote[0] += 1
            return out
        except Exception as ex:                      # type only: messages can carry what was said
            return cid, f"error {type(ex).__name__}"

    counts: dict[str, int] = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:    # CloudTalk 429s above ~3 downloads at once
        for i, (cid, res) in enumerate(ex.map(one, todo), 1):
            counts[res] = counts.get(res, 0) + 1
            if res in ("voicemail", "not-a-conversation", "empty"):
                skipped[cid] = res
                skip_f.parent.mkdir(parents=True, exist_ok=True)
                skip_f.write_text(json.dumps(skipped))
            print(f"   [{i}/{len(todo)}] {res}  ({round(time.time() - t0)}s)", flush=True)
    print("result: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
