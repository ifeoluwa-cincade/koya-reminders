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
                if r.status_code != 429:
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
        return r

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
            d = self.req("POST", f"/objects/{obj}/records/query",
                         json={"limit": 500, "offset": off}).json().get("data", [])
            out += d
            if len(d) < 500:
                return out
            off += 500
