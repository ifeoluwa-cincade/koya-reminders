#!/usr/bin/env python3
"""Lite audit: same website fetch and same ICP rules as audit_v5, at a fraction of the Claude usage (Ife 2026-10-21).

audit_v5 sends every company through THREE Haiku steps (summarize -> self-classify -> ICP route), each reading up
to ~9,000 characters. Lite does:
  1. the same stage 1 fetch (audit_v5.stage1_fetch, cache and rescues included)
  2. a free pre-filter: no website / dead site / unmistakable self-serve SaaS never reach Claude
  3. ONE Haiku call per company, on the first LITE_CAP characters of site text (default 4,000), 40 companies per call,
     using audit_v5's own ICP routing rules (AIA_ICP_FIT_PROMPT) so buckets and routes mean the same thing.

Output columns match the ones downstream tools read (company_url, audit_route, audit_classification, audit_crm_type,
audit_confidence, audit_reason, fetch_status) plus audit_mode = lite / prefilter.

    python3 tools/audit_lite.py --input <csv> --output <csv> [--limit N]
Validate against full v5 results before trusting it (tools/audit_compare.py).
"""
import argparse, asyncio, csv, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools")); sys.path.insert(0, str(ROOT))
import audit_v5 as v5  # noqa: E402

CAP = int(os.environ.get("LITE_CAP", "4000"))
BATCH = int(os.environ.get("LITE_BATCH", "40"))
DEAD = {"dns_fail", "connect_timeout"}

LITE_PROMPT = ("You will be given the company's own WEBSITE TEXT (homepage and a few subpages, possibly cut short), "
               "not a summary. Read it and apply the routing rules below directly.\n\n" + v5.AIA_ICP_FIT_PROMPT)

# Pre-filter: only the unmistakable cases skip Claude. Self-serve signals with zero service signals.
SAAS = re.compile(r"start (your )?free trial|sign up free|try (it )?free|create (a free |your )?account|log ?in|"
                  r"per (user|seat)|/ ?(mo|month)\b|pricing plans|download the app|api (docs|reference)", re.I)
SERVICE = re.compile(r"\bwe (build|help|design|implement|automate)|our clients|case stud|book a (call|meeting)|agency|"
                     r"done[- ]for[- ]you|consult|for clients|custom (ai|automation|solution)", re.I)


# --- optional founder step (Ife 2026-10-22): read the founder off the company's own pages, free apart from Claude.
# Same prompt and the same strict checks as founder_waterfall.py route R2 (name must appear in the text, founder-type role).
FOUNDER_PROMPT = """For each company below you get text from its own website. Name the ONE person who founded or runs it (founder, co-founder, CEO, owner, president, managing director).
Rules: use ONLY what the text says. The person's full name (first and last) must appear in the text. If the text does not clearly name a founder/CEO/owner of THIS company, return null for name, title and quote.
Do not use clients, case-study people, testimonials, advisors or team members who are not the founder/CEO/owner. quote = the exact words from the text that name them (max 200 chars).
Return JSON {"items":[{"domain","name","title","quote"}]} with one item per company, same domains.

"""
FOUNDER_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "domain": {"type": "string"}, "name": {"type": ["string", "null"]}, "title": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
    "required": ["domain", "name", "title", "quote"]}}}, "required": ["items"]}
ROLE = re.compile(r"co-?founder|founder|\bceo\b|chief executive|\bowner\b|\bpresident\b|managing director|principal", re.I)


def good_name(n):
    n = (n or "").strip()
    return bool(re.fullmatch(r"[^\W\d_][\w'’.\- ]+", n)) and 2 <= len(n.split()) <= 4 and len(n) <= 40


def windows(text, n=1500):
    hits = [m.start() for m in re.finditer(r"founder|co-founder|\bceo\b|chief executive|\bowner\b|meet |about us|our story|managing director", text or "", re.I)]
    if not hits:
        return (text or "")[:n]
    out, last = [], -9999
    for h in hits:
        if h - last > 400:
            out.append(text[max(0, h - 200):h + 300]); last = h
        if sum(map(len, out)) >= n:
            break
    return " ... ".join(out)[:n + 600]


def find_founders(targets):
    """targets: [(domain, text)]. Returns {domain: {name, title, quote}} for confirmed founders only."""
    from concurrent.futures import ThreadPoolExecutor
    from pipeline.lite_claude import call_claude_lite
    found, text = {}, dict(targets)
    batches = [targets[i:i + 8] for i in range(0, len(targets), 8)]

    def run(b):
        body = "\n\n".join(f"### {d}\n{windows(t)}" for d, t in b)
        try:
            res = call_claude_lite(FOUNDER_PROMPT + body, json_schema=FOUNDER_SCHEMA, model="haiku", timeout_sec=240)
        except Exception:
            return
        for it in (res or {}).get("items", []):
            d, nm = (it.get("domain") or "").lower(), it.get("name")
            tx = text.get(d, "").lower()
            if nm and good_name(nm) and all(p.lower() in tx for p in nm.split()[:2]) \
                    and ROLE.search((it.get("title") or "") + " " + (it.get("quote") or "")):
                found[d] = {"name": nm.strip(), "title": it.get("title") or "Founder", "quote": (it.get("quote") or "")[:200]}
    with ThreadPoolExecutor(int(os.environ.get("AUDIT_WORKERS", "4"))) as ex:
        list(ex.map(run, batches))
    return found


def prefilter(text):
    saas = len(set(m.group(0).lower() for m in SAAS.finditer(text)))
    return saas >= 3 and not SERVICE.search(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--founders", action="store_true", help="also read the founder off the site text for agency/GTM keepers")
    a = ap.parse_args()

    leads = v5.read_deduped_csv(a.input)
    if a.limit:
        leads = leads[:a.limit]
    cache = asyncio.run(v5.stage1_fetch(leads))

    out, items = {}, []
    for i, l in enumerate(leads):
        url = (l.get("companyWebsite") or "").strip()
        rid = str(i)
        if not url:
            out[rid] = {"route": "excluded", "bucket": "no_website", "reason": "no website", "mode": "prefilter", "fetch": "no_website"}
            continue
        entry = cache.get(url)
        text, status = v5._text_from_entry(entry), v5._status_from_entry(entry)
        if status != "ok" or len(text) < 200:
            out[rid] = {"route": "excluded" if status in DEAD else "review", "bucket": "unknown",
                        "reason": f"site not readable ({status})", "mode": "prefilter", "fetch": status}
            continue
        if prefilter(text):
            out[rid] = {"route": "excluded", "bucket": "saas_product", "reason": "self-serve signals, no service signals",
                        "mode": "prefilter", "fetch": status}
            continue
        items.append({"id": rid, "url": url, "website_text": text[:CAP]})
        out[rid] = {"fetch": status}

    print(f"  lite: {len(leads)} companies | {len(items)} to Claude | {len(leads) - len(items)} settled free")
    res, errs = v5._run_batched_stage(items, LITE_PROMPT, v5.AIA_ICP_FIT_SCHEMA, label="lite", batch_size=BATCH)
    for it in items:
        r = res.get(it["id"])
        if r:
            out[it["id"]].update({"route": r["route"], "bucket": r["bucket"], "confidence": r.get("confidence", ""),
                                  "reason": r.get("reason", ""), "mode": "lite"})
        else:
            out[it["id"]].update({"route": "review", "bucket": "unknown", "reason": "lite call failed", "mode": "error"})

    founders = {}
    if a.founders:
        keep = [(v5._norm_url(l.get("companyWebsite", "")), v5._text_from_entry(cache.get((l.get("companyWebsite") or "").strip())))
                for i, l in enumerate(leads)
                if out[str(i)].get("route") == "leads" and out[str(i)].get("bucket") in ("ai_agency", "ai_gtm_systems")]
        keep = [(d.split("//")[-1].split("/")[0].removeprefix("www.").lower(), t) for d, t in keep if t]
        founders = find_founders(keep)
        print(f"  founders: {len(founders)} of {len(keep)} agency/GTM keepers named on their own site")

    cols = ["company_url", "name", "audit_route", "audit_classification", "audit_crm_type", "audit_confidence",
            "audit_reason", "fetch_status", "audit_mode", "founder_name", "founder_title", "founder_quote"]
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for i, l in enumerate(leads):
            o = out[str(i)]
            fd = founders.get((l.get("companyWebsite") or "").split("//")[-1].split("/")[0].removeprefix("www.").lower(), {})
            w.writerow({"founder_name": fd.get("name", ""), "founder_title": fd.get("title", ""), "founder_quote": fd.get("quote", ""),"company_url": l.get("companyWebsite", ""), "name": l.get("name", ""), "audit_route": o["route"],
                        "audit_classification": o["bucket"], "audit_crm_type": v5.AIA_CRM_TYPES.get(o["bucket"], ""),
                        "audit_confidence": o.get("confidence", ""), "audit_reason": o.get("reason", ""),
                        "fetch_status": o.get("fetch", ""), "audit_mode": o.get("mode", "")})
    print(f"  lite: wrote {len(leads)} rows | batch errors {len(errs)}")


if __name__ == "__main__":
    main()
