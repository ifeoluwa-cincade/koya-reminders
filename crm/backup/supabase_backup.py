"""
supabase_backup.py - nightly backup of the team app's Supabase data, and a restore into ANOTHER database.

This repository is PUBLIC: this script prints counts only, never names, emails, ids or document contents.

What a backup holds (one JSON document, gzipped and encrypted by the workflow, kept in the PRIVATE store repository):
  docs            every row of the `docs` table (path, coll, id, data, version, updated_at, updated_by)
  app_secrets     the NAMES only. Values are never read, so a restore cannot bring keys back: re-enter them by hand.
  auth_users      id, email, app_metadata, created_at of every login (never password hashes)
  storage         a manifest per bucket: object name, size, updated_at. NOT the file bytes (call audio is too big
                  for a git store), so lost audio, voice notes and avatars cannot be brought back from this backup.
  digest          sha256 over every doc's path + content hash, used to prove a restore is exact

    python crm/backup/supabase_backup.py export --out backup.json           # SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (read only)
    python crm/backup/supabase_backup.py restore BACKUP --target-url URL --target-key-env VAR [--rest-prefix /rest/v1]
    python crm/backup/supabase_backup.py verify  BACKUP --target-url URL --target-key-env VAR [--rest-prefix /rest/v1]
    python crm/backup/supabase_backup.py digest  BACKUP                     # prints count + digest (for an SQL cross-check)

BACKUP may be .json, .json.gz or .json.gz.enc (decrypted with openssl and ENC_CACHE_KEY, like the encrypted caches).

Restore refuses, before any network call, when the target is the PRODUCTION project. Production is read from
PROD_SUPABASE_URL (or SUPABASE_URL); when neither is set it refuses too, because it cannot tell. The target must
already have the docs table (koya-app/db/schema.sql). Only the docs table is restored; auth users and storage are
reported as counts so a person can re-invite and re-upload.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

FORMAT = "koya-supabase-backup/1"
PAGE = 1000
TIMEOUT = 60


class Fail(Exception):
    pass


def _h(key: str, extra: dict | None = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    h.update(extra or {})
    return h


def _req(method: str, url: str, what: str, **kw) -> requests.Response:
    """One HTTP call. Any non-2xx is a failure; only the status code is printed (bodies may hold data)."""
    try:
        r = requests.request(method, url, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        raise Fail(f"{what}: no answer ({type(e).__name__})")
    if r.status_code >= 300:
        raise Fail(f"{what}: HTTP {r.status_code}")
    return r


# ---------------------------------------------------------------- hashing

def doc_hash(row: dict) -> str:
    body = json.dumps([row.get("coll"), row.get("id"), row.get("data")], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def docs_digest(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for path, dh in sorted((r["path"], doc_hash(r)) for r in rows):
        h.update(f"{path}\t{dh}\n".encode("utf-8"))
    return h.hexdigest()


def paths_md5(rows: list[dict]) -> str:
    """md5 of all paths joined by ',' in byte order: the same as SQL  md5(string_agg(path, ',' order by path collate "C"))."""
    return hashlib.md5(",".join(sorted((r["path"] for r in rows), key=lambda p: p.encode("utf-8"))).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- reading a project

DOC_COLS = "path,coll,id,data,version,updated_at,updated_by"


def read_docs(base: str, key: str) -> list[dict]:
    """Every docs row, keyset-paged by path (stable while the app keeps writing)."""
    rows: list[dict] = []
    last = None
    while True:
        params = {"select": DOC_COLS, "order": "path.asc", "limit": str(PAGE)}
        if last is not None:
            params["path"] = f"gt.{last}"
        page = _req("GET", f"{base}/docs", "read docs", headers=_h(key), params=params).json()
        if not isinstance(page, list):
            raise Fail("read docs: unexpected answer")
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        last = page[-1]["path"]


def count_docs(base: str, key: str) -> int:
    r = _req("HEAD", f"{base}/docs", "count docs", headers=_h(key, {"Prefer": "count=exact", "Range": "0-0"}), params={"select": "path"})
    cr = r.headers.get("content-range", "")
    try:
        return int(cr.split("/")[-1])
    except ValueError:
        raise Fail("count docs: no total in the answer")


def read_secret_names(base: str, key: str) -> list[str]:
    rows = _req("GET", f"{base}/app_secrets", "read secret names", headers=_h(key), params={"select": "name", "order": "name.asc"}).json()
    return [r["name"] for r in rows]


def read_auth_users(url: str, key: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        js = _req("GET", f"{url}/auth/v1/admin/users", "read auth users", headers=_h(key), params={"page": page, "per_page": PAGE}).json()
        users = js.get("users") if isinstance(js, dict) else js
        if not isinstance(users, list):
            raise Fail("read auth users: unexpected answer")
        for u in users:
            out.append({"id": u.get("id"), "email": u.get("email"), "app_metadata": u.get("app_metadata") or {}, "created_at": u.get("created_at")})
        if len(users) < PAGE:
            return out
        page += 1
        if page > 100:
            raise Fail("read auth users: too many pages")


def read_storage(url: str, key: str) -> dict:
    buckets = _req("GET", f"{url}/storage/v1/bucket", "list buckets", headers=_h(key)).json()
    out: dict = {}
    for b in sorted(x["id"] for x in buckets):
        objs: list[dict] = []
        todo = [""]
        while todo:
            prefix = todo.pop()
            offset = 0
            while True:
                body = {"prefix": prefix, "limit": PAGE, "offset": offset, "sortBy": {"column": "name", "order": "asc"}}
                items = _req("POST", f"{url}/storage/v1/object/list/{b}", "list storage objects", headers=_h(key), json=body).json()
                for it in items:
                    full = f"{prefix}/{it['name']}" if prefix else it["name"]
                    if it.get("id") is None:          # a folder
                        todo.append(full)
                    else:
                        md = it.get("metadata") or {}
                        objs.append({"name": full, "size": md.get("size"), "updated_at": it.get("updated_at")})
                if len(items) < PAGE:
                    break
                offset += PAGE
        out[b] = sorted(objs, key=lambda o: o["name"])
    return out


def cmd_export(a) -> None:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise Fail("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    base = f"{url}/rest/v1"
    expected = count_docs(base, key)
    docs = read_docs(base, key)
    if not docs:
        raise Fail("the docs table came back empty: refusing to save an empty backup")
    if len({d["path"] for d in docs}) != len(docs):
        raise Fail("docs: the same path was read twice")
    names = read_secret_names(base, key)
    users = read_auth_users(url, key)
    storage = read_storage(url, key)
    n_obj = sum(len(v) for v in storage.values())
    n_bytes = sum((o.get("size") or 0) for v in storage.values() for o in v)
    doc = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "storage holds a manifest only (names, sizes, updated_at); file bytes are NOT in this backup. app_secrets holds names only.",
        "counts": {"docs": len(docs), "docs_counted_before_read": expected, "app_secret_names": len(names), "auth_users": len(users),
                   "storage_buckets": len(storage), "storage_objects": n_obj, "storage_bytes_not_backed_up": n_bytes},
        "digest": docs_digest(docs),
        "docs": docs,
        "app_secret_names": names,
        "auth_users": users,
        "storage": storage,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    c = doc["counts"]
    print(f"docs {c['docs']} (count before read {expected}), collections {len({d['coll'] for d in docs})}")
    print(f"app_secrets names {c['app_secret_names']} (values not read)")
    print(f"auth users {c['auth_users']}")
    print(f"storage: {c['storage_buckets']} buckets, {n_obj} objects listed, {n_bytes / 1e6:.1f} MB of file bytes NOT backed up (manifest only)")


# ---------------------------------------------------------------- loading a backup

def load_backup(path: str) -> dict:
    if path.endswith(".enc"):
        if not os.environ.get("ENC_CACHE_KEY"):
            raise Fail("ENC_CACHE_KEY must be set to decrypt the backup")
        p = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-in", path, "-pass", "env:ENC_CACHE_KEY"],
                           capture_output=True)
        if p.returncode != 0:
            raise Fail("could not decrypt the backup (wrong ENC_CACHE_KEY or damaged file)")
        raw = p.stdout
    else:
        with open(path, "rb") as f:
            raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("format") != FORMAT:
        raise Fail("not a Koya Supabase backup")
    if docs_digest(doc["docs"]) != doc["digest"]:
        raise Fail("the backup's own digest does not match its docs: damaged backup")
    return doc


# ---------------------------------------------------------------- the production guard

def _host(u: str) -> str:
    return (urlparse(u if "://" in u else "https://" + u).hostname or "").lower()


def guard_target(target_url: str) -> None:
    """Raise unless the target is clearly NOT the production project. Runs before any network call."""
    prod = (os.environ.get("PROD_SUPABASE_URL") or os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    if not prod:
        raise Fail("REFUSED: production URL unknown (set PROD_SUPABASE_URL or SUPABASE_URL) so the target cannot be checked")
    if not target_url.strip():
        raise Fail("REFUSED: no target URL")
    ph, th = _host(prod), _host(target_url)
    ref = ph.split(".")[0] if ph.endswith(".supabase.co") else ""
    if th == ph or (ref and ref in target_url.lower()) or target_url.strip().rstrip("/").lower() == prod.lower():
        raise Fail("REFUSED: the target is the production project. Restore only into a separate database.")


def _target(a) -> tuple[str, str]:
    guard_target(a.target_url)
    key = os.environ.get(a.target_key_env, "")
    if not key:
        raise Fail(f"the target key is empty (env {a.target_key_env})")
    return a.target_url.rstrip("/") + a.rest_prefix, key


def cmd_restore(a) -> None:
    guard_target(a.target_url)                    # before even reading the file
    doc = load_backup(a.backup)
    base, key = _target(a)
    rows = [{k: d.get(k) for k in DOC_COLS.split(",")} for d in doc["docs"]]
    hdr = _h(key, {"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"})
    for i in range(0, len(rows), 500):
        _req("POST", f"{base}/docs", "write docs", headers=hdr, params={"on_conflict": "path"}, data=json.dumps(rows[i:i + 500], ensure_ascii=False).encode("utf-8"))
    print(f"restored docs {len(rows)} (backup of {doc['created_at'][:10]})")
    print(f"not restored, by design: app_secrets values (names listed: {len(doc['app_secret_names'])}), "
          f"auth users {len(doc['auth_users'])} (re-invite), storage objects {sum(len(v) for v in doc['storage'].values())} (bytes not in backup)")
    verify(doc, base, key, a.sample)


def verify(doc: dict, base: str, key: str, sample: int) -> None:
    got = read_docs(base, key)
    src = {d["path"]: doc_hash(d) for d in doc["docs"]}
    dst = {d["path"]: doc_hash(d) for d in got}
    missing = sum(1 for p in src if p not in dst)
    extra = sum(1 for p in dst if p not in src)
    differ = sum(1 for p in src if p in dst and src[p] != dst[p])
    step = max(1, len(src) // max(1, sample))
    picked = sorted(src)[::step][:sample]
    sample_ok = sum(1 for p in picked if dst.get(p) == src[p])
    meta_differ = 0
    by_path = {d["path"]: d for d in got}
    for d in doc["docs"]:
        t = by_path.get(d["path"])
        if t and (t.get("version") != d.get("version") or t.get("updated_by") != d.get("updated_by")
                  or _ts(t.get("updated_at")) != _ts(d.get("updated_at"))):
            meta_differ += 1
    digest_ok = docs_digest(got) == doc["digest"]
    print(f"verify: source docs {len(src)}, target docs {len(dst)}, missing {missing}, extra {extra}, content differs {differ}, "
          f"version/updated_at/updated_by differ {meta_differ}")
    print(f"verify: sample {sample_ok}/{len(picked)} ids with matching content hash; full digest {'MATCHES' if digest_ok else 'DIFFERS'}")
    if missing or differ or meta_differ or not digest_ok or sample_ok != len(picked):
        raise Fail("restore does NOT match the backup")


def _ts(v):
    if not v:
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return v


def cmd_verify(a) -> None:
    guard_target(a.target_url)
    doc = load_backup(a.backup)
    base, key = _target(a)
    verify(doc, base, key, a.sample)


def cmd_digest(a) -> None:
    doc = load_backup(a.backup)
    print(json.dumps({"docs": len(doc["docs"]), "paths_md5": paths_md5(doc["docs"]), "digest": doc["digest"], "created_at": doc["created_at"]}))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("--out", required=True); e.set_defaults(fn=cmd_export)
    for name, fn in (("restore", cmd_restore), ("verify", cmd_verify)):
        r = sub.add_parser(name)
        r.add_argument("backup")
        r.add_argument("--target-url", required=True, help="the TARGET project URL (never production)")
        r.add_argument("--target-key-env", required=True, help="name of the env var holding the target's service key")
        r.add_argument("--rest-prefix", default="/rest/v1", help="'/rest/v1' for Supabase, '' for a bare PostgREST")
        r.add_argument("--sample", type=int, default=50)
        r.set_defaults(fn=fn)
    d = sub.add_parser("digest"); d.add_argument("backup"); d.set_defaults(fn=cmd_digest)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except Fail as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
