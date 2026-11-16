#!/usr/bin/env python3
"""Download public files from a US GitHub runner (state sites block our Nigerian IP). Ife 2026-11-17.

    python3 tools/cloud_fetch.py run --batch states1 --urls urls.json      # dispatch cloud_fetch.yml
    python3 tools/cloud_fetch.py collect --batch states1 --out <dir>       # fetch, decrypt, unpack
urls.json = [{"name": "fl_hca.xlsx", "url": "https://..."}]. Results come back encrypted with AUDIT_ENC_KEY
(same key and openssl settings as cloud_audit.py; the key is read from sales-nav-pipeline/.env).
"""
import argparse, json, os, subprocess, sys, tarfile, io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from cloud_audit import crypt  # noqa: E402  (same encryption as the audit)


def run(a):
    urls = json.loads(Path(a.urls).read_text())
    subprocess.run(["gh", "workflow", "run", "cloud_fetch.yml", "-f", f"batch={a.batch}", "-f", f"urls={json.dumps(urls)}"],
                   cwd=ROOT, check=True)
    print(f"dispatched {len(urls)} urls as batch {a.batch}")


def collect(a):
    b = f"cloud-fetch-out/{a.batch}"
    subprocess.run(["git", "fetch", "-q", "origin", f"+{b}:refs/cloudfetch/{b}"], cwd=ROOT, check=True)
    enc = subprocess.run(["git", "show", f"refs/cloudfetch/{b}:out.enc"], cwd=ROOT, capture_output=True, check=True).stdout
    data = crypt(enc, decrypt=True)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for m in t.getmembers():
            if m.isfile() and "/" in m.name and ".." not in m.name:
                (out / os.path.basename(m.name)).write_bytes(t.extractfile(m).read())
    rep = json.loads((out / "_report.json").read_text())
    for r in rep: print(f"{r['name']:40s} http {r['http']}  {r['bytes']:>10,} bytes  {r['err']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run"); r.add_argument("--batch", required=True); r.add_argument("--urls", required=True)
    c = sp.add_parser("collect"); c.add_argument("--batch", required=True); c.add_argument("--out", required=True)
    a = ap.parse_args(); {"run": run, "collect": collect}[a.cmd](a)
