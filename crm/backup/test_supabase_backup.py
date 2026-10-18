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

# encrypted backups: the new key first, the old one as a fallback for backups made before the switch
import subprocess
with tempfile.TemporaryDirectory() as t:
    gz = os.path.join(t, "b.json.gz"); open(gz, "wb").write(gzip.compress(json.dumps(doc).encode()))
    def enc(keyval, out):
        subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-in", gz, "-out", out, "-pass", "env:K"], check=True, env=dict(os.environ, K=keyval))
    new, old = os.path.join(t, "new.json.gz.enc"), os.path.join(t, "old.json.gz.enc")
    enc("new-key", new); enc("old-key", old)
    def load(p, **keys):
        for k in B.KEY_ENVS: os.environ.pop(k, None)
        os.environ.update(keys)
        with contextlib.redirect_stderr(io.StringIO()) as e:
            try:
                B.load_backup(p); return e.getvalue().strip()
            except B.Fail:
                return "FAIL"
    assert load(new, BACKUP_ENC_KEY="new-key") == "decrypted with BACKUP_ENC_KEY"
    assert load(old, BACKUP_ENC_KEY="new-key", ENC_CACHE_KEY="old-key") == "decrypted with ENC_CACHE_KEY"
    assert load(new, BACKUP_ENC_KEY="new-key", ENC_CACHE_KEY="old-key") == "decrypted with BACKUP_ENC_KEY"
    assert load(old, BACKUP_ENC_KEY="new-key") == "FAIL"
    assert load(new) == "FAIL"
print("supabase_backup offline checks: all passed")

# Team chat v2 tables (18 Oct 2026): digests ignore row order and timestamp spelling, and a damaged table is refused
ch = [{"id": "c2", "key": "b", "created_at": "2026-10-18T08:00:00+00:00"}, {"id": "c1", "key": "a", "created_at": "2026-10-18T08:00:00.5Z"}]
assert B.table_digest(ch, ["id"]) == B.table_digest(list(reversed(ch)), ["id"])
assert B.table_digest(ch, ["id"]) == B.table_digest([{**ch[0], "created_at": "2026-10-18T08:00:00Z"}, {**ch[1], "created_at": "2026-10-18 08:00:00.5+00"}], ["id"])
assert B.table_digest(ch, ["id"]) != B.table_digest([{**ch[0], "key": "x"}, ch[1]], ["id"])
tabs = {"chat_channels": ch, "chat_reactions": [{"message_id": "m1", "user_id": "u", "emoji": "\U0001F44D", "channel_id": "c1"}]}
doc2 = dict(doc, tables=tabs, tables_digest=B.tables_digest(tabs))
with tempfile.TemporaryDirectory() as t:
    p = os.path.join(t, "b.json.gz")
    open(p, "wb").write(gzip.compress(json.dumps(doc2).encode()))
    assert len(B.load_backup(p)["tables"]["chat_channels"]) == 2
    open(p, "wb").write(gzip.compress(json.dumps(dict(doc2, tables={"chat_channels": ch[:1], "chat_reactions": tabs["chat_reactions"]})).encode()))
    try:
        B.load_backup(p); raise AssertionError("damaged chat tables accepted")
    except B.Fail:
        pass
    open(p, "wb").write(gzip.compress(json.dumps(doc).encode()))        # an older backup without chat tables still loads
    assert B.load_backup(p)["tables"] == {}
print("chat tables: ok")

# escrow: to-do rows sealed with TODOS_ENC_KEY (v 2) need that key in the backup; old (v 1) or no to-do rows do not
todo2 = [{"path": "todo/u", "coll": "todo", "id": "u", "data": {"v": 2, "e": "x"}}]
B.check_escrow(rows, {})
B.check_escrow([{"path": "todo/u", "coll": "todo", "id": "u", "data": {"v": 1, "e": "x"}}], {})
B.check_escrow(todo2, {"TODOS_ENC_KEY": "k"})
try:
    B.check_escrow(todo2, {}); raise AssertionError("to-do rows without their key accepted")
except B.Fail:
    pass
assert B.ESCROW == ("TODOS_ENC_KEY",)
print("ok   escrow of the to-do key")

