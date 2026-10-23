#!/usr/bin/env python3
"""Geo check for domain-only leads (Ife 2026-10-23: PublicWWW gives no country, so NA/EU was never checked).

For each company: fetch the homepage and the usual contact/about paths (raw HTML, same as cloud_phones.py), then
collect three free signals and one cheap Claude read:
  1. phone countries: every valid number on the site (tel: links and text), via phonenumbers
  2. country-code web address (.in, .com.au, .co.uk ...); .ai/.io/.co and other "generic" endings are ignored
  3. Haiku reads short windows of site text around address words (address, based in, headquarters, ltd, inc ...)
     and names the country the business is based in, with the exact words as evidence. 25 companies per call.

Verdict: NA / EU when the signals that exist agree on it, other when they point outside, mixed when they disagree,
unknown when there is no signal. Only NA / EU should be pushed; mixed and unknown are for a human look.

    python3 tools/cloud_geo.py --input <csv with companyWebsite or domain> --output <csv>
Runs in the cloud job (mode=geo).
"""
import argparse, asyncio, csv, json, os, re, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx, phonenumbers

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
from cloud_phones import PATHS, TEL, INTL, US, TAG, UA, NA, EU  # noqa: E402

EU = EU | {"NO", "CH", "IS", "LI", "RS", "UA", "BA", "ME", "MK", "AL", "MD"}      # Europe, not only the EU
GENERIC = {"ai", "io", "co", "me", "tv", "app", "gg", "ly", "so", "to", "sh", "fm", "cc", "ws", "vc", "eu"}
CC = {"uk": "GB"}
ADDR = re.compile(r"address|based in|headquarter|\bhq\b|located in|office|registered|ltd|llc|\binc\b|gmbh|b\.v\.|s\.l\.|"
                  r"pvt|pty|sarl|street|\bst\.|avenue|suite|road|postcode|zip|copyright|©", re.I)
PROMPT = """For each company below you get short pieces of text from its own website. Say which COUNTRY the business itself is based in.
Use only what the text says: a postal address, "based in", a registered company line, an office location. Client locations, "we serve clients worldwide" and lists of countries they work with do NOT count.
If several offices, give the one that looks like the headquarters. If the text does not say, return null.
country = ISO 3166 two-letter code (US, GB, DE, IN ...). evidence = the exact words from the text, max 120 chars.
Return JSON {"items":[{"domain","country","evidence"}]} with one item per company, same domains.

"""
SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "domain": {"type": "string"}, "country": {"type": ["string", "null"]}, "evidence": {"type": ["string", "null"]}},
    "required": ["domain", "country", "evidence"]}}}, "required": ["items"]}


def dom(u):
    return (u or "").lower().split("//")[-1].split("/")[0].removeprefix("www.")


def region(cc):
    return "NA" if cc in NA else "EU" if cc in EU else "other" if cc else ""


def phone_countries(html):
    out = []
    for m in TEL.finditer(html):
        for reg in (None, "US"):
            try:
                n = phonenumbers.parse(m.group(1).strip(), reg)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_valid_number(n):
                out.append(phonenumbers.region_code_for_number(n)); break
    text = TAG.sub(" ", html)
    for pat, reg in ((INTL, None), (US, "US")):
        for m in pat.finditer(text):
            try:
                n = phonenumbers.parse(m.group(0), reg)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_valid_number(n):
                out.append(phonenumbers.region_code_for_number(n))
    return out


def snippets(text, n=1200):
    out, last = [], -9999
    for m in ADDR.finditer(text):
        if m.start() - last > 300:
            out.append(text[max(0, m.start() - 150):m.start() + 200]); last = m.start()
        if sum(map(len, out)) >= n:
            break
    return " ... ".join(out)[:n + 400]


async def fetch(client, sem, site):
    base = site.rstrip("/") if site.startswith("http") else "https://" + site.strip("/")
    phones, text = [], ""
    async with sem:
        for p in PATHS:
            try:
                r = await client.get(base + p)
            except Exception:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                continue
            phones += phone_countries(r.text)
            text += " " + re.sub(r"\s+", " ", TAG.sub(" ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", r.text)))
    return phones, snippets(text)


async def fetch_all(sites):
    sem = asyncio.Semaphore(30)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client:
        return await asyncio.gather(*[fetch(client, sem, s) for s in sites])


def read_countries(items):
    from pipeline.lite_claude import call_claude_lite
    found = {}

    def run(b):
        body = "\n\n".join(f"### {d}\n{t}" for d, t in b)
        try:
            res = call_claude_lite(PROMPT + body, json_schema=SCHEMA, model="haiku", timeout_sec=240)
        except Exception:
            return
        for it in (res or {}).get("items", []):
            c = (it.get("country") or "").upper().strip()
            if len(c) == 2:
                found[(it.get("domain") or "").lower()] = (c.replace("UK", "GB"), (it.get("evidence") or "")[:120])
    batches = [items[i:i + 25] for i in range(0, len(items), 25)]
    with ThreadPoolExecutor(int(os.environ.get("AUDIT_WORKERS", "4"))) as ex:
        list(ex.map(run, batches))
    return found


def verdict(site_cc, phone_ccs, tld_cc):
    """NA / EU / other when every signal agrees on in-or-out of NA+EU, mixed when they disagree, unknown with none."""
    regs = [region(c) for c in [site_cc, tld_cc] + phone_ccs if c]
    if not regs:
        return "unknown"
    inside = {r in ("NA", "EU") for r in regs}
    if inside == {False}:
        return "other"
    if inside == {True}:
        return region(site_cc) if site_cc else Counter(regs).most_common(1)[0][0]
    return "mixed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.input, encoding="utf-8")))
    if a.limit:
        rows = rows[:a.limit]
    sites = [(r.get("companyWebsite") or r.get("website") or r.get("domain") or "").strip() for r in rows]
    fetched = asyncio.run(fetch_all(sites))
    items = [(dom(s), t) for s, (_, t) in zip(sites, fetched) if t]
    site = read_countries(items)
    cols = ["company_url", "domain", "geo", "site_country", "site_evidence", "phone_countries", "tld_country"]
    counts = Counter()
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r, s, (ph, _) in zip(rows, sites, fetched):
            d = dom(s)
            tld = d.rsplit(".", 1)[-1]
            tld_cc = "" if tld in GENERIC or len(tld) != 2 else CC.get(tld, tld.upper())
            sc, ev = site.get(d, ("", ""))
            ph = sorted(set(ph))
            g = verdict(sc, ph, tld_cc)
            counts[g] += 1
            w.writerow({"company_url": s, "domain": r.get("domain") or d, "geo": g, "site_country": sc, "site_evidence": ev,
                        "phone_countries": " ".join(ph), "tld_country": tld_cc})
    print(f"  geo: {len(rows)} companies | " + " ".join(f"{k} {v}" for k, v in counts.most_common()))


if __name__ == "__main__":
    main()
