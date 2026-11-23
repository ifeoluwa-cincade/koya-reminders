#!/usr/bin/env python3
"""Texas business officers from the Comptroller's free public franchise tax data (23 Nov 2026, home care: prove whether
a licence "Administrator" is the owner). Texas blocks non-US connections, so this runs in the cloud job (mode=txofficers).

For each row (name = legal entity name, person = the administrator): search franchise tax entities by name, take the
best name match, read its details (officers / directors / managing members from the Public Information Report) and say
whether the person is listed and with what title.
    python3 tools/tx_officers.py --input <csv: key,name,person> --output <csv> [--limit N] [--probe]
"""
import argparse, csv, json, re, time, urllib.parse
import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Origin": "https://comptroller.texas.gov",
      "Referer": "https://comptroller.texas.gov/taxes/franchise/account-status/search"}
BASES = ["https://api.comptroller.texas.gov/public-data/v1/public"]
SEARCH = ["{b}/franchise-tax-list?name={q}", "{b}/franchise-tax?name={q}"]
DETAIL = ["{b}/franchise-tax/{id}"]


def norm(s):
    s = re.sub(r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|ltd|pllc|lp|the|dba)\b\.?", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def get(c, url):
    for t in range(3):
        try:
            r = c.get(url, headers=UA, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (t + 1)); continue
            return r
        except Exception as e:
            err = e; time.sleep(3)
    return None


def probe(c, name):
    q = urllib.parse.quote(name)
    for b in BASES:
        for s in SEARCH:
            u = s.format(b=b, q=q); r = get(c, u)
            print("PROBE", u, r.status_code if r else None, (r.text[:1500] if r else ""), flush=True)
    for u in ["https://api-doc.comptroller.texas.gov/public-data/", "https://comptroller.texas.gov/taxes/franchise/account-status/search"]:
        r = get(c, u)
        if r is not None:
            js = re.findall(r'src="([^"]+\.js[^"]*)"', r.text)
            print("PAGE", u, r.status_code, "scripts:", js[:10], flush=True)
            for sc in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.S):
                if "apiUrl" in sc or "officer" in sc.lower():
                    print("SCRIPT", re.sub(r"\s+", " ", sc)[:6000], flush=True)
            for m in re.finditer(r"(api[a-z0-9.\-]*\.comptroller[^\"'\s<]*|x-api-key[^\n]{0,120}|apikey[^\n]{0,120}|ajax\([^\n]{0,300}|fetch\([^\n]{0,300}|url\s*:\s*[^\n]{0,200})", r.text, re.I):
                print("INLINE", m.group(0)[:300], flush=True)
            for j in [x for x in js if "comptroller" in x or x.startswith("/")][:8]:
                ju = urllib.parse.urljoin(u, j); jr = get(c, ju)
                if jr is not None:
                    hits = sorted(set(re.findall(r'https?://[a-z0-9.\-]*comptroller[a-z0-9.\-/{}_:?=&]*', jr.text)))[:40]
                    paths = sorted(set(re.findall(r'["\'](/[a-z0-9\-/{}_]*franchise[a-z0-9\-/{}_]*)["\']', jr.text, re.I)))[:40]
                    print("JS", ju, hits, paths, flush=True)


def records(j):
    if isinstance(j, dict):
        for k in ("data", "results", "items", "records"):
            if isinstance(j.get(k), list):
                return j[k]
        return [j]
    return j if isinstance(j, list) else []


def one(c, row):
    q = urllib.parse.quote(re.split(r"\bdba\b", row["name"], flags=re.I)[0].strip()[:50])
    best = None
    for b in BASES:
        for s in SEARCH:
            r = get(c, s.format(b=b, q=q))
            if r is None or r.status_code != 200:
                continue
            try:
                recs = records(r.json())
            except ValueError:
                continue
            for x in recs:
                nm = x.get("name") or x.get("taxpayerName") or x.get("entityName") or ""
                if norm(nm) == norm(row["name"]) or (norm(row["name"]) and norm(row["name"]) in norm(nm)):
                    best = x; break
            if best: break
        if best: break
    if not best:
        return {**row, "tx_id": "", "officers": "", "person_listed": "", "person_title": "", "status": "no_entity"}
    tid = best.get("taxpayerId") or best.get("id") or best.get("taxpayerNumber") or ""
    det = None
    for b in BASES:
        for d in DETAIL:
            r = get(c, d.format(b=b, id=tid))
            if r is not None and r.status_code == 200:
                try: det = r.json(); break
                except ValueError: pass
        if det: break
    blob = json.dumps(det or best)
    offs = []
    def walk(o):
        if isinstance(o, dict):
            n = o.get("name") or o.get("officerName") or o.get("agentName")
            t = o.get("title") or o.get("officerTitle") or ""
            if n and t: offs.append((str(n), str(t)))
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(det or best)
    pt = [t for t in re.findall(r"[a-z]+", row["person"].lower()) if len(t) > 2]
    hit = [(n, t) for n, t in offs if pt and pt[-1] in n.lower() and (pt[0] in n.lower() or len(pt) == 1)]
    return {**row, "tx_id": tid, "officers": "; ".join(f"{n} ({t})" for n, t in offs)[:500],
            "person_listed": "yes" if hit else "no", "person_title": hit[0][1] if hit else "", "status": "ok" if offs else "no_officers",
            "raw": blob[:300] if not offs else ""}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--probe", action="store_true"); a = ap.parse_args()
    rows = list(csv.DictReader(open(a.input)))
    if a.limit: rows = rows[: a.limit]
    with httpx.Client(follow_redirects=True) as c:
        if a.probe:
            probe(c, rows[0]["name"])
        out = []
        for r in rows:
            out.append(one(c, r)); time.sleep(0.4)
    keys = []
    for o in out:
        for k in o:
            if k not in keys: keys.append(k)
    with open(a.output, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys); w.writeheader(); w.writerows(out)
    from collections import Counter
    print("txofficers:", dict(Counter(o["status"] for o in out)), dict(Counter(o["person_listed"] for o in out)))


if __name__ == "__main__":
    main()
