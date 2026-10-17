"""Offline checks for supabase_backup.py: the production guard and the hashes. Run: python crm/backup/test_supabase_backup.py"""
import contextlib, gzip, hashlib, io, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
import supabase_backup as B

PROD = "https://abcdefghijklmnop.supabase.co"


def refused(url, prod=PROD, env="SUPABASE_URL"):
    for k in ("SUPABASE_URL", "PROD_SUPABASE_URL"):
        os.environ.pop(k, None)
    if prod is not None:
        os.environ[env] = prod
    try:
        B.guard_target(url)
        return False
    except B.Fail as e:
        return str(e).startswith("REFUSED")


assert refused(PROD)
assert refused(PROD + "/")
assert refused("HTTPS://ABCDEFGHIJKLMNOP.supabase.co")
assert refused("abcdefghijklmnop.supabase.co")
assert refused("postgresql://postgres.abcdefghijklmnop:pw@aws-0-eu.pooler.supabase.com:6543/postgres")
assert refused(PROD, env="PROD_SUPABASE_URL")
assert refused("http://localhost:3000", prod=None), "unknown production must refuse"
assert refused("", prod=PROD)
assert not refused("http://localhost:3000")
assert not refused("https://zyxwvutsrqponmlk.supabase.co")

# restore refuses before reading the file or touching the network
os.environ["SUPABASE_URL"] = PROD
err = io.StringIO()
with contextlib.redirect_stderr(err):
    assert B.main(["restore", "/nonexistent.json.gz.enc", "--target-url", PROD, "--target-key-env", "X"]) == 1
assert "REFUSED" in err.getvalue()

rows = [{"path": "b/2", "coll": "b", "id": "2", "data": {"x": [1, 2], "a": "é"}},
        {"path": "a/1", "coll": "a", "id": "1", "data": {}},
        {"path": "a/10", "coll": "a", "id": "10", "data": {"n": 1.5}}]
assert B.docs_digest(rows) == B.docs_digest(list(reversed(rows)))
assert B.doc_hash(rows[0]) == B.doc_hash({"path": "b/2", "coll": "b", "id": "2", "data": {"a": "é", "x": [1, 2]}})
assert B.doc_hash(rows[0]) != B.doc_hash({**rows[0], "data": {"x": [2, 1], "a": "é"}})
assert B.paths_md5(rows) == hashlib.md5(b"a/1,a/10,b/2").hexdigest()

# a backup round-trips through gzip and refuses a damaged one
doc = {"format": B.FORMAT, "created_at": "2026-10-17T00:00:00+00:00", "digest": B.docs_digest(rows), "docs": rows,
       "app_secret_names": [], "auth_users": [], "storage": {}}
with tempfile.TemporaryDirectory() as t:
    p = os.path.join(t, "b.json.gz")
    open(p, "wb").write(gzip.compress(json.dumps(doc).encode()))
    assert B.load_backup(p)["digest"] == doc["digest"]
    bad = dict(doc, docs=rows[:2])
    open(p, "wb").write(gzip.compress(json.dumps(bad).encode()))
    try:
        B.load_backup(p); raise AssertionError("damaged backup accepted")
    except B.Fail:
        pass
print("supabase_backup offline checks: all passed")
