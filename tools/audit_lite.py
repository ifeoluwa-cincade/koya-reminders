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


def prefilter(text):
    saas = len(set(m.group(0).lower() for m in SAAS.finditer(text)))
    return saas >= 3 and not SERVICE.search(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
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

    cols = ["company_url", "name", "audit_route", "audit_classification", "audit_crm_type", "audit_confidence",
            "audit_reason", "fetch_status", "audit_mode"]
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for i, l in enumerate(leads):
            o = out[str(i)]
            w.writerow({"company_url": l.get("companyWebsite", ""), "name": l.get("name", ""), "audit_route": o["route"],
                        "audit_classification": o["bucket"], "audit_crm_type": v5.AIA_CRM_TYPES.get(o["bucket"], ""),
                        "audit_confidence": o.get("confidence", ""), "audit_reason": o.get("reason", ""),
                        "fetch_status": o.get("fetch", ""), "audit_mode": o.get("mode", "")})
    print(f"  lite: wrote {len(leads)} rows | batch errors {len(errs)}")


if __name__ == "__main__":
    main()
