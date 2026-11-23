#!/usr/bin/env python3
"""Find a small business's own website from its name + city + state, free and without Claude (23 Nov 2026, home care
agencies from state licence lists that have an owner name but no website or email).

For each row: Exa search (EXA_API_KEY_2, ~$0.007/search; DuckDuckGo blocks bots) for "<name> home care <city> <state>",
skip directory/social sites, open each
candidate homepage (+ /contact, /about) and accept it only if the page proves it is that business:
  - the business phone number appears on it, or
  - every distinctive word of the business name appears on it (generic words like home/care/services are ignored;
    a name made only of generic words needs the phone match).
    python3 tools/cloud_sitefind.py --input <csv: key,name,city,state,phone> --output <csv>
Runs in the cloud job (mode=sitefind).
"""
import argparse, asyncio, csv, html as html_lib, random, re, urllib.parse
import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
SKIP = re.compile(r"(yelp|care\.com|caring\.com|facebook|linkedin|instagram|twitter|x\.com|youtube|indeed|glassdoor|bbb\.org|"
                  r"npiregistry|npino|npidb|hipaaspace|opencorporates|bizapedia|mapquest|yellowpages|manta|dnb\.com|zoominfo|"
                  r"seniorly|aplaceformom|homecare\.com|homeadvisor|angi|chamberofcommerce|buzzfile|cms\.gov|medicare\.gov|"
                  r"\.gov\b|wikipedia|google\.|bing\.|duckduckgo|apple\.com|nextdoor|ziprecruiter|simplyhired|salary|"
                  r"homecareassistance\.com/locations|agingcare|seniorcareauthority|carelistings|providerdata|healthgrades|"
                  r"wellness\.com|webmd|vitals\.com|hometownlocator|allbiz|corporationwiki|sos\.|govtribe|birdeye|"
                  r"carecompare|caregiverlist|homecareadvocacy|nccareconnect|homecarepulse|healthcarefacilities|licenseindex|"
                  r"directory|listing|finder|locator|reviews?\.|npi|provider-view|carepathways|seniorhomes|homecaremag|exa\.ai|careavailability|homecarelist|carelisting)", re.I)
GENERIC = {"home", "homes", "care", "cares", "caring", "services", "service", "llc", "inc", "the", "of", "and", "in", "health",
           "healthcare", "agency", "homecare", "senior", "seniors", "solutions", "group", "corp", "corporation", "company",
           "co", "ltd", "pllc", "dba", "assisted", "living", "in-home", "inhome", "personal", "a", "an", "at", "for", "by",
           "nc", "ca", "of", "staffing", "support", "supportive", "management", "family", "comfort", "quality", "loving",
           "angels", "angel", "touch", "hands", "heart", "hearts", "plus", "llp", "lp", "enterprises", "enterprise",
           "provider", "providers", "residential", "elder", "eldercare", "companion", "companions", "nursing"}
TAG = re.compile(r"<[^>]+>"); HIDDEN = re.compile(r"<(script|style|svg|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)


def words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())]


def distinctive(name):
    name = re.split(r"\bdba\b", name, flags=re.I)[-1]
    return [w for w in words(name) if w not in GENERIC and len(w) > 2]


def digits(p):
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else ""


async def search(c, q):
    import os
    key = os.environ.get("EXA_API_KEY_2") or os.environ.get("EXA_API_KEY")
    for attempt in range(4):
        try:
            r = await c.post("https://api.exa.ai/search", headers={"x-api-key": key, "Content-Type": "application/json"},
                             json={"query": q, "numResults": 8})
            if r.status_code == 200:
                return [x["url"] for x in r.json().get("results", [])]
            if r.status_code in (402, 403) or "credits" in r.text.lower():
                print("exa out of credits", flush=True); return None
        except Exception:
            pass
        await asyncio.sleep(3 * (attempt + 1) + random.random() * 2)
    return None


async def page_text(c, url):
    try:
        r = await c.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
        if r.status_code >= 400:
            return ""
        h = r.text[:600000]
        tel = " ".join(re.findall(r'href=["\']tel:([^"\']+)', h, re.I))
        return (html_lib.unescape(TAG.sub(" ", HIDDEN.sub(" ", h))) + " " + tel).lower()
    except Exception:
        return ""


async def check(c, url, row):
    p = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    root = f"{p.scheme or 'https'}://{p.netloc}"
    dw, ph = distinctive(row["name"]), digits(row.get("phone"))
    for path in ("", "/contact", "/contact-us", "/about", "/about-us"):
        t = await page_text(c, root + path)
        if not t:
            if path == "":
                return None
            continue
        if ph and ph in re.sub(r"\D", "", t):
            return root, "phone"
        if dw and all(re.search(r"\b" + re.escape(w) + r"\b", t) for w in dw):
            host = re.sub(r"[^a-z0-9]", "", p.netloc.lower())
            if len(dw) >= 2 or dw[0] in host:          # one-word names must also be in the web address
                return root, "name:" + "+".join(dw)
    return None


async def one(c, sem, row):
    async with sem:
        nm = re.split(r"\bdba\b", row["name"], flags=re.I)[-1].strip()
        q = f'{nm} home care {row.get("city", "")} {row.get("state", "")}'
        await asyncio.sleep(random.random() * 2)
        urls = await search(c, q)
        if urls is None:
            return {**row, "site": "", "domain": "", "match": "search_failed"}
        seen = set()
        for u in urls[:6]:
            host = urllib.parse.urlparse(u).netloc.lower().removeprefix("www.")
            if not host or SKIP.search(host) or host in seen:
                continue
            seen.add(host)
            hit = await check(c, u, row)
            if hit:
                return {**row, "site": hit[0], "domain": host, "match": hit[1]}
        return {**row, "site": "", "domain": "", "match": "not_found"}


async def main_async(a):
    rows = list(csv.DictReader(open(a.input)))
    if a.limit:
        rows = rows[: a.limit]
    sem = asyncio.Semaphore(a.parallel)
    async with httpx.AsyncClient(timeout=25, http2=False) as c:
        res = await asyncio.gather(*[one(c, sem, r) for r in rows])
    with open(a.output, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(res[0].keys())); w.writeheader(); w.writerows(res)
    from collections import Counter
    print("sitefind:", dict(Counter(r["match"].split(":")[0] for r in res)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--parallel", type=int, default=3)
    asyncio.run(main_async(ap.parse_args()))
