#!/usr/bin/env python3
"""
Founder waterfall (Ife 2026-10-14: "we need a better system for getting founders" -- 515 good agencies had no founder).

For every audit keeper AI Ark could not give a founder for, try the next source, cheapest first, and STOP at the first confirmed person:
  R1 Blitz employee-finder (C-Team, max 2)  free credits on our 2 keys; returns the LinkedIn URL directly
  R2 the company's own saved site text      claude CLI (subscription), name must appear verbatim in the text
  R3 Exa people search                      ~$0.007/company; the result must name the company AND a founder-type role
  R4 Exa LinkedIn lookup for names that have no LinkedIn URL yet (R2 finds names only)
Clinical rule: a person is accepted ONLY with evidence in the source text tying THEM to THIS company. No evidence -> null.
Every route caches by company the moment it answers; a re-run costs nothing. Output is in build_tab.py's founders format.

    python3 tools/founder_waterfall.py --limit 100 --out data/working/fw_sample.csv      # measure each route first
    python3 tools/founder_waterfall.py --out data/working/tierA_founders_waterfall.csv   # all of them
"""
import argparse, csv, json, re, sys, time, collections
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools")); sys.path.insert(0, str(ROOT / "pipeline"))
csv.field_size_limit(10**9)
from aiark_q4_lookalike import env, bare          # noqa: E402
from pipeline.lite_claude import call_claude_lite  # noqa: E402

C = ROOT / "data/caches"
RANK = [(r'\b(ceo|chief executive)', 0), (r'co-?founder|founder', 1), (r'\bowner\b', 2), (r'\bpresident\b', 3), (r'managing director|principal|managing partner', 4)]
ROLE = re.compile(r"co-?founder|founder|\bceo\b|chief executive|\bowner\b|\bpresident\b|managing director|principal", re.I)
norm = lambda s: re.sub(r'[^a-z0-9]', '', (s or '').lower())
slug = lambda u: (re.search(r'/in/([^/?#"]+)', u or '') or [None, ''])[1].lower().rstrip('/')


def rank(t):
    for p, n in RANK:
        if re.search(p, (t or '').lower()):
            return n
    return 9


def load(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {}


def save(p, o):
    Path(p).write_text(json.dumps(o))


def good_name(n):
    n = (n or '').strip()
    return bool(re.fullmatch(r"[^\W\d_][\w'’.\- ]+", n)) and 2 <= len(n.split()) <= 4 and len(n) <= 40


# ---------------- R1 Blitz
def r1_blitz(targets, cap):
    cache = load(C / "fw_blitz.json"); keys = [env("BLITZAPI_KEY_2"), env("BLITZAPI_KEY")]; keys = [k for k in keys if k]
    used = 0

    def one(t):
        nonlocal used
        u = (t["company_linkedin"] or "").strip()
        if not u or u in cache or used >= cap:
            return
        r = None                            # stays None if every attempt raised (network down / timeouts)
        for att in range(5):
            try:
                r = requests.post("https://api.blitz-api.ai/v2/search/employee-finder", headers={"x-api-key": keys[att % len(keys)], "Content-Type": "application/json"},
                                  json={"company_linkedin_url": u, "job_level": ["C-Team"], "max_results": 2}, timeout=60)
            except Exception:
                time.sleep(3); continue
            if r.status_code in (429, 500, 503):
                time.sleep(3 * (att + 1)); continue
            break
        if r is not None and r.status_code == 200:
            cache[u] = r.json().get("results") or []; used += len(cache[u])
        time.sleep(0.25)
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(one, targets))
    save(C / "fw_blitz.json", cache)
    out = {}
    for t in targets:
        res = cache.get((t["company_linkedin"] or "").strip()) or []
        def title(p): return ((p.get('experiences') or [{}])[0].get('job_title') if p.get('experiences') else None) or p.get('headline') or ''
        ok = [p for p in res if rank(title(p)) < 9 and p.get('linkedin_url') and good_name(p.get('full_name'))]
        if ok:
            p = sorted(ok, key=lambda p: rank(title(p)))[0]
            out[t["domain"]] = {"name": p["full_name"], "title": title(p), "linkedin": p["linkedin_url"], "route": "blitz"}
    return out


# ---------------- R2 site text via the claude CLI
SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "domain": {"type": "string"}, "name": {"type": ["string", "null"]}, "title": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
    "required": ["domain", "name", "title", "quote"]}}}, "required": ["items"]}
PROMPT = """For each company below you get text from its own website. Name the ONE person who founded or runs it (founder, co-founder, CEO, owner, president, managing director).
Rules: use ONLY what the text says. The person's full name (first and last) must appear in the text. If the text does not clearly name a founder/CEO/owner of THIS company, return null for name, title and quote.
Do not use clients, case-study people, testimonials, advisors or team members who are not the founder/CEO/owner. quote = the exact words from the text that name them (max 200 chars).
Return JSON {"items":[{"domain","name","title","quote"}]} with one item per company, same domains.

"""


def windows(text, n=1500):
    t = text or ""
    hits = [m.start() for m in re.finditer(r"founder|co-founder|\bceo\b|chief executive|\bowner\b|meet |about us|our story|managing director", t, re.I)]
    if not hits:
        return t[:n]
    out, last = [], -9999
    for h in hits:
        if h - last > 400:
            out.append(t[max(0, h - 200):h + 300]); last = h
        if sum(map(len, out)) >= n:
            break
    return " ... ".join(out)[:n + 600]


def r2_sitetext(targets, multi):
    cache = load(C / "fw_sitetext.json"); out = {}
    todo = [t for t in targets if t["domain"] not in cache]
    textof = {}
    for t in targets:
        e = None
        for u in (t.get("website"), "https://" + t["domain"], "http://" + t["domain"], "https://www." + t["domain"], "http://www." + t["domain"]):
            e = multi.get(u) if u else None
            if e: break
        tx = e.get("text", "") if isinstance(e, dict) else (e if isinstance(e, str) else "")
        textof[t["domain"]] = tx or ""
    batches = [todo[i:i + 8] for i in range(0, len(todo), 8)]

    def run(b):
        body = "\n\n".join(f"### {t['domain']} ({t['company']})\n{windows(textof[t['domain']])}" for t in b)
        try:
            res = call_claude_lite(PROMPT + body, json_schema=SCHEMA, model="haiku", timeout_sec=240)
            for it in (res or {}).get("items", []):
                d = (it.get("domain") or "").lower()
                nm = it.get("name"); tx = textof.get(d, "")
                ok = bool(nm) and good_name(nm) and all(p.lower() in tx.lower() for p in nm.split()[:2]) and ROLE.search((it.get("title") or "") + " " + (it.get("quote") or ""))
                cache[d] = {"name": nm, "title": it.get("title"), "quote": (it.get("quote") or "")[:200]} if ok else {"name": None}
        except Exception as ex:
            print("  r2 batch failed:", str(ex)[:100], flush=True)
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(run, batches))
    save(C / "fw_sitetext.json", cache)
    return {t["domain"]: {"name": cache[t["domain"]]["name"], "title": cache[t["domain"]].get("title") or "Founder", "linkedin": "", "route": "sitetext"}
            for t in targets if cache.get(t["domain"], {}).get("name")}


# ---------------- R3 Exa people search / R4 LinkedIn lookup
def exa(q, n=5):
    for att in range(4):
        try:
            x = requests.post("https://api.exa.ai/search", headers={"x-api-key": env("EXA_API_KEY"), "Content-Type": "application/json"},
                              json={"query": q, "category": "people", "numResults": n, "contents": {"text": {"maxCharacters": 500}}}, timeout=60)
        except Exception:
            time.sleep(3); continue
        if x.status_code == 429:
            time.sleep(3 * (att + 1)); continue
        return x.json().get("results", []) if x.status_code == 200 else None
    return None


def brand(t):
    return {b for b in {norm(t["company"]), norm(t["domain"].split(".")[0])} if len(b) >= 4}


def r3_exa(targets):
    cache = load(C / "fw_exa.json")

    def one(t):
        if t["domain"] in cache:
            return
        res = exa(f"{t['company']} founder CEO owner", 5)
        if res is None:
            return
        best = None
        for it in res:
            u = it.get("url") or ""
            if "/in/" not in u:
                continue
            title = it.get("title") or ""; blob = norm(title + " " + (it.get("text") or ""))
            nm = title.split(" - ")[0].split(" | ")[0].strip()
            if good_name(nm) and any(b in blob for b in brand(t)) and ROLE.search(title + " " + (it.get("text") or "")[:300]):
                best = {"name": nm, "title": (title.split(" - ")[1] if " - " in title else "Founder")[:60], "linkedin": "https://www.linkedin.com/in/" + slug(u)}; break
        cache[t["domain"]] = best or {"name": None}
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, targets))
    save(C / "fw_exa.json", cache)
    return {t["domain"]: {**cache[t["domain"]], "route": "exa"} for t in targets if cache.get(t["domain"], {}).get("name")}


def r4_linkedin(found, byd):
    cache = load(C / "fw_exa_li.json"); n = 0

    def one(item):
        d, f = item
        k = d + "|" + f["name"].lower()
        if f.get("linkedin") or k in cache:
            return
        res = exa(f"{f['name']} {byd[d]['company']} founder CEO owner LinkedIn", 5)
        if res is None:
            return
        nm = norm(f["name"]); best = None
        for it in res:
            u = it.get("url") or ""
            if "/in/" in u and nm in norm((it.get("title") or "") + " " + (it.get("text") or "") + " " + u) and any(b in norm((it.get("title") or "") + (it.get("text") or "")) for b in brand(byd[d])):
                best = "https://www.linkedin.com/in/" + slug(u); break
        cache[k] = best or ""
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, [(d, f) for d, f in found.items() if not f.get("linkedin")]))
    save(C / "fw_exa_li.json", cache)
    for d, f in found.items():
        if not f.get("linkedin"):
            f["linkedin"] = cache.get(d + "|" + f["name"].lower(), "")
            n += bool(f["linkedin"])
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default="data/working/tierA_lookalike_audit.csv")
    ap.add_argument("--companies", default="data/working/q4_lookalike_tierA_companies.csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--blitz-cap", type=int, default=450, help="max Blitz records to spend (1 per person returned)")
    ap.add_argument("--no-exa", action="store_true")
    a = ap.parse_args()
    aiark = load(C / "aiark_q4_founders_by_id.json")
    comp = {bare(r["domain"]): r for r in csv.DictReader(open(ROOT / a.companies)) if r.get("domain")}
    keep = [r for r in csv.DictReader(open(ROOT / a.audit)) if r["audit_route"] == "leads" and r["audit_crm_type"] != "Other Biz"]
    targets = []
    for r in keep:
        d = bare(r["company_url"]); c = comp.get(d)
        if not c or aiark.get(c.get("aiark_id")):
            continue
        targets.append({"domain": d, "company": c["name"], "website": r["company_url"], "company_linkedin": c.get("linkedin") or "", "type": r["audit_crm_type"], "aiark_id": c.get("aiark_id")})
    if a.limit:
        targets = targets[:a.limit]
    byd = {t["domain"]: t for t in targets}
    print(f"keepers without a founder from AI Ark: {len(targets)}", flush=True)
    found = {}
    multi = load(C / "multipage_audit_cache.json")
    for name, fn in (("R1 Blitz", lambda ts: r1_blitz(ts, a.blitz_cap)), ("R2 site text", lambda ts: r2_sitetext(ts, multi)),
                     ("R3 Exa search", r3_exa)):
        left = [t for t in targets if t["domain"] not in found]
        if not left or (name == "R3 Exa search" and a.no_exa):
            continue
        got = fn(left); found.update(got)
        print(f"{name}: asked {len(left)} -> new founders {len(got)} | total so far {len(found)} of {len(targets)}", flush=True)
    nli = r4_linkedin(found, byd) if not a.no_exa else 0
    print(f"R4 LinkedIn links added to names that had none: {nli}", flush=True)
    rows = []
    for d, f in found.items():
        t = byd[d]; parts = f["name"].split()
        rows.append({"FullName": f["name"], "FirstName": parts[0], "CompanyName": t["company"], "Website": t["website"], "Domain": d,
                     "Title": f.get("title") or "Founder", "LinkedIn": f.get("linkedin") or "", "Type": t["type"], "route": f["route"]})
    with open(ROOT / a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["FullName", "FirstName", "CompanyName", "Website", "Domain", "Title", "LinkedIn", "Type", "route"]); w.writeheader(); w.writerows(rows)
    print(f"\nfounders found: {len(rows)} of {len(targets)} ({len(rows)*100//max(len(targets),1)}%) | by route {dict(collections.Counter(r['route'] for r in rows))} | with a LinkedIn link {sum(1 for r in rows if r['LinkedIn'])}")
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
