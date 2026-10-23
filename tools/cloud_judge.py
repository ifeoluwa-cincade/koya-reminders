#!/usr/bin/env python3
"""Call judge in the cloud (Ife 2026-10-23): the same Sonnet prompt and schema as pipeline/judge_agencies.py
(SCHEMA and PROMPT below are copied from it verbatim), but the website text comes from audit_v5's own fetch,
so no Serper credits and no Mac compute.

    python3 tools/cloud_judge.py --input <csv with companyWebsite> --output <csv>
Output: company_url, bucket, fit, proof_of_clients, what_they_do, reason, call_card (JSON), named_clients (JSON).
"""
import argparse, asyncio, csv, json, os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools")); sys.path.insert(0, str(ROOT))
import audit_v5 as v5  # noqa: E402
from pipeline.lite_claude import call_claude_lite  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "is_ai_automation_agency": {"type": "boolean"},
        "bucket": {"type": "string", "enum": [
            "ai_automation_agency", "ml_data_science", "ai_consultation",
            "ai_gtm_systems", "saas_product", "other_biz", "no_website"]},
        "fit": {"type": "integer", "enum": [0, 60, 100]},
        "proof_of_clients": {"type": "integer", "enum": [0, 6, 18, 30]},
        "named_clients": {"type": "array", "items": {"type": "string"}},
        "what_they_do": {"type": "string"},
        "reason": {"type": "string"},
        "call_card": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_ai_automation_agency", "bucket", "fit", "proof_of_clients",
                 "named_clients", "what_they_do", "reason", "call_card"],
}

PROMPT = """You are qualifying a company for a COLD CALLING list. Read its website text below and answer strictly from what the text says. Never guess.

WHO WE WANT — an AI AUTOMATION AGENCY.
They BUILD, DEPLOY and often MAINTAIN custom AI/automation systems FOR CLIENT BUSINESSES. Done-for-you. The agency does the work; the client does not build it themselves.

The actual work looks like:
- Workflow automation and systems integration — wiring up CRMs, email, forms, spreadsheets, internal tools. Platforms like Zapier, Make.com, n8n, Airtable, Power Automate.
- AI agents and assistants built for a client's own processes.
- Chatbots and voice/calling agents (support, intake, booking, outbound).
- RPA and business-process automation.
- Custom builds on top of existing LLM APIs (OpenAI, Anthropic, etc.) — prompt/agent engineering, retrieval over the client's documents.
- Lead-gen / sales / ops automation delivered as a service.

WHO WE DO NOT WANT:
- ml_data_science — the work is model building or data engineering, NOT workflow automation. Custom model training, MLOps, computer vision, edge ML, IoT/sensor systems, data platforms/warehouses, predictive analytics and forecasting, embedded/staff-augmentation engineering teams. These build AI, but they are not automation agencies. EXCLUDE even when the delivery is clearly done-for-you.
- ai_consultation — advises, trains, coaches, audits, or writes strategy/roadmaps but does not ship the build.
- ai_gtm_systems — AI-powered sales/marketing/outbound services. A MAYBE. Tag it, never call it a yes.
- saas_product — THE DECIDING TEST, apply it BEFORE anything else. Ask: at the end of the engagement, does the customer log into a tool THE VENDOR builds, hosts and maintains for everyone (= saas, REJECT), or does the vendor hand this client their own bespoke build/configuration (= agency)?
  Strong saas signals, any one is enough to reject: a named product ("X is your ...", "X PRO"), self-serve sign-up, free trial, per-seat or per-month subscription pricing, "Download for Mac/Windows", a login/dashboard in the nav, an app you install.
  A saas company describing "we design, automate and deploy AI systems" is STILL saas. Marketing copy about deployment does not make it an agency — the question is only who owns the thing at the end.
- other_biz — anything else (marketing, web dev, recruiting, design, general consulting with no real AI delivery).
- no_website — text is empty, parked, or not a real company site.

FIT — how squarely they sit in the target, scored 0-100:
  100 = CORE. Workflow automation / agents / chatbots / RPA is plainly the main business.
   60 = MIXED. Real automation and agent work, but sold alongside ML, data platforms, app development or general software engineering.
    0 = NOT a fit (any of the exclusion buckets above).
A company whose work is mostly model building or data engineering is ml_data_science with fit 0, even if it also mentions agents in passing.

PROOF OF CLIENTS, score exactly one:
  30 = 3+ NAMED client case studies WITH results/outcomes
  18 = 1-2 named case studies, OR a wall of real testimonials with attributed names
   6 = client logos only, no story and no named results
   0 = nothing

Also give 3-5 CALL CARD facts: short, specific, verifiable things from the site a caller could reference. No fluff, no adjectives. If the site is thin, return fewer.

WEBSITE TEXT:
{text}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    leads = v5.read_deduped_csv(a.input)
    if a.limit:
        leads = leads[:a.limit]
    cache = asyncio.run(v5.stage1_fetch(leads))

    def one(l):
        url = (l.get("companyWebsite") or "").strip()
        text = v5._text_from_entry(cache.get(url))
        if len(text) < 200:
            return url, {"bucket": "no_website", "fit": 0, "proof_of_clients": 0, "reason": "site not readable"}
        try:
            return url, call_claude_lite(PROMPT.format(text=text[:12000]), json_schema=SCHEMA, model="sonnet", timeout_sec=240)
        except Exception as ex:
            return url, {"bucket": "", "reason": f"judge failed: {type(ex).__name__}"}

    with ThreadPoolExecutor(int(os.environ.get("AUDIT_WORKERS", "4"))) as ex:
        res = list(ex.map(one, leads))
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company_url", "bucket", "fit", "proof_of_clients", "what_they_do", "reason", "call_card", "named_clients"])
        w.writeheader()
        for url, r in res:
            w.writerow({"company_url": url, "bucket": r.get("bucket", ""), "fit": r.get("fit", ""), "proof_of_clients": r.get("proof_of_clients", ""),
                        "what_they_do": r.get("what_they_do", ""), "reason": r.get("reason", ""),
                        "call_card": json.dumps(r.get("call_card", [])), "named_clients": json.dumps(r.get("named_clients", []))})
    ok = sum(1 for _, r in res if r.get("bucket"))
    print(f"  judge: {ok} of {len(res)} judged | agencies {sum(1 for _, r in res if r.get('bucket') == 'ai_automation_agency')}")


if __name__ == "__main__":
    main()
