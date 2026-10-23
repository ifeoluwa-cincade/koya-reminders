#!/usr/bin/env python3
"""Founders in the cloud (Ife 2026-10-23: "this should be in the cloud"): for audit keepers that the free website step
did not name a founder for, run Blitz and Exa on GitHub's machines instead of the Mac.

  1. Blitz /v2/enrichment/domain-to-linkedin for rows with no company LinkedIn (1 record per hit, 0 on a miss),
     capped by BLITZ_DOMAIN_CAP (default 200) so the monthly records are not burnt in one run.
  2. tools/founder_waterfall.py on the result, unchanged: Blitz employee-finder (cap BLITZ_PEOPLE_CAP, default 150),
     site text, Exa people search, Exa LinkedIn lookup. Same evidence rules as on the Mac.

Input CSV columns: name, domain, companyWebsite, linkedin (the cloud_audit shard format), plus audit_classification.
Output (data/working/cloud_shard_audit.csv via --output): founder rows in build_tab.py's format with a Domain column.
Keys come from the environment (GitHub secrets): BLITZAPI_KEY, BLITZAPI_KEY_2, EXA_API_KEY.
"""
import argparse, csv, os, subprocess, sys, time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def dom(u):
    return (u or "").lower().split("//")[-1].split("/")[0].removeprefix("www.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.input, encoding="utf-8")))
    if a.limit:
        rows = rows[:a.limit]
    keys = [k for k in (os.environ.get("BLITZAPI_KEY_2"), os.environ.get("BLITZAPI_KEY")) if k]
    cap, used = int(os.environ.get("BLITZ_DOMAIN_CAP", "200")), 0
    for r in rows:
        if r.get("linkedin") or not keys or used >= cap:
            continue
        for k in keys:
            try:
                j = requests.post("https://api.blitz-api.ai/v2/enrichment/domain-to-linkedin",
                                  headers={"x-api-key": k, "Content-Type": "application/json"},
                                  json={"domain": r["domain"]}, timeout=60).json()
            except Exception:
                time.sleep(2); continue
            if j.get("found"):
                used += 1
                r["linkedin"] = j.get("company_linkedin_url") or ""
                r["name"] = r.get("name") or j.get("company_name") or ""
            break
    print(f"  founders: blitz company pages {sum(1 for r in rows if r.get('linkedin'))} of {len(rows)} ({used} records)")

    work = ROOT / "data/working"; work.mkdir(parents=True, exist_ok=True); (ROOT / "data/caches").mkdir(parents=True, exist_ok=True)
    aud, comp = work / "cf_audit.csv", work / "cf_companies.csv"
    crm = {"ai_agency": "AI Agency", "ai_gtm_systems": "AI GTM Systems"}
    with open(aud, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["company_url", "audit_route", "audit_crm_type"]); w.writeheader()
        for r in rows:
            w.writerow({"company_url": r.get("companyWebsite") or "https://" + r["domain"], "audit_route": "leads",
                        "audit_crm_type": crm.get(r.get("audit_classification", ""), "AI Agency")})
    with open(comp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "domain", "website", "linkedin", "aiark_id"]); w.writeheader()
        for r in rows:
            w.writerow({"name": r.get("name") or r["domain"].split(".")[0].replace("-", " ").title(), "domain": r["domain"],
                        "website": r.get("companyWebsite", ""), "linkedin": r.get("linkedin", ""), "aiark_id": ""})
    p = subprocess.run([sys.executable, str(ROOT / "tools/founder_waterfall.py"), "--audit", str(aud), "--companies", str(comp),
                        "--out", a.output, "--blitz-cap", os.environ.get("BLITZ_PEOPLE_CAP", "150")],
                       capture_output=True, text=True)
    for line in (p.stdout + p.stderr).splitlines():
        if line.startswith(("R1", "R2", "R3", "R4", "founders found")):
            print("  " + line.split("|")[0].strip() if "->" not in line else "  " + line[:120])
    if not Path(a.output).exists():
        with open(a.output, "w") as f:
            f.write("FullName,FirstName,CompanyName,Website,Domain,Title,LinkedIn,Type\n")


if __name__ == "__main__":
    main()
