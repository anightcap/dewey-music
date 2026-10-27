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
def dry(**p): return job("reorg-apply", **p)
def real(d, confirm, cmd="reorg-apply"):
    return call("POST", "/api/runner/run", {"cmd": cmd, "dry_job": d, "confirm": confirm})
def ex(root, rel): return os.path.exists(os.path.join(root, rel))
def eligible(): return call("GET", "/api/runner")[1]["eligible"].get("reorg-apply")
def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()

s, j = call("POST", "/api/runner/arm", {"code": code()}); check("armed", s == 200 and j["armed"], (s, j))
st = call("GET", "/api/runner")[1]
check("runner offers reorg-apply with phrase MOVE {n}", "reorg-apply" in st["commands"] and st["confirm"]["reorg-apply"] == "MOVE {n}", st)
rs = json.load(open(R + "/queue/state.json"))
check("runner state names reorg-plan.json as the plan file for reorg-apply and plan.json for the others", rs["plan_file"].get("reorg-apply") == "reorg-plan.json" and rs["plan_file"].get("apply") == "plan.json" and "place" not in rs["plan_file"], rs.get("plan_file"))
s, cmds = call("GET", "/api/commands"); ra = [c for c in cmds if c["id"] == "reorg-apply"][0]
check("card blocked before a layout plan exists", ra["blocked"] == ["no reorg plan yet: run Plan the folder layout first"], ra["blocked"])
_, r = job("reorg"); check("layout plan written", r["meta"]["state"] == "done" and os.path.exists(R + "/out/reorg-plan.json") and not os.path.exists(R + "/out/plan.json"), r["text"][-300:])
planhash = sha(R + "/out/reorg-plan.json")
# --- first batch: only Alpha Band
d1, r = dry(artist="Alpha")
check("dry run done", r and r["meta"]["state"] == "done", r and r["text"][-500:])
check("dry run recorded the layout plan's hash (not plan.json's)", r["meta"].get("plan_sha") == planhash, r["meta"])
check("dry run says selected 3", "| selected 3 (" in r["text"] and "| skipped 0" in r["text"], r["text"][:500])
el = eligible(); check("eligible with n=3, a size and no skips", el and el["job"] == d1 and el["n"] == 3 and el["skipped"] == 0 and el["size"], el)
for bad in ("MOVE", "MOVE 2", "move 3", "MOVE 3 ", "QUARANTINE 3", ""):
    s, j = real(d1, bad); check("wrong phrase %r refused" % bad, s == 400 and "MOVE 3" in j["error"], (s, j))
check("nothing moved yet", ex(M, "Alpha Band/First Album/a1.flac") and not os.listdir(RO))
s, j = real(d1, "MOVE 3"); check("real run starts", s == 201 and j.get("job"), (s, j))
r = wait_job(j["job"]); t = r["text"]
check("real run finished OK, marked as a runner job", r["meta"]["state"] == "done" and r["meta"]["runner"], t[-800:])
snap = glob.glob(R + "/state/plans/*-reorg-apply-plan.json")
check("argv: --artist=Alpha --execute --expect-moves=3 --plan=<snapshot of the layout plan>", "--artist=Alpha" in t and "--execute" in t and "--expect-moves=3" in t and len(snap) == 1 and "--plan=" + snap[0] in t, t[:500])
check("snapshot is byte-identical to the plan the dry run used", snap and sha(snap[0]) == planhash)
check("run meta keeps the plan hash and the copy", r["meta"].get("plan_sha") == planhash and r["meta"].get("plan_copy") == snap[0], r["meta"])
for a in ("01 - Opening", "02 - Middle", "03 - Closing"):
    check("moved: " + a, ex(M, "Alpha Band/First Album (2001)/%s.flac" % a) and not ex(M, "Alpha Band/First Album/a%s.flac" % a[1]))
check("lyrics file followed its song", ex(M, "Alpha Band/First Album (2001)/01 - Opening.lrc") and not ex(M, "Alpha Band/First Album/a1.lrc"))
check("cover art followed the album; the emptied old folder is gone", ex(M, "Alpha Band/First Album (2001)/cover.jpg") and not ex(M, "Alpha Band/First Album"), os.listdir(M + "/Alpha Band"))
check("other artist untouched", ex(M, "Beta Group/Second Album/b1.flac") and ex(M, "Gamma/Done Album (2020)/01 - Fine.flac"))
pl = open(M + "/Playlists/Mix.m3u").read()
check("playlist repointed for the moved file only", "../Alpha Band/First Album (2001)/02 - Middle.flac" in pl and "../Beta Group/Second Album/b1.flac" in pl, pl)
runs = sorted(os.listdir(RO)); check("one run folder in _reorg (the runner's default location)", len(runs) == 1, runs)
rd = os.path.join(RO, runs[0])
check("Navidrome database backed up first", os.path.getsize(rd + "/navidrome.db.bak") > 0 and "Navidrome database backed up" in t)
import sqlite3
check("the backup is a readable copy of the database", sqlite3.connect(rd + "/navidrome.db.bak").execute("select count(*) from media_file").fetchone()[0] == 2)
man = json.load(open(rd + "/manifest.json"))
check("manifest: kind reorg, 3 audio + 1 lrc + 1 cover, playlist backup kept", man["kind"] == "reorg" and sum(1 for f in man["files"] if f["kind"] == "audio") == 3 and len(man["playlists"]) == 1 and ex(rd, "_playlists/Mix.m3u"), man["kind"])
check("reorg-moves.tsv written", open(R + "/out/reorg-moves.tsv").read().count("\n") == 3)
s, j = real(d1, "MOVE 3"); check("the dry run cannot authorise a second real run", s == 400 and "already" in j["error"], (s, j))
# --- undo (CLI, as today) puts everything back, playlist too
u = subprocess.run([sys.executable, _os.path.join(REPO, "dewey.py"), "undo", "--manifest=" + rd + "/manifest.json", "--execute"], capture_output=True, text=True, env=dict(os.environ, MD_OUT=R + "/out"))
check("undo restores files, lyrics, cover and the playlist", u.returncode == 0 and ex(M, "Alpha Band/First Album/a1.flac") and ex(M, "Alpha Band/First Album/a1.lrc") and ex(M, "Alpha Band/First Album/cover.jpg")
      and "../Alpha Band/First Album/a2.flac" in open(M + "/Playlists/Mix.m3u").read(), u.stdout[-500:] + u.stderr[-300:])
# --- the plan is still the same, so the next batches can reuse it; limit finishes the album that crosses it
d2, r = dry(limit="1")
check("limit=1 selects the whole album (3 files), not 1", r and "| selected 3 (" in r["text"] or "| selected 4 (" in r["text"], r and r["text"][:400])
n2 = eligible()["n"]
# --- drift: a file changes between the dry run and the real run
p0 = M + "/Alpha Band/First Album/a3.flac"; t0_ = os.stat(p0).st_mtime; os.utime(p0, (t0_ + 5, t0_ + 5))
s, j = real(d2, "MOVE %d" % n2); check("drifted run starts (the tool decides)", s == 201, (s, j))
r = wait_job(j["job"]); check("drift: tool REFUSED (count differs), nothing moved, no new run folder", r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and ex(M, "Alpha Band/First Album/a1.flac") and len(os.listdir(RO)) == 1, r["text"][-500:])
# --- plan replaced between dry run and real run
d3, r = dry(artist="Alpha"); n3 = eligible()["n"]
shutil.copy(R + "/out/reorg-plan.json", R + "/out/reorg-plan.keep")
pj = json.load(open(R + "/out/reorg-plan.json")); pj["generated"] = "2099-01-01 00:00"; json.dump(pj, open(R + "/out/reorg-plan.json", "w"))
s, j = real(d3, "MOVE %d" % n3); check("changed layout plan refused by the runner", s == 400 and "plan changed" in j["error"] and "layout plan" in j["error"], (s, j))
check("... and the dry run is no longer offered", eligible() is None, eligible())
shutil.copy(R + "/out/reorg-plan.keep", R + "/out/reorg-plan.json")
# put a3's time back, new dry run for both artists
os.utime(p0, (t0_, t0_))
_, r = job("reorg"); planhash = sha(R + "/out/reorg-plan.json")
d4, r = dry(); el = eligible()
check("full dry run: 7 files, eligible", el and el["job"] == d4 and el["n"] == 7, el)
# --- navidrome database missing: the tool refuses, nothing moves
nd = R + "/nd/navidrome.db"; os.rename(nd, nd + ".off")
s, j = real(d4, "MOVE 7"); r = wait_job(j["job"])
check("no Navidrome database: REFUSED before any move, runner job failed", r["meta"]["state"] == "failed" and "cannot be backed up" in r["text"] and ex(M, "Alpha Band/First Album/a1.flac") and ex(M, "Beta Group/Second Album/b1.flac"), r["text"][-400:])
os.rename(nd + ".off", nd)
d5, r = dry(album="Second"); n5 = eligible()["n"]
check("album filter: dry run selects the 4 Beta files", n5 == 4, n5)
s, j = real(d5, "MOVE 4"); r = wait_job(j["job"])
check("second batch (album=Second): only Beta moved", r["meta"]["state"] == "done" and ex(M, "Beta Group/Second Album (2010)/04 - Cuatro.flac") and ex(M, "Alpha Band/First Album/a1.flac"), r["text"][-500:])
check("second run folder, own Navidrome backup", len(os.listdir(RO)) == 2 and all(os.path.exists(os.path.join(RO, x, "navidrome.db.bak")) for x in os.listdir(RO)))
d6, r = dry(); el = eligible()
check("next dry run: the 4 Beta moves count as already moved, 3 left", r and "already moved by earlier batches 4" in r["text"] and "| selected 3 (" in r["text"] and el and el["n"] == 3, r and r["text"][:300])
s, j = real(d6, "MOVE 3"); r = wait_job(j["job"])
check("third batch finishes the layout", r["meta"]["state"] == "done" and ex(M, "Alpha Band/First Album (2001)/01 - Opening.flac") and len(os.listdir(RO)) == 3, r["text"][-400:])
pl = open(M + "/Playlists/Mix.m3u").read()
check("playlist now points at all three new places", "Alpha Band/First Album (2001)/02 - Middle.flac" in pl and "Beta Group/Second Album (2010)/01 - Uno.flac" in pl and "Gamma/Done Album (2020)/01 - Fine.flac" in pl, pl)
d7, r = dry(); check("nothing left to do: dry run selects 0, no real run offered", r and "| selected 0 (" in r["text"] and eligible() is None, r and r["text"][:300])
# --- forged dry-run records
def forge(name, log=None, plan_file="reorg-plan.json", **kw):
    m = {"id": name, "cmd": "reorg-apply", "title": "x", "args": [], "started": "2026-10-24 12:00:00", "t0": 0, "state": "done", "rc": 0,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "dry", "plan_sha": sha(R + "/out/" + plan_file) if os.path.exists(R + "/out/" + plan_file) else None}
    m.update(kw); json.dump(m, open("%s/out/web-jobs/%s.json" % (R, name), "w"))
    if log is not False:
        open("%s/out/web-jobs/%s.log" % (R, name), "w").write(log or "$ dewey reorg-apply\n\nplan generated 2026-10-24 12:00 | 7 changes in the plan | already moved by earlier batches 0 | selected 7 (300.0 KB) | skipped 0\n" + "pad\n" * 9000 + "\nDRY RUN\n")
    return name
for i, a in enumerate(["--nd-db=/x", "--no-navidrome-backup", "--reorg-root=/tmp/x", "--bump-m3u-mtime", "--plan=/etc/passwd", "--execute", "--expect-moves=1", "--music=/", "--limit=abc", "--show=abc", "--artist=", "--playlist-dir=/x", "--quarantine=/x", "--out=/x"]):
    n = forge("20261024-1800%02d-reorg-apply" % i, args=[a]); s, j = real(n, "MOVE 7"); check("forged option %s refused" % a, s == 400 and "not allowed" in j["error"], (s, j))
n = forge("20261024-180020-reorg-apply", args=["--artist=Alpha", "--album=First", "--limit=5", "--show=20"]); s, j = real(n, "MOVE 7")
check("all whitelisted options accepted", s == 201, (s, j)); r = wait_job(j.get("job", "x"))
check("it passed --expect-moves=7 from the log; the tool REFUSED (it would move 0)", r and "--expect-moves=7" in r["text"] and "REFUSED" in r["text"] and r["meta"]["state"] == "failed" and len(os.listdir(RO)) == 3, r and r["text"][:700])
n = forge("20261024-180021-reorg-apply", log=False); s, j = real(n, "MOVE 7"); check("no log refused", s == 400, (s, j))
n = forge("20261024-180022-reorg-apply", log="nothing useful\n"); s, j = real(n, "MOVE 7"); check("no summary line refused", s == 400 and "what it would do" in j["error"], (s, j))
n = forge("20261024-180023-reorg-apply", log="plan generated x | 7 changes in the plan | already moved by earlier batches 0 | selected 0 (0 B) | skipped 2\n"); s, j = real(n, "MOVE 0"); check("selected 0 refused: nothing to do", s == 400 and "nothing to do" in j["error"], (s, j))
n = forge("20261024-180024-reorg-apply", plan_file="plan.json"); s, j = real(n, "MOVE 7"); check("a dry run tied to some other file's hash is refused", s == 400 and "plan changed" in j["error"], (s, j))
n = forge("20261024-180025-reorg-apply", plan_sha=None); s, j = real(n, "MOVE 7"); check("a dry run with no plan hash is refused", s == 400 and "plan changed" in j["error"], (s, j))
n = forge("20261024-180026-reorg-apply", cmd="apply"); s, j = real(n, "MOVE 7"); check("another command's dry run refused for reorg-apply", s == 400, (s, j))
n = forge("20261024-180027-reorg-apply"); os.unlink(R + "/out/reorg-plan.json"); s, j = real(n, "MOVE 7"); check("no reorg-plan.json at all refused", s == 400 and "no reorg-plan.json" in j["error"], (s, j))
print("FAILS:", fails)
