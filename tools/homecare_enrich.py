#!/usr/bin/env python3
"""Home care lead enrichment in the cloud (Ife 2026-11-17: names are a must; "use web search ... in the cloud, super fast").

For each state-licensed home care agency: Claude (Haiku) with WebSearch/WebFetch finds
  - the official website
  - the OWNER / founder (else CEO / President / Administrator / Director) full name + title
  - that person's LinkedIn profile URL
Clinical rule: a person is accepted only when a source ties them to THIS agency (name + city/state); the evidence URL
and a short quote come back with every answer. Unsure -> null. Uses the Max subscription (claude CLI), no API key.

Input CSV (cloud_audit shard format + extras): name, domain, companyWebsite, linkedin, key, legal, city, state, email, contact
Output: --output CSV with key + website, owner_full_name, owner_first_name, owner_title, linkedin_url, evidence_url,
        evidence_quote, confidence, error.
"""
import argparse, csv, json, os, re, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["website", "owner_full_name", "owner_title", "linkedin_url", "evidence_url", "evidence_quote", "confidence"],
          "properties": {"website": {"type": ["string", "null"]}, "owner_full_name": {"type": ["string", "null"]},
                         "owner_title": {"type": ["string", "null"]}, "linkedin_url": {"type": ["string", "null"]},
                         "evidence_url": {"type": ["string", "null"]}, "evidence_quote": {"type": ["string", "null"]},
                         "confidence": {"enum": ["high", "medium", "low"]}}}

PROMPT = """Research this US home care agency (state-licensed, non-medical in-home care) and return JSON.

Agency (DBA): {name}
Legal entity: {legal}
Location: {city}, {state}
Agency email on the state licence: {email}
Contact named on the state licence: {contact}
Known website: {site}

Find:
1. website: the agency's own official website (not a directory like caring.com, yelp, care.com, a franchise HQ national homepage
   is OK only if the location page is not findable; prefer the location page URL).
2. owner_full_name + owner_title: the OWNER or founder of THIS agency/location. If no owner is findable, the CEO, President,
   Administrator or Agency Director. If a contact is named on the licence and you can confirm their role, use them.
3. linkedin_url: that person's personal LinkedIn profile (linkedin.com/in/...), only if the profile clearly shows this agency.

Rules: use web search. Accept a person ONLY if a source you saw ties that person to THIS agency (same name and same city/state):
the agency's own site (About/Our Team), a franchise location page, a state/BBB/OpenCorporates filing, a news article, or their
LinkedIn. Put that source in evidence_url and a short verbatim quote in evidence_quote. Never guess from the email address alone.
Use null for anything you cannot confirm. Do at most 4 searches."""

lock = threading.Lock()


def ask(row):
    p = PROMPT.format(name=row.get("name", ""), legal=row.get("legal", ""), city=row.get("city", ""), state=row.get("state", ""),
                      email=row.get("email", "") or "none", contact=row.get("contact", "") or "none",
                      site=row.get("companyWebsite", "") or "unknown")
    cmd = ["claude", "-p", "--no-session-persistence", "--model", "haiku", "--output-format", "json",
           "--permission-mode", "bypassPermissions", "--strict-mcp-config", "--tools", "WebSearch,WebFetch",
           "--json-schema", json.dumps(SCHEMA)]
    for att in range(2):
        try:
            r = subprocess.run(cmd, input=p, capture_output=True, text=True, timeout=240)
            if r.returncode != 0:
                err = f"exit {r.returncode}: {r.stderr[:150]}"; continue
            out = json.loads(r.stdout)
            so = out.get("structured_output")
            if isinstance(so, dict):
                return so, ""
            err = "no structured output: " + str(out.get("result", ""))[:150]
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:150]}"
    return {}, err


def clean_name(n):
    n = re.sub(r"\s+", " ", (n or "").strip())
    n = re.sub(r"^(mr|mrs|ms|miss|dr)\.?\s+", "", n, flags=re.I)
    n = re.sub(r",?\s+(rn|lpn|bsn|msn|mba|cna|phd|cdp|cmc|jr\.?|sr\.?)\b.*$", "", n, flags=re.I)
    return n.title() if n.isupper() or n.islower() else n


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--workers", type=int, default=int(os.environ.get("AUDIT_WORKERS", "4")))
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.input, encoding="utf-8")))
    if a.limit: rows = rows[:a.limit]
    res = {}

    def one(i):
        so, err = ask(rows[i])
        with lock:
            res[i] = (so, err)
            if len(res) % 25 == 0: print(f"  {len(res)}/{len(rows)}", flush=True)
    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, range(len(rows))))
    cols = ["key", "name", "website", "owner_full_name", "owner_first_name", "owner_title", "linkedin_url", "evidence_url",
            "evidence_quote", "confidence", "error", "audit_route", "audit_classification"]
    found = 0
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for i, r in enumerate(rows):
            so, err = res.get(i, ({}, "not run"))
            full = clean_name(so.get("owner_full_name"))
            if full and not so.get("evidence_url"): full = ""          # no evidence, no name
            found += bool(full)
            li = so.get("linkedin_url") or ""
            if li and "linkedin.com/in/" not in li: li = ""
            w.writerow({"key": r.get("key", ""), "name": r.get("name", ""), "website": so.get("website") or "",
                        "owner_full_name": full, "owner_first_name": full.split()[0] if full else "", "owner_title": so.get("owner_title") or "",
                        "linkedin_url": li, "evidence_url": so.get("evidence_url") or "", "evidence_quote": (so.get("evidence_quote") or "")[:300],
                        "confidence": so.get("confidence") or "", "error": err,
                        "audit_route": "leads" if full else "review", "audit_classification": "named" if full else "no_name"})
    print(f"  enrich: {len(rows)} agencies | named {found} | errors {sum(1 for v in res.values() if v[1])}", flush=True)


if __name__ == "__main__":
    main()
