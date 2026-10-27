import json, base64, time, sys, os, http.client, importlib.util, subprocess, glob, shutil, hashlib
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
spec = importlib.util.spec_from_file_location("runner", _os.path.join(REPO, "runner.py")); rn = importlib.util.module_from_spec(spec); spec.loader.exec_module(rn)
AUTH = "Basic " + base64.b64encode(b"tester:test-password-1234").decode()
M = R + "/media/Music"; RO = R + "/media/_reorg"
def call(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", 18099, timeout=60)
    h = {"Host": "127.0.0.1:18099", "Authorization": AUTH, "X-Forwarded-Proto": "https"}
    if method == "POST": h["X-MD-Web"] = "1"; h["Content-Type"] = "application/json"
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse(); d = r.read()
    try: j = json.loads(d)
    except Exception: j = d.decode()
    return r.status, j
fails = 0
def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:900]) if (extra and not cond) else ""))
    if not cond: fails += 1
def code():
    return rn.totp_code(open(os.path.join(R, "state", "totp.key")).read().strip(), int(time.time() // 30))
def wait_job(jid, secs=90):
    end = time.time() + secs
    while time.time() < end:
        s, j = call("GET", "/api/jobs/%s/log?offset=0" % jid)
        if s == 200 and j["meta"]["state"] != "running": return j
        time.sleep(0.4)
def job(cmd, **p):
    s, j = call("POST", "/api/jobs", {"cmd": cmd, "params": p})
    if s != 201: return None, (s, j)
    return j["id"], wait_job(j["id"])
def dry(**p): return job("beets-remap", **p)
def real(d, confirm, cmd="beets-remap"):
    return call("POST", "/api/runner/run", {"cmd": cmd, "dry_job": d, "confirm": confirm})
def ex(root, rel): return os.path.exists(os.path.join(root, rel))
def eligible(): return call("GET", "/api/runner")[1]["eligible"].get("beets-remap")
def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()


import sqlite3
DB = R + "/beets/library.db"
def rows(): return sqlite3.connect(DB).execute("select id, path from items order by id").fetchall()
def art(): return sqlite3.connect(DB).execute("select id, artpath from albums where artpath is not null").fetchall()
def baks(): return sorted(glob.glob(DB + ".bak-reorg-*"))
s, j = call("POST", "/api/runner/arm", {"code": code()}); check("armed", s == 200 and j["armed"], (s, j))
st = call("GET", "/api/runner")[1]
check("runner offers beets-remap with phrase REPOINT {n}", "beets-remap" in st["commands"] and st["confirm"]["beets-remap"] == "REPOINT {n}", st)
rs = json.load(open(R + "/queue/state.json"))
check("no plan file for beets-remap in the runner state", "beets-remap" not in rs["plan_file"] and rs["needs_plan"]["beets-remap"] is False, rs.get("plan_file"))
s, cmds = call("GET", "/api/commands"); bc = [c for c in cmds if c["id"] == "beets-remap"][0]
check("card is available (Beets database mounted)", bc["blocked"] == [], bc["blocked"])
before = rows(); before_art = art()
check("fixture: Beets still points at the OLD paths", any("First Album/a1.flac" in p.decode() for _, p in before) and not any("(2001)" in p.decode() for _, p in before), before[:2])
# --- dry run
d1, r = dry()
check("dry run done", r and r["meta"]["state"] == "done", r and r["text"][-500:])
t = r["text"]
check("dry run: 7 items and 1 art path to repoint, nothing missing, nothing outside, a fingerprint", "to repoint 7 | file not found anywhere 0 | outside /music 0" in t and "album art paths: ok 0 | to repoint 1" in t and "plan fingerprint: " in t, t[:900])
fp = t.split("plan fingerprint: ")[1][:16]
check("dry run has no plan hash (no plan file)", not r["meta"].get("plan_sha"), r["meta"])
check("dry run changed nothing", rows() == before and not baks())
el = eligible(); check("eligible with n=8 (7 items + 1 art), items=7 art=1 missing=0 outside=0 and the fingerprint", el and el["job"] == d1 and el["n"] == 8 and el["items"] == 7 and el["art"] == 1 and el["missing"] == 0 and el["outside"] == 0 and el["fp"] == fp, el)
for bad in ("REPOINT", "REPOINT 7", "repoint 8", "REPOINT 8 ", "MOVE 8", ""):
    s, j = real(d1, bad); check("wrong phrase %r refused" % bad, s == 400 and "REPOINT 8" in j["error"], (s, j))
# --- Beets is running (holds the write lock): refused, nothing written, not even a backup
lk = sqlite3.connect(DB, timeout=0.1); lk.execute("BEGIN IMMEDIATE")
s, j = real(d1, "REPOINT 8"); check("real run starts (the tool decides)", s == 201, (s, j))
r = wait_job(j["job"])
check("Beets locked: the tool stopped, nothing changed, no backup file left behind", r["meta"]["state"] == "failed" and "locked" in r["text"] and "nothing was changed" in r["text"] and rows() == before and not baks(), r["text"][-500:])
lk.rollback(); lk.close()
s, j = real(d1, "REPOINT 8"); check("the dry run was used up by that attempt (a fresh one is needed)", s == 400 and "already" in j["error"], (s, j))
d2, r = dry(); el = eligible(); check("fresh dry run offered", el and el["job"] == d2 and el["n"] == 8, el); fp2 = el["fp"]
check("same state gives the same fingerprint", fp2 == fp, (fp, fp2))
s, j = real(d2, "REPOINT 8"); check("real run starts", s == 201 and j.get("job"), (s, j))
r = wait_job(j["job"]); t = r["text"]
check("real run finished OK, marked as a runner job", r["meta"]["state"] == "done" and r["meta"]["runner"], t[-800:])
check("argv: only --execute and --expect-plan=<fp>", t.split("\n")[0].strip() == "$ dewey beets-remap --execute --expect-plan=" + fp, t[:200])
after = rows()
check("every item repointed, ids unchanged", [i for i, _ in after] == [i for i, _ in before] and not any("First Album/a" in p.decode() or "Second Album/b" in p.decode() for _, p in after) and any("Alpha Band/First Album (2001)/01 - Opening.flac" == p.decode() for _, p in after), after)
check("album art path repointed (absolute style kept)", art() == [(before_art[0][0], b"/music/Alpha Band/First Album (2001)/cover.jpg")], art())
check("every repointed path exists on disk", all(os.path.isfile(os.path.join(M, p.decode())) for _, p in after))
check("backup next to the database, a readable consistent copy with the OLD paths", len(baks()) == 1 and sqlite3.connect(baks()[0]).execute("select id, path from items order by id").fetchall() == before and sqlite3.connect(baks()[0]).execute("pragma integrity_check").fetchone()[0] == "ok", baks())
check("integrity ok reported; undo hint mentions the backup", "database integrity: ok" in t and "to go back: stop Beets" in t and baks()[0] in t, t[-600:])
check("remap record written to out/", glob.glob(R + "/out/beets-remap-*.tsv") and open(glob.glob(R + "/out/beets-remap-*.tsv")[0]).read().count("\n") == 8)
check("real Beets reads the database and finds the files", subprocess.run(["beet", "-c", R + "/beets/config.yaml", "ls", "-p"], capture_output=True, text=True).stdout.count("/music/") == 8)
s, j = real(d2, "REPOINT 8"); check("the dry run cannot authorise a second real run", s == 400 and "already" in j["error"], (s, j))
d3, r = dry(); check("second dry run: nothing to change, nothing offered", r and "nothing to change" in r["text"] and eligible() is None, r and r["text"][-300:])
# --- drift: Beets changes between the dry run and the real run
shutil.copy(baks()[0], DB)                       # "to go back": the documented restore
for suf in ("-wal", "-shm"):
    if os.path.exists(DB + suf): os.unlink(DB + suf)
check("restore from the backup puts the old paths back", rows() == before)
d4, r = dry(); el = eligible(); check("stale again: eligible n=8", el and el["job"] == d4 and el["n"] == 8, el)
c = sqlite3.connect(DB); c.execute("update items set path=? where id=?", (b"Alpha Band/First Album (2001)/01 - Opening.flac", before[0][0])); c.commit(); c.close()
s, j = real(d4, "REPOINT 8"); r = wait_job(j["job"])
check("drift: tool REFUSED (fingerprint differs), nothing changed", r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and len(baks()) == 1 and rows()[1:] == before[1:], r["text"][-500:])
d5, r = dry(); el = eligible(); check("fresh dry run after the drift: 7 left (one item was already right)", el and el["n"] == 7 and el["job"] == d5, el)
s, j = real(d5, "REPOINT 7"); r = wait_job(j["job"]); check("real run on the fresh dry run works", r["meta"]["state"] == "done" and not any("First Album/a" in p.decode() for _, p in rows()), r["text"][-400:])
# --- the files move back (undo of a layout run): the same command repoints the other way
runs = sorted(os.listdir(R + "/media/_reorg"))
for rn_ in reversed(runs):
    u = subprocess.run([sys.executable, _os.path.join(REPO, "dewey.py"), "undo", "--manifest=" + R + "/media/_reorg/" + rn_ + "/manifest.json", "--execute"], capture_output=True, text=True, env=dict(os.environ, MD_OUT=R + "/out"))
check("undo of the layout run put the files back", u.returncode == 0 and os.path.isfile(M + "/Alpha Band/First Album/a1.flac"), u.stdout[-300:] + u.stderr[-300:])
d6, r = dry(); el = eligible(); check("after the undo the same command repoints Beets back (8 rows)", el and el["n"] == 8, (el, r and r["text"][:600]))
s, j = real(d6, "REPOINT 8"); r = wait_job(j["job"]); check("... and the real run does it", r["meta"]["state"] == "done" and rows() == before, r["text"][-300:])
# --- forged dry-run records
def forge(name, log=None, **kw):
    m = {"id": name, "cmd": "beets-remap", "title": "x", "args": [], "started": "2026-10-24 12:00:00", "t0": 0, "state": "done", "rc": 0,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "dry"}
    m.update(kw); json.dump(m, open("%s/out/web-jobs/%s.json" % (R, name), "w"))
    if log is not False:
        open("%s/out/web-jobs/%s.log" % (R, name), "w").write(log or GOOD)
    return name
GOOD = "$ dewey beets-remap\n\nBeets items 8 | already correct 0 | to repoint 7 | file not found anywhere 0 | outside /music 0\nalbum art paths: ok 0 | to repoint 1 | art file not found 0\nplan fingerprint: 0123456789abcdef\nDRY RUN\n"
for i, a in enumerate(["--beets-db=/etc/passwd", "--beets-prefix=/", "--music=/", "--out=/x", "--execute", "--expect-plan=0123456789abcdef", "--nothing", "--show=5", "--limit=1"]):
    n = forge("20261024-1900%02d-beets-remap" % i, args=[a]); s, j = real(n, "REPOINT 8"); check("forged option %s refused (the command takes none)" % a, s == 400 and "not allowed" in j["error"], (s, j))
n = forge("20261024-190020-beets-remap"); s, j = real(n, "REPOINT 8"); r = wait_job(j.get("job", "x"))
check("a log with a fingerprint that is not the real one: started, then the tool REFUSED, nothing changed", s == 201 and "--expect-plan=0123456789abcdef" in r["text"] and "REFUSED" in r["text"] and r["meta"]["state"] == "failed" and rows() == before, (s, j, r and r["text"][-300:]))
n = forge("20261024-190021-beets-remap", log=False); s, j = real(n, "REPOINT 8"); check("no log refused", s == 400, (s, j))
n = forge("20261024-190022-beets-remap", log="nothing useful\n"); s, j = real(n, "REPOINT 8"); check("no counts refused", s == 400 and "what it would do" in j["error"], (s, j))
n = forge("20261024-190023-beets-remap", log=GOOD.replace("album art paths: ok 0 | to repoint 1 | art file not found 0\n", "")); s, j = real(n, "REPOINT 8"); check("no art line refused", s == 400, (s, j))
n = forge("20261024-190024-beets-remap", log=GOOD.replace("plan fingerprint: 0123456789abcdef\n", "")); s, j = real(n, "REPOINT 8"); check("no fingerprint refused", s == 400, (s, j))
n = forge("20261024-190025-beets-remap", log=GOOD.replace("0123456789abcdef", "XYZ")); s, j = real(n, "REPOINT 8"); check("malformed fingerprint refused", s == 400, (s, j))
n = forge("20261024-190026-beets-remap", log=GOOD.replace("to repoint 7", "to repoint 0").replace("to repoint 1", "to repoint 0")); s, j = real(n, "REPOINT 0"); check("nothing to change refused", s == 400 and "nothing to do" in j["error"], (s, j))
n = forge("20261024-190027-beets-remap", cmd="reorg-apply"); s, j = real(n, "REPOINT 8"); check("another command's dry run refused", s == 400, (s, j))
print("FAILS:", fails)
