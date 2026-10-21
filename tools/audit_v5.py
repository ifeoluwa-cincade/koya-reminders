"""Audit v5 — AI-agency ICP classifier for the sales-nav pipeline.

Ported from prospeo-pipeline/tools/audit_v5.py on 2026-06-11. Same 3-stage
engine, different rubric: instead of the broad 17-segment Koya ICP, stage 2c
routes against the original Sourcing Sprints AI-agency ICP (the same
keep/reject classes the v2 audit used). Replaces BOTH find_websites'
Playwright fetch quality issues and tools/audit_categories_v2.py.

  Stage 1 (fetch)     multi-page website scrape per lead (httpx + rescue
                      passes: 30s-retry, http-downgrade, crawl4ai), cached by URL
  Stage 2 (classify)  Haiku 3-step chain — summarize → self-classify → icp-route
  Stage 3 (output)    write audit CSV (cascade disabled, review bucket handles it)

ROUTES:
  leads     ai_agency / ai_consultation / ai_gtm_systems / other_biz  → keep
  excluded  saas_product / off_icp / no_website / dead site           → reject
  review    unknown (unreadable or contradictory)                     → human look

USAGE — pipeline mode (reads phase1_categorized.csv + find_websites_cache.json):

    python tools/audit_v5.py --phase1-pipeline --verdicts yes,maybe \\
                              --output data/working/audit_v5_pipeline.csv

USAGE — ad-hoc CSV (needs a companyWebsite column):

    python tools/audit_v5.py --phase1-pipeline --input <leads.csv> --output <audit.csv>

USAGE — staged (when usage limits are tight):

    python tools/audit_v5.py --stage 1 --input <leads.csv> --batch-id <id>
    python tools/audit_v5.py --stage 2 --input <leads.csv> --batch-id <id>
    python tools/audit_v5.py --stage 3 --input <leads.csv> --batch-id <id> --output <audit.csv>

BILLING: defaults to batched `claude -p` subprocess calls (Max-plan billing,
no API key). Set AUDIT_USE_CLI=0 to use the Anthropic SDK (needs
ANTHROPIC_API_KEY in .env).

CACHE STRATEGY (all under this project's data/):
  data/caches/multipage_audit_cache.json   - keyed by website URL, GLOBAL
  data/caches/audit_summary_cache.json     - Stage 2a (summarize) cache, GLOBAL
  data/caches/audit_judgment_cache.json    - Stage 2b (self-classify) cache, GLOBAL
  data/working/<batch-id>_v5_three_stage.json - per-batch results (resumable)

Stage 2c (ICP-fit) is intentionally NOT cached — the routing rubric is what
we tune most often, and old cached decisions would mask improvements.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import psutil
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.lite_claude import call_claude_lite
load_dotenv(ROOT / ".env")

# ----------------- Constants -----------------

CACHE_DIR        = ROOT / "data" / "caches"
MULTIPAGE_CACHE  = CACHE_DIR / "multipage_audit_cache.json"
SUMMARY_CACHE    = CACHE_DIR / "audit_summary_cache.json"
JUDGMENT_CACHE   = CACHE_DIR / "audit_judgment_cache.json"
ICP_FIT_CACHE    = CACHE_DIR / "audit_icp_fit_cache.json"  # stage 2c cache (keyed by URL)
WORKING_DIR      = ROOT / "data" / "working"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Multi-page fetch (same as v4)
SUBPAGES = [
    ("", "HOMEPAGE"),
    ("/about", "ABOUT"), ("/about-us", "ABOUT"), ("/aboutus", "ABOUT"),
    ("/services", "SERVICES"), ("/what-we-do", "SERVICES"),
    ("/solutions", "SOLUTIONS"),
    ("/products", "PRODUCTS"),
    ("/pricing", "PRICING"),
]
FETCH_TIMEOUT = 8.0
# Caps are env-overridable. The 9000 default truncates mid-page and is the
# single biggest source of wrong classifications (a lead's revenue model often
# appears past char 4,000). Personalization runs should set AUDIT_TOTAL_CAP=14000
# so the evidence layer sees the whole site. Default left unchanged so the main
# pipeline's token cost does not move.
PER_PAGE_CAP = int(os.environ.get("AUDIT_PER_PAGE_CAP", "1800"))
TOTAL_CAP = int(os.environ.get("AUDIT_TOTAL_CAP", "9000"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

HAIKU_CONCURRENCY = 4   # SDK path: each worker runs 3 sequential Haiku calls

# CLI-batched path (default — Max-plan billing via `claude -p` subprocess).
# Re-ports the v4 batching pattern: one CLI invocation processes a list of
# leads and returns an array of results. Amortizes the ~32k system-prompt
# overhead per CLI call across the whole batch, making Max-plan viable.
HAIKU_BATCH_SIZE     = 20   # leads per CLI subprocess call
HAIKU_BATCH_WORKERS = int(os.environ.get("AUDIT_WORKERS", "4"))    # parallel CLI subprocesses per stage — DO NOT raise above 4 on Max plan; 8 drains quota mid-run and causes partial results that look like failures

# Set AUDIT_USE_CLI=0 to revert to the SDK path (API-key billing).
USE_CLI_DEFAULT      = os.environ.get("AUDIT_USE_CLI", "1") == "1"

# ----------------- AI-agency ICP buckets + routing -----------------
# Same keep/reject classes as tools/audit_categories_v2.py (ai_marketing
# retired 2026-05-03 — marketing agencies that merely use AI are off_icp).

AIA_BUCKETS = [
    # ---- Keep (4) — route to "leads" ----
    "ai_agency",
    "ai_consultation",
    "ai_gtm_systems",
    "other_biz",
    # ---- Reject (3) — route to "excluded" ----
    "saas_product",
    "off_icp",
    "no_website",
    # ---- Uncertain — route to "review" ----
    "unknown",
]

AIA_ROUTES = {
    # Keep → leads
    "ai_agency":        "leads",
    "ai_consultation":  "leads",
    "ai_gtm_systems":   "leads",
    "other_biz":        "leads",
    # Reject → excluded
    "saas_product":     "excluded",
    "off_icp":          "excluded",
    "no_website":       "excluded",
    # Uncertain → review
    "unknown":          "review",
}

# Human-readable label per bucket — pushed to Instantly as a custom variable.
AIA_SEGMENT_LABELS = {
    "ai_agency":        "AI agencies",
    "ai_consultation":  "AI consulting firms",
    "ai_gtm_systems":   "AI GTM systems firms",
    "other_biz":        "service businesses",
    # excluded / unknown deliberately have no label — they don't go to a campaign
}

# Bucket → the CRM Sourcing tab's Type dropdown value (column H).
# make_yes_csv.py consumes the audit CSV's `audit_crm_type` column directly.
AIA_CRM_TYPES = {
    "ai_agency":        "AI Agency",
    "ai_consultation":  "AI Consultation",
    "ai_gtm_systems":   "AI GTM Systems",
    "other_biz":        "Other Biz",
}

# ----------------- Stage 2 prompts -----------------

# 2a — SUMMARIZE: same generic factual extraction as v4's three-stage chain.
SUMMARIZE_PROMPT = """You are reading a company's website text. Your job is NOT to classify or judge the company. Your job is to extract a structured factual summary of what the company is and how it sells.

Be neutral. Don't decide if they're a "good lead" or a "bad lead." Just describe what the website says about them.

Return a JSON object with these fields:

- "company_one_liner": one sentence in your own words describing what this company is. Not their tagline — your synthesis.
- "what_they_sell": the concrete thing(s) being sold or offered. Be specific.
- "delivery_model": one of: "saas_self_serve" | "productized_service" | "done_for_you_agency" | "advisory_consulting" | "training_or_courses" | "physical_product" | "hybrid" | "unclear". Pick the BEST single fit. Use "hybrid" only if the page clearly shows two distinct delivery modes.
- "who_they_sell_to": ICP signals from the page — industries, company sizes, roles, geographies they target.
- "ai_centrality": one of: "core" | "peripheral" | "absent". "core" = AI is what they sell. "peripheral" = AI mentioned but not the main offering. "absent" = no real AI in the offering.
- "saas_signals": list of verbatim phrases or UI elements that suggest self-serve SaaS. Empty list if none.
- "service_signals": list of verbatim phrases that suggest custom/agency/consulting work. Empty list if none.
- "physical_product_signals": list of phrases that suggest a physical/tangible product (CPG, apparel, hardware, food, manufacturing). Empty list if none.
- "regulated_signals": list of phrases that suggest a regulated vertical (healthcare/medical, legal/law, financial services, defense, insurance). Empty list if none.
- "company_size_signals": any headcount, team size, founding year, geography hints.
- "ambiguity_notes": anything contradictory, confusing, or thin in the source text. Empty string if the page is clear.

Return ONLY the JSON object. No markdown fences. No preamble.

WEBSITE TEXT:
"""

SUMMARIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "company_one_liner":        {"type": "string"},
        "what_they_sell":           {"type": "string"},
        "delivery_model":           {"enum": [
            "saas_self_serve", "productized_service", "done_for_you_agency",
            "advisory_consulting", "training_or_courses", "physical_product",
            "hybrid", "unclear",
        ]},
        "who_they_sell_to":         {"type": "string"},
        "ai_centrality":            {"enum": ["core", "peripheral", "absent"]},
        "saas_signals":             {"type": "array", "items": {"type": "string"}},
        "service_signals":          {"type": "array", "items": {"type": "string"}},
        "physical_product_signals": {"type": "array", "items": {"type": "string"}},
        "regulated_signals":        {"type": "array", "items": {"type": "string"}},
        "company_size_signals":     {"type": "string"},
        "ambiguity_notes":          {"type": "string"},
    },
    "required": ["company_one_liner", "what_they_sell", "delivery_model",
                 "who_they_sell_to", "ai_centrality", "saas_signals",
                 "service_signals", "physical_product_signals",
                 "regulated_signals", "company_size_signals", "ambiguity_notes"],
    "additionalProperties": False,
}

# 2b — SELF-CLASSIFY: same generic free-form judgment as v4. No buckets imposed.
SELF_CLASSIFY_PROMPT = """You are reviewing a structured summary of a company. Your job is to form your OWN judgment about what kind of company this is — in plain English.

You are NOT picking from a list of categories. You are NOT thinking about whether this is a "good lead" or a "bad lead." You are simply naming what this company is, the way you would explain it to a colleague in one short phrase.

Examples of good natural labels:
- "B2B SaaS platform for legal document review"
- "Done-for-you AI agency building custom GPT chatbots for ecommerce stores"
- "Marketing agency that runs paid social for DTC brands"
- "Multi-location restaurant group, four locations in the Pacific Northwest"
- "Boutique exec search firm focused on fractional CFO placements"
- "Solo creator running a Substack about AI tooling — no team, no service offering"
- "Mid-size manufacturer of industrial coatings, sells to construction contractors"

Return JSON with these fields:

- "natural_label": one short phrase (max 20 words) describing what kind of company this is. Be specific. Use plain language, not jargon. If genuinely hybrid, say so.
- "primary_revenue_model": one short phrase (max 15 words) describing how they make money. Examples: "monthly SaaS subscription", "project-based custom builds", "retainer-based consulting", "course sales", "physical product sales", "ad revenue + paid subscriptions".
- "key_evidence": list of 2-5 specific facts FROM THE SUMMARY that support your natural_label. Each fact should be a short string. Quote summary fields where useful.
- "ai_role": one sentence describing what role AI plays in this business. Is AI core to the offering, peripheral, or just a buzzword? Use the ai_centrality field but explain it in plain English.
- "uncertainty": anything you're genuinely unsure about. Empty string if the picture is clear.

Return ONLY the JSON object. No markdown fences. No preamble.

COMPANY SUMMARY:
"""

SELF_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "natural_label":          {"type": "string"},
        "primary_revenue_model":  {"type": "string"},
        "key_evidence":           {"type": "array", "items": {"type": "string"}},
        "ai_role":                {"type": "string"},
        "uncertainty":            {"type": "string"},
    },
    "required": ["natural_label", "primary_revenue_model", "key_evidence",
                 "ai_role", "uncertainty"],
    "additionalProperties": False,
}

# 2c — AI-AGENCY ICP-FIT: map the natural judgment to a keep/reject bucket + route.
# Rubric carried over from tools/audit_categories_v2.py (94% binary keep accuracy
# on the labeled validation set).
AIA_ICP_FIT_PROMPT = """You are routing an AI-adjacent prospect to one of THREE outcomes for an outbound sourcing pipeline: `leads`, `excluded`, or `review`. You will be given a natural-language description of the company plus key facts. Pick the single best-fit bucket, then route.

THE ICP, IN ONE SENTENCE:
Small founder-led businesses that SELL AI SERVICES to clients (agencies, consultants, done-for-you AI systems) — NOT companies that sell self-serve software products, and NOT businesses where AI is just a buzzword.

KEEP BUCKETS — if the company fits any of these, route to `leads`:

- `ai_agency` — builds AI implementations FOR clients on a contracted basis. Ships concrete AI deliverables: custom AI agents, automation builds, integrations, fractional AI teams that ship code. Signals: case studies of "we built X for Y", "build your AI agent", "AI implementation service", "deploy custom AI for your business". PRODUCTIZED SERVICES (one-time setup + monthly retainer) belong here, not in SaaS.
- `ai_consultation` — advises, coaches, or trains clients on AI without shipping the implementation themselves. Fractional CDO/CAIO advisory, AI literacy programs, AI strategy, AI governance, executive AI coaching, AI roadmap workshops. They TALK about AI / advise on AI; they don't BUILD it for clients.
- `ai_gtm_systems` — AI-powered sales/marketing/outbound automation services for clients. Lead gen automation, AI sales assistants built for clients, AI outreach as a service, AI-powered SDR augmentation. Distinct from a SaaS sales tool because it's done-for-you, not self-serve.
- `other_biz` — not AI-centric but a plausible outbound target: small consultancies, agencies, or solo professionals where the founder might still buy AI services. Use SPARINGLY. If the company does ANY AI work for clients, pick the matching AI bucket instead.

REJECT BUCKETS — route to `excluded`:

- `saas_product` — customer USES self-serve software. Distinguishing test: does the customer log into a dashboard / app / tool the vendor maintains (= SaaS = exclude), or does the vendor ship them a custom build/configuration (= productized service = ai_agency)? Strong SaaS signals: "Sign up free", "Start trial", login button in nav, in-product demo, subscription tiers WITH a self-serve sign-up flow. Tiered pricing ALONE does not = SaaS — only the login/dashboard does.
- `off_icp` — not AI-related at all. Pure web dev, marketing, recruiting, real estate, design, finance — even if they mention AI as a buzzword. The AI must be CENTRAL to the offering, not sprinkled.

REVIEW:
- `unknown` — genuinely can't tell what the company is, contradictory signals, or content too thin to judge.

DECISION TREE (apply in order; first match wins):

1. Self-serve SaaS (login button, free trial, self-checkout subscription, dashboard-as-the-product)? → `excluded` / `saas_product`
2. AI absent or merely sprinkled on a non-AI business (pure marketing / web dev / recruiting / design / finance)? → `excluded` / `off_icp` — UNLESS the company still looks like a small founder-led consultancy or agency that could plausibly buy AI services, then → `leads` / `other_biz` (sparingly).
3. Ships AI builds for clients? → `leads` / `ai_agency`
4. AI-powered sales/marketing/outbound done FOR clients? → `leads` / `ai_gtm_systems`
5. Advises/trains on AI without building? → `leads` / `ai_consultation`
6. Genuinely can't tell? → `review` / `unknown`

CRITICAL DISTINCTIONS (where misclassifications usually happen):

- **ai_agency vs ai_consultation**: do they SHIP code/builds (agency) or ADVISE (consultation)? "We deployed an AI sales bot for Acme" = agency. "We help Acme think about AI strategy" = consultation. Both are keeps; pick the right one.
- **ai_agency vs saas_product**: does the customer get a custom deliverable shipped to them, or do they use a tool the vendor maintains? Productized services (setup fee + retainer for ongoing custom work) ARE ai_agency, not saas_product. An agency whose website ALSO demos an internal tool is still an agency if clients pay the team, not the product.
- **ai_consultation vs off_icp**: an AI strategy consultant = ai_consultation (keep). A general management consultant who mentions AI on one page = off_icp (reject). The test: is AI ADVISORY their core offering?
- **ai_gtm_systems vs off_icp**: a marketing/growth agency that merely USES AI internally to deliver ordinary marketing = off_icp. The keep requires the AI system itself to be the deliverable.

EMPLOYEE COUNT:
The input may include an `employee_count` field. Treat it as a soft signal, not a hard gate: ≤10 is the ICP sweet spot; well over 10 can still route to `leads` but drop confidence a notch; missing or 0 — ignore it and judge from the evidence alone.

CALIBRATION:
- LEAN INTO AI service providers. A company that builds AI automations, agents, or AI solutions FOR clients routes to `leads` even when the offering is broad, partly productized, or has freemium/SaaS-flavored pricing alongside the service. "Worth reaching out" beats "review".
- Don't hide in `review`. Review is ONLY for genuinely unreadable or contradictory pages. Make the call.

Return JSON:
- "route":          "leads" | "excluded" | "review"
- "bucket":         one of: ai_agency | ai_consultation | ai_gtm_systems | other_biz | saas_product | off_icp | unknown
- "segment_label":  short human-readable plural noun for the bucket (e.g. "AI agencies"). Empty string for excluded/review.
- "confidence":     "high" | "medium" | "low"
- "reason":         ONE short sentence (max 25 words) citing natural_label and/or specific key_evidence. Show your work.

Return ONLY the JSON object. No markdown fences. No preamble.

INPUT:
"""

AIA_ICP_FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "route":         {"enum": ["leads", "excluded", "review"]},
        "bucket":        {"enum": [b for b in AIA_BUCKETS if b != "no_website"]},
        "segment_label": {"type": "string"},
        "confidence":    {"enum": ["high", "medium", "low"]},
        "reason":        {"type": "string"},
    },
    "required": ["route", "bucket", "segment_label", "confidence", "reason"],
    "additionalProperties": False,
}


# ----------------- Helpers -----------------

def _norm_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def load_cache(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(path, data):
    # Per-call unique tmp suffix — Stage 2 runs save_cache from N concurrent
    # threads on the same path. Sharing a single ".tmp" name lets workers
    # overwrite each other's tmp file and race on replace(), throwing
    # FileNotFoundError on the loser. PID+uuid keeps each tmp distinct.
    import os, uuid
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_deduped_csv(path):
    with Path(path).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_phase1_with_websites(phase1_csv, websites_cache_path, verdicts=("yes",)):
    """Pipeline mode: join phase1_categorized.csv with find_websites_cache.json
    (cache keyed by phase1 id, entry carries 'website'). Returns v5-shaped
    lead dicts with companyWebsite populated. Leads whose Verdict isn't in
    `verdicts` are dropped; leads with no cached website keep an empty
    companyWebsite and get auto-rejected as no_website downstream."""
    websites = {}
    p = Path(websites_cache_path)
    if p.exists():
        try:
            websites = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write(f"[warn] could not load {p.name}: {exc}\n")
    accepted = tuple(v.strip().lower() for v in verdicts if v.strip())
    leads = []
    with Path(phase1_csv).open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("Verdict") or "").strip().lower() not in accepted:
                continue
            pid = str(r.get("id", "")).strip()
            site = websites.get(pid) or {}
            leads.append({
                "id":             pid,
                "Full Name":      (r.get("Full Name") or "").strip(),
                "LinkedIn URL":   (r.get("LinkedIn URL") or "").strip(),
                "headline":       (r.get("Headline / Title") or "").strip(),
                "companyWebsite": (site.get("website") or "").strip(),
                "Verdict":        (r.get("Verdict") or "").strip(),
                "Category":       (r.get("Category") or "").strip(),
            })
    return leads


def derive_batch_id(input_path):
    name = Path(input_path).stem
    if name.endswith("_deduped"):
        name = name[:-len("_deduped")]
    return name


# ----------------- Stage 1: multi-page fetch -----------------
#
# Layer 1 — diagnostic fetch. Every subpage attempt produces a structured result
# with a status enum, so the downstream classifier and Review tab can tell apart
# "site is dead" from "site is a JS SPA" from "site blocked us". Cache shape is
# {url: {text, status, detail, http_code, fetcher, fetched_at, subpages: [...]}}.
# Legacy {url: "text"} entries are still readable via the _text_from_entry shim
# so existing caches keep working through the transition.

import socket
from datetime import datetime, timezone, timedelta

MIN_TEXT_CHARS = 80
STALE_FAILURE_DAYS = 14   # re-fetch failed URLs older than this; sites come and go

# Worst-to-least bad ordering for the multipage roll-up (when no subpage is ok)
_WORST_ORDER = ["dns_fail", "connect_timeout", "tls_error", "http_4xx",
                "http_5xx", "read_timeout", "empty_shell", "unknown_error"]
_WORST_IDX = {s: i for i, s in enumerate(_WORST_ORDER)}


def _strip_html(html):
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fr(url, status, text, http_code, detail, elapsed_ms):
    return {"url": url, "status": status, "text": text,
            "http_code": http_code, "detail": detail, "elapsed_ms": elapsed_ms}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _multipage_entry(text, status, detail, http_code, fetcher, subpages):
    return {"text": text, "status": status, "detail": detail,
            "http_code": http_code, "fetcher": fetcher,
            "fetched_at": _now_iso(), "subpages": subpages}


def _text_from_entry(entry):
    """Cache backward-compat: dict (new shape) OR string (legacy)."""
    if isinstance(entry, dict):
        return entry.get("text", "")
    if isinstance(entry, str):
        return entry
    return ""


def _status_from_entry(entry):
    if isinstance(entry, dict):
        return entry.get("status", "unknown_error")
    if isinstance(entry, str) and entry:
        return "ok"   # legacy non-empty text = treated as ok
    return "unknown_error"


def _is_stale_failure(entry):
    """A non-ok cache entry older than STALE_FAILURE_DAYS is worth retrying.
    Sites die and come back; the cache shouldn't pin them as dead forever."""
    if not isinstance(entry, dict):
        return False
    if entry.get("status") == "ok":
        return False
    ts_str = entry.get("fetched_at", "")
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts) > timedelta(days=STALE_FAILURE_DAYS)
    except Exception:
        return False


async def _fetch_page(client, url, timeout=FETCH_TIMEOUT):
    """Returns {url, status, text, http_code, detail, elapsed_ms}.

    Status is one of: ok | dns_fail | connect_timeout | read_timeout |
    tls_error | http_4xx | http_5xx | empty_shell | unknown_error.
    """
    t0 = time.monotonic()
    elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)
    try:
        r = await client.get(url, follow_redirects=True, timeout=timeout)
        code = r.status_code
        if 200 <= code < 300:
            text = _strip_html(r.text)[:PER_PAGE_CAP]
            if len(text) >= MIN_TEXT_CHARS:
                return _fr(url, "ok", text, code, "", elapsed_ms())
            return _fr(url, "empty_shell", "", code,
                       f"HTTP 200 but only {len(text)} chars stripped", elapsed_ms())
        if code == 525:
            return _fr(url, "tls_error", "", code, "Cloudflare 525", elapsed_ms())
        if 400 <= code < 500:
            return _fr(url, "http_4xx", "", code, f"HTTP {code}", elapsed_ms())
        if 500 <= code < 600:
            return _fr(url, "http_5xx", "", code, f"HTTP {code}", elapsed_ms())
        return _fr(url, "unknown_error", "", code,
                   f"unexpected status {code}", elapsed_ms())
    except httpx.ConnectTimeout:
        return _fr(url, "connect_timeout", "", None,
                   f"ConnectTimeout after {timeout}s", elapsed_ms())
    except httpx.ConnectError as e:
        cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        msg = str(e)
        is_dns = (isinstance(cause, socket.gaierror)
                  or "Name or service not known" in msg
                  or "nodename nor servname" in msg
                  or "Temporary failure in name resolution" in msg)
        status = "dns_fail" if is_dns else "connect_timeout"
        return _fr(url, status, "", None,
                   f"{type(e).__name__}: {msg[:80]}", elapsed_ms())
    except (httpx.ReadTimeout, httpx.WriteTimeout):
        return _fr(url, "read_timeout", "", None,
                   f"ReadTimeout after {timeout}s", elapsed_ms())
    except httpx.HTTPError as e:
        msg = str(e).lower()
        if "ssl" in msg or "tls" in msg or "handshake" in msg or "certificate" in msg:
            return _fr(url, "tls_error", "", None, str(e)[:80], elapsed_ms())
        return _fr(url, "unknown_error", "", None,
                   f"{type(e).__name__}: {str(e)[:80]}", elapsed_ms())
    except Exception as e:
        return _fr(url, "unknown_error", "", None,
                   f"{type(e).__name__}: {str(e)[:80]}", elapsed_ms())


async def _fetch_multipage(base_url, client):
    """Returns the new multipage cache entry dict, not a string."""
    base = _norm_url(base_url)
    if not base:
        return _multipage_entry("", "unknown_error", "empty url", None, "httpx", [])

    # Homepage first — short-circuit on dead host (saves 8 redundant 8s timeouts)
    homepage = await _fetch_page(client, base)
    if homepage["status"] in ("dns_fail", "connect_timeout"):
        return _multipage_entry("", homepage["status"],
                                f"homepage {homepage['status']}; subpages skipped",
                                homepage["http_code"], "httpx", [homepage])

    # Fan out the rest of the subpages
    rest = [(label, base + path) for path, label in SUBPAGES if path]
    rest_results = await asyncio.gather(*[_fetch_page(client, u) for _, u in rest])

    all_results = [homepage] + list(rest_results)
    all_labels  = ["HOMEPAGE"] + [label for label, _ in rest]

    # Aggregate text (ok subpages only), dedup by label keeping the longest
    by_label = {}
    for label, fr in zip(all_labels, all_results):
        if fr["status"] != "ok":
            continue
        prev = by_label.get(label)
        if prev is None or len(fr["text"]) > len(prev["text"]):
            by_label[label] = fr
    sections = [f"=== {lbl} ===\n{fr['text']}" for lbl, fr in by_label.items()]
    joined = "\n\n".join(sections)[:TOTAL_CAP]

    # Roll up: ok if ANY ok; otherwise worst non-ok wins
    if any(fr["status"] == "ok" for fr in all_results):
        status = "ok"
    else:
        status = min((fr["status"] for fr in all_results),
                     key=lambda s: _WORST_IDX.get(s, 999))

    counts = {}
    for fr in all_results:
        counts[fr["status"]] = counts.get(fr["status"], 0) + 1
    detail = ", ".join(f"{n}x {s}" for s, n in sorted(counts.items()))

    return _multipage_entry(joined, status, detail,
                            homepage["http_code"], "httpx", all_results)


def _print_status_breakdown(cache, websites):
    counts = {}
    for w in websites:
        counts[_status_from_entry(cache.get(w))] = \
            counts.get(_status_from_entry(cache.get(w)), 0) + 1
    total = sum(counts.values())
    print(f"  fetch status breakdown ({total} sites):")
    for st in sorted(counts.keys(), key=lambda s: (-counts[s], s)):
        n = counts[st]
        print(f"    {st:18s} {n:>4d}  ({n/max(total,1)*100:>4.1f}%)")


# ---------------- Layer 2: local rescue paths ----------------
#
# After the diagnostic fetch, retry the recoverable failure modes:
#   empty_shell  -> Crawl4AI (render JS, re-extract). System Chrome on Mac.
#   read_timeout -> retry once with a 30s timeout
#   tls_error    -> retry once on http:// (many 525s have working plain HTTP)
# dns_fail / connect_timeout are NOT retried — they're dead.
# http_4xx / http_5xx are NOT retried locally — different IP / proxy needed.
#
# Each rescue runs sequentially after the parallel httpx fan-out, against only
# the URLs that need it. Crawl4AI imports are soft so a fresh checkout without
# Playwright still runs (just without that rescue path).

CRAWL4AI_CONCURRENCY = 3   # each Playwright instance is ~150 MB RAM


def _reap_orphaned_crawl4ai_processes() -> int:
    """Kill any Playwright/Chrome processes left over from a crawl4ai rescue
    that didn't clean up after itself.

    Why this exists (found 2026-08-04): `_rescue_empty_shell` wraps its browser
    session in `async with AsyncWebCrawler(...)`, which should close the
    browser on exit even when cancelled. But the outer `asyncio.wait_for(...,
    timeout=60)` in `_run_rescues` can cancel the task WHILE that async
    cleanup is still running its own awaits — crawl4ai's shutdown needs
    several awaits of its own (closing the browser context, the driver
    connection, etc.), and a cancellation landing mid-cleanup can cut it off
    before the underlying Chrome subprocess is actually told to exit. Each
    orphaned "browser" is really 5-6 OS processes (main + GPU/renderer/
    network/storage/notification helpers), so a handful of leaked rescues
    compounds into real system load fast — measured hitting a load average of
    41 on an 8-core machine from just two small batches.

    This can't be fixed by being more careful inside the cancelled coroutine —
    once asyncio delivers a cancellation, we can't guarantee our own cleanup
    code finishes running either. The reliable fix is a belt-and-suspenders
    sweep: explicitly find and kill any Chrome/Playwright process tagged with
    crawl4ai/Playwright's own temp profile directory, regardless of why it's
    still alive. Safe because that temp-dir tag only ever appears on processes
    THIS pipeline launched — never the user's own browser session.
    """
    killed = 0
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "playwright_chromiumdev_profile" in cmdline or (
            "playwright/driver/node" in cmdline and "run-driver" in cmdline
        ):
            try:
                proc.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return killed


async def _rescue_read_timeout(client, base_url):
    return await _fetch_multipage_with_timeout(client, base_url, timeout=30.0)


async def _rescue_tls_error(client, base_url):
    base = _norm_url(base_url)
    if not base.startswith("https://"):
        return None
    http_url = "http://" + base[len("https://"):]
    return await _fetch_multipage(http_url, client)


async def _fetch_multipage_with_timeout(client, base_url, timeout):
    """Re-runs _fetch_multipage with a custom per-page timeout via per-call override."""
    # _fetch_page accepts a timeout kwarg; reuse the multipage skeleton inline.
    base = _norm_url(base_url)
    if not base:
        return None
    homepage = await _fetch_page(client, base, timeout=timeout)
    if homepage["status"] in ("dns_fail", "connect_timeout"):
        return _multipage_entry("", homepage["status"],
                                f"homepage {homepage['status']} (retry)",
                                homepage["http_code"], "httpx_retry", [homepage])
    rest = [(label, base + path) for path, label in SUBPAGES if path]
    rest_results = await asyncio.gather(
        *[_fetch_page(client, u, timeout=timeout) for _, u in rest])
    all_results = [homepage] + list(rest_results)
    all_labels  = ["HOMEPAGE"] + [label for label, _ in rest]
    by_label = {}
    for label, fr in zip(all_labels, all_results):
        if fr["status"] != "ok": continue
        prev = by_label.get(label)
        if prev is None or len(fr["text"]) > len(prev["text"]):
            by_label[label] = fr
    sections = [f"=== {lbl} ===\n{fr['text']}" for lbl, fr in by_label.items()]
    joined = "\n\n".join(sections)[:TOTAL_CAP]
    if any(fr["status"] == "ok" for fr in all_results):
        status = "ok"
    else:
        status = min((fr["status"] for fr in all_results),
                     key=lambda s: _WORST_IDX.get(s, 999))
    counts = {}
    for fr in all_results:
        counts[fr["status"]] = counts.get(fr["status"], 0) + 1
    detail = ", ".join(f"{n}x {s}" for s, n in sorted(counts.items()))
    return _multipage_entry(joined, status, detail,
                            homepage["http_code"], "httpx_retry", all_results)


async def _rescue_empty_shell(base_url):
    """Render homepage + /about with a real browser to capture JS-only content.
    Soft-imported so missing Playwright doesn't crash the rest of the pipeline.
    """
    try:
        from crawl4ai import (AsyncWebCrawler, BrowserConfig,
                              CrawlerRunConfig, CacheMode)
    except Exception as exc:
        # Broad catch: crawl4ai may be missing (ImportError) OR installed-but-broken
        # (TypeError on Python 3.9 due to PEP-604 `X | None` type hints in its code).
        # Either way, soft-degrade — empty_shell sites just lose the rescue.
        sys.stderr.write(f"[crawl4ai] rescue unavailable: {type(exc).__name__}: {str(exc)[:120]}\n")
        return None
    base = _norm_url(base_url)
    if not base:
        return None
    cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS,
                            page_timeout=20000,
                            wait_until="domcontentloaded",
                            word_count_threshold=10)
    # Use system Chrome to bypass the Playwright Chromium CDN download issue
    browser_cfg = BrowserConfig(headless=True, verbose=False,
                                browser_type="chromium", chrome_channel="chrome")
    try:
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            results = []
            for path in ("", "/about"):
                try:
                    r = await crawler.arun(url=base + path, config=cfg)
                    results.append((path, r))
                except Exception:
                    results.append((path, None))
    except Exception as e:
        return _multipage_entry("", "unknown_error",
                                f"crawl4ai launch failed: {type(e).__name__}",
                                None, "crawl4ai", [])

    sections, subpages = [], []
    for path, r in results:
        if r is None or not getattr(r, "success", False):
            subpages.append({"url": base + path, "status": "unknown_error",
                             "text": "", "http_code": None,
                             "detail": "crawl4ai failed", "elapsed_ms": 0})
            continue
        md = (r.markdown or "")[:PER_PAGE_CAP]
        if len(md) >= MIN_TEXT_CHARS:
            label = "HOMEPAGE" if path == "" else "ABOUT"
            sections.append(f"=== {label} ===\n{md}")
            subpages.append({"url": base + path, "status": "ok",
                             "text": md, "http_code": getattr(r, "status_code", None),
                             "detail": "", "elapsed_ms": 0})
        else:
            subpages.append({"url": base + path, "status": "empty_shell",
                             "text": "", "http_code": getattr(r, "status_code", None),
                             "detail": f"crawl4ai got {len(md)} chars",
                             "elapsed_ms": 0})

    joined = "\n\n".join(sections)[:TOTAL_CAP]
    status = "ok" if joined else "empty_shell"
    detail = ("recovered via crawl4ai" if joined
              else "crawl4ai also returned empty (truly JS-walled or proprietary)")
    return _multipage_entry(joined, status, detail, None, "crawl4ai", subpages)


async def _run_rescues(cache, client, only=None):
    """After Stage 1's first pass, retry the recoverable failure buckets.

    `only` = the websites of the CURRENT run. The cache is global (60k+ urls
    from every past batch), so without this the rescue pass walks all of them
    and a 1,000-lead batch spends hours re-crawling other batches' dead sites.
    Bug found 2026-09-20; callers should always pass it.
    """
    scope = cache.items() if only is None else [(w, cache[w]) for w in set(only) if w in cache]
    if only is not None and len(scope) < len(cache):
        print(f"  rescue scope: {len(scope)} sites from this run (cache holds {len(cache)})")
    to_rescue_browser  = [w for w, e in scope
                           if isinstance(e, dict) and e.get("status") == "empty_shell"]
    to_rescue_timeout  = [w for w, e in scope
                           if isinstance(e, dict) and e.get("status") == "read_timeout"]
    to_rescue_tls      = [w for w, e in scope
                           if isinstance(e, dict) and e.get("status") == "tls_error"]

    if not (to_rescue_browser or to_rescue_timeout or to_rescue_tls):
        return cache

    print(f"  rescue passes: empty_shell={len(to_rescue_browser)} "
          f"read_timeout={len(to_rescue_timeout)} tls_error={len(to_rescue_tls)}")

    # read_timeout retries (cheap, run in parallel)
    if to_rescue_timeout:
        results = await asyncio.gather(*[_rescue_read_timeout(client, w)
                                          for w in to_rescue_timeout])
        rescued = 0
        for w, new_entry in zip(to_rescue_timeout, results):
            if new_entry and new_entry["status"] == "ok":
                cache[w] = new_entry; rescued += 1
        print(f"    read_timeout rescue: {rescued}/{len(to_rescue_timeout)} recovered")

    # tls_error http:// downgrade (cheap, parallel)
    if to_rescue_tls:
        results = await asyncio.gather(*[_rescue_tls_error(client, w)
                                          for w in to_rescue_tls])
        rescued = 0
        for w, new_entry in zip(to_rescue_tls, results):
            if new_entry and new_entry["status"] == "ok":
                cache[w] = new_entry; rescued += 1
        print(f"    tls_error rescue: {rescued}/{len(to_rescue_tls)} recovered")

    # empty_shell -> Crawl4AI (heaviest, gate concurrency)
    if to_rescue_browser and os.environ.get("AUDIT_SKIP_CRAWL4AI") == "1":
        print(f"    empty_shell rescue: skipped (AUDIT_SKIP_CRAWL4AI=1)")
        to_rescue_browser = []
    if to_rescue_browser:
        # Sweep first: a prior run's timed-out rescues may have left orphaned
        # Chrome processes running from before this call even started.
        n_pre = _reap_orphaned_crawl4ai_processes()
        if n_pre:
            sys.stderr.write(f"[crawl4ai] reaped {n_pre} orphaned process(es) from a prior run before starting\n")
        sem = asyncio.Semaphore(CRAWL4AI_CONCURRENCY)
        async def go(w):
            # Per-site hard timeout. A hung crawl4ai browser (startup/teardown
            # deadlock — not covered by page_timeout) would otherwise block the
            # whole asyncio.gather forever. On timeout, skip that one site.
            async with sem:
                try:
                    return await asyncio.wait_for(_rescue_empty_shell(w), timeout=60)
                except asyncio.TimeoutError:
                    sys.stderr.write(f"[crawl4ai] rescue timed out (60s), skipping: {w}\n")
                    # NOTE: don't sweep here — up to CRAWL4AI_CONCURRENCY-1
                    # sibling rescues can still be legitimately mid-fetch, and
                    # the sweep can't tell "this site's orphan" from "that
                    # site's still-working browser". Only safe to sweep before
                    # this batch starts and after it fully completes (below),
                    # when nothing legitimate should still be running.
                    return None
                except Exception as exc:
                    sys.stderr.write(f"[crawl4ai] rescue error {type(exc).__name__}, skipping: {w}\n")
                    return None
        results = await asyncio.gather(*[go(w) for w in to_rescue_browser])
        # Final sweep: catches anything that leaked without hitting the
        # per-site timeout path (e.g. a crash inside crawl4ai's own cleanup).
        n_post = _reap_orphaned_crawl4ai_processes()
        if n_post:
            sys.stderr.write(f"[crawl4ai] reaped {n_post} orphaned process(es) after the batch\n")
        rescued = 0
        c4ai_available = any(r is not None for r in results)
        if not c4ai_available:
            print(f"    empty_shell rescue: skipped (crawl4ai not installed)")
        else:
            for w, new_entry in zip(to_rescue_browser, results):
                if new_entry and new_entry["status"] == "ok":
                    cache[w] = new_entry; rescued += 1
                elif new_entry is not None:
                    # Keep the diagnostic, just update detail
                    cache[w]["detail"] += " | crawl4ai also empty"
            print(f"    empty_shell rescue: {rescued}/{len(to_rescue_browser)} recovered via crawl4ai")

    return cache



RESCUE_OVERLAYS = ("jina_rescue_cache.json", "exa_rescue_cache.json", "crawl4ai_rescue_cache.json")
_FETCH_FAILED = {"connect_timeout", "read_timeout", "tls_error", "http_4xx", "http_5xx",
                 "empty_shell", "unknown_error", "dns_fail"}


def _apply_rescue_overlays(cache):
    """Read-only overlay (Ife 2026-10-07: 'why don't we have a separate cache for that?').

    The rescue tools (Jina Reader / Exa / crawl4ai) write ONLY to their own cache files. Here, in memory, any url this
    cache holds as a failed fetch is replaced by the rescued page text, so no tool ever has to rewrite the shared
    multipage cache and nothing needs to be paused while an audit runs. First overlay with usable text wins.
    """
    import json as _json
    overlays = []
    for name in RESCUE_OVERLAYS:
        p = CACHE_DIR / name
        try:
            overlays.append((name.split("_")[0], _json.loads(p.read_text())))
        except Exception:
            continue
    n = 0
    for url, e in list(cache.items()):
        if not isinstance(e, dict) or e.get("status") not in _FETCH_FAILED:
            continue
        for tag, ov in overlays:
            v = ov.get(url)
            text = (v.get("text") if isinstance(v, dict) and v.get("status") == "ok" else v if isinstance(v, str) else "") or ""
            if len(text) >= 300:
                cache[url] = {"text": text if text.startswith("===") else "=== HOMEPAGE ===\n" + text, "status": "ok",
                              "detail": f"{tag} rescue (overlay)", "http_code": "200", "fetcher": tag,
                              "fetched_at": _now_iso(), "subpages": []}
                n += 1
                break
    if n:
        print(f"  rescue overlays applied in memory: {n} unreadable sites now have page text", flush=True)
    return cache


async def stage1_fetch(leads):
    cache = _apply_rescue_overlays(load_cache(MULTIPAGE_CACHE))
    websites = sorted(set((l.get("companyWebsite") or "").strip()
                          for l in leads if l.get("companyWebsite")))

    # Re-fetch if: missing, legacy-empty-string, or stale-failure
    todo = []
    n_stale = 0
    for w in websites:
        entry = cache.get(w)
        if entry is None:
            todo.append(w)
        elif isinstance(entry, str) and not entry:
            todo.append(w)
        elif _is_stale_failure(entry):
            todo.append(w); n_stale += 1
    cached_n = len(websites) - len(todo)
    print(f"  websites: {len(websites)} unique, {len(todo)} to fetch "
          f"({cached_n} cached; of which {n_stale} stale-failure retries)")
    if not todo and not any(isinstance(cache.get(w), dict)
                             and cache.get(w, {}).get("status")
                                 in ("empty_shell", "read_timeout", "tls_error")
                             for w in websites):
        _print_status_breakdown(cache, websites)
        return cache

    sem = asyncio.Semaphore(int(os.environ.get("AUDIT_FETCH_PARALLEL", "15")))  # 40 choked the VPN: live sites came back connect_timeout (2026-10-04)
    async with httpx.AsyncClient(headers={"User-Agent": UA},
                                  timeout=FETCH_TIMEOUT,
                                  follow_redirects=True) as client:
        if todo:
            async def go(w, idx):
                async with sem:
                    entry = await _fetch_multipage(w, client)
                    cache[w] = entry
                    if (idx + 1) % int(os.environ.get("AUDIT_SAVE_EVERY", "50")) == 0:   # each save rewrites the ~440MB cache and blocks the event loop (w1018: set 400)
                        print(f"    [{idx+1}/{len(todo)}] cached")
                        save_cache(MULTIPAGE_CACHE, cache)
            await asyncio.gather(*[go(w, i) for i, w in enumerate(todo)])
            save_cache(MULTIPAGE_CACHE, cache)

        # Layer 2 — local rescue passes
        await _run_rescues(cache, client, only=websites)
        save_cache(MULTIPAGE_CACHE, cache)

    _print_status_breakdown(cache, websites)
    return cache


# ----------------- Stage 2: Haiku 3-step chain -----------------

# --- Anthropic SDK adapter ---
# Ported from `claude` CLI on 2026-05-17 for the cloud port. Lazy singleton +
# schema-constraint stripping (output_config.format rejects min/max).
_anthropic_client = None


def _strip_unsupported_schema_constraints(node):
    """Recursively strip JSON schema constraints unsupported by Anthropic's
    output_config.format: numerical (minimum/maximum/multipleOf) and string
    (minLength/maxLength) constraints. Schemas in this file don't currently
    use these — included for safety + parity with rank_posts_test.py."""
    if isinstance(node, dict):
        cleaned = {}
        for k, v in node.items():
            if k in ("minimum", "maximum", "multipleOf", "minLength", "maxLength",
                     "minItems", "maxItems", "minProperties", "maxProperties"):
                continue
            cleaned[k] = _strip_unsupported_schema_constraints(v)
        return cleaned
    if isinstance(node, list):
        return [_strip_unsupported_schema_constraints(v) for v in node]
    return node


def _get_anthropic_client():
    """Singleton anthropic.Anthropic client. Reads ANTHROPIC_API_KEY from env
    (load_dotenv has already loaded this project's .env at module init).
    Falls back to checking the w11-w12-pipeline sibling .env so callers can
    keep the key in one place."""
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Fall back to the w11-w12-pipeline sibling .env (parent dir)
        sibling_env = ROOT.parent / "w11-w12-pipeline" / ".env"
        if sibling_env.exists():
            try:
                from dotenv import dotenv_values
                values = dotenv_values(str(sibling_env))
                api_key = values.get("ANTHROPIC_API_KEY")
            except Exception:
                pass
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing — add it to prospeo-pipeline/.env "
            "or w11-w12-pipeline/.env. Stage 6 audit was ported from the "
            "`claude` CLI to the Anthropic SDK on 2026-05-17."
        )
    _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


def _call_claude(prompt, schema, timeout_sec=120):
    """Run a single Haiku call with a JSON schema. Returns (result_dict, error_str).

    Ported from `claude` CLI to the Anthropic SDK on 2026-05-17. Uses
    output_config.format with a json_schema to constrain output (same
    behavior as the old --json-schema CLI flag).
    """
    import anthropic
    try:
        client = _get_anthropic_client()
    except RuntimeError as exc:
        return None, str(exc)
    api_schema = _strip_unsupported_schema_constraints(schema)
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": api_schema}},
            timeout=float(timeout_sec),
        )
    except anthropic.APIError as exc:
        return None, f"API error: {exc}"
    except Exception as exc:
        return None, f"call failed: {exc}"

    # output_config.format guarantees first text block contains valid JSON
    try:
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text), ""
    except StopIteration:
        return None, "no text block in response"
    except json.JSONDecodeError as exc:
        return None, f"decode fail: {exc}"


# ----------------- CLI-batched LLM path (Max-plan billing) -----------------
#
# Re-ports v4's batched-CLI pattern: each `claude -p` subprocess processes a
# list of N leads and returns an array of N results. Two reasons this beats
# the per-lead CLI approach the v5 port abandoned:
#   1. Anthropic auth via keychain (Max plan), not API key — no API spend.
#   2. The ~32k token system-prompt overhead per CLI call amortizes across N
#      leads instead of being paid per lead.
#
# A minimal `--system-prompt` is passed so Claude Code doesn't load CLAUDE.md
# / skills / hooks (which would push cache_creation from ~32k to ~80k).

_CLI_SYSTEM_PROMPT = (
    "You are a JSON output engine. Return ONLY a JSON object matching the "
    "schema. No prose, no markdown. Each input item carries an 'id' — every "
    "result you return must include the same 'id' so the caller can match "
    "results back to inputs."
)


def _make_batched_schema(item_schema):
    """Wrap a per-item schema as {results: [items_with_id]}.

    Adds an 'id' string field to the per-item schema (required) so the model
    must echo the id with each result. Strips unsupported constraints first.
    """
    base = _strip_unsupported_schema_constraints(item_schema)
    wrapped_item = json.loads(json.dumps(base))  # deep copy
    props = wrapped_item.setdefault("properties", {})
    props["id"] = {"type": "string"}
    req = wrapped_item.setdefault("required", [])
    if "id" not in req:
        wrapped_item["required"] = list(req) + ["id"]
    return {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": wrapped_item},
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _call_claude_cli_batch(items, base_prompt, item_schema, timeout_sec=600,
                           model=HAIKU_MODEL):
    """Batched `claude -p` subprocess call. Returns ({id: result_dict}, err_str).

    items: list of dicts, each with an 'id' field (str) plus content fields.
    base_prompt: the prompt header (without trailing input — this fn appends).
    item_schema: the per-item output schema (id is auto-added).

    Returns:
        ({id: result_minus_id}, "")    on success
        ({}, "<error message>")         on failure
    """
    if not items:
        return {}, ""
    batched_schema = _make_batched_schema(item_schema)
    array_tail = (
        "\n\n"
        "You will be given a list of items below, each with an 'id' field plus "
        "content fields. Process EACH item independently and return one result "
        "per item. Each result MUST include the same 'id' as its input item.\n\n"
        "INPUT ITEMS:\n"
        + json.dumps(items, ensure_ascii=False)
    )
    prompt = base_prompt + array_tail
    try:
        so = call_claude_lite(
            prompt, json_schema=batched_schema, model=model,
            timeout_sec=timeout_sec, max_retries=0,
            system_prompt=_CLI_SYSTEM_PROMPT,
        )
    except Exception as exc:
        return {}, f"CLI call failed: {exc!r}"
    results_list = so.get("results", []) if isinstance(so, dict) else []
    out = {}
    for r in results_list:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if rid is None:
            continue
        out[str(rid)] = {k: v for k, v in r.items() if k != "id"}
    return out, ""


def _run_batched_stage(items, base_prompt, item_schema, *, label, batch_size=None,
                       workers=None, timeout_sec=600, model=HAIKU_MODEL, on_batch=None):
    """Run a batched-CLI stage across `items` in chunks of batch_size with
    `workers` parallel subprocesses. Returns ({id: result_dict}, errors).

    items: must have 'id' field. The caller assembles per-item payloads.
    """
    if not items:
        return {}, []
    bs = batch_size or HAIKU_BATCH_SIZE
    wk = workers or HAIKU_BATCH_WORKERS
    batches = [items[i:i + bs] for i in range(0, len(items), bs)]
    print(f"  [{label}] {len(items)} items -> {len(batches)} batches of {bs}, "
          f"workers={wk}")
    out = {}
    errors = []
    t0 = time.time()
    done_n = 0
    with ThreadPoolExecutor(max_workers=wk) as ex:
        futs = {ex.submit(_call_claude_cli_batch, b, base_prompt, item_schema,
                          timeout_sec, model): b for b in batches}
        for fut in as_completed(futs):
            results, err = fut.result()
            done_n += 1
            if err:
                errors.append(err)
                sys.stderr.write(f"    [{label}] batch err: {err}\n")
            out.update(results)
            if on_batch and results:
                # save after EVERY batch so a stop/restart never loses a whole
                # stage (2026-10-05: killing mid-stage threw away ~40 min of 2b)
                on_batch(results)
            if done_n % 5 == 0 or done_n == len(batches):
                el = time.time() - t0
                rate = done_n / el if el > 0 else 0
                eta = (len(batches) - done_n) / rate if rate > 0 else 0
                print(f"    [{label}] {done_n}/{len(batches)} batches done "
                      f"({el:.0f}s elapsed, eta {eta:.0f}s, {len(out)} results)")
    return out, errors


def _summarize(website_text, cache_key):
    cache = load_cache(SUMMARY_CACHE)
    if cache_key and cache_key in cache:
        return cache[cache_key], ""
    result, err = _call_claude(SUMMARIZE_PROMPT + website_text, SUMMARIZE_SCHEMA)
    if cache_key and result and not err:
        cache[cache_key] = result
        save_cache(SUMMARY_CACHE, cache)
    return result, err


def _self_classify(summary, url, linkedin_headline, cache_key):
    cache = load_cache(JUDGMENT_CACHE)
    if cache_key and cache_key in cache:
        return cache[cache_key], ""
    payload = {
        "homepage_url":      url,
        "linkedin_headline": linkedin_headline,
        "summary":           summary,
    }
    result, err = _call_claude(SELF_CLASSIFY_PROMPT + json.dumps(payload, ensure_ascii=False),
                                SELF_CLASSIFY_SCHEMA)
    if cache_key and result and not err:
        cache[cache_key] = result
        save_cache(JUDGMENT_CACHE, cache)
    return result, err


def _aia_icp_fit(natural_judgment, url, linkedin_headline, employee_count=""):
    """NOT cached — routing rubric tunes often, stale cache would mask improvements."""
    payload = {
        "homepage_url":      url,
        "linkedin_headline": linkedin_headline,
        "employee_count":    employee_count,
        "company_judgment":  natural_judgment,
    }
    return _call_claude(AIA_ICP_FIT_PROMPT + json.dumps(payload, ensure_ascii=False),
                         AIA_ICP_FIT_SCHEMA)


# ---------------- Layer 2.5: Apify-data fallback ----------------
#
# When the website fetch returns empty (SPA shell, 403, dead, etc.), we fall
# back to the Apify-enriched fields gathered upstream. This is strictly thinner
# signal than reading the actual website, so the prompt makes that explicit and
# tells Haiku to lean toward `review` if uncertain. The cost is ONE Haiku call
# instead of three.

APIFY_FALLBACK_PROMPT = """You are routing an AI-adjacent prospect to one of THREE outcomes for an outbound sourcing pipeline: `leads`, `excluded`, or `review`. You are given THIN third-party data (not the company's own website — their website fetch failed). Be appropriately cautious: prefer `review` over a confident misroute.

THE ICP, IN ONE SENTENCE:
Small founder-led businesses that SELL AI SERVICES to clients (agencies, consultants, done-for-you AI systems) — NOT companies that sell self-serve software products, and NOT businesses where AI is just a buzzword.

KEEP BUCKETS (route to `leads` if a clear fit):
- `ai_agency` (builds AI implementations for clients — agents, automations, integrations)
- `ai_consultation` (advises/trains on AI without building — strategy, governance, coaching)
- `ai_gtm_systems` (AI-powered sales/marketing/outbound done FOR clients)
- `other_biz` (not AI-centric but a plausible small consultancy/agency target — use sparingly)

REJECT BUCKETS (route to `excluded` if clearly fits):
- `saas_product` (self-serve SaaS with login + free trial + dashboard product)
- `off_icp` (not AI-related at all — pure web dev, marketing, recruiting, real estate, design, finance; AI as a buzzword doesn't count)

REVIEW:
- `unknown` — anything you can't confidently place, or where the description is too vague.

HOW TO USE THIN INPUT:
- A 1-2 sentence LinkedIn description and a headline are usually enough to flag the obvious exclusions (SaaS product, clearly non-AI business) — do that aggressively.
- For keep buckets, only route to `leads` if the description AND headline are clearly consistent with selling AI services to clients. If the description is generic ("we help businesses grow") with no specific service, route to `review`.
- Confidence MUST reflect the thin input: prefer `low` confidence unless the input is unusually concrete.

Return JSON:
- "route":          "leads" | "excluded" | "review"
- "bucket":         one of: ai_agency | ai_consultation | ai_gtm_systems | other_biz | saas_product | off_icp | unknown
- "segment_label":  short human-readable plural noun (e.g. "AI agencies"). Empty for excluded/review.
- "confidence":     "high" | "medium" | "low" — keep low unless input is unusually concrete
- "reason":         ONE short sentence citing the specific phrase from the description/headline you relied on. Show your work.

Return ONLY the JSON object. No markdown fences. No preamble.

INPUT (third-party data, not the company's own website):
"""


def _classify_from_apify_fields(url, linkedin_headline, company_description,
                                 industry, employee_count, fetch_status):
    """Single Haiku call using Apify-enriched fields as the fallback signal.
    Returns the same flat dict shape as the 3-stage chain emits, or None on error.
    """
    payload = {
        "homepage_url":         url,
        "linkedin_headline":    linkedin_headline,
        "company_description":  company_description,
        "industry":             industry,
        "employee_count":       employee_count,
        "fetch_status":         fetch_status,
    }
    fit, err = _call_claude(
        APIFY_FALLBACK_PROMPT + json.dumps(payload, ensure_ascii=False),
        AIA_ICP_FIT_SCHEMA,
    )
    if err or not fit:
        return None
    bucket = fit.get("bucket", "unknown")
    return {
        "route":                fit.get("route") or AIA_ROUTES.get(bucket, "review"),
        "audit_classification": bucket,
        "audit_segment_label":  fit.get("segment_label") or AIA_SEGMENT_LABELS.get(bucket, ""),
        "audit_confidence":     fit.get("confidence", "low"),
        "audit_reason":         f"[apify-fallback] {fit.get('reason', '')}",
        "audit_label":          "",
        "audit_revenue_model":  "",
        "audit_ai_role":        "",
        "audit_summary_json":   "",
        "audit_uncertainty":    "thin third-party data only (website fetch failed)",
    }


def _classify_one(website_text, url, linkedin_headline, fetch_status="ok",
                  fetch_detail="", company_description="", industry="",
                  employee_count=""):
    """End-to-end AI-agency classify for ONE lead. Returns a flat dict.

    fetch_status flows through from Stage 1 so the Review tab can show WHY a
    lead got "no website text" — site_dead vs SPA shell vs 4xx block — instead
    of collapsing everything to one opaque reason.

    When website_text is empty/short, Layer 2.5 falls back to the upstream
    Apify-enriched description + industry + headline. That's strictly worse
    signal than the real website, but it's almost always enough to make a
    coarse leads/excluded/review call instead of dumping to Review blind.
    """
    base = {
        "route":                "review",
        "audit_classification": "unknown",
        "audit_segment_label":  "",
        "audit_confidence":     "low",
        "audit_reason":         "",
        "audit_label":          "",
        "audit_revenue_model":  "",
        "audit_ai_role":        "",
        "audit_summary_json":   "",
        "audit_uncertainty":    "",
        "fetch_status":         fetch_status,
        "fetch_detail":         fetch_detail,
    }
    if not website_text or len(website_text) < MIN_TEXT_CHARS:
        # Empty company URL — validated 100% reject signal. No LLM spend.
        if fetch_status == "no_website":
            base["route"] = "excluded"
            base["audit_classification"] = "no_website"
            base["audit_confidence"] = "high"
            base["audit_reason"] = ("empty company URL — auto-reject "
                                     "(validated 100% reject signal)")
            return base

        # Layer 2.5 — try the Apify-data fallback before giving up
        has_apify_signal = (len((company_description or "").strip()) >= 30
                            or len((industry or "").strip()) >= 3)
        if has_apify_signal:
            result = _classify_from_apify_fields(
                url=url,
                linkedin_headline=linkedin_headline,
                company_description=company_description,
                industry=industry,
                employee_count=employee_count,
                fetch_status=fetch_status,
            )
            if result is not None:
                base.update(result)
                base["fetch_status"] = fetch_status
                base["fetch_detail"] = fetch_detail
                return base
            # fall through to the no-text branches below if the fallback errored

        # Short-circuit dead hosts: route to excluded (no LLM spend, no review-tab noise)
        if fetch_status in ("dns_fail", "connect_timeout"):
            base["route"] = "excluded"
            base["audit_classification"] = "unknown"
            base["audit_reason"] = f"site_dead ({fetch_status}): {fetch_detail or 'host unreachable'}"
        else:
            base["audit_reason"] = f"no website text ({fetch_status})"
        return base

    cache_key = _norm_url(url)

    summary, err = _summarize(website_text, cache_key)
    if err or not summary:
        base["audit_reason"] = f"stage2a fail: {err[:120] if err else 'no output'}"
        return base
    base["audit_summary_json"] = json.dumps(summary, ensure_ascii=False)

    judgment, err = _self_classify(summary, url, linkedin_headline, cache_key)
    if err or not judgment:
        base["audit_reason"] = f"stage2b fail: {err[:120] if err else 'no output'}"
        return base
    base["audit_label"]         = judgment.get("natural_label", "")
    base["audit_revenue_model"] = judgment.get("primary_revenue_model", "")
    base["audit_ai_role"]       = judgment.get("ai_role", "")
    base["audit_uncertainty"]   = judgment.get("uncertainty", "")

    fit, err = _aia_icp_fit(judgment, url, linkedin_headline,
                              employee_count=employee_count)
    if err or not fit:
        base["audit_reason"] = f"stage2c fail: {err[:120] if err else 'no output'}"
        return base

    bucket = fit.get("bucket", "unknown")
    base["route"]                = fit.get("route") or AIA_ROUTES.get(bucket, "review")
    base["audit_classification"] = bucket
    base["audit_segment_label"]  = fit.get("segment_label") or AIA_SEGMENT_LABELS.get(bucket, "")
    base["audit_confidence"]     = fit.get("confidence", "low")
    base["audit_reason"]         = fit.get("reason", "")
    return base


def _stage2_classify_batched(leads, multipage, batch_id, model=None):
    """Batched-CLI variant of stage2_classify (Max-plan billing).

    Processes the three Stage-2 LLM calls (summarize -> self-classify -> icp
    ICP fit) in batched chunks, one batch per `claude -p` subprocess. Same
    result shape as the per-lead SDK path — write_audit_csv works unchanged.
    """
    state_path = WORKING_DIR / f"{batch_id}_v5_three_stage.json"
    results = load_cache(state_path)
    print(f"  v5 cache: {len(results)} already classified")

    # ---- Build todo ----
    todo = []
    for idx, lead in enumerate(leads):
        rid = (lead.get("id") or "").strip() or str(idx)
        if rid in results:
            continue
        website = (lead.get("companyWebsite") or "").strip()
        entry = multipage.get(website) if website else None
        page_text = _text_from_entry(entry)
        if website:
            fetch_status = _status_from_entry(entry)
            fetch_detail = entry.get("detail", "") if isinstance(entry, dict) else ""
        else:
            fetch_status, fetch_detail = "no_website", ""
        todo.append({
            "id":                  rid,
            "website":             website,
            "headline":            lead.get("Headline / Title", "") or lead.get("headline", ""),
            "page_text":           page_text,
            "fetch_status":        fetch_status,
            "fetch_detail":        fetch_detail,
            "company_description": lead.get("companyDescription", ""),
            "industry":            lead.get("industry", ""),
            "employee_count":      lead.get("employeeCount", ""),
        })
    print(f"  to classify (batched-CLI/Max): {len(todo)}")
    if not todo:
        return results

    # ---- Per-lead result skeletons ----
    bases = {}
    for t in todo:
        bases[t["id"]] = {
            "route":                "review",
            "audit_classification": "unknown",
            "audit_segment_label":  "",
            "audit_confidence":     "low",
            "audit_reason":         "",
            "audit_label":          "",
            "audit_revenue_model":  "",
            "audit_ai_role":        "",
            "audit_summary_json":   "",
            "audit_uncertainty":    "",
            "fetch_status":         t["fetch_status"],
            "fetch_detail":         t["fetch_detail"],
        }

    # ---- Routing: 3-stage chain vs Apify-fallback vs dead-site short-circuit ----
    chain_path, fallback_path, dead_path = [], [], []
    for t in todo:
        if t["page_text"] and len(t["page_text"]) >= MIN_TEXT_CHARS:
            chain_path.append(t)
        elif t["fetch_status"] == "no_website":
            # Empty company URL — validated 100% reject signal (2026-05-02,
            # 24/24 manually-reviewed no-URL leads rejected). No LLM spend.
            dead_path.append(t)
        else:
            has_apify_signal = (
                len((t["company_description"] or "").strip()) >= 30
                or len((t["industry"] or "").strip()) >= 3
            )
            (fallback_path if has_apify_signal else dead_path).append(t)
    print(f"  routing: chain={len(chain_path)}, "
          f"apify-fallback={len(fallback_path)}, dead-short-circuit={len(dead_path)}")

    # ---- Dead-site short-circuit (no LLM call) ----
    for t in dead_path:
        b = bases[t["id"]]
        if t["fetch_status"] == "no_website":
            b["route"] = "excluded"
            b["audit_classification"] = "no_website"
            b["audit_confidence"] = "high"
            b["audit_reason"] = ("empty company URL — auto-reject "
                                  "(validated 100% reject signal)")
        elif t["fetch_status"] in ("dns_fail", "connect_timeout"):
            b["route"] = "excluded"
            b["audit_reason"] = (f"site_dead ({t['fetch_status']}): "
                                  f"{t['fetch_detail'] or 'host unreachable'}")
        else:
            b["audit_reason"] = f"no website text ({t['fetch_status']})"
        results[t["id"]] = {**b, "id": t["id"]}
    if dead_path:
        save_cache(state_path, results)

    # ---- Stage 2a: SUMMARIZE (cached by url) ----
    summary_cache = load_cache(SUMMARY_CACHE)
    summaries = {}  # id -> summary dict
    summarize_items = []
    for t in chain_path:
        ck = _norm_url(t["website"])
        if ck in summary_cache:
            summaries[t["id"]] = summary_cache[ck]
        else:
            summarize_items.append({"id": t["id"], "website_text": t["page_text"]})
    print(f"\n  === Stage 2a summarize === "
          f"{len(chain_path) - len(summarize_items)} cached, "
          f"{len(summarize_items)} to call")
    if summarize_items:
        _key_2a = {t["id"]: _norm_url(t["website"]) for t in chain_path}
        def _save_2a(res):
            for _id, _r in res.items():
                if _key_2a.get(_id):
                    summary_cache[_key_2a[_id]] = _r
            save_cache(SUMMARY_CACHE, summary_cache)
        out_2a, _ = _run_batched_stage(
            summarize_items, SUMMARIZE_PROMPT, SUMMARIZE_SCHEMA,
            label="2a-summarize", on_batch=_save_2a,
            **({"model": model} if model else {}),
        )
        for t in chain_path:
            if t["id"] in out_2a:
                summary_cache[_norm_url(t["website"])] = out_2a[t["id"]]
                summaries[t["id"]] = out_2a[t["id"]]
        save_cache(SUMMARY_CACHE, summary_cache)
    for t in chain_path:
        if t["id"] not in summaries:
            bases[t["id"]]["audit_reason"] = "stage2a fail: no result for id"

    # ---- Stage 2b: SELF_CLASSIFY (cached by url) ----
    judgment_cache = load_cache(JUDGMENT_CACHE)
    judgments = {}
    classify_items = []
    chain_with_summary = [t for t in chain_path if t["id"] in summaries]
    for t in chain_with_summary:
        ck = _norm_url(t["website"])
        if ck in judgment_cache:
            judgments[t["id"]] = judgment_cache[ck]
        else:
            classify_items.append({
                "id":                t["id"],
                "homepage_url":      t["website"],
                "linkedin_headline": t["headline"],
                "summary":           summaries[t["id"]],
            })
    print(f"\n  === Stage 2b self-classify === "
          f"{len(chain_with_summary) - len(classify_items)} cached, "
          f"{len(classify_items)} to call")
    if classify_items:
        _key_2b = {t["id"]: _norm_url(t["website"]) for t in chain_path}
        def _save_2b(res):
            for _id, _r in res.items():
                if _key_2b.get(_id):
                    judgment_cache[_key_2b[_id]] = _r
            save_cache(JUDGMENT_CACHE, judgment_cache)
        out_2b, _ = _run_batched_stage(
            classify_items, SELF_CLASSIFY_PROMPT, SELF_CLASSIFY_SCHEMA,
            label="2b-classify", on_batch=_save_2b,
            **({"model": model} if model else {}),
        )
        for t in chain_with_summary:
            if t["id"] in out_2b:
                judgment_cache[_norm_url(t["website"])] = out_2b[t["id"]]
                judgments[t["id"]] = out_2b[t["id"]]
        save_cache(JUDGMENT_CACHE, judgment_cache)
    for t in chain_with_summary:
        if t["id"] not in judgments:
            bases[t["id"]]["audit_reason"] = "stage2b fail: no result for id"

    # ---- Stage 2c: AIA_ICP_FIT (cached by URL) ----
    icp_fit_cache = load_cache(ICP_FIT_CACHE)
    chain_with_judgment = [t for t in chain_with_summary if t["id"] in judgments]
    fits = {}
    fit_items = []
    for t in chain_with_judgment:
        url_key = _norm_url(t["website"])
        if url_key and url_key in icp_fit_cache:
            fits[t["id"]] = icp_fit_cache[url_key]
        else:
            fit_items.append({
                "id":                t["id"],
                "homepage_url":      t["website"],
                "linkedin_headline": t["headline"],
                "employee_count":    t["employee_count"],
                "company_judgment":  judgments[t["id"]],
            })
    print(f"\n  === Stage 2c aia-icp-fit === {len(fit_items)} to call ({len(chain_with_judgment)-len(fit_items)} cached)")
    if fit_items:
        _key_2c = {t["id"]: _norm_url(t["website"]) for t in chain_path}
        def _save_2c(res):
            for _id, _r in res.items():
                if _key_2c.get(_id):
                    icp_fit_cache[_key_2c[_id]] = _r
            save_cache(ICP_FIT_CACHE, icp_fit_cache)
        new_fits, _ = _run_batched_stage(
            fit_items, AIA_ICP_FIT_PROMPT, AIA_ICP_FIT_SCHEMA,
            label="2c-icp", on_batch=_save_2c,
            **({"model": model} if model else {}),
        )
        fits.update(new_fits)
        # Write new results to cache keyed by URL
        for t in chain_with_judgment:
            if t["id"] in new_fits:
                icp_fit_cache[_norm_url(t["website"])] = new_fits[t["id"]]
        save_cache(ICP_FIT_CACHE, icp_fit_cache)
    for t in chain_with_judgment:
        if t["id"] not in fits and not bases[t["id"]]["audit_reason"]:
            bases[t["id"]]["audit_reason"] = "stage2c fail: no result for id"

    # ---- Assemble chain-path results ----
    for t in chain_path:
        b = bases[t["id"]]
        sm = summaries.get(t["id"])
        if sm:
            b["audit_summary_json"] = json.dumps(sm, ensure_ascii=False)
        jd = judgments.get(t["id"])
        if jd:
            b["audit_label"]         = jd.get("natural_label", "")
            b["audit_revenue_model"] = jd.get("primary_revenue_model", "")
            b["audit_ai_role"]       = jd.get("ai_role", "")
            b["audit_uncertainty"]   = jd.get("uncertainty", "")
        ft = fits.get(t["id"])
        if ft:
            bucket = ft.get("bucket", "unknown")
            b["route"]                = ft.get("route") or AIA_ROUTES.get(bucket, "review")
            b["audit_classification"] = bucket
            b["audit_segment_label"]  = (ft.get("segment_label")
                                          or AIA_SEGMENT_LABELS.get(bucket, ""))
            b["audit_confidence"]     = ft.get("confidence", "low")
            b["audit_reason"]         = ft.get("reason", b["audit_reason"])
        results[t["id"]] = {**b, "id": t["id"]}

    # ---- Apify-fallback path (one batched call) ----
    if fallback_path:
        fb_items = [{
            "id":                  t["id"],
            "homepage_url":        t["website"],
            "linkedin_headline":   t["headline"],
            "company_description": t["company_description"],
            "industry":            t["industry"],
            "employee_count":      t["employee_count"],
            "fetch_status":        t["fetch_status"],
        } for t in fallback_path]
        print(f"\n  === Apify-fallback === {len(fb_items)} to call")
        out_fb, _ = _run_batched_stage(
            fb_items, APIFY_FALLBACK_PROMPT, AIA_ICP_FIT_SCHEMA,
            label="apify-fallback",
        )
        for t in fallback_path:
            b = bases[t["id"]]
            ft = out_fb.get(t["id"])
            if ft:
                bucket = ft.get("bucket", "unknown")
                b["route"]                = ft.get("route") or AIA_ROUTES.get(bucket, "review")
                b["audit_classification"] = bucket
                b["audit_segment_label"]  = (ft.get("segment_label")
                                              or AIA_SEGMENT_LABELS.get(bucket, ""))
                b["audit_confidence"]     = ft.get("confidence", "low")
                b["audit_reason"]         = "[apify-fallback] " + ft.get("reason", "")
                b["audit_uncertainty"]    = "thin third-party data only (website fetch failed)"
            else:
                if t["fetch_status"] in ("dns_fail", "connect_timeout"):
                    b["route"] = "excluded"
                    b["audit_reason"] = (f"site_dead ({t['fetch_status']}); "
                                          "apify-fallback also failed")
                else:
                    b["audit_reason"] = (f"no website text ({t['fetch_status']}); "
                                          "apify-fallback also failed")
            results[t["id"]] = {**b, "id": t["id"]}

    save_cache(state_path, results)
    return results


def stage2_classify(leads, multipage, batch_id, model=None):
    """Run the 3-step chain across all leads. Resumable via per-batch state file.

    Dispatches to the batched-CLI/Max-plan variant by default (AUDIT_USE_CLI=1).
    Set AUDIT_USE_CLI=0 to use the legacy per-lead SDK path (API-key billing).
    """
    if USE_CLI_DEFAULT:
        return _stage2_classify_batched(leads, multipage, batch_id, model=model)

    state_path = WORKING_DIR / f"{batch_id}_v5_three_stage.json"
    results = load_cache(state_path)
    print(f"  v5 cache: {len(results)} already classified")

    todo = []
    for idx, lead in enumerate(leads):
        rid = (lead.get("id") or "").strip() or str(idx)
        if rid in results:
            continue
        website = (lead.get("companyWebsite") or "").strip()
        entry = multipage.get(website) if website else None
        page_text = _text_from_entry(entry)
        if website:
            fetch_status = _status_from_entry(entry)
            fetch_detail = entry.get("detail", "") if isinstance(entry, dict) else ""
        else:
            fetch_status, fetch_detail = "no_website", ""
        todo.append({
            "id":                  rid,
            "website":             website,
            "headline":            lead.get("Headline / Title", "") or lead.get("headline", ""),
            "page_text":           page_text,
            "fetch_status":        fetch_status,
            "fetch_detail":        fetch_detail,
            "company_description": lead.get("companyDescription", ""),
            "industry":            lead.get("industry", ""),
            "employee_count":      lead.get("employeeCount", ""),
        })
    print(f"  to classify: {len(todo)}")
    if not todo:
        return results

    def _work(lead):
        result = _classify_one(
            lead["page_text"], lead["website"], lead["headline"],
            fetch_status=lead["fetch_status"],
            fetch_detail=lead["fetch_detail"],
            company_description=lead["company_description"],
            industry=lead["industry"],
            employee_count=lead["employee_count"],
        )
        return lead["id"], result

    t0 = time.time()
    done_n = 0
    with ThreadPoolExecutor(max_workers=HAIKU_CONCURRENCY) as ex:
        futs = [ex.submit(_work, l) for l in todo]
        for fut in as_completed(futs):
            lid, result = fut.result()
            results[lid] = {**result, "id": lid}
            done_n += 1
            # Save every batch so a kill mid-run preserves tokens already spent.
            if done_n % 20 == 0 or done_n == len(todo):
                save_cache(state_path, results)
                el = time.time() - t0
                rate = done_n / el if el > 0 else 0
                eta = (len(todo) - done_n) / rate if rate > 0 else 0
                print(f"    [{done_n}/{len(todo)}] classified ({el:.0f}s elapsed, eta {eta:.0f}s)")
    save_cache(state_path, results)
    return results


# ----------------- Stage 3: cascade (disabled by default; review tier handles it) -----------------
# Kept for symmetry with v4's CLI. In v5 the review tab is the cascade equivalent.


# ----------------- Output CSV -----------------

def write_audit_csv(leads, results, output_path):
    """Output schema is a superset of v4's. New field: `audit_segment_label`.

    Legacy fields (kept for any consumer that still reads them):
      audit_classification, audit_confidence, audit_reason, fetch_status, fetch_tier
    3-stage fields (matching v4):
      audit_route, audit_label, audit_revenue_model, audit_ai_role,
      audit_summary_json, audit_uncertainty
    NEW v5 field:
      audit_segment_label  -- pushed to Instantly as a custom variable
    """
    cols = ["row", "name", "linkedin", "company_url",
            "current_type", "current_status",
            "audit_classification", "audit_confidence", "audit_reason",
            "fetch_status", "fetch_tier",
            "audit_route", "audit_label", "audit_revenue_model",
            "audit_ai_role", "audit_summary_json", "audit_uncertainty",
            "audit_segment_label", "audit_crm_type"]

    n = 0
    with Path(output_path).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for idx, lead in enumerate(leads):
            rid = (lead.get("id") or "").strip() or str(idx)
            h = results.get(rid, {})
            bucket = h.get("audit_classification") or "unknown"
            route = h.get("route") or AIA_ROUTES.get(bucket, "review")
            w.writerow({
                "row":                  rid,
                "name":                 lead.get("Full Name", ""),
                "linkedin":             lead.get("LinkedIn URL", ""),
                "company_url":          lead.get("companyWebsite", ""),
                "current_type":         lead.get("Category", ""),
                "current_status":       lead.get("Verdict", ""),
                "audit_classification": bucket,
                "audit_confidence":     h.get("audit_confidence", ""),
                "audit_reason":         h.get("audit_reason", ""),
                "fetch_status":         h.get("fetch_status")
                                         or ("ok" if lead.get("companyWebsite") else "no_website"),
                "fetch_tier":           "v5_aia",
                "audit_route":          route,
                "audit_label":          h.get("audit_label", ""),
                "audit_revenue_model":  h.get("audit_revenue_model", ""),
                "audit_ai_role":        h.get("audit_ai_role", ""),
                "audit_summary_json":   h.get("audit_summary_json", ""),
                "audit_uncertainty":    h.get("audit_uncertainty", ""),
                "audit_segment_label":  h.get("audit_segment_label", ""),
                "audit_crm_type":       AIA_CRM_TYPES.get(bucket, ""),
            })
            n += 1
    return n


# ----------------- CLI -----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, choices=[1, 2, 3],
                    help="Run a specific stage and exit")
    ap.add_argument("--batch-id", default="",
                    help="Batch identifier (auto-derived from input filename if omitted)")
    ap.add_argument("--input", default="",
                    help="Path to deduped CSV")
    # v4/v3 drop-in compat
    ap.add_argument("--phase1-pipeline", action="store_true",
                    help="Run all 3 stages back-to-back (drop-in for v4 CLI)")
    ap.add_argument("--phase1-csv", default="data/working/phase1_categorized.csv",
                    help="Phase-1 CSV to join with the websites cache "
                         "(pipeline mode; ignored when --input is given)")
    ap.add_argument("--websites-cache", default="data/caches/find_websites_cache.json",
                    help="find_websites cache to source companyWebsite from "
                         "(pipeline mode; ignored when --input is given)")
    ap.add_argument("--verdicts", default="yes",
                    help="Comma-separated phase-1 Verdict values to audit in pipeline "
                         "mode. Default 'yes'; pass 'yes,maybe' to include the maybes pool.")
    ap.add_argument("--output", default="",
                    help="Path for final audit CSV (needed for stage 3 or auto mode)")
    ap.add_argument("--resume", action="store_true",
                    help="(v3-compat) ignored — v5 is always resumable via batch state")
    ap.add_argument("--limit", type=int, default=0,
                    help="Macro chunk: process at most N leads (after --offset). 0 = all.")
    ap.add_argument("--offset", type=int, default=0,
                    help="Macro chunk: skip the first N leads of the input CSV.")
    ap.add_argument("--model", default="",
                    help="Override the Claude model for Stage 2 (e.g. claude-sonnet-5). "
                         "Defaults to claude-haiku-4-5-20251001.")
    args = ap.parse_args()

    input_path = args.input or args.phase1_csv
    if not input_path:
        sys.exit("--input (or --phase1-csv) required")
    batch_id = args.batch_id or derive_batch_id(input_path)

    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    if args.input:
        # Ad-hoc CSV: must already carry a companyWebsite column.
        leads = read_deduped_csv(input_path)
    else:
        # Pipeline mode: join phase1_categorized.csv with the websites cache.
        verdict_tuple = tuple(v.strip() for v in args.verdicts.split(",") if v.strip())
        leads = load_phase1_with_websites(
            ROOT / args.phase1_csv, ROOT / args.websites_cache,
            verdicts=verdict_tuple or ("yes",))
        print(f"[pipeline-mode] verdicts={','.join(verdict_tuple or ('yes',))} "
              f"-> {len(leads)} leads; "
              f"{sum(1 for l in leads if not l['companyWebsite'])} without a website "
              f"(will auto-reject as no_website)")
    total_in_csv = len(leads)
    if args.offset:
        leads = leads[args.offset:]
    if args.limit:
        leads = leads[:args.limit]
    if args.offset or args.limit:
        print(f"[chunk] offset={args.offset} limit={args.limit} -> "
              f"{len(leads)} of {total_in_csv} leads selected")
    print(f"[load] {len(leads)} leads from {input_path}")
    print(f"[batch] id={batch_id}\n")

    if not leads:
        output_path = args.output or str(Path(input_path).with_name(
            Path(input_path).stem.replace("_deduped", "") + "_audit.csv"))
        write_audit_csv(leads, {}, output_path)
        print(f"[noop] 0 leads to audit. Wrote empty audit CSV: {output_path}")
        return

    stages_to_run = []
    if args.stage is not None:
        stages_to_run = [args.stage]
    elif args.phase1_pipeline:
        stages_to_run = [1, 2, 3]
    else:
        sys.exit("specify --stage <N> or --phase1-pipeline")

    multipage = {}
    results = {}

    if 1 in stages_to_run:
        print(f"=== Stage 1 — multi-page website fetch ===")
        multipage = asyncio.run(stage1_fetch(leads))
        print(f"v Stage 1 done. cache size: {len(multipage)} sites\n")

    if 2 in stages_to_run:
        print(f"=== Stage 2 — AI-agency 3-step classify (summarize -> self-classify -> icp-route) ===")
        if not multipage:
            multipage = _apply_rescue_overlays(load_cache(MULTIPAGE_CACHE))
        results = stage2_classify(leads, multipage, batch_id,
                                   model=args.model or None)
        route_counts: dict = {}
        bucket_counts: dict = {}
        for r in results.values():
            route_counts[r.get("route", "?")] = route_counts.get(r.get("route", "?"), 0) + 1
            b = r.get("audit_classification", "?")
            bucket_counts[b] = bucket_counts.get(b, 0) + 1
        print(f"v Stage 2 done. classified {len(results)} / {len(leads)} leads")
        print(f"  routes:  {dict(sorted(route_counts.items()))}")
        print(f"  buckets: {dict(sorted(bucket_counts.items(), key=lambda x: -x[1]))}\n")

    if 3 in stages_to_run:
        # Cascade is intentionally skipped — review tab captures borderlines.
        print(f"=== Stage 3 — skipped (cascade disabled; review tab handles borderlines) ===")
        if not results:
            state_path = WORKING_DIR / f"{batch_id}_v5_three_stage.json"
            results = load_cache(state_path)
            if not results:
                sys.exit("no Stage 2 state found — run --stage 2 first")

        output_path = args.output or str(Path(input_path).with_name(
            Path(input_path).stem.replace("_deduped", "") + "_audit.csv"))
        n = write_audit_csv(leads, results, output_path)
        cls_counts: dict = {}
        route_counts: dict = {}
        for idx, lead in enumerate(leads):
            rid = (lead.get("id") or "").strip() or str(idx)
            h = results.get(rid, {})
            cls = h.get("audit_classification") or "unknown"
            cls_counts[cls] = cls_counts.get(cls, 0) + 1
            r = h.get("route", "")
            if r:
                route_counts[r] = route_counts.get(r, 0) + 1
        print(f"\nFinal segment breakdown ({n} leads):")
        for cls, c in sorted(cls_counts.items(), key=lambda x: -x[1]):
            print(f"  {cls:34} {c:5}")
        if route_counts:
            print(f"\nRoute breakdown:")
            for r, c in sorted(route_counts.items(), key=lambda x: -x[1]):
                print(f"  {r:10} {c:5}")
        print(f"\nFinal CSV: {output_path}")

    if args.stage is not None and args.stage < 3:
        next_stage = args.stage + 1
        print(f"\nTo continue: python tools/audit_v5.py --stage {next_stage} "
              f"--input {input_path} --batch-id {batch_id}")


if __name__ == "__main__":
    main()
