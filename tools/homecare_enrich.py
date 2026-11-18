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
import argparse, csv, json, os, re, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["business_type", "type_evidence", "website", "owner_full_name", "owner_title", "linkedin_url",
                       "evidence_url", "evidence_quote", "confidence", "named_person_confirmed"],
          "properties": {"business_type": {"enum": ["private_home_care", "home_health_medical", "supported_living_or_disability",
                                                    "senior_living_facility", "staffing", "big_chain_corporate", "closed_or_not_found", "unclear"]},
                         "type_evidence": {"type": ["string", "null"]},
                         "named_person_confirmed": {"enum": ["yes", "no", "not_found"]},
                         "website": {"type": ["string", "null"]}, "owner_full_name": {"type": ["string", "null"]},
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
0. business_type: what this business REALLY is today. private_home_care = sends caregivers into clients' homes for
   NON-MEDICAL care (companion, personal care, homemaker, respite, sitter, in-home dementia care), small/independent or a
   franchise LOCATION. home_health_medical = mainly nurses/therapy/Medicare home health. supported_living_or_disability =
   IDD, supported/shared living, group homes, day programmes, behavioural services. big_chain_corporate = a national
   multi-state operator's corporate entity. closed_or_not_found = no trace it operates. type_evidence = short quote.
   named_person_confirmed: "yes" if a source ties the licence contact ({contact}) to this agency, "no" if sources show
   someone else runs it now, "not_found" if nothing either way.
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
LIMIT_HIT = threading.Event()   # once the account says "session limit", stop calling (18 Nov: 1,172 calls wasted retrying)


def ask(row):
    p = PROMPT.format(name=row.get("name", ""), legal=row.get("legal", ""), city=row.get("city", ""), state=row.get("state", ""),
                      email=row.get("email", "") or "none", contact=row.get("contact", "") or "none",
                      site=row.get("companyWebsite", "") or "unknown")
    cmd = ["claude", "-p", "--no-session-persistence", "--model", "haiku", "--output-format", "json",
           "--permission-mode", "bypassPermissions", "--strict-mcp-config", "--tools", "WebSearch,WebFetch",
           "--json-schema", json.dumps(SCHEMA)]
    for att in range(3):
        if LIMIT_HIT.is_set():
            return {}, "skipped: account hit its usage limit"
        try:
            r = subprocess.run(cmd, input=p, capture_output=True, text=True, timeout=240)
            if r.returncode != 0:
                # claude -p puts the real reason (limit, auth) in stdout JSON, not stderr
                err = f"exit {r.returncode}: {(r.stderr or '')[:100]} | {(r.stdout or '')[:200]}"
                if "limit" in (r.stdout or "").lower() and "429" in (r.stdout or ""):
                    LIMIT_HIT.set(); return {}, err
                time.sleep(30 * (att + 1)); continue
            out = json.loads(r.stdout)
            so = out.get("structured_output")
            if isinstance(so, dict):
                u = out.get("usage") or {}
                stu = u.get("server_tool_use") or {}
                so["_meter"] = {"cost_usd": out.get("total_cost_usd"), "turns": out.get("num_turns"),
                                "in": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                                "cache_read": u.get("cache_read_input_tokens") or 0, "out": u.get("output_tokens") or 0,
                                "searches": stu.get("web_search_requests") or 0, "secs": round((out.get("duration_ms") or 0) / 1000)}
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
    cols = ["key", "name", "business_type", "type_evidence", "named_person_confirmed", "website", "owner_full_name", "owner_first_name", "owner_title", "linkedin_url", "evidence_url",
            "evidence_quote", "confidence", "error", "audit_route", "audit_classification",
            "m_cost_usd", "m_turns", "m_in", "m_cache_read", "m_out", "m_searches", "m_secs"]
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
            w.writerow({"key": r.get("key", ""), "name": r.get("name", ""), "business_type": so.get("business_type") or "",
                        "type_evidence": (so.get("type_evidence") or "")[:200], "named_person_confirmed": so.get("named_person_confirmed") or "",
                        "website": so.get("website") or "",
                        "owner_full_name": full, "owner_first_name": full.split()[0] if full else "", "owner_title": so.get("owner_title") or "",
                        "linkedin_url": li, "evidence_url": so.get("evidence_url") or "", "evidence_quote": (so.get("evidence_quote") or "")[:300],
                        "confidence": so.get("confidence") or "", "error": err,
                        "audit_route": "leads" if full else "review", "audit_classification": "named" if full else "no_name",
                        **{f"m_{k}": v for k, v in (so.get("_meter") or {}).items()}})
    print(f"  enrich: {len(rows)} agencies | named {found} | errors {sum(1 for v in res.values() if v[1])}", flush=True)


if __name__ == "__main__":
    main()
