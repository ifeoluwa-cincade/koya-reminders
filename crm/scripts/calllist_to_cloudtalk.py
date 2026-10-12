"""
calllist_to_cloudtalk.py - Attio call list membership becomes a CloudTalk contact tag, so a person added to a list
lands in the matching dialler campaign with nobody running anything.

PUBLIC REPO: this prints COUNTS ONLY (no names, emails or numbers) and error STATUS CODES only (never response text).

A person is tagged only if ALL of these hold (Ife 2026-10-12; the 8 Oct dialler incident was entries with no phone):
  1. they have a VALID international number (the list entry's phone, else the Attio person's phone);
  2. (a region mismatch between the number and the list is only COUNTED, not blocked: Ife 2026-10-12);
  3. their CloudTalk contact exists and carries a number (a missing contact is created, with that number).
Anyone failing a check is skipped and counted, never tagged.
Removing someone from the list removes the tag (prune), unless that would untag more than 25% of a tag's contacts at once (a safety stop).

    python crm/scripts/calllist_to_cloudtalk.py            # dry run
    python crm/scripts/calllist_to_cloudtalk.py --commit
"""
from __future__ import annotations
import argparse, base64, json, os, re, sys, time
from collections import Counter, defaultdict
from pathlib import Path

import phonenumbers
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from attio_client import AttioClient

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC = ROOT / "crm/schema/call_lists.json"
CT = "https://my.cloudtalk.io/api"
NA_CC = {"US", "CA"}
EU_CC = {"GB", "IE", "DE", "FR", "ES", "IT", "NL", "BE", "SE", "NO", "DK", "FI", "PL", "PT", "AT", "CH", "CZ", "RO", "GR", "HU", "BG", "HR", "SK", "SI", "LT", "LV", "EE", "LU", "MT", "CY"}


def number_region(raw: str):
    """(e164, 'NA'/'EU'/'OTHER') for a valid number, else (None, None)."""
    v = (raw or "").strip()
    if not v:
        return None, None
    try:
        p = phonenumbers.parse(v if v.startswith("+") else "+" + re.sub(r"\D", "", v), None)
        if not phonenumbers.is_valid_number(p):
            return None, None
        cc = phonenumbers.region_code_for_number(p)
        return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164), ("NA" if cc in NA_CC else "EU" if cc in EU_CC else "OTHER")
    except Exception:
        return None, None


def auth() -> dict:
    t = base64.b64encode(f"{os.environ['CLOUD_TALK_API_KEY_ID']}:{os.environ['CLOUD_TALK_API_KEY']}".encode()).decode()
    return {"Authorization": f"Basic {t}", "Content-Type": "application/json"}


def req(method: str, path: str, H: dict, tries: int = 5, **kw):
    delay, last = 2.0, None
    for _ in range(tries):
        try:
            r = requests.request(method, CT + path, headers=H, timeout=90, **kw)
            if r.status_code != 429:
                return r
        except requests.exceptions.RequestException as ex:
            last = type(ex).__name__
        time.sleep(delay); delay *= 2
    if last:
        raise RuntimeError(f"CloudTalk request failed ({last})")
    return r


DIALER = "https://api.cloudtalk.io/v1"


def campaign_statuses(H):
    """({legacy id: status}, {dialer id: (name, status)}). Read-only."""
    legacy, dialer = {}, {}
    r = req("GET", "/campaigns/index.json", H, params={"limit": 100})
    for x in (r.json().get("responseData", {}).get("data") or []):
        c0 = x.get("Campaign", x); legacy[str(c0.get("id"))] = c0.get("status")
    cursor = None
    for _ in range(20):
        prm = {"limit": 100}
        if cursor:
            prm["cursor"] = cursor
        r = requests.get(DIALER + "/dialer/campaigns", headers=H, params=prm, timeout=60)
        if r.status_code >= 300:
            break
        j = r.json()
        for x in j.get("data") or []:
            dialer[str(x["id"])] = (x.get("name"), x.get("status"))
        cursor = (j.get("meta") or {}).get("nextCursor")
        if not cursor:
            break
    return legacy, dialer


def create_dialer_campaign(H, tag, tag_id, template_id):
    """New dialler campaign for a tag, same settings and agents as the template, then activated (a new one stays 'initializing' = not dialable until then)."""
    tpl = requests.get(DIALER + f"/dialer/campaigns/{template_id}", headers=H, timeout=60).json()
    assoc = requests.get(DIALER + f"/dialer/campaigns/{template_id}/associations", headers=H, timeout=60).json()
    body = {"name": tag, **{k: tpl[k] for k in ("mode", "calling_policy", "queue_policy", "retry_policy", "schedule_policy", "ttl_policy", "waterfall_order", "agent_permissions") if k in tpl}}
    r = requests.post(DIALER + "/dialer/campaigns", headers=H, json=body, timeout=60)
    if r.status_code >= 300:
        return None, f"create HTTP {r.status_code}"
    cid = (r.json().get("id") or (r.json().get("data") or {}).get("id"))
    a = requests.put(DIALER + f"/dialer/campaigns/{cid}/associations", headers=H, timeout=60,
                     json={"target_tag": [{"ref_id": int(tag_id), "priority": 1}], "queue_group": [], "agent": assoc.get("agent", [])})
    s = requests.patch(DIALER + f"/dialer/campaigns/{cid}", headers=H, json={"status": "active"}, timeout=60)
    return cid, f"associations HTTP {a.status_code}, activate HTTP {s.status_code}"


def alert(text):
    """Discord alert: COUNTS and list names only. Goes to the Captain Hook TEST channel for now (Ife 2026-10-12)."""
    url = os.environ.get("TEST_WEBHOOK_URL", "")
    if not url:
        print("alert not sent (no webhook configured)"); return
    r = requests.post(url, json={"content": text[:1800]}, timeout=30)
    print(f"alert sent: HTTP {r.status_code}")


def listify(x):
    return [] if x is None else x if isinstance(x, list) else [x]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--only", help="one list slug")
    ap.add_argument("--test-alert", action="store_true", help="send one sample alert to the test channel and exit")
    a = ap.parse_args()
    H = auth()
    cfg = json.loads(SPEC.read_text()); spec = cfg["lists"]
    if a.test_alert:
        alert("Call list labels: TEST alert (no action needed). Format: 'NO ACTIVE CAMPAIGN for <list>: N people labelled and waiting.'"); return 0

    # 1. every CloudTalk contact: email -> id, number -> id, and whether it holds a number
    by_email, by_phone, has_number, rows_n = {}, {}, set(), 0
    page = 1
    while True:
        r = req("GET", "/contacts/index.json", H, params={"limit": 1000, "page": page})
        if r.status_code >= 300:
            print(f"CloudTalk contacts: HTTP {r.status_code}"); return 1
        d = r.json().get("responseData", {}) or {}
        rows = d.get("data") or []
        for row in rows:
            cid = str((row.get("Contact", row)).get("id") or "")
            if not cid:
                continue
            for e in listify(row.get("ContactEmail")):
                k = (e.get("email") or "").strip().lower()
                if k:
                    by_email.setdefault(k, cid)
            for n in listify(row.get("ContactNumber")):
                e164, _ = number_region(n.get("public_number") or "")
                if e164:
                    by_phone.setdefault(e164, cid); has_number.add(cid)
        rows_n += len(rows)
        if rows_n >= int(d.get("itemsCount") or 0) or not rows:
            break
        page += 1
    print(f"CloudTalk contacts read: {rows_n:,}")

    tag_ids = {}
    for row in (req("GET", "/tags/index.json", H, params={"limit": 200}).json().get("responseData", {}).get("data") or []):
        tg = row.get("Tag", row); tag_ids[tg.get("name")] = tg.get("id")
    tagged_now = defaultdict(set)
    for L in spec:
        tid = tag_ids.get(L["tag"])
        if not tid:
            continue
        pg = 1
        while True:
            d = req("GET", "/contacts/index.json", H, params={"tag_id": tid, "limit": 1000, "page": pg}).json().get("responseData", {}) or {}
            rows = d.get("data") or []
            tagged_now[L["tag"]] |= {str((x.get("Contact", x)).get("id")) for x in rows}
            if len(tagged_now[L["tag"]]) >= int(d.get("itemsCount") or 0) or not rows:
                break
            pg += 1

    c = AttioClient(workers=8)
    lists = {l["api_slug"]: l for l in c.req("GET", "/lists").json()["data"]}
    allp = {p["id"]["record_id"]: p for p in c.query_all("people")}
    print(f"Attio people read: {len(allp):,}")

    plan_add, plan_create, plan_remove, summary = [], [], [], {}
    for L in spec:
        if a.only and L["slug"] != a.only:
            continue
        meta = lists.get(L["slug"])
        if not meta:
            print(f"{L['slug']}: Attio list missing"); continue
        lid = meta["id"]["list_id"]
        entries, off = [], 0
        while True:
            e = c.req("POST", f"/lists/{lid}/entries/query", json={"limit": 500, "offset": off}).json()["data"]
            entries += e
            if len(e) < 500:
                break
            off += 500
        want, skip, note = set(), Counter(), Counter()
        for e in entries:
            p = allp.get(e["parent_record_id"])
            if not p:
                skip["person not found"] += 1; continue
            ev = e.get("entry_values") or {}
            cands = [(ev.get("phone") or [{}])[0].get("value") or ""] + [x.get("original_phone_number") or x.get("phone_number") or "" for x in (p["values"].get("phone_numbers") or [])]
            e164, nreg = next(((x, y) for x, y in (number_region(z) for z in cands) if x), (None, None))
            if not e164:
                skip["no callable number"] += 1; continue
            if nreg != L["region"]:
                note["number country differs from the list region (still labelled, Ife 2026-10-12)"] += 1
            emails = [x.get("email_address", "").lower() for x in (p["values"].get("email_addresses") or [])]
            cid = next((by_email[x] for x in emails if x in by_email), None) or by_phone.get(e164)
            if cid and cid not in has_number:
                skip["CloudTalk contact has no number"] += 1; continue
            if cid:
                want.add(cid)
                if cid not in tagged_now[L["tag"]]:
                    plan_add.append((cid, L["tag"], lid, e["id"]["entry_id"]))
            else:
                full = (p["values"].get("name") or [{}])[0].get("full_name") or (emails[0] if emails else "")
                plan_create.append((full, emails[0] if emails else "", e164, L["tag"]))
        rm = tagged_now[L["tag"]] - want
        if tagged_now[L["tag"]] and len(rm) > 0.25 * len(tagged_now[L["tag"]]) and len(rm) > 3:
            skip["PRUNE STOPPED: would untag more than 25%"] += len(rm)
        else:
            plan_remove += [(cid, L["tag"]) for cid in rm]
        summary[L["slug"]] = (len(entries), len(want), dict(skip))
        print(f"{L['slug']}: {len(entries)} entries, {len(want)} ready, skipped {dict(skip)}, notes {dict(note)}")

    print(f"to tag: {len(plan_add)} | to create in CloudTalk: {len(plan_create)} | to untag: {len(plan_remove)}")

    # ---- the campaign check (Ife 2026-10-12): a label is only useful if a campaign that uses it is ACTIVE. Never switch an inactive one on.
    legacy_st, dialer_st = campaign_statuses(H)
    new_by_tag = Counter(tag for _, tag, _, _ in plan_add) + Counter(tag for *_, tag in plan_create)
    campaign_lines, to_create, to_alert = [], [], []
    for L in spec:
        if a.only and L["slug"] != a.only:
            continue
        sts = []
        for cc in L.get("campaigns", []):
            sts.append(legacy_st.get(str(cc["id"])) if cc["kind"] == "legacy" else (dialer_st.get(str(cc["id"])) or (None, None))[1])
        sts += [st for nm, st in dialer_st.values() if nm == L["tag"]]
        if not sts:
            campaign_lines.append(f"{L['slug']}: NO CAMPAIGN -> one will be created"); to_create.append(L)
        elif "active" in sts:
            campaign_lines.append(f"{L['slug']}: campaign active")
        else:
            campaign_lines.append(f"{L['slug']}: NO ACTIVE CAMPAIGN (statuses {sorted(set(map(str, sts)))}), not switched on")
            if new_by_tag.get(L["tag"], 0) > 0:
                to_alert.append((L["slug"], new_by_tag[L["tag"]]))
    for line in campaign_lines:
        print(line)
    if not a.commit:
        print("DRY RUN: nothing written"); return 0

    ok = bad = 0
    for cid, tag, lid, eid in plan_add:
        r = req("PUT", f"/contacts/addTags/{cid}.json", H, json={"tags": [tag]})
        if r.status_code < 300:
            ok += 1
            c.req("PATCH", f"/lists/{lid}/entries/{eid}", json={"data": {"entry_values": {"cloudtalk_contact_id": [{"value": str(cid)}]}}})
        else:
            bad += 1
    print(f"tagged {ok}, failed {bad}")
    n_un = 0
    for cid, tag in plan_remove:
        if req("DELETE", f"/contacts/removeTags/{cid}.json", H, json={"tags": [tag]}).status_code < 300:
            n_un += 1
    print(f"untagged {n_un}")
    made = 0
    for i in range(0, len(plan_create), 10):
        chunk = plan_create[i:i + 10]
        payload = [{"action": "add_contact", "command_id": str(i + j), "data": {"name": n or e or p, "ContactEmail": ([{"email": e}] if e else []), "ContactNumber": [{"public_number": p}], "ContactsTag": [{"name": tag}]}} for j, (n, e, p, tag) in enumerate(chunk)]
        r = req("POST", "/bulk/contacts.json", H, json=payload)
        if r.status_code < 300:
            made += len(chunk)
        else:
            print(f"create failed: HTTP {r.status_code}")
    print(f"created {made}")
    if to_create:
        tag_ids2 = {}
        for row in (req("GET", "/tags/index.json", H, params={"limit": 200}).json().get("responseData", {}).get("data") or []):
            tg = row.get("Tag", row); tag_ids2[tg.get("name")] = tg.get("id")
        for L in to_create:
            if not tag_ids2.get(L["tag"]):
                print(f"{L['slug']}: no CloudTalk tag yet, campaign not created"); continue
            cid, note = create_dialer_campaign(H, L["tag"], tag_ids2[L["tag"]], cfg.get("template_dialer_campaign_id"))
            print(f"{L['slug']}: created dialer campaign {cid} ({note})")
    for slug, n in to_alert:
        alert(f"Call list labels: NO ACTIVE CAMPAIGN for {slug}: {n} people labelled and waiting. Turn one on in CloudTalk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
