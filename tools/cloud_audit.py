#!/usr/bin/env python3
"""Run tools/audit_v5.py on GitHub Actions instead of the Mac (Ife 2026-10-21: the Mac has no compute left).

Everything that leaves the Mac is encrypted with AUDIT_ENC_KEY (openssl aes-256-cbc, pbkdf2), the same
pattern koya-reminders uses with ENC_CACHE_KEY. The repo is private as well, but no plain lead data is
ever committed, and the job log prints counts only.

  Local:
    python3 tools/cloud_audit.py prep --batch tam1021 --input data/working/w1018/tam/tam_audit_input.csv --shards 4
        -> splits the CSV, encrypts it, pushes it to branch cloud-audit-in/<batch>
    gh workflow run cloud_audit.yml -f batch=tam1021 -f shards=4
    python3 tools/cloud_audit.py collect --batch tam1021 --shards 4 --out data/working/w1018/tam/tam_audited.csv
        -> fetches every cloud-audit-out/<batch>/<shard> branch, decrypts, joins into one audit CSV

  Runner (called by .github/workflows/cloud_audit.yml):
    python3 tools/cloud_audit.py unpack --batch X --shard N    -> data/working/cloud_shard.csv
    python3 tools/cloud_audit.py pack   --batch X --shard N    -> out.enc (audit CSV + run.log), pushed by the workflow
"""
import argparse, csv, io, os, subprocess, sys, tarfile, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
csv.field_size_limit(10**9)


def key():
    k = os.environ.get("AUDIT_ENC_KEY")
    if not k:
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("AUDIT_ENC_KEY="):
                k = line.split("=", 1)[1].strip()
    if not k:
        sys.exit("AUDIT_ENC_KEY missing (.env locally, secret on the runner)")
    os.environ["AUDIT_ENC_KEY"] = k
    return k


def crypt(data: bytes, decrypt=False) -> bytes:
    key()
    args = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-pass", "env:AUDIT_ENC_KEY"] + (["-d"] if decrypt else [])
    return subprocess.run(args, input=data, capture_output=True, check=True).stdout


def tar_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name); info.size = len(data); t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def untar(data: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}


def git(*a, cwd=ROOT, check=True):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check)


def push_blob(branch: str, filename: str, data: bytes):
    """Commit one file as the only content of an orphan branch and force-push it (no working-tree changes)."""
    with tempfile.TemporaryDirectory() as d:
        url = git("remote", "get-url", "origin").stdout.strip()
        git("init", "-q", cwd=d); git("checkout", "-q", "--orphan", branch, cwd=d)
        Path(d, filename).write_bytes(data)
        git("add", filename, cwd=d)
        git("-c", "user.name=cloud-audit", "-c", "user.email=cloud-audit@users.noreply.github.com",
            "commit", "-q", "-m", f"cloud audit {branch}", cwd=d)
        git("push", "-q", "-f", url, f"{branch}:{branch}", cwd=d)


def fetch_blob(branch: str, filename: str) -> bytes | None:
    r = git("fetch", "-q", "origin", f"+{branch}:refs/cloudaudit/{branch}", check=False)
    if r.returncode:
        return None
    return subprocess.run(["git", "show", f"refs/cloudaudit/{branch}:{filename}"], cwd=ROOT, capture_output=True, check=True).stdout


def prep(a):
    rows = list(csv.DictReader(open(a.input, encoding="utf-8")))
    hdr = list(rows[0].keys())
    shards = [rows[i::a.shards] for i in range(a.shards)]
    files = {}
    for i, part in enumerate(shards):
        s = io.StringIO(); w = csv.DictWriter(s, fieldnames=hdr); w.writeheader(); w.writerows(part)
        files[f"shard_{i}.csv"] = s.getvalue().encode()
    push_blob(f"cloud-audit-in/{a.batch}", "in.enc", crypt(tar_bytes(files)))
    print(f"{a.batch}: {len(rows)} rows -> {a.shards} shards {[len(p) for p in shards]} | encrypted and pushed")


def unpack(a):
    files = untar(crypt(Path("in.enc").read_bytes(), decrypt=True))
    out = ROOT / "data/working/cloud_shard.csv"
    out.parent.mkdir(parents=True, exist_ok=True); (ROOT / "data/caches").mkdir(parents=True, exist_ok=True)
    out.write_bytes(files[f"shard_{a.shard}.csv"])
    print("rows in shard:", max(0, out.read_text().count("\n") - 1))


def pack(a):
    files = {}
    for name, p in (("audit.csv", ROOT / "data/working/cloud_shard_audit.csv"), ("run.log", ROOT / "run.log")):
        if p.exists():
            files[name] = p.read_bytes()
    Path("out.enc").write_bytes(crypt(tar_bytes(files)))
    n = max(0, files.get("audit.csv", b"").decode(errors="ignore").count("\n") - 1)
    print("audited rows packed:", n)


def collect(a):
    rows, hdr, missing = [], None, []
    logdir = ROOT / f"data/working/cloud_logs/{a.batch}"; logdir.mkdir(parents=True, exist_ok=True)
    for i in range(a.shards):
        blob = fetch_blob(f"cloud-audit-out/{a.batch}/{i}", "out.enc")
        if blob is None:
            missing.append(i); continue
        files = untar(crypt(blob, decrypt=True))
        if "run.log" in files:
            (logdir / f"shard_{i}.log").write_bytes(files["run.log"])
        if "audit.csv" not in files:
            missing.append(i); continue
        part = list(csv.DictReader(io.StringIO(files["audit.csv"].decode())))
        hdr = hdr or (list(part[0].keys()) if part else None)
        rows += part
    if hdr:
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=hdr); w.writeheader(); w.writerows(rows)
    import collections
    print(f"{a.batch}: {len(rows)} audited rows -> {a.out} | shards missing/incomplete: {missing or 'none'}")
    print(collections.Counter((r.get("audit_route"), r.get("audit_classification")) for r in rows).most_common(8))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep"); p.add_argument("--batch", required=True); p.add_argument("--input", required=True); p.add_argument("--shards", type=int, default=4)
    for n in ("unpack", "pack"):
        p = sub.add_parser(n); p.add_argument("--batch", required=True); p.add_argument("--shard", type=int, required=True)
    p = sub.add_parser("collect"); p.add_argument("--batch", required=True); p.add_argument("--shards", type=int, required=True); p.add_argument("--out", required=True)
    a = ap.parse_args()
    {"prep": prep, "unpack": unpack, "pack": pack, "collect": collect}[a.cmd](a)


if __name__ == "__main__":
    main()
