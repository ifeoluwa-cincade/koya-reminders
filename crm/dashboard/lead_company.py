"""
lead_company.py: a lead's company, one rule everywhere (Ife, 17 Oct 2026: new leads from calls and replies showed with NO company).

The same chain runs here (the hourly build, collect_ops.py), in the team app's Attio.lead-companies tool (koya-app lib/company.js,
which the page's live paths call) and on the page itself. First non-empty answer wins:
  1. person     the person's linked company record in Attio
  2. deal       a deal's associated company record (open deals first)
  3. domain     the Attio company whose domain is the person's work email domain (never gmail, outlook and the like)
  4. plusvibe   the PlusVibe lead's company_name (most cold-email repliers: a gmail address and no company link)
  5. cloudtalk  the CloudTalk contact's company
  6. deal_name  the second half of a "<lead> / <company>" deal name, ONLY for non-placement deals (placement-tracker deals,
                external id 'aat-...', are "<client> / <developer placed>": the second half is a developer, never a company)
  else empty.
pick() is pure; the fixtures (lead_company_fixtures.json) are the same file as koya-app test/fixtures/lead_company.json.
    python crm/dashboard/test_lead_company.py
"""
from __future__ import annotations
import re

FREE = set("""gmail.com googlemail.com yahoo.com yahoo.co.uk yahoo.fr yahoo.de yahoo.in ymail.com rocketmail.com hotmail.com hotmail.co.uk
hotmail.fr hotmail.de hotmail.it outlook.com outlook.fr outlook.de live.com live.co.uk live.fr msn.com icloud.com me.com mac.com aol.com
proton.me protonmail.com pm.me gmx.com gmx.de gmx.net gmx.at web.de mail.com mail.ru yandex.com yandex.ru zoho.com zohomail.com
fastmail.com hey.com tutanota.com tuta.io qq.com 163.com 126.com sina.com naver.com daum.net libero.it orange.fr free.fr laposte.net
sfr.fr wanadoo.fr t-online.de freenet.de seznam.cz wp.pl o2.pl interia.pl btinternet.com sky.com virginmedia.com comcast.net
verizon.net att.net sbcglobal.net bellsouth.net cox.net charter.net shaw.ca rogers.com bigpond.com optusnet.com.au rediffmail.com""".split())


def clean(s) -> str:
    t = re.sub(r"\s+", " ", str("" if s is None else s)).strip()
    return "" if (not t or re.fullmatch(r"(?i)n/?a|none|null|undefined|-", t)) else t


def domain_of(email) -> str | None:
    m = re.fullmatch(r"[^@\s]+@([a-z0-9.-]+\.[a-z]{2,})", str(email or "").strip().lower())
    if not m:
        return None
    d = re.sub(r"^www\.", "", m.group(1))
    return None if d in FREE else d


def is_placement(deal: dict) -> bool:
    return str((deal or {}).get("ext") or "").lower().startswith("aat-")


def deal_name_company(deals) -> str:
    """'<lead> / <company>': the company half, never for a placement deal."""
    for d in deals or []:
        if not d or is_placement(d):
            continue
        nm = str(d.get("name") or "")
        i = nm.find(" / ")
        if i > 0:
            c = clean(nm[i + 3:])
            if c:
                return c
    return ""


def site(v) -> str | None:
    s = clean(v)
    if not s:
        return None
    if re.match(r"(?i)https?://", s):
        return s
    return "https://" + s.lstrip("/") if re.fullmatch(r"(?i)[a-z0-9.-]+\.[a-z]{2,}(/.*)?", s) else None


def pick(x: dict) -> dict:
    """x = {person: {company: {name, domain}|None, email, phone}, deals: [{name, ext, stage, company}], domain_company, plusvibe, cloudtalk}
    -> {company, website, from} (company '' and from None when nothing knows it)."""
    x = x or {}
    p, deals = x.get("person") or {}, x.get("deals") or []
    pc = p.get("company") or {}
    dc = next((d.get("company") for d in deals if d and d.get("company") and clean(d["company"].get("name"))), None) or {}
    dom = x.get("domain_company") or {}
    pv = x.get("plusvibe") or {}
    ct = x.get("cloudtalk") or {}
    steps = [("person", pc.get("name"), pc.get("domain")),
             ("deal", dc.get("name"), dc.get("domain")),
             ("domain", dom.get("name") if domain_of(p.get("email")) else None, dom.get("domain")),
             ("plusvibe", pv.get("company_name"), pv.get("company_website")),
             ("cloudtalk", ct.get("company"), None),
             ("deal_name", deal_name_company(deals), None)]
    for frm, name, web in steps:
        c = clean(name)
        if c:
            return {"company": c, "website": site(web), "from": frm}
    return {"company": "", "website": None, "from": None}


def needs_reads(x: dict) -> bool:
    """Still unknown after what Attio gave (the deal name is the last resort: the PlusVibe / CloudTalk reads come first)."""
    return pick(x)["from"] in (None, "deal_name")
