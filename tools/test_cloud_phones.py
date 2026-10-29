"""Offline checks for cloud_phones.pick (no network). Run: python tools/test_cloud_phones.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_phones as CP  # noqa: E402

bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    bad += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


def num(html):
    v, how = CP.pick(html)
    return v[0] if v else None


# 29 Oct 2026: an SVG icon's path data read as +1 582-553-9942 (same icon on 4 agency sites)
PATH = "M.4302 6.3043c.0409.818.5069 3.334 1.2423 5.7436.4598 1.5065.9387 2.7019 1.4334 3.582.553.9942 1.1259 1.5933 1.7143 1.7895"
check("svg path in an svg tag", num(f'<p>About us</p><svg viewBox="0 0 24 24"><path d="{PATH}"/></svg>'), None)
check("svg path in a framework script payload", num('<script>self.__next_f.push([1,"{\\"d\\":\\"' + PATH + '\\"}"])</script><p>Hi</p>'), None)
check("svg path loose in text", num(f"<div>{PATH}</div>"), None)
check("decimals glued to the number", num("<p>width 3.582.553.9942 px</p>"), None)
check("longer digit run", num("<p>order 1234 582 553 9942 55</p>"), None)
check("css in a style block", num("<style>.a{margin:415 555 0199}</style><p>x</p>"), None)
# real numbers still found
check("US number in text", num("<p>Call us: (415) 555-2671</p>"), "+14155552671")
check("US number with dots", num("<p>Phone 415.555.2671.</p>"), "+14155552671")
check("international number in text", num("<p>Tel: +44 20 7946 0958</p>"), "+442079460958")
check("tel link wins", num('<a href="tel:+1-415-555-2671">call</a> <p>(212) 555-0147</p>'), "+14155552671")
check("schema.org telephone in a JSON-LD script", num('<script type="application/ld+json">{"@type":"Organization","telephone":"+1-267-555-0109"}</script>'), "+12675550109")
check("schema telephone in an escaped payload", num('<script>push("{\\"telephone\\":\\"+44 7911 123456\\"}")</script>'), "+447911123456")
check("another svg path (2.228.408.5608)", num('<script>x="1.8053.2445 2.228.408.5608.216.96.4754"</script><div>2.228.408.5608.216</div>'), None)
check("number after a colon", num("<p>Phone:415-555-2671</p>"), "+14155552671")

# a text number on two different sites is dropped, a tel link is kept
sites = ["https://a.com", "b.com", "https://www.c.com/x", "d.com"]
res = [(("+15825539942", "US", 2), "website/ (text (US format))"), (("+15825539942", "US", 2), "website/about (text (US format))"),
       (("+14155552671", "US", 2), "website/ (tel link)"), (("+14155552671", "US", 2), "website/ (tel link)")]
out = CP.drop_shared(sites, res)
check("shared text number dropped", [v[0] if v else None for v, _ in out], [None, None, "+14155552671", "+14155552671"])
check("same site twice is not shared", [v[0] if v else None for v, _ in CP.drop_shared(["a.com", "https://www.a.com"], res[:2])],
      ["+15825539942", "+15825539942"])

print("all ok" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
