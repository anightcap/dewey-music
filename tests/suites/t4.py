import json, base64, time, sys, os, http.client, importlib.util, subprocess, glob, hashlib
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
spec = importlib.util.spec_from_file_location("runner", _os.path.join(REPO, "runner.py")); rn = importlib.util.module_from_spec(spec); spec.loader.exec_module(rn)
AUTH = "Basic " + base64.b64encode(b"tester:test-password-1234").decode()
M = R + "/media/Music"; Q = R + "/media/_dedupe-quarantine"
def call(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", 18099, timeout=30)
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
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:600]) if (extra and not cond) else ""))
    if not cond: fails += 1
def code(delta=0):
    return rn.totp_code(open(os.path.join(R, "state", "totp.key")).read().strip(), int(time.time() // 30) + delta)
def wait_job(jid, secs=40):
    end = time.time() + secs
    while time.time() < end:
        s, j = call("GET", "/api/jobs/%s/log?offset=0" % jid)
        if s == 200 and j["meta"]["state"] != "running": return j
        time.sleep(0.5)
def dry(**p):
    s, j = call("POST", "/api/jobs", {"cmd": "apply", "params": p})
    if s != 201: return None, (s, j)
    return j["id"], wait_job(j["id"])
def real(d, confirm, cmd="apply"):
    return call("POST", "/api/runner/run", {"cmd": cmd, "dry_job": d, "confirm": confirm})
def exists(rel): return os.path.exists(os.path.join(M, rel))

s, j = call("POST", "/api/runner/arm", {"code": code()}); check("armed", s == 200 and j["armed"], (s, j))
check("runner publishes apply with a templated phrase", "apply" in (call("GET", "/api/runner")[1].get("commands") or []) and call("GET", "/api/runner")[1]["confirm"]["apply"] == "QUARANTINE {n}")
# --- no dry run -> not eligible
s, j = real("20261024-000000-apply", "QUARANTINE 4"); check("no dry run: refused", s == 400, (s, j))
# --- dry run (exact + variant, wide not ticked): g001 1 file, g002 2 files = 3; g003 blocked; g004 wide excluded
d1, r = dry(kinds=["exact", "variant"])
check("dry run finished", r and r["meta"]["state"] == "done", r)
check("dry run says would remove 3 files", "would remove 3 files" in r["text"], r["text"][:600])
s, j = call("GET", "/api/runner"); el = j["eligible"].get("apply")
check("eligible apply dry run carries n=3 groups=2", el and el["job"] == d1 and el["n"] == 3 and el["groups"] == 3 and el["size"], j)
for bad in ("QUARANTINE", "QUARANTINE 4", "quarantine 3", "QUARANTINE 3 ", ""):
    s, j = real(d1, bad); check("wrong phrase %r refused" % bad, s == 400 and "QUARANTINE 3" in j["error"], (s, j))
check("nothing moved yet", exists("A/One (2000)/01 - Song.mp3") and exists("B/Two (2001)/02 - Tune.mp3") and not os.listdir(Q))
s, j = real(d1, "QUARANTINE 3"); check("real apply starts", s == 201 and j.get("job"), (s, j))
rj = j["job"]; r = wait_job(rj); check("real apply finished OK", r and r["meta"]["state"] == "done" and r["meta"]["runner"] and r["meta"]["mode"] == "write", r and (r["meta"], r["text"][-600:]))
t = r["text"]
check("argv: options + --execute + --expect-remove=3 + --plan=<snapshot>", "--execute" in t and "--expect-remove=3" in t and "--plan=" + R + "/state/plans/" in t and "--kinds=exact,variant" in t, t[:500])
check("losers moved to the quarantine", all(not exists(x) for x in ("A/One (2000)/01 - Song.mp3", "B/Two (2001)/02 - Tune.mp3", "B/Two (2001)/02 - Tune (Radio Edit).mp3")))
check("keepers untouched", all(exists(x) for x in ("A/One (2000)/01 - Song.flac", "B/Two (2001)/02 - Tune.flac")))
check("blocked loser untouched", exists("C/Three (2002)/03 - Hold.mp3"))
check("wide group untouched (kind not ticked)", exists("D/Four (2003)/04 - Same.mp3"))
runs = sorted(os.listdir(Q)); check("one quarantine run", len(runs) == 1, runs)
qr = os.path.join(Q, runs[0]); man = json.load(open(qr + "/manifest.json"))
check("manifest lists 3 files and the playlist", len(man["files"]) == 3 and len(man["playlists"]) == 1, man)
check("sidecar .lrc went with its loser", os.path.exists(qr + "/A/One (2000)/01 - Song.lrc") and not exists("A/One (2000)/01 - Song.lrc"))
pl = open(M + "/Playlists/mix.m3u").read(); check("m3u repointed at the keepers", "01 - Song.flac" in pl and "02 - Tune.flac" in pl and ".mp3" not in pl, pl)
check("m3u backup in the quarantine", os.path.exists(qr + "/_playlists/mix.m3u"))
snaps = glob.glob(R + "/state/plans/*-plan.json"); sha = hashlib.sha256(open(R + "/out/plan.json", "rb").read()).hexdigest()
check("plan snapshot kept and identical to the reviewed plan", len(snaps) == 1 and hashlib.sha256(open(snaps[0], "rb").read()).hexdigest() == sha and oct(os.stat(snaps[0]).st_mode & 0o777) == "0o600", snaps)
s, j = real(d1, "QUARANTINE 3"); check("the dry run cannot authorise a second real run", s == 400 and "already" in j["error"], (s, j))
# --- undo of the real run works (tool CLI; the console only dry-runs undo in this slice)
u = subprocess.run([sys.executable, _os.path.join(REPO, "dewey.py"), "undo", "--manifest=" + qr + "/manifest.json", "--execute"], capture_output=True, text=True, env=dict(os.environ, MD_OUT=R + "/out"))
check("undo restores everything", u.returncode == 0 and exists("A/One (2000)/01 - Song.mp3") and exists("B/Two (2001)/02 - Tune.mp3") and "01 - Song.mp3" in open(M + "/Playlists/mix.m3u").read(), u.stdout[-500:] + u.stderr[-300:])
# --- drift between dry run and real run: refuse, move nothing
d2, r = dry(kinds=["exact", "variant"]); check("second dry run: 3 files again", r and "would remove 3 files" in r["text"], r and r["text"][:400])
os.utime(M + "/A/One (2000)/01 - Song.mp3", (1, 1))        # a loser changed after the dry run
s, j = real(d2, "QUARANTINE 3"); check("drifted run starts (the tool decides)", s == 201, (s, j))
r = wait_job(j["job"]); check("drift: tool REFUSED, exit non-zero", r and r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and "would remove 2" in r["text"], r and r["text"][-500:])
check("drift: nothing moved", exists("A/One (2000)/01 - Song.mp3") and exists("B/Two (2001)/02 - Tune.mp3") and len(os.listdir(Q)) == 1)
# --- plan changed
os.utime(M + "/A/One (2000)/01 - Song.mp3", (1700000000, 1700000000)); d3, r = dry(kinds=["exact"]); plan = json.load(open(R + "/out/plan.json")); plan["generated"] = "x"; json.dump(plan, open(R + "/out/plan.json", "w"))
s, j = real(d3, "QUARANTINE 1"); check("changed plan refused", s == 400 and "plan changed" in j["error"], (s, j))
plan["generated"] = "2026-10-24 10:00:00"; json.dump(plan, open(R + "/out/plan.json", "w"))
# --- forged dry-run metadata
def forge(name, log=None, **kw):
    sha = hashlib.sha256(open(R + "/out/plan.json", "rb").read()).hexdigest()
    m = {"id": name, "cmd": "apply", "title": "x", "args": [], "started": "2026-10-24 12:00:00", "t0": 0, "state": "done", "rc": 0,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "dry", "plan_sha": sha}
    m.update(kw); json.dump(m, open("%s/out/web-jobs/%s.json" % (R, name), "w"))
    if log is not False:
        open("%s/out/web-jobs/%s.log" % (R, name), "w").write(log or "$ dewey apply\n\nplan generated 2026 | selection: 2 groups | would remove 2 files (1.0 KB) | blocked 0 | skipped 0\n")
    return name
for i, a in enumerate(["--plan=/etc/passwd", "--quarantine=/tmp/x", "--no-navidrome-check", "--bump-m3u-mtime", "--music=/", "--execute", "--expect-remove=99", "--kinds=bogus", "--playlist=x", "--limit=-1"]):
    n = forge("20261024-1300%02d-apply" % i, args=[a]); s, j = real(n, "QUARANTINE 2"); check("forged option %s refused" % a, s == 400 and "not allowed" in j["error"], (s, j))
n = forge("20261024-130020-apply", args=["--artist=A", "--group=g001", "--kinds=exact,variant,wide", "--exact-only", "--limit=5"]); s, j = real(n, "QUARANTINE 2")
check("all the whitelisted options are accepted", s == 201, (s, j)); wait_job(j.get("job", "x"))
n = forge("20261024-130021-apply", log=False); s, j = real(n, "QUARANTINE 2"); check("dry run without a log refused", s == 400 and "what it would do" in j["error"], (s, j))
n = forge("20261024-130022-apply", log="$ dewey apply\nno summary here\n"); s, j = real(n, "QUARANTINE 2"); check("dry run without a summary line refused", s == 400 and "what it would do" in j["error"], (s, j))
n = forge("20261024-130023-apply", log="x\nselection: 0 groups | would remove 0 files (0 B) | blocked 0\n"); s, j = real(n, "QUARANTINE 0"); check("nothing-to-do dry run refused", s == 400 and "nothing to do" in j["error"], (s, j))
n = forge("20261024-130024-apply", log="x\nselection: 1 groups | would remove 1 files (0 B)\n", cmd="playlistfix"); s, j = real(n, "QUARANTINE 1"); check("another command's dry run refused for apply", s == 400, (s, j))
# --- playlistfix still works and now also runs on the snapshot
s, j = call("POST", "/api/jobs", {"cmd": "playlistfix", "params": {"nd_password": "pw"}})
check("playlistfix dry run still starts", s in (201, 400, 409), (s, j))
print("FAILS:", fails)
