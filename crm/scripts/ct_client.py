"""
ct_client.py - ONE way for every job to talk to CloudTalk, so no job can starve the others.

CloudTalk allows 60 requests a minute for the WHOLE company (every job, the Apps Script call watcher and the team app
share it). Every answer, not only a refusal, carries how much is left in the current minute:
    X-CloudTalkAPI-Limit, X-CloudTalkAPI-Remaining, X-CloudTalkAPI-ResetTime (a Unix time)
(checked 16 Oct 2026 on a normal 200). So the pacing is shared without any shared server:
  1. one process never sends faster than CT_MIN_INTERVAL seconds apart (default 1.1 s, about 55 a minute at most);
  2. when the company has CT_RESERVE requests or fewer left in this minute (default 12), a background job waits for the
     reset, leaving that room to the call watcher and to people using the app;
  3. a 429 waits for the reset time CloudTalk gives (never a blind hammer), a dropped connection is retried a few times;
  4. every run has a total WAIT budget (CT_WAIT_BUDGET seconds, default 300). Once it is used up, request() raises
     CTBusy instead of waiting more: the caller saves what it did and leaves the rest for the next run, so a busy hour
     can never turn into a job that runs into its timeout and loses everything.

    from ct_client import CT
    ct = CT.from_env()
    r = ct.get("/calls/index.json", params={"limit": 100, "page": 1})   # a requests.Response
    print(ct.summary())                                                 # counts only, safe for public logs

Thread safe. Logs carry counts only (this repository is public).
"""
from __future__ import annotations
import base64, os, threading, time
from pathlib import Path

import requests

BASE = "https://my.cloudtalk.io/api"
ROOT = Path(__file__).resolve().parent.parent.parent


class CTBusy(RuntimeError):
    """CloudTalk is too busy for this run: stop, keep what is done, the next run carries on."""


class BusyResponse:
    """Stands in for a response when the wait budget is used up, for callers that look at status codes (never sent)."""
    status_code = 429
    text = ""
    content = b""
    headers: dict = {}

    def json(self):
        return {}


def _env(k: str) -> str:
    if os.environ.get(k):
        return os.environ[k]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip()
    return ""


def auth_header() -> dict:
    kid, key = _env("CLOUD_TALK_API_KEY_ID"), _env("CLOUD_TALK_API_KEY")
    if not kid or not key:
        raise SystemExit("CLOUD_TALK_API_KEY_ID / CLOUD_TALK_API_KEY are not set")
    return {"Authorization": "Basic " + base64.b64encode(f"{kid}:{key}".encode()).decode()}


class CT:
    def __init__(self, headers: dict | None = None, *, min_interval: float | None = None, reserve: int | None = None,
                 wait_budget: float | None = None, tries: int = 6, timeout: float = 90):
        self.H = dict(headers or auth_header())
        self.min_interval = float(min_interval if min_interval is not None else (os.environ.get("CT_MIN_INTERVAL") or 1.1))
        self.reserve = int(reserve if reserve is not None else (os.environ.get("CT_RESERVE") or 12))
        self.wait_budget = float(wait_budget if wait_budget is not None else (os.environ.get("CT_WAIT_BUDGET") or 300))
        self.tries, self.timeout = tries, timeout
        self._lock = threading.Lock()
        self._next_at = 0.0          # earliest time the next request may go out (normal spacing)
        self._hold_until = 0.0       # CloudTalk asked us to wait until then (reset time after a 429 or a low budget)
        self._counted_until = 0.0
        self.stats = {"requests": 0, "ok": 0, "http_errors": 0, "refused_429": 0, "dropped": 0, "waits": 0, "waited_s": 0.0,
                      "low_budget_waits": 0, "busy": 0}
        self.last_remaining = None

    @classmethod
    def from_env(cls, **kw) -> "CT":
        return cls(auth_header(), **kw)

    # ------------------------------------------------------------------ pacing
    def _reserve_slot(self) -> None:
        """Wait for this process's turn. Raises CTBusy when waiting would go past the run's wait budget."""
        with self._lock:
            now = time.time()
            hold = max(0.0, self._hold_until - now)                 # CloudTalk said wait (429, low budget, dropped line): counts against the budget
            new = max(0.0, self._hold_until - max(now, self._counted_until))   # several threads waiting out the same pause count it once
            if new > 0 and self.stats["waited_s"] + new > self.wait_budget:
                self.stats["busy"] += 1
                raise CTBusy(f"CloudTalk wait budget used up ({int(self.stats['waited_s'])} s)")
            at = max(now + hold, self._next_at)                     # plus the normal spacing between two requests (free)
            self._next_at = at + self.min_interval
            if new > 0:
                self.stats["waits"] += 1
                self.stats["waited_s"] += new
                self._counted_until = self._hold_until
            wait = at - now
        if wait > 0:
            time.sleep(wait)

    def _push_back(self, seconds: float) -> None:
        with self._lock:
            self._hold_until = max(self._hold_until, time.time() + seconds)

    @staticmethod
    def _reset_in(r) -> float | None:
        try:
            reset = float(r.headers.get("X-CloudTalkAPI-ResetTime") or 0)
            if reset > 0:
                return min(max(reset - time.time(), 0.0) + 1.5, 65.0)
            ra = float(r.headers.get("Retry-After") or 0)
            if ra > 0:
                return min(ra + 1.0, 65.0)
        except (TypeError, ValueError):
            pass
        return None

    def _note_budget(self, r) -> None:
        """Every answer says how much of the company's minute is left: below the reserve, wait for the reset."""
        try:
            rem = r.headers.get("X-CloudTalkAPI-Remaining")
            if rem is None:
                return
            rem = int(rem)
            self.last_remaining = rem
            if rem <= self.reserve:
                wait = self._reset_in(r)
                if wait:
                    self.stats["low_budget_waits"] += 1
                    self._push_back(wait)
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------ requests
    def request(self, method: str, path: str, **kw) -> requests.Response:
        """path is relative to https://my.cloudtalk.io/api, or a full URL. Returns the last answer (which may be an
        HTTP error the caller should look at). Raises CTBusy when the run's wait budget is used up, and
        requests.RequestException when the connection kept dropping."""
        url = path if path.startswith("http") else BASE + path
        kw.setdefault("timeout", self.timeout)
        backoff, last_exc, r = 2.0, None, None
        for _ in range(self.tries):
            self._reserve_slot()
            self.stats["requests"] += 1
            try:
                r = requests.request(method, url, headers=self.H, **kw)
            except requests.exceptions.RequestException as ex:      # CloudTalk stalls or drops connections under load
                self.stats["dropped"] += 1
                last_exc = ex
                if method.upper() not in ("GET", "PUT", "DELETE"):  # a POST may have landed (a created contact): never send it twice
                    raise
                self._push_back(backoff); backoff = min(backoff * 2, 30)
                continue
            last_exc = None
            if r.status_code == 429:
                self.stats["refused_429"] += 1
                self._push_back(self._reset_in(r) or backoff); backoff = min(backoff * 2, 60)
                continue
            self._note_budget(r)
            if r.status_code in (502, 503, 504) and method.upper() == "GET":
                self.stats["http_errors"] += 1
                self._push_back(backoff); backoff = min(backoff * 2, 30)
                continue
            if r.status_code < 300:
                self.stats["ok"] += 1
            else:
                self.stats["http_errors"] += 1
            return r
        if last_exc is not None:
            raise last_exc
        return r

    def get(self, path: str, **kw) -> requests.Response:
        return self.request("GET", path, **kw)

    def summary(self) -> str:
        s = self.stats
        return (f"CloudTalk: {s['requests']} requests, {s['ok']} ok, {s['refused_429']} refused (429), {s['dropped']} dropped, "
                f"{s['http_errors']} errors, waited {int(s['waited_s'])} s over {s['waits']} pauses"
                + (f", stopped early {s['busy']}x (busy)" if s["busy"] else "")
                + (f", company budget left {self.last_remaining}/60" if self.last_remaining is not None else ""))


_shared: CT | None = None
_shared_lock = threading.Lock()


def shared(**kw) -> CT:
    """One client per process, so every module in a run paces against the same clock and budget."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = CT.from_env(**kw)
        return _shared
