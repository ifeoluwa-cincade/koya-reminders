"""AI Ark lookalike companies for the Q4 AI Agency Owners campaign.

Seeds = AI agencies that booked a call / had an ongoing convo (AAT sheet) plus
Attio deals at Won / Trial / Meeting Booked. For each seed, one lookalike page
(up to 100 companies, 0.1 credit each) limited to North America (US, CA) and
Europe, 1-50 staff. Results are cached per seed, so a re-run costs nothing.

Then drops anything we already know: Attio company domains, Attio people email
domains and every email domain on the central blocklist.

    python3 tools/aiark_q4_lookalike.py --limit 10          # sample
    python3 tools/aiark_q4_lookalike.py                     # all seeds

Writes data/working/q4_aiark_lookalike_companies.csv (new companies only).
"""
import argparse, csv, json, os, sys, time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))

CACHE = ROOT / "data/caches/aiark_q4_lookalike"
OUT = ROOT / "data/working/q4_aiark_lookalike_companies.csv"
SEEDS = ROOT / "data/working/q4_aiark_seeds.json"
URL = "https://api.ai-ark.com/api/developer-portal/v1/companies"
BLOCKLIST_ID = "1Ju0bPJR85z4O3iflKrxeqEoaAos37BcAdJBeSzgS-UI"

# Ife 2026-10-04: North America and Europe only (no Mexico, no Turkey).
LOCATIONS = [
    "United States", "Canada",
    "United Kingdom", "Ireland", "Germany", "France", "Netherlands", "Belgium",
    "Luxembourg", "Spain", "Portugal", "Italy", "Malta", "Switzerland", "Austria",
    "Sweden", "Denmark", "Norway", "Finland", "Iceland", "Poland", "Czech Republic",
    "Slovakia", "Hungary", "Romania", "Bulgaria", "Greece", "Cyprus", "Croatia",
    "Slovenia", "Serbia", "Lithuania", "Latvia", "Estonia", "Ukraine",
]
SIZES = [{"start": 1, "end": 10}, {"start": 11, "end": 50}]
FREE = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
        "aol.com", "live.com", "protonmail.com", "proton.me", "me.com", "msn.com",
        "googlemail.com", "yahoo.co.uk", "hotmail.co.uk", "mail.com", "gmx.com"}


def env(name):
    if os.environ.get(name):
        return os.environ[name]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit(f"{name} missing")


def bare(d):
    d = (d or "").lower().strip()
    for p in ("https://", "http://"):
        d = d.removeprefix(p)
    return d.removeprefix("www.").split("/")[0].split("?")[0]


def lookalike(key, seed):
    cp = CACHE / f"{seed.replace('.', '_')}.json"
    if cp.exists():
        return json.loads(cp.read_text()), True
    body = {"lookalikeDomains": [seed],
            "account": {"employeeSize": {"type": "RANGE", "range": SIZES},
                        "location": {"any": {"include": LOCATIONS}}},
            "page": 0, "size": 100}
    for attempt in range(4):
        try:
            r = requests.post(URL, headers={"X-TOKEN": key, "Content-Type": "application/json"},
                              json=body, timeout=90)
        except requests.RequestException as e:
            print(f"  {seed}: {type(e).__name__}, retrying"); time.sleep(5 * (attempt + 1)); continue
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1)); continue
        if r.status_code != 200:
            print(f"  {seed}: HTTP {r.status_code} {r.text[:300]}")
            return None, False
        data = r.json()
        CACHE.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(data))
        return data, False
    return None, False


def parse(item, seed):
    s = item.get("summary") or {}
    links = item.get("link") or item.get("links") or {}
    loc = item.get("location") or {}
    hq = loc.get("headquarter") or loc.get("headquarters") or loc
    website = links.get("website") or item.get("website") or ""
    staff = s.get("staff") or {}
    return {
        "name": s.get("name") or item.get("name") or "",
        "domain": bare(links.get("domain") or item.get("domain") or website),
        "website": website,
        "linkedin": links.get("linkedin") or "",
        "country": (hq.get("country") if isinstance(hq, dict) else "") or "",
        "staff": staff.get("total") if isinstance(staff, dict) else staff,
        "industry": s.get("industry") or "",
        "description": (s.get("description") or "").replace("\n", " ")[:600],
        "aiark_id": item.get("id") or "",
        "seed": seed,
    }


def known_domains():
    from attio_client import AttioClient
    os.environ.setdefault("ATTIO_KEY_VAR", "ATTIO_API_KEY_2")
    H = AttioClient()
    doms = set()
    for c in H.query_all("companies"):
        for v in c["values"].get("domains") or []:
            doms.add(bare(v.get("domain")))
    n_co = len(doms)
    for p in H.query_all("people"):
        for v in p["values"].get("email_addresses") or []:
            e = (v.get("email_address") or "").lower()
            if "@" in e and e.split("@")[1] not in FREE:
                doms.add(e.split("@")[1])
    n_attio = len(doms)
    # central blocklist: read the real row count first (a fixed range truncates)
    import subprocess
    def gws(args):
        out = subprocess.run(["gws", "sheets", "spreadsheets"] + args, capture_output=True, text=True).stdout
        return json.loads("\n".join(l for l in out.splitlines() if not l.startswith("Using keyring")))
    meta = gws(["get", "--params", json.dumps({"spreadsheetId": BLOCKLIST_ID, "fields": "sheets.properties"})])
    for sh in meta["sheets"]:
        t, rows = sh["properties"]["title"], sh["properties"]["gridProperties"]["rowCount"]
        if "blocklist" not in t.lower():
            continue
        vals = gws(["values", "get", "--params", json.dumps(
            {"spreadsheetId": BLOCKLIST_ID, "range": f"'{t}'!A1:Z{rows}"})]).get("values", [])
        hdr = [h.lower() for h in vals[0]] if vals else []
        ei = hdr.index("email") if "email" in hdr else None
        for r in vals[1:]:
            for cell in r:
                cell = cell.lower()
                if "@" in cell and " " not in cell.strip():
                    d = cell.strip().split("@")[1]
                    if d not in FREE:
                        doms.add(d)
        print(f"  blocklist tab '{t}': {len(vals) - 1} rows")
    print(f"  known domains: Attio companies {n_co}, +people {n_attio - n_co}, total with blocklist {len(doms)}")
    if len(doms) < 5000:
        sys.exit("known-domain set looks too small - a read failed; refusing to continue")
    return doms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    key = env("AI_ARK_KEY_2")
    seeds = json.loads(SEEDS.read_text())
    seeds = seeds[: a.limit] if a.limit else seeds
    cached = sum((CACHE / f"{s.replace('.', '_')}.json").exists() for s in seeds)
    print(f"{len(seeds)} seeds, {cached} cached, {len(seeds) - cached} will call AI Ark (max {(len(seeds) - cached) * 10} credits)")

    known = known_domains()
    seen, rows, per_seed = set(), [], []
    for i, seed in enumerate(seeds, 1):
        data, hit = lookalike(key, seed)
        if data is None:
            continue
        items = data.get("content") or data.get("data") or []
        new = 0
        for it in items:
            c = parse(it, seed)
            if not c["domain"] or c["domain"] in known or c["domain"] in seen or c["domain"] in seeds:
                continue
            seen.add(c["domain"]); rows.append(c); new += 1
        per_seed.append((seed, len(items), new))
        print(f"  [{i}/{len(seeds)}] {seed}: {len(items)} returned, {new} new{' (cache)' if hit else ''}")
        if not hit:
            time.sleep(0.4)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["domain"])
        w.writeheader(); w.writerows(rows)
    raw = sum(n for _, n, _ in per_seed)
    print(f"\nreturned {raw}, new unique companies {len(rows)} -> {OUT}")


if __name__ == "__main__":
    main()
