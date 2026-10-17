"""
attio_client.py — one shared, fast, rate-limited HTTP client for every Attio script.

Why this exists: the first loader opened a NEW TLS connection per request. Measured on this
machine (behind a VPN) that cost 6,808 ms per call — 0.15 requests/sec, which would have made
migrating 14,300 people a 26-hour job. Reusing a single connection drops it to 220 ms, a 31x
speedup, and 8 workers then saturate Attio's published ceiling of 25 writes/sec.

    26 hours  ->  9.5 minutes

It also centralises the two failure modes we actually hit: 429 rate limiting, and transient
TLS errors from the VPN (SSLEOFError / BAD_RECORD_MAC / TLSV1_ALERT_DECODE_ERROR) which killed
two long runs mid-flight.
"""
from __future__ import annotations
import os, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parent.parent.parent
BASE = "https://api.attio.com/v2"
READ_LIMIT, WRITE_LIMIT = 100, 25      # Attio's published per-second ceilings


def api_key(var: str | None = None) -> str:
    """Which workspace to talk to.

    The trial workspace (Koya GTM / koyatalent) is ATTIO_API_KEY. The PAID workspace Ife created
    is ATTIO_API_KEY_2. Every migration script must be able to point at either, so set
    ATTIO_KEY_VAR=ATTIO_API_KEY_2 (or pass var=) rather than editing .env and risking a write
    landing in the wrong workspace. Writes are hard to undo — Attio has no record-level undo.
    """
    var = var or os.environ.get("ATTIO_KEY_VAR", "ATTIO_API_KEY_2")   # LIVE workspace = Koya Talent. The default was the old Koya GTM key until 2026-10-10 and Q4 loads landed there.
    # In GitHub Actions there is no .env — the key arrives as an environment variable (a secret).
    if os.environ.get(var):
        return os.environ[var].strip()
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(var + "="):
            v = line.split("=", 1)[1].strip()
            if v:
                return v
    raise SystemExit(f"{var} not in .env")


class _Bucket:
    """Simple sliding-window limiter, shared across threads."""
    def __init__(self, per_sec: int):
        self.per_sec = per_sec
        self.hits: deque[float] = deque()
        self.lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.hits and now - self.hits[0] > 1.0:
                    self.hits.popleft()
                if len(self.hits) < self.per_sec:
                    self.hits.append(now)
                    return
                wait = 1.0 - (now - self.hits[0])
            time.sleep(max(wait, 0.01))


class AttioClient:
    def __init__(self, workers: int = 8):
        self.workers = workers
        self._local = threading.local()
        self._read = _Bucket(READ_LIMIT)
        self._write = _Bucket(WRITE_LIMIT)
        self._key = api_key()

    @property
    def session(self) -> requests.Session:
        """One Session per thread, each holding its own warm TLS connection."""
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"Authorization": f"Bearer {self._key}",
                              "Content-Type": "application/json"})
            ad = HTTPAdapter(pool_connections=self.workers * 2,
                             pool_maxsize=self.workers * 2, max_retries=0)
            s.mount("https://", ad)
            self._local.s = s
        return s

    def req(self, method: str, path: str, tries: int = 6, **kw) -> requests.Response:
        (self._write if method.upper() in ("POST", "PUT", "PATCH", "DELETE") else self._read).take()
        delay, last = 1.5, None
        for _ in range(tries):
            try:
                r = self.session.request(method, BASE + path, timeout=90, **kw)
                if r.status_code != 429 and r.status_code < 500:
                    return r
                # Retry-After on Attio is an HTTP-date, not seconds — just back off.
            except requests.exceptions.RequestException as e:
                last = e
                # A broken TLS connection must be discarded, not reused.
                try:
                    self._local.s.close()
                except Exception:
                    pass
                self._local.s = None
            time.sleep(delay)
            delay *= 1.7
        if last:
            raise last
        return r        # the last answer (a 429 or a 5xx) after every retry

    # ---- idempotent note creation ----
    # A POST that times out (or answers 5xx) may still have created the note. Retrying it blindly made duplicate
    # notes. So every note we create carries a stable last line "ref: <marker>", and before each retry we look
    # for a note on the same record with that line. Found = the earlier attempt worked: stop, nothing is sent twice.

    @staticmethod
    def note_ref(marker: str) -> str:
        return f"ref: {marker}"

    def find_note_with_marker(self, parent_object: str, parent_record_id: str, marker: str) -> dict | None:
        """The existing note on this record that carries this marker, or None. Raises if the notes cannot be read
        (the caller must not guess 'not there' and post a second copy)."""
        line, off = self.note_ref(marker), 0
        while True:
            r = self.req("GET", "/notes", params={"parent_object": parent_object, "parent_record_id": parent_record_id,
                                                  "limit": 50, "offset": off})
            if r.status_code >= 300:
                raise RuntimeError(f"Attio notes read failed {r.status_code}")
            data = r.json().get("data", [])
            for n in data:
                text = (n.get("content_plaintext") or "") + "\n" + (n.get("content_markdown") or "")
                if any(l.strip() == line for l in text.splitlines()):
                    return n
            if len(data) < 50 or off >= 1000:
                return None
            off += 50

    def create_note(self, data: dict, marker: str, tries: int = 6, check_first: bool = False):
        """POST /notes at most once in effect. `data` is the note body (parent_object, parent_record_id, title,
        format, content, ...); the marker line is appended to the content. Returns (status_code, note_or_None, existed).
        check_first=True also looks before the first attempt (for callers with no duplicate check of their own)."""
        data = dict(data)
        line = self.note_ref(marker)
        if line not in (data.get("content") or ""):
            data["content"] = ((data.get("content") or "").rstrip() + "\n\n" + line).lstrip()
        po, pid = data["parent_object"], data["parent_record_id"]
        if check_first:
            found = self.find_note_with_marker(po, pid, marker)
            if found:
                return 200, found, True
        delay, last_status = 1.5, None
        for i in range(tries):
            if i:                                      # an earlier attempt failed in a way that may still have written: look first
                time.sleep(delay); delay *= 1.7
                try:
                    found = self.find_note_with_marker(po, pid, marker)
                except Exception:                     # cannot tell: stop rather than risk a second copy
                    break
                if found:
                    return 200, found, True
            try:
                r = self.req("POST", "/notes", tries=1, json={"data": data})
            except requests.exceptions.RequestException:
                last_status = None
                continue
            if r.status_code < 300:
                try:
                    return r.status_code, r.json().get("data"), False
                except ValueError:
                    return r.status_code, None, False
            last_status = r.status_code
            if r.status_code != 429 and r.status_code < 500:
                return r.status_code, None, False     # a plain 4xx: the note was refused, not written
        return (last_status or 599), None, False

    def map(self, fn, items, label: str = ""):
        """Run fn over items across the pool, reporting progress."""
        out, done, total, t0 = [], 0, len(items), time.monotonic()
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for res in ex.map(fn, items):
                out.append(res)
                done += 1
                if done % 25 == 0 or done == total:
                    rate = done / max(time.monotonic() - t0, 0.001)
                    eta = (total - done) / rate if rate else 0
                    print(f"  {label}{done}/{total}  {rate:.1f}/sec  eta {eta/60:.1f} min", flush=True)
        return out

    # convenience
    def query_all(self, obj: str) -> list[dict]:
        out, off = [], 0
        while True:
            d = None
            for attempt in range(6):                       # an error page instead of data is retried, never read as an empty list
                r = self.req("POST", f"/objects/{obj}/records/query", json={"limit": 500, "offset": off})
                try:
                    d = r.json().get("data", []) if r.status_code < 300 else None
                except ValueError:
                    d = None
                if d is not None:
                    break
                time.sleep(2.0 * (attempt + 1))
            if d is None:
                raise RuntimeError(f"Attio kept answering {r.status_code} while reading {obj}")
            out += d
            if len(d) < 500:
                return out
            off += 500
