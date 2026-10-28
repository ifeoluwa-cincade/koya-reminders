"""
collect_ops.py — what the Opportunities and Systems & operations tabs show. READ ONLY.

Runs inside the hourly dashboard job (and by hand), writes data/working/ops.json which build_cold_email.py bakes
into the page. Every part is allowed to fail on its own: a failed part is recorded as a red check or an empty list,
so the page still builds and says what it could not read.

OPPORTUNITIES  one row per person who showed real interest, from two places:
  - open deals in Attio (Interested, Meeting Booked, Later, Profiles Sent, Trial)
  - call-list entries whose call result is a real one (meeting booked, send follow-up, callback, not now)
  Each row carries the links Ife asked for (Attio, LinkedIn, CloudTalk), the call summary, and the last few
  messages of the PlusVibe email thread, so a lead can be read without opening four tools.
  The follow-up date is the deal's own next_action_at when set, otherwise last touch + a rule per outcome (Ife 2026-10-09):
  meeting booked +3 days (check after the meeting), follow-up and interested +2, callback +1, not now / later +30.

CHECKS  one per automation, each with a status: ok / warn / bad / info.

    ATTIO_KEY_VAR=ATTIO_API_KEY_2 python crm/dashboard/collect_ops.py
"""
from __future__ import annotations
import collections, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crm" / "scripts"))
from attio_client import AttioClient, api_key  # noqa: E402
import collect_metrics as CM  # noqa: E402
sys.path.insert(0, str(ROOT / "crm" / "dashboard"))
import lead_company as LC  # noqa: E402
import lead_bucket as LB  # noqa: E402
import lead_source as LS  # noqa: E402

OUT = ROOT / "data/working/ops.json"
SCHEMA = ROOT / "crm/schema/attio-schema.yaml"
WS = "6a6e634971ad0f0e174a5471"
PV = "https://api.plusvibe.ai/api/v1"
CT = "https://my.cloudtalk.io/api"
REPO = os.environ.get("GITHUB_REPOSITORY") or "ifeoluwa-cincade/koya-reminders"
CALLS_FROM = "2026-10-11"   # the call job on the public repo starts here; older calls were handled by the old job and are already in the CRM
OPEN_STAGES = {"Interested", "Multi-touch", "Meeting Booked", "Later", "Profiles Sent", "Trial"}
# A call is an opportunity only when something was agreed or asked for. "Not now" (a polite decline with no current need) is NOT one (Ife, 2026-10-12);
# the caller's own note decides first, so "send email" / "follow up" on a call makes it SEND_FOLLOW_UP and it appears here.
REAL_CALL = LB.REAL_CALL
# days after the last touch before the lead is due again
DUE_DAYS = {"meeting": 3, "aftercall": 2, "followup": 2, "callback": 1, "later": 30, "chasing": 2, "lost": 0, "won": 0}
BUCKET_LABEL = {"meeting": "Meeting booked", "aftercall": "After the call", "followup": "Follow up", "chasing": "Multi-touch", "callback": "Callback", "later": "Not now", "lost": "Lost", "won": "Won"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(ts) -> datetime | None:
    x = str(ts or "").replace("Z", "+00:00")
    m = re.match(r"(.*\.\d{6})\d*(.*)", x)
    if m:
        x = m.group(1) + m.group(2)
    try:
        d = datetime.fromisoformat(x)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def val(rec, slug):
    x = (rec["values"].get(slug) or [{}])[0]
    return ((x.get("status") or {}).get("title") or (x.get("option") or {}).get("title")
            or x.get("full_name") or x.get("email_address") or x.get("original_phone_number")
            or x.get("value") or x.get("target_record_id") or x.get("domain"))


def stage_since(d):
    """When the deal's current stage was set (Attio keeps it as active_from on the stage value)."""
    x = (d["values"].get("stage") or [{}])[0]
    return x.get("active_from")


def closed_on(d):
    """Won / Lost date: the close_date when someone filled it, else the moment the stage became Won / Lost."""
    if not str(val(d, "stage") or "").startswith(("Lost", "Won")):
        return None
    t = parse(val(d, "close_date") or stage_since(d))      # Attio gives nanoseconds; the page's Date() wants a plain ISO time
    return t.isoformat() if t else None


def placement_of(d):
    """Placement-tracker deals are named '<client> / <developer placed>' (crm/scripts/load_placements.py, external id 'aat-...').
    The second half is the DEVELOPER, not a company: returned here so the page can list the placements."""
    ext, name = str(val(d, "external_opportunity_id") or ""), str(val(d, "name") or "")
    if ext.startswith("aat-") and " / " in name:
        return name.split(" / ", 1)[1].strip() or None
    return None


def vals(rec, slug):
    return rec["values"].get(slug) or []


# ------------------------------------------------------------------ opportunities
def collect_opportunities(c: AttioClient) -> list[dict]:
    # open deals, plus deals closed (Won / Lost) after the 1 Oct bulk import: those are real decisions made since the dashboard started
    CLOSED_AFTER = "2026-10-02"
    # "after the import" is judged by WHEN THE STAGE BECAME Won/Lost, not when the deal was created: placement deals were all
    # created by the 1 Oct import, so one moved Trial -> Won on 8 Oct was left out of the build and only appeared when a page
    # opened it live (17 Oct 2026)
    deals = [d for d in c.query_all("deals") if val(d, "stage") in OPEN_STAGES
             or (str(val(d, "stage") or "").startswith(("Lost", "Won")) and str(stage_since(d) or d.get("created_at") or "") >= CLOSED_AFTER)]
    print(f"  open deals: {sum(1 for d in deals if val(d, 'stage') in OPEN_STAGES)}, closed since the import: {sum(1 for d in deals if val(d, 'stage') not in OPEN_STAGES)}")
    people: dict[str, dict] = {}                     # person record id -> accumulating row

    def row(pid):
        return people.setdefault(pid, {"pid": pid, "deals": [], "calls": [], "src": set()})

    for d in deals:
        for a in vals(d, "associated_people"):
            pid = a.get("target_record_id")
            if pid:
                r = row(pid)
                r["deals"].append({"stage": val(d, "stage"), "since": stage_since(d), "url": d.get("web_url"),
                                   "last": val(d, "last_activity_at"),
                                   "next": val(d, "next_action_at"), "missed": val(d, "missed_followup_at"), "created": d.get("created_at"),
                                   "channel": val(d, "source_channel"), "campaign": val(d, "source_campaign"),
                                   "reason": val(d, "lost_reason"), "close": closed_on(d),
                                   "name": val(d, "name"), "ext": val(d, "external_opportunity_id"), "placement": placement_of(d), "company_id": val(d, "associated_company")})
                r["src"].add("email" if (val(d, "source_channel") or "").lower() == "cold email" else "deal")

    # call-list entries with a real result
    spec = yaml.safe_load(SCHEMA.read_text())
    lists = {l["api_slug"]: l for l in c.req("GET", "/lists").json()["data"]}
    n_entries = 0
    called: dict[str, list] = {}
    for L in spec["call_lists"]:
        slug = ("call_" + re.sub(r"[^a-z0-9]+", "_", L["name"].lower()).strip("_"))[:50]
        meta = lists.get(slug)
        if not meta:
            continue
        lid, off = meta["id"]["list_id"], 0
        while True:
            e = c.req("POST", f"/lists/{lid}/entries/query", json={"limit": 500, "offset": off}).json()["data"]
            for x in e:
                ev = x.get("entry_values") or {}
                g = lambda k: (ev.get(k) or [{}])[0]
                out = (g("call_outcome").get("option") or {}).get("title")
                if g("called_at").get("value"):                     # the earliest call time a call list knows, for the lead's first touch (lead_source.py)
                    called.setdefault(x["parent_record_id"], []).append(g("called_at").get("value"))
                if g("called_at").get("value") and x["parent_record_id"] in people:      # any call attempt on a person we already list is a touch, answered or not
                    people[x["parent_record_id"]].setdefault("touch_calls", []).append(g("called_at").get("value"))
                if out in REAL_CALL:
                    n_entries += 1
                    r = row(x["parent_record_id"])
                    r["calls"].append({"outcome": out, "summary": g("call_summary").get("value"),
                                       "at": g("called_at").get("value"), "ctid": g("cloudtalk_contact_id").get("value"),
                                       "attempts": g("attempts").get("value"), "list": L["name"]})
                    r["src"].add("call")
            if len(e) < 500:
                break
            off += 500
    print(f"  call entries with a real result: {n_entries}")

    # fetch the people and their companies
    def fetch_person(pid):
        r = c.req("GET", f"/objects/people/records/{pid}")
        return pid, (r.json().get("data") if r.status_code < 300 else None)

    got = dict(ThreadPoolExecutor(max_workers=6).map(fetch_person, list(people)))
    comp_ids = {val(p, "company") for p in got.values() if p and val(p, "company")} | {
        dl["company_id"] for r in people.values() for dl in r["deals"] if dl.get("company_id")}

    def fetch_company(cid):
        r = c.req("GET", f"/objects/companies/records/{cid}")
        return cid, (r.json().get("data") if r.status_code < 300 else None)

    comps = dict(ThreadPoolExecutor(max_workers=6).map(fetch_company, list(comp_ids)))

    opps = []
    for pid, r in people.items():
        p = got.get(pid)
        if not p:
            continue
        stage = val(p, "relationship_stage")
        # a person marked lost / excluded or on the do-not-contact list is never an opportunity
        has_open = any(dl["stage"] in OPEN_STAGES for dl in r["deals"])      # a person marked LOST by an old auto-label still counts when a human has since opened a deal for them (Carsten Isert, 14 Oct 2026)
        has_closed = any(str(dl["stage"] or "").startswith(("Lost", "Won")) for dl in r["deals"])      # a deal closed since the import is shown in the closed view with its real date
        if val(p, "dnc_status") == "DO_NOT_CONTACT" or (stage in ("LOST", "CUSTOMER") and not has_open and not has_closed):
            continue
        calls = sorted(r["calls"], key=lambda z: z["at"] or "")
        last_call = calls[-1] if calls else None
        r["deals"].sort(key=lambda dl: str(dl["stage"] or "").startswith(("Lost", "Won")))      # an open deal wins over an old closed one
        bucket = LB.pick(r["deals"], last_call)      # lead_bucket.py: the deal's stage, a call only when it came after the stage was set
        if bucket is None:
            continue
        # last touch = the most recent of: a deal activity, a call, the last reply
        touches = [parse(dl["last"]) for dl in r["deals"]] + [parse(z["at"]) for z in calls] + [parse(t) for t in r.get("touch_calls", [])] + [
            parse(val(p, "last_reply_at")), parse(val(p, "last_contacted_at"))]
        last_touch = max([t for t in touches if t], default=None)
        nxt = next((parse(dl["next"]) for dl in r["deals"] if dl["next"]), None)
        due = nxt or ((last_touch + timedelta(days=DUE_DAYS[bucket])) if last_touch else None)
        missed = None if nxt else next((parse(dl["missed"]) for dl in r["deals"] if dl.get("missed")), None)      # a follow-up that came due and was not done: shown as overdue
        # the company: lead_company.py's chain (person link, deal company, work email domain, PlusVibe, CloudTalk, then the deal
        # name for non-placement deals only). The Attio parts are known here; fill_companies() below does the other reads.
        co = lambda rec: {"name": val(rec, "name"), "domain": val(rec, "domains")} if rec else None
        email = val(p, "email_addresses")
        cx = {"person": {"company": co(comps.get(val(p, "company"))), "email": email, "phone": val(p, "phone_numbers")},
              "deals": [{"name": dl.get("name"), "ext": dl.get("ext"), "stage": dl["stage"], "company": co(comps.get(dl.get("company_id")))} for dl in r["deals"]]}
        got_co = LC.pick(cx)
        cname = got_co["company"] or None
        ctid = next((z["ctid"] for z in reversed(calls) if z["ctid"]), None)
        # Source = the channel of the first touch (lead_source.py): set by set_sources() once the PlusVibe sends and CloudTalk calls are read.
        # Until then (and if those reads fail) the call lists' and deals' own times decide.
        src_in = {"deals": [{"channel": dl.get("channel"), "created": dl.get("created")} for dl in r["deals"]],
                  "calls": [z["at"] for z in calls if z.get("at")] + called.get(pid, [])}
        src = LS.pick(None, min(src_in["calls"], key=lambda z: LS._t(z) or now(), default=None), src_in["deals"])
        opps.append({
            "id": pid, "name": val(p, "name") or email, "company": cname, "email": email,
            "phone": val(p, "phone_numbers"), "linkedin": val(p, "linkedin"), "li_conn": val(p, "linkedin_connection"),
            "bucket": bucket, "status": (bucket == "aftercall" and LB.STAGE_LABEL.get(next((dl["stage"] for dl in r["deals"]), None))) or BUCKET_LABEL[bucket],      # Profiles sent / Trial show their own word (29 Oct 2026)
            "stage": next((dl["stage"] for dl in r["deals"]), None), "source": src["source"], "also": src["also"],
            "first_touch": {"ch": src["first"], "at": src["at"]} if src["first"] else None, "_src": src_in,
            "reason": next((dl["reason"] for dl in r["deals"] if dl.get("reason")), None),
            "closed_at": max([dl["close"] for dl in r["deals"] if dl.get("close") and bucket in ("won", "lost")
                              and str(dl["stage"] or "").lower().startswith(bucket)], default=None),
            "placements": [{"name": dl["placement"], "stage": dl["stage"], "deal": dl["url"]} for dl in r["deals"] if dl.get("placement")],
            "last_touch": last_touch.isoformat() if last_touch else None,
            "due": due.isoformat() if due else None, "due_set": bool(nxt), "missed": missed.isoformat() if missed else None,
            "created": min([z for z in (dl["created"] for dl in r["deals"]) if z], default=None),
            "summary": (last_call or {}).get("summary") or val(p, "last_reply_summary"),
            "call_at": (last_call or {}).get("at"), "attempts": (last_call or {}).get("attempts"),
            "campaign": next((dl["campaign"] for dl in r["deals"] if dl["campaign"]), None) or val(p, "current_campaign"),
            "company_from": got_co["from"], "_cx": cx,
            "links": {"attio": p.get("web_url"), "deal": next((dl["url"] for dl in r["deals"] if dl["url"]), None),
                      "website": got_co["website"],
                      "linkedin": val(p, "linkedin"),
                      "cloudtalk": f"https://dashboard.cloudtalk.io/menu/contacts/details/{ctid}" if ctid else None},
            "thread": [],
            "lists": sorted({("call_" + re.sub(r"[^a-z0-9]+", "_", z["list"].lower()).strip("_"))[:50] for z in calls}),
        })
    try:
        fill_companies(c, opps)
    except Exception as ex:                                     # never stops the build: the leads keep what Attio's records gave
        print("  company reads failed:", type(ex).__name__)
        for o in opps:
            o.pop("_cx", None)
    link_cloudtalk(opps)
    print(f"  opportunities: {len(opps)}")
    return opps


CO_CT_MAX = int(os.environ.get("CO_CT_MAX") or 10)     # CloudTalk company lookups per run (its 60 a minute is shared by everything)


def fill_companies(c: AttioClient, opps: list[dict]) -> None:
    """The reads of lead_company.py's chain that Attio's records alone could not answer, only for leads still without a company:
    the Attio company with the person's work email domain, the PlusVibe lead's company_name, the CloudTalk contact's company.
    Every read may fail on its own: the lead then keeps what the chain had (at worst the non-placement deal name, else empty)."""
    todo = [o for o in opps if LC.needs_reads(o["_cx"])]
    doms = sorted({LC.domain_of(o["_cx"]["person"].get("email")) for o in todo} - {None})
    for i in range(0, len(doms), 50):
        chunk = doms[i:i + 50]
        try:
            r = c.req("POST", "/objects/companies/records/query",
                      json={"filter": {"$or": [{"domains": {"domain": {"$eq": d}}} for d in chunk]}, "limit": 500})
            hits = r.json().get("data") or [] if r.status_code < 300 else []
        except Exception:
            hits = []
        by = {}
        for rec in hits:
            for x in rec["values"].get("domains") or []:
                by.setdefault(str(x.get("domain") or "").lower(), rec)
        for o in todo:
            d = LC.domain_of(o["_cx"]["person"].get("email"))
            if d in by:
                o["_cx"]["domain_company"] = {"name": val(by[d], "name"), "domain": d}
    todo = [o for o in todo if LC.needs_reads(o["_cx"])]
    key = os.environ.get("PLUS_VIBE_API_KEY") or CM.env().get("PLUS_VIBE_API_KEY")
    if key:
        for o in todo:
            em = o["_cx"]["person"].get("email")
            if not em:
                continue
            rows = _pv_get("/lead/get", {"x-api-key": key}, {"workspace_id": WS, "email": em}) or []
            rows = rows if isinstance(rows, list) else (rows.get("data") or [])
            ld = next(((x.get("lead_data") or x) for x in rows if str(((x.get("lead_data") or x) or {}).get("company_name") or "").strip()), None)
            if ld:
                o["_cx"]["plusvibe"] = {"company_name": ld.get("company_name"), "company_website": ld.get("company_website")}
    todo = [o for o in todo if LC.needs_reads(o["_cx"])]
    if todo:
        try:
            import verify_cloudtalk_tags as V
            H = V.auth()
        except Exception:
            H = None
        n = 0
        for o in todo:
            if H is None or n >= CO_CT_MAX:
                break
            ph, em = o["_cx"]["person"].get("phone"), o["_cx"]["person"].get("email")
            kw = (re.sub(r"\D", "", str(ph))[-9:] if ph else None) or em
            if not kw:
                continue
            n += 1
            try:
                r = V.ct("GET", "/contacts/index.json", H, params={"keyword": kw, "limit": 5})
            except Exception:
                break
            if r.status_code == 429:
                break                                          # CloudTalk is busy: the rest keep the deal name or wait for the next run
            items = r.json().get("responseData", {}).get("data", []) if r.status_code < 300 else []
            ctc = next((x.get("Contact") for x in items if str((x.get("Contact") or {}).get("company") or "").strip()), None)
            if ctc:
                o["_cx"]["cloudtalk"] = {"company": ctc.get("company")}
    found = {}
    for o in opps:
        before = o.get("company_from")
        got = LC.pick(o.pop("_cx"))                         # the chain's final answer (a deal-name stand-in gives way to PlusVibe / CloudTalk)
        if got["company"]:
            o["company"], o["company_from"] = got["company"], got["from"]
            if got["from"] != before:
                found[got["from"]] = found.get(got["from"], 0) + 1
            if got["website"] and not o["links"].get("website"):
                o["links"]["website"] = got["website"]
    print(f"  companies: {sum(1 for o in opps if o.get('company'))} of {len(opps)} known; filled by the later steps: {found or 'none'}; "
          f"still empty: {sum(1 for o in opps if not o.get('company'))}")


LINK_CACHE = Path(os.environ.get("CT_LINK_CACHE") or ROOT / "data/caches/cloudtalk_link_cache.json")
LINK_MAX = int(os.environ.get("CT_LINK_MAX") or 25)              # leads looked up in CloudTalk per run; the rest next run
LINK_MISS_DAYS = float(os.environ.get("CT_LINK_MISS_DAYS") or 3)  # a lead not found in CloudTalk is looked up again after this


def link_cloudtalk(opps: list[dict]) -> None:
    """A lead can be in CloudTalk without any Attio call list recording its CloudTalk id (called by hand, or a list that never synced).
    Look the contact up by phone number, then by email, so the lead window can open it.
    The answers are KEPT BETWEEN RUNS (the workflow restores and saves the file in the private store): this used to be the
    biggest CloudTalk burst of the hour (50-120 lookups every run, because the memory was thrown away each time).
    Found: kept for good. Not found: asked again after LINK_MISS_DAYS. At most LINK_MAX leads are looked up per run."""
    try:
        import verify_cloudtalk_tags as V
        H = V.auth()
    except Exception as ex:
        print("  cloudtalk lookup skipped:", type(ex).__name__); return
    try:
        cache = json.loads(LINK_CACHE.read_text())
    except Exception:
        cache = {}
    digits = lambda x: re.sub(r"\D", "", str(x or ""))
    now_s = time.time()

    def known(key):                     # -> (decided, contact id or None)
        v = cache.get(key, "unset")
        if v == "unset":
            return False, None
        if isinstance(v, dict):
            if v.get("id"):
                return True, v["id"]
            return now_s - float(v.get("miss") or 0) < LINK_MISS_DAYS * 86400, None
        if v:                           # older files: the contact id itself
            return True, v
        return False, None              # older files: None meant "not found" with no date, so ask again once

    found = from_cache = looked = deferred = 0
    busy = False
    for o in opps:
        if o["links"].get("cloudtalk"):
            continue
        key = o["id"]
        decided, cid = known(key)
        if decided:
            from_cache += 1
        elif busy or looked >= LINK_MAX:
            deferred += 1
            continue
        else:
            looked += 1
            pd, em = digits(o.get("phone")), (o.get("email") or "").lower()
            answered = True
            for kw in [x for x in (o.get("phone"), o.get("email")) if x]:
                try:
                    r = V.ct("GET", "/contacts/index.json", H, params={"keyword": kw, "limit": 10})
                    if r.status_code == 429:
                        busy = True; answered = False; break     # CloudTalk is busy: stop looking up this run
                    items = r.json().get("responseData", {}).get("data", []) if r.status_code < 300 else []
                    if r.status_code >= 300:
                        answered = False
                except Exception:
                    items = []; answered = False
                for it in items:
                    one_num = lambda n: n.get("public_number") or n.get("number") if isinstance(n, dict) else n
                    one_mail = lambda m: (m.get("email") or "") if isinstance(m, dict) else m
                    as_list = lambda v: v if isinstance(v, list) else ([v] if v else [])      # CloudTalk gives one number or email as an object, several as a list
                    nums = [digits(one_num(n)) for n in as_list(it.get("ContactNumber"))]
                    mails = [str(one_mail(m) or "").lower() for m in as_list(it.get("ContactEmail"))]
                    if (pd and any(n and (n.endswith(pd[-9:]) or pd.endswith(n[-9:])) for n in nums)) or (em and em in mails):
                        cid = it["Contact"]["id"]; break
                if cid:
                    break
            if cid:
                cache[key] = {"id": cid, "at": int(now_s)}
            elif answered:
                cache[key] = {"miss": int(now_s)}
            # a lookup CloudTalk did not answer is not remembered: it is asked again next run
        if cid:
            o["links"]["cloudtalk"] = f"https://dashboard.cloudtalk.io/menu/contacts/details/{cid}"; found += 1
    try:
        LINK_CACHE.parent.mkdir(parents=True, exist_ok=True); LINK_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass
    print(f"  CloudTalk contact links: {found} linked, {from_cache} answered from memory, {looked} looked up now, "
          f"{deferred} left for the next run{' (CloudTalk busy)' if busy else ''}")


FIRST_CALLS = Path(os.environ.get("CT_FIRST_CALLS") or ROOT / "data/caches/cloudtalk_first_calls.json")


def first_calls() -> dict:
    """The first OUTGOING CloudTalk call to every number, contact and email: {"through", "phone": {last 9 digits: iso}, "contact": {id: iso},
    "email": {address: iso}}. KEPT BETWEEN RUNS (the workflow restores and saves the file in the private store, like the link cache):
    a first call never changes, so each run reads only the calls since the last one (one or two pages); with no file, the whole
    history (3,048 calls on 18 Oct 2026, four pages of 1,000). A run CloudTalk is too busy for keeps the old file and the call lists' times."""
    try:
        fc = json.loads(FIRST_CALLS.read_text())
    except Exception:
        fc = {}
    for k in ("phone", "contact", "email"):
        fc.setdefault(k, {})
    try:
        import ct_client
        cl = ct_client.shared()
    except Exception as ex:
        print("  first calls skipped:", type(ex).__name__); return fc
    since = parse(fc.get("through"))
    params = {"limit": 1000}
    if since:
        params["date_from"] = (since - timedelta(days=1)).strftime("%Y-%m-%d")      # a day of overlap: a call is never missed at the edge
    digits = lambda x: re.sub(r"\D", "", str(x or ""))
    keep = lambda m, k, t: m.__setitem__(k, t) if k and (k not in m or t < m[k]) else None
    page, newest, n = 1, since, 0
    while True:
        try:
            r = cl.get("/calls/index.json", params=dict(params, page=page), timeout=60)
        except Exception as ex:
            print("  first calls: CloudTalk busy, kept what was known:", type(ex).__name__); return fc
        if r.status_code != 200:
            print(f"  first calls: CloudTalk answered {r.status_code}, kept what was known"); return fc
        d = r.json().get("responseData") or {}
        for x in d.get("data") or []:
            cdr, con = x.get("Cdr") or {}, x.get("Contact") or {}
            t = parse(cdr.get("started_at"))
            if not t:
                continue
            newest = max(newest, t) if newest else t
            if cdr.get("type") != "outgoing":
                continue
            n += 1
            ti = t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            p9 = digits(cdr.get("public_external"))[-9:]
            keep(fc["phone"], p9 if len(p9) >= 7 else None, ti)
            keep(fc["contact"], str(con.get("id") or "") or None, ti)
            for em in con.get("contact_emails") or []:
                keep(fc["email"], str(em or "").strip().lower() or None, ti)
        if page >= int(d.get("pageCount") or 1) or not d.get("data"):
            break
        page += 1
    if newest:
        fc["through"] = newest.isoformat()
    try:
        FIRST_CALLS.parent.mkdir(parents=True, exist_ok=True); FIRST_CALLS.write_text(json.dumps(fc))
    except Exception:
        pass
    print(f"  first calls: {n} outgoing calls read ({page} page{'s' if page > 1 else ''}), {len(fc['phone'])} numbers known")
    return fc


def first_email(o: dict):
    """The first email a PlusVibe sequence sent the lead; when the sequence read gave nothing, the oldest email WE sent in the thread."""
    sent = [m.get("t") for q in o.get("sequence") or [] for m in q.get("sent") or [] if m.get("t")]
    if not sent:
        sent = [m.get("t") for m in o.get("thread") or [] if m.get("t") and not m.get("from_lead")]
    return min(sent, key=lambda z: parse(z) or now(), default=None)


def set_sources(opps: list[dict], fc: dict) -> None:
    """Source = the channel of the first touch (lead_source.py, the same rule as the page's look/leadkey.js sourceOf)."""
    digits = lambda x: re.sub(r"\D", "", str(x or ""))
    for o in opps:
        si = o.pop("_src", None) or {}
        p9 = digits(o.get("phone"))[-9:]
        ctid = str(o["links"].get("cloudtalk") or "").rsplit("/", 1)[-1]
        calls = list(si.get("calls") or [])
        calls += [x for x in (fc.get("phone", {}).get(p9) if len(p9) >= 7 else None, fc.get("contact", {}).get(ctid) if ctid else None,
                              fc.get("email", {}).get(str(o.get("email") or "").strip().lower())) if x]
        g = LS.pick(first_email(o), min(calls, key=lambda z: parse(z) or now(), default=None), si.get("deals"))
        o["source"], o["also"] = g["source"], g["also"]
        o["first_touch"] = {"ch": g["first"], "at": g["at"]} if g["first"] else None


def as_text(v) -> str:
    """PlusVibe gives the body as text, or as an object with text and html parts."""
    if isinstance(v, dict):
        v = v.get("text") or v.get("html") or v.get("plain") or " ".join(str(z) for z in v.values())
    return v if isinstance(v, str) else ("" if v is None else str(v))


def finalize_dates(o: dict) -> None:
    """The last touch is the newest of everything we know: deal and call activity, the newest email in the thread, and
    (only when nothing else exists) the day the deal was created. The follow-up date follows from it."""
    cands = [parse(o.get("last_touch"))] + [parse(m.get("t")) for m in o.get("thread") or []]
    last = max([t for t in cands if t], default=None) or parse(o.get("created"))
    o["last_touch"] = last.isoformat() if last else None
    if not o.get("due_set"):
        o["due"] = (last + timedelta(days=DUE_DAYS[o["bucket"]])).isoformat() if last else None


pv_ids: dict = {}


def pv_thread(email: str, hdr: dict, n: int = 5) -> list[dict]:
    delay = 2.0
    for _ in range(5):
        try:
            r = requests.get(PV + "/unibox/emails", headers=hdr, timeout=60,
                             params={"workspace_id": WS, "lead": email, "preview_only": "false"})
        except requests.exceptions.RequestException:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code == 429:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code >= 300:
            return []
        items = (r.json().get("data") or [])
        items.sort(key=lambda x: x.get("timestamp_created") or "", reverse=True)
        out = []
        # where the lead lives in PlusVibe, taken from the newest message, so the page can link to its campaign
        pv_ids.clear()
        if items:
            pv_ids.update({k: items[0].get(k) for k in ("campaign_id", "lead_id", "thread_id")})
        for x in items[:n]:
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", as_text(x.get("content_preview")) or as_text(x.get("body")))).strip()
            sender = (x.get("from_address_email") or "").lower()
            out.append({"t": x.get("timestamp_created"), "from_lead": sender == email.lower(),
                        "subject": x.get("subject"), "text": txt[:340]})
        return out
    return []


def _pv_get(path: str, hdr: dict, params: dict):
    """GET with back-off; None when PlusVibe refuses (a 429 looks exactly like an empty answer, so retry first)."""
    delay = 2.0
    for _ in range(5):
        try:
            r = requests.get(PV + path, headers=hdr, timeout=60, params=params)
        except requests.exceptions.RequestException:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code == 429:
            time.sleep(delay); delay *= 2
            continue
        if r.status_code >= 300:
            return None
        return r.json()
    return None


def pv_campaigns(hdr: dict) -> dict:
    """id -> {name, parent, steps} for every campaign and subsequence in the workspace."""
    j = _pv_get("/campaign/list-all", hdr, {"workspace_id": WS, "limit": 100}) or []
    items = j if isinstance(j, list) else (j.get("data") or [])
    out = {}
    for c in items:
        seq = c.get("sequences")
        out[c.get("id")] = {"name": c.get("camp_name") or "", "parent": c.get("parent_camp_id") or "", "status": c.get("status") or "",
                            "steps": len(seq) if isinstance(seq, list) else (c.get("sequence_steps") or 0)}
    return out


def pv_sequence(email: str, hdr: dict, camps: dict) -> list[dict]:
    """Where the lead is in every PlusVibe sequence: the main campaign and each subsequence under it.
    One entry per sequence: name, status, step reached out of total, last send, next send, label, and the emails sent."""
    sent_j = _pv_get("/unibox/campaign-emails", hdr, {"workspace_id": WS, "lead": email}) or []
    sent = sent_j if isinstance(sent_j, list) else (sent_j.get("data") or [])
    by_camp: dict = {}
    for x in sent:
        by_camp.setdefault(x.get("campaign_id"), []).append(x)
    rows = _pv_get("/lead/get", hdr, {"workspace_id": WS, "email": email}) or []
    out, seen = [], set()

    def add(cid: str, row: dict | None, is_sub: bool):
        if cid in seen:
            return
        seen.add(cid)
        ld = (row or {}).get("lead_data") or {}
        mails = sorted(by_camp.get(cid, []), key=lambda m: m.get("sent_on") or "")
        c = camps.get(cid, {})
        if not row and not mails:
            return
        out.append({"id": cid, "name": c.get("name") or "", "sub": is_sub, "status": (row or {}).get("status") or "", "camp": c.get("status") or "",
                    "label": ld.get("label") or "", "step": int(ld.get("sent_step") or (mails[-1].get("current_step") if mails else 0) or 0),
                    "steps": c.get("steps") or 0, "last": ld.get("last_sent_at") or (mails[-1].get("sent_on") if mails else None),
                    "next": ld.get("next_email_time") if (row or {}).get("status") not in ("REPLIED", "COMPLETED", "BOUNCED", "UNSUBSCRIBED") else None,
                    "sent": [{"step": m.get("current_step"), "t": m.get("sent_on"), "subject": m.get("subject"),
                                  "text": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", as_text(m.get("body")))).strip()[:340]} for m in mails]})

    for row in rows:
        add(row.get("campaign"), row, bool((camps.get(row.get("campaign")) or {}).get("parent")))
    mains = {r.get("campaign") for r in rows}
    for cid, c in camps.items():                                  # subsequences hanging off those campaigns
        if c.get("parent") in mains and cid not in seen:
            j = _pv_get("/lead/get", hdr, {"workspace_id": WS, "email": email, "campaign_id": cid})
            time.sleep(0.3)
            r0 = next((r for r in (j or []) if r.get("campaign") == cid), None)
            add(cid, r0, True)
    for cid in by_camp:                                           # anything that sent mail but did not show above
        add(cid, None, bool((camps.get(cid) or {}).get("parent")))
    out.sort(key=lambda e: (e["sub"], e["last"] or ""))
    return out


# ------------------------------------------------------------------ checks
def ago(ts) -> str:
    d = parse(ts)
    if not d:
        return "never"
    m = int((now() - d).total_seconds() // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 90 else f"{m // 60} h ago" if m < 2880 else f"{m // 1440} days ago"


def mk(cid, node, name, status, detail, at=None):
    return {"id": cid, "node": node, "name": name, "status": status, "detail": detail, "at": at}


def github_checks() -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            token = ""
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"} if token else {}
    # file, node, name, max age in minutes, weekday-only window (UTC hours) or None
    flows = [("crm_dashboard.yml", "github", "Dashboard build", 240, (7, 23)),
             ("crm_sync.yml", "github", "PlusVibe replies into Attio deals", 240, None),
             ("plusvibe_phone_lookup.yml", "github", "Phone lookup for interested leads", 540, None),
             ("call_dispositions.yml", "github", "Call outcomes into Attio", 800, None),
             ("discord_reminder.yml", "discord", "Discord reminders", 10080, None),
             ("sync_plusvibe.yml", "github", "PlusVibe dates into Attio", 27 * 60, None),
             ("crm_daily.yml", "cloudtalk", "Daily check of dialler tags", 27 * 60, None)]
    out = []
    for fname, node, name, limit, window in flows:
        try:
            r = requests.get(f"https://api.github.com/repos/{REPO}/actions/workflows/{fname}/runs",
                             headers=hdr, params={"per_page": 20, "status": "completed"}, timeout=30)
            runs = r.json().get("workflow_runs") or [] if r.status_code == 200 else None
            if runs is not None:        # a run the job skipped on purpose (it only runs at set hours to save minutes) is not a run
                runs = [z for z in runs if z.get("conclusion") not in ("skipped", "cancelled")]
        except Exception:
            runs = None
        if runs is None:
            out.append(mk(fname, node, name, "warn", "could not read the run history from GitHub"))
            continue
        if not runs:
            out.append(mk(fname, node, name, "warn", "no completed run found"))
            continue
        last = runs[0]
        at = last.get("updated_at") or last.get("created_at")
        age = (now() - parse(at)).total_seconds() / 60 if parse(at) else 1e9
        in_window = True
        if window:
            in_window = now().weekday() < 5 and window[0] <= now().hour <= window[1]
        recent_fail = sum(1 for z in runs[:3] if z.get("conclusion") not in ("success", "skipped"))
        if last.get("conclusion") != "success":
            st, det = "bad", f"last run {last.get('conclusion')}, {ago(at)}"
        elif age > limit and in_window:
            st, det = "warn", f"last run succeeded {ago(at)}, later than expected"
        else:
            st, det = "ok", f"last run succeeded {ago(at)}" + (" (outside its working hours)" if not in_window else "")
        if recent_fail and st == "ok":
            det += f" · {recent_fail} of the last 3 runs failed"
            st = "warn"
        out.append(mk(fname, node, name, st, det, at))
    return out


def cloudtalk_checks(spec: dict, opps: list[dict]) -> list[dict]:
    import ct_client                      # the shared CloudTalk pacer: this check and the link lookups above share one clock
    cl = ct_client.shared()
    out = []
    r = cl.get("/calls/index.json", params={"limit": 1}, timeout=60)
    rows = ((r.json().get("responseData") or {}).get("data") or []) if r.status_code == 200 else []
    if rows:
        at = rows[0]["Cdr"]["started_at"]
        out.append(mk("ct_last_call", "cloudtalk", "CloudTalk reachable, latest call", "ok", f"latest call {ago(at)}", at))
    else:
        out.append(mk("ct_last_call", "cloudtalk", "CloudTalk reachable, latest call", "bad", f"CloudTalk answered {r.status_code} with no calls"))
    # calls the job has not worked out yet
    state_path = Path(os.environ.get("CALL_DATA_DIR") or ROOT / "data/working/live") / "live_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    calls, page = [], 1
    while True:
        rr = cl.get("/calls/index.json", params={"limit": 1000, "page": page, "date_from": CALLS_FROM})
        d = (rr.json().get("responseData") or {}) if rr.status_code == 200 else {}
        calls += d.get("data") or []
        if page >= int(d.get("pageCount") or 1) or not d.get("data"):
            break
        page += 1
    out_calls = [x for x in calls if (x.get("Cdr") or {}).get("type") == "outgoing"]
    waiting = [x for x in out_calls if str(x["Cdr"]["id"]) not in state or state[str(x["Cdr"]["id"])].get("retry")]
    oldest = min((parse(x["Cdr"]["started_at"]) for x in waiting), default=None)
    stale = oldest and (now() - oldest).total_seconds() > 3 * 3600
    out.append(mk("calls_waiting", "github", "Calls worked out into dispositions",
                  "bad" if stale else ("warn" if (oldest and (now() - oldest).total_seconds() > 20 * 60) else "ok"),      # a burst of calls is worked through in minutes; only warn when the oldest has waited over 20 minutes
                  f"{len(out_calls)} calls since {CALLS_FROM}, {len(waiting)} waiting to be worked out"
                  + (f" (oldest {ago(oldest.isoformat())})" if waiting and oldest else "")))
    # people with a real result still in the dial queue
    tags = {}
    for row in cl.get("/tags/index.json", params={"limit": 200}, timeout=60).json()["responseData"]["data"]:
        t = row.get("Tag", row); tags[t["name"]] = t["id"]
    real_ct = {o["links"]["cloudtalk"].rsplit("/", 1)[-1] for o in opps if o["links"].get("cloudtalk") and o["bucket"] in ("meeting", "aftercall", "followup", "later")}
    tagged = set()
    for tg in ("Attio | NA | A+B", "Attio | NA | C", "Attio | EU | A+B", "Attio | EU | C"):
        if tg not in tags:
            continue
        pg = 1
        while True:
            rr = cl.get("/contacts/index.json", params={"tag_id": tags[tg], "limit": 1000, "page": pg}).json().get("responseData", {})
            tagged |= {str((x.get("Contact") or {}).get("id")) for x in rr.get("data") or []}
            if pg >= int(rr.get("pageCount") or 1) or not rr.get("data"):
                break
            pg += 1
    still = len(real_ct & tagged)
    out.append(mk("dialler_overlap", "cloudtalk", "Leads with a result still in the dial queue", "info",
                  f"{still} of {len(real_ct)} leads with a booked meeting, follow-up or not-now result are still tagged in the dialler. "
                  "Answered contacts have not been redialled so far; this line tells us if that changes."))
    print("  " + cl.summary())
    return out


def plusvibe_checks(hdr_key: str) -> list[dict]:
    e = CM.env()
    try:
        days = CM.plusvibe_days(e, (now() - timedelta(days=4)).date())
    except Exception as ex:
        return [mk("pv_sends", "plusvibe", "PlusVibe sending", "bad", f"could not read the email stats ({type(ex).__name__})")]
    today = now().date().isoformat()
    t = days.get(today, {})
    last_send = max((d for d, v in days.items() if v.get("sent")), default=None)
    working = now().weekday() < 5 and 13 <= now().hour <= 21         # Mon-Fri 9-5 New York is roughly 13:00-21:00 UTC
    if t.get("sent"):
        st, det = "ok", f"{t['sent']} emails sent today, {t.get('contacted', 0)} to new leads, {t.get('replies', 0)} replies"
    elif working:
        st, det = "warn", "no emails sent yet today, though it is inside the sending window"
    else:
        st, det = "ok", f"nothing sent yet today (outside the sending window); last sending day {last_send}"
    return [mk("pv_sends", "plusvibe", "PlusVibe sending", st, det)]


def attio_checks(c: AttioClient, n_opps: int) -> list[dict]:
    try:
        r = c.req("GET", "/self")
        ok = r.status_code < 300
    except Exception:
        ok = False
    return [mk("attio_up", "attio", "Attio reachable", "ok" if ok else "bad",
               f"{n_opps} open opportunities read" if ok else "Attio did not answer")]


def watchdog_checks(c: AttioClient) -> list[dict]:
    """The two things that can stop quietly: the call watcher (the Google script that opens a deal the moment a call books a meeting) and Attio's webhooks
    (the reminders webhook went 'degraded' for days once and nobody saw it)."""
    out = []
    url, hook = os.environ.get("LEAD_ACTIONS_URL"), os.environ.get("LEAD_ACTIONS_HOOK")
    if url and hook:
        try:
            last = int(float(requests.get(url, params={"cwlast": hook}, timeout=60).text.strip() or 0))
            age = (now().timestamp() * 1000 - last) / 60000 if last else None
        except Exception:
            age = None
        if age is None:
            out.append(mk("call_watch", "cloudtalk", "Call watcher (meetings from calls)", "warn", "could not read when the watcher last ran"))
        elif age > 5:
            out.append(mk("call_watch", "cloudtalk", "Call watcher (meetings from calls)", "bad",
                          f"has not run for {int(age)} minutes: a meeting booked on a call will not open a deal until it runs again", None))
        else:
            out.append(mk("call_watch", "cloudtalk", "Call watcher (meetings from calls)", "ok", f"ran {max(0, int(age * 60))} s ago, checks every minute"))
    try:
        r = c.req("GET", "/webhooks")
        rows = r.json().get("data", []) if r.status_code < 300 else None
    except Exception:
        rows = None
    if rows is None:
        out.append(mk("attio_webhooks", "attio", "Attio webhooks", "warn", "could not read the webhook list"))
    else:
        bad = [w for w in rows if w.get("status") != "active"]
        if bad:
            kinds = ", ".join(sorted({(x.get("subscriptions") or [{}])[0].get("event_type", "?") for x in bad}))
            out.append(mk("attio_webhooks", "attio", "Attio webhooks", "bad",
                          f"{len(bad)} of {len(rows)} webhooks are not active ({kinds}). Attio disables a webhook that keeps failing, and what depends on it stops"))
        else:
            out.append(mk("attio_webhooks", "attio", "Attio webhooks", "ok", f"all {len(rows)} webhooks active"))
    return out


def fetch_activity() -> list:
    """What the reminder script (Apps Script) did recently: follow-ups set, reminders sent, Monday list, errors."""
    url, hook = os.environ.get("LEAD_ACTIONS_URL"), os.environ.get("LEAD_ACTIONS_HOOK")
    if not (url and hook):
        return []
    try:
        r = requests.get(url, params={"activity": hook, "n": 300}, timeout=60)
        j = r.json() if r.status_code == 200 else []
        return j if isinstance(j, list) else []
    except Exception:
        return []


def reminder_check(activity: list) -> dict:
    """Reminders reaching Discord. Discord sometimes refuses a send (429); the job retries. Bad only when a refused reminder never got through."""
    cutoff = now() - timedelta(hours=48)
    rows = [r for r in activity if parse(r.get("t")) and parse(r.get("t")) >= cutoff]
    errors = [r for r in rows if r.get("kind") == "error" and "Reminder" in str(r.get("text"))]
    sent = [r for r in rows if r.get("kind") == "reminder"]
    lost = [e for e in errors if not any(x.get("lead") and x.get("lead") == e.get("lead") and x.get("t") > e.get("t") for x in sent)]
    if lost:
        return mk("reminders", "discord", "Reminders reaching the team", "bad", f"{len(lost)} reminder(s) were refused by Discord and have not gone out since", lost[0].get("t"))
    if errors:
        return mk("reminders", "discord", "Reminders reaching the team", "info", f"Discord refused {len(errors)} send(s) in the last 48 h; each went through on a retry", errors[0].get("t"))
    return mk("reminders", "discord", "Reminders reaching the team", "ok", f"{len(sent)} reminder(s) sent in the last 48 h, none refused")


def fetch_notes(c: AttioClient, opps: list[dict]) -> None:
    """Every Attio note on the person and on their deal, so the lead panel shows what the team wrote, not only the email thread
    and the call summary. kind: note (written by a person), call (a call result), auto (a script)."""
    def one(o):
        out, seen = [], set()
        targets = [("people", o["id"])]
        m = re.search(r"([0-9a-f-]{36})\s*$", (o.get("links") or {}).get("deal") or "")
        if m:
            targets.append(("deals", m.group(1)))
        for obj, rid in targets:
            try:
                r = c.req("GET", "/notes", params={"parent_object": obj, "parent_record_id": rid, "limit": 50})
                data = r.json().get("data", []) if r.status_code < 300 else []
            except Exception:
                data = []
            for n in data:
                nid = (n.get("id") or {}).get("note_id")
                if nid in seen:
                    continue
                seen.add(nid)
                title = n.get("title") or ""
                actor = (n.get("created_by_actor") or {}).get("type")
                kind = "call" if title.startswith("Call") else "auto" if (actor == "api-token" or "(automatic)" in title) else "note"
                body = n.get("content_plaintext") or ""
                when, label = n.get("created_at"), None
                m = re.match(r"Call · (\d{4}-\d\d-\d\d)(?: (\d\d:\d\d))?", title)
                if m:                                                   # a note about one call: show the call's own day (and time when the title has one), not when the note was written
                    when = f"{m.group(1)}T{m.group(2)}:00+02:00" if m.group(2) else f"{m.group(1)}T12:00:00+00:00"   # the time in the title is CloudTalk's own clock (UTC+2), not UTC
                elif title.startswith("Call history"):                 # written on 1-2 Oct when the call lists were loaded; the real call dates are in the text
                    mons = {k: i + 1 for i, k in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
                    ds = sorted(f"{y}-{mons[mo]:02d}-{int(d):02d}" for d, mo, y in re.findall(r"(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})", body))
                    if ds:
                        when = ds[-1] + "T12:00:00+00:00"
                        label = "Calls " + (ds[0] if len(ds) == 1 or ds[0] == ds[-1] else f"{ds[0]} to {ds[-1]}")
                out.append({"id": nid, "title": title, "text": body[:1500], "t": when, "label": label,
                            "kind": kind, "on": "deal" if obj == "deals" else "person"})
        out.sort(key=lambda x: x["t"] or "", reverse=True)
        o["notes"] = out[:30]
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(one, opps))


def main() -> int:
    c = AttioClient(workers=6)
    c._key = api_key(os.environ.get("ATTIO_KEY_VAR", "ATTIO_API_KEY_2")); c._local = type(c._local)()
    res = {"generated_at": now().isoformat(timespec="seconds"), "opps": [], "checks": [], "errors": []}

    print("opportunities...")
    try:
        res["opps"] = collect_opportunities(c)
    except Exception as ex:
        res["errors"].append(f"opportunities: {type(ex).__name__}: {ex}")
        print("  FAILED", type(ex).__name__)

    print("email threads...")
    key = os.environ.get("PLUS_VIBE_API_KEY") or CM.env().get("PLUS_VIBE_API_KEY")
    if key:
        hdr = {"x-api-key": key}
        try:
            camps = pv_campaigns(hdr)
        except Exception as ex:
            camps = {}; res["errors"].append(f"campaign list: {type(ex).__name__}: {ex}")
        for o in res["opps"]:
            if o["email"]:
                try:
                    o["sequence"] = pv_sequence(o["email"], hdr, camps) if camps else []
                except Exception as ex:
                    o["sequence"] = []; res["errors"].append(f"sequence {o['email']}: {type(ex).__name__}: {ex}")
                try:
                    pv_ids.clear()
                    o["thread"] = pv_thread(o["email"], hdr)
                    o["links"]["plusvibe"] = (f"https://app.plusvibe.ai/v2/unibox/inbox/?mail={pv_ids['thread_id']}"
                                              if pv_ids.get("thread_id") else None)
                    o["links"]["plusvibe_campaign"] = (f"https://app.plusvibe.ai/v2/campaigns/{pv_ids['campaign_id']}/leads/"
                                                       if pv_ids.get("campaign_id") else None)
                except Exception as ex:               # one odd message must never stop the run
                    res["errors"].append(f"thread {o['email']}: {type(ex).__name__}: {ex}")
                if not o["thread"]:                   # never replied: the inbox has nothing, but the emails we SENT are in the campaign history
                    sent = [{"t": m.get("t"), "from_lead": False, "subject": m.get("subject"), "text": m.get("text") or ""}
                            for q in o.get("sequence") or [] for m in q.get("sent") or []]
                    sent.sort(key=lambda m: m.get("t") or "", reverse=True)
                    o["thread"] = sent[:5]
                    o["thread_sent_only"] = bool(sent)
                time.sleep(0.4)
        print(f"  threads read for {sum(1 for o in res['opps'] if o['thread'])} of {len(res['opps'])}")
    print("first touch...")
    try:
        set_sources(res["opps"], first_calls())
    except Exception as ex:                                   # never stops the build: the call lists' and deals' times stay
        res["errors"].append(f"first touch: {type(ex).__name__}: {ex}")
    for o in res["opps"]:
        o.pop("_src", None)
    print("  source by first touch: " + ", ".join(f"{k} {v}" for k, v in sorted(collections.Counter(o["source"] for o in res["opps"]).items())))
    print("notes...")
    try:
        fetch_notes(c, res["opps"])
        print(f"  notes read for {sum(1 for o in res['opps'] if o.get('notes'))} of {len(res['opps'])}")
    except Exception as ex:
        res["errors"].append(f"notes: {type(ex).__name__}: {ex}")
    for o in res["opps"]:
        finalize_dates(o)

    print("activity log...")
    res["activity"] = fetch_activity()
    print(f"  {len(res['activity'])} entries from the reminder script")
    print("checks...")
    try:
        res["checks"].append(reminder_check(res.get("activity") or []))
    except Exception as ex:
        res["errors"].append(f"reminder check: {type(ex).__name__}: {ex}")
    spec = yaml.safe_load(SCHEMA.read_text())
    for fn, args in ((github_checks, ()), (lambda: attio_checks(c, len(res["opps"])), ()),
                     (lambda: plusvibe_checks(key or ""), ()), (lambda: cloudtalk_checks(spec, res["opps"]), ()), (lambda: watchdog_checks(c), ())):
        try:
            res["checks"] += fn(*args)
        except Exception as ex:
            res["errors"].append(f"check: {type(ex).__name__}: {ex}")
            print("  check FAILED", type(ex).__name__)
    try:                                                    # one record per run, for the uptime bars on the Systems page
        import check_history
        check_history.record(res["checks"])
    except Exception as ex:
        res["errors"].append(f"history: {type(ex).__name__}: {ex}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(f"wrote {OUT.relative_to(ROOT)}: {len(res['opps'])} opportunities, {len(res['checks'])} checks, {len(res['errors'])} errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
