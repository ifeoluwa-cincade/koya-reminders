#!/usr/bin/env python3
"""Free phone numbers straight off agencies' own websites (Ife 2026-10-23: no paid phone credits for now).

For each company: fetch the homepage and the usual contact/about paths (raw HTML, so `tel:` links survive), take
tel: links first, then international / North-American-looking numbers in the text, and keep only numbers that
`phonenumbers` says are valid. Every number is tagged source=website: for a small agency it is often the owner's
mobile, but it can be a main line, so callers can tell.

    python3 tools/cloud_phones.py --input <csv with companyWebsite or domain> --output <csv>
Runs in the cloud job (mode=phones); needs no API keys and no Claude.
"""
import argparse, asyncio, csv, html as html_lib, re
import httpx, phonenumbers

PATHS = ("", "/contact", "/contact-us", "/about", "/about-us", "/kontakt", "/contacto")
TEL = re.compile(r"""href=["']tel:([^"']{7,30})["']""", re.I)
# A number must stand on its own: no digit, dot, comma, slash or letter glued to either end, and no digit one space away. Without this, SVG path data
# ("... 3.582.553.9942 1.1259 ...", an icon used on several agency sites) read
# as the US number +1 582-553-9942 and went to four different agencies' call-list entries (Ife 29 Oct 2026).
EDGE_L, EDGE_R = r"(?<![\w.,/+\-])(?<!\d\s)", r"(?![\w,/]|[.\-]?\d|\s\d)"
INTL = re.compile(EDGE_L + r"\+\d{1,3}[\s.\-()]{0,2}\d{1,4}[\s.\-()]{0,2}\d{2,4}[\s.\-]?\d{2,4}(?:[\s.\-]?\d{2,4})?" + EDGE_R)
US = re.compile(EDGE_L + r"(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}" + EDGE_R)
TAG = re.compile(r"<[^>]+>")
# Never read as page text: scripts (framework payloads carry the SVG paths again as JSON), styles, SVG drawings, templates.
# ...except a structured "telephone" field (schema.org JSON-LD, also inside framework payloads): the site's own stated number.
LDTEL = re.compile(r"""\\?["']telephone\\?["']\s*:\s*\\?["']([+\d][\d\s.\-()]{6,28})\\?["']""", re.I)
HIDDEN = re.compile(r"<(script|style|svg|template|noscript)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.I | re.S)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
NA = {"US", "CA"}
EU = {"GB", "IE", "DE", "FR", "ES", "IT", "NL", "BE", "SE", "NO", "DK", "FI", "PL", "PT", "AT", "CH", "CZ", "RO", "GR", "HU",
      "BG", "HR", "SK", "SI", "LT", "LV", "EE", "LU", "MT", "CY"}


def valid(raw, default_region=None):
    try:
        n = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(n):
        return None
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164), phonenumbers.region_code_for_number(n), \
        phonenumbers.number_type(n)


def pick(html):
    for m in TEL.finditer(html):
        v = valid(m.group(1).strip()) or valid(m.group(1).strip(), "US")
        if v:
            return v, "tel link"
    for m in LDTEL.finditer(html):
        v = valid(m.group(1).strip()) or valid(m.group(1).strip(), "US")
        if v:
            return v, "schema telephone"
    text = html_lib.unescape(TAG.sub(" ", HIDDEN.sub(" ", html)))
    for m in INTL.finditer(text):
        v = valid(m.group(0))
        if v:
            return v, "text"
    for m in US.finditer(text):
        v = valid(m.group(0), "US")
        if v:
            return v, "text (US format)"
    return None, ""


async def one(client, sem, site):
    base = site.rstrip("/") if site.startswith("http") else "https://" + site.strip("/")
    async with sem:
        for p in PATHS:
            try:
                r = await client.get(base + p)
            except Exception:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                continue
            v, how = pick(r.text)
            if v:
                return v, f"website{p or '/'} ({how})"
    return None, ""


def drop_shared(sites, res):
    """A number read from page text (not a tel: link) that turns up on two or more different sites is a template or
    page artefact, not the owner's phone (29 Oct 2026: one SVG artefact on 4 sites, another on 26): dropped."""
    seen = {}
    for s, (v, how) in zip(sites, res):
        if v and ("tel link" not in how and "schema" not in how):
            seen.setdefault(v[0], set()).add(re.sub(r"^(https?://)?(www\.)?", "", s.lower()).split("/")[0])
    shared = {n for n, d in seen.items() if len(d) > 1}
    if shared:
        print(f"  dropped {len(shared)} number(s) found in the page text of more than one site")
    return [((None, "") if v and v[0] in shared and "tel link" not in how and "schema" not in how else (v, how)) for v, how in res]


async def run(sites):
    sem = asyncio.Semaphore(30)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client:
        return await asyncio.gather(*[one(client, sem, s) for s in sites])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.input, encoding="utf-8")))
    if a.limit:
        rows = rows[:a.limit]
    sites = [(r.get("companyWebsite") or r.get("website") or r.get("domain") or "").strip() for r in rows]
    res = asyncio.run(run(sites))
    res = drop_shared(sites, res)
    found = 0
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company_url", "domain", "phone", "phone_country", "phone_region", "phone_kind", "phone_source"])
        w.writeheader()
        for r, s, (v, how) in zip(rows, sites, res):
            row = {"company_url": s, "domain": r.get("domain", ""), "phone": "", "phone_country": "", "phone_region": "", "phone_kind": "", "phone_source": ""}
            if v:
                e164, cc, kind = v
                row.update(phone=e164, phone_country=cc, phone_region="NA" if cc in NA else "EU" if cc in EU else "other",
                           phone_kind={1: "mobile", 2: "fixed or mobile", 0: "landline"}.get(kind, "other"), phone_source=how)
                found += 1
            w.writerow(row)
    print(f"  phones: {found} of {len(rows)} companies have a valid number on their website")


if __name__ == "__main__":
    main()
