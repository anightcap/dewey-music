import json, base64, time, sys, os, http.client, importlib.util, subprocess, glob, shutil
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
spec = importlib.util.spec_from_file_location("runner", _os.path.join(REPO, "runner.py")); rn = importlib.util.module_from_spec(spec); spec.loader.exec_module(rn)
AUTH = "Basic " + base64.b64encode(b"tester:test-password-1234").decode()
M = R + "/media/Music"; Q = R + "/media/_dedupe-quarantine"; I = R + "/media/_incoming"
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
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:700]) if (extra and not cond) else ""))
    if not cond: fails += 1
def code():
    return rn.totp_code(open(os.path.join(R, "state", "totp.key")).read().strip(), int(time.time() // 30))
def wait_job(jid, secs=60):
    end = time.time() + secs
    while time.time() < end:
        s, j = call("GET", "/api/jobs/%s/log?offset=0" % jid)
        if s == 200 and j["meta"]["state"] != "running": return j
        time.sleep(0.5)
def dry(**p):
    s, j = call("POST", "/api/jobs", {"cmd": "place", "params": p})
    if s != 201: return None, (s, j)
    return j["id"], wait_job(j["id"])
def real(d, confirm, cmd="place"):
    return call("POST", "/api/runner/run", {"cmd": cmd, "dry_job": d, "confirm": confirm})
def ex(root, rel): return os.path.exists(os.path.join(root, rel))
def eligible(): return call("GET", "/api/runner")[1]["eligible"].get("place")

s, j = call("POST", "/api/runner/arm", {"code": code()}); check("armed", s == 200 and j["armed"], (s, j))
st = call("GET", "/api/runner")[1]
check("runner offers place, phrase PLACE {n}, no plan.json needed", "place" in st["commands"] and st["confirm"]["place"] == "PLACE {n}", st)
check("no plan.json in this rig (place does not need one)", not os.path.exists(R + "/out/plan.json"))
d1, r = dry()
check("dry run done", r and r["meta"]["state"] == "done", r and r["text"][-500:])
check("dry run prints counts and a fingerprint", "to place 4 | already in the library / arrive twice 1" in r["text"] and "plan fingerprint:" in r["text"], r["text"][-400:])
fp = r["text"].split("plan fingerprint: ")[1][:16]
el = eligible(); check("eligible with n=5 place=4 dup=1 held=0 and the fingerprint", el and el["job"] == d1 and el["n"] == 5 and el["place"] == 4 and el["dup"] == 1 and el["held"] == 0 and el["fp"] == fp, el)
for bad in ("PLACE", "PLACE 4", "place 5", "PLACE 5 ", ""):
    s, j = real(d1, bad); check("wrong phrase %r refused" % bad, s == 400 and "PLACE 5" in j["error"], (s, j))
check("nothing moved yet", ex(I, "Fresh Mix/Existing Song.flac") and ex(I, "Loose Song.flac") and not ex(M, "Loose Artist"))
s, j = real(d1, "PLACE 5"); check("real place starts", s == 201 and j.get("job"), (s, j))
r = wait_job(j["job"]); t = r["text"]
check("real place finished OK", r["meta"]["state"] == "done" and r["meta"]["runner"], t[-700:])
check("argv: --execute --expect-plan=<fp>, no --plan, no snapshot", "--execute" in t and "--expect-plan=" + fp in t and "--plan=" not in t and not glob.glob(R + "/state/plans/*"), t[:400])
for rel in ("New Artist/New Album (2020)/01 - New Song.flac", "New Artist/New Album (2020)/02 - Another One.mp3", "Loose Artist/Loose Album (2022)/01 - Loose Song.flac", "Third Artist/Third Album (2021)/01 - Brand New.flac"):
    check("placed: " + rel, ex(M, rel))
check("library's own copy untouched", ex(M, "Lib Artist/Old Album (1999)/01 - Existing Song.flac"))
runs = sorted(os.listdir(Q)); check("one quarantine run named place-...", len(runs) == 1 and runs[0].startswith("place-"), runs)
qr = os.path.join(Q, runs[0]); man = json.load(open(qr + "/manifest.json"))
check("duplicate set aside + manifest kind place", man["kind"] == "place" and len(man["files"]) == 1 and ex(qr, "Fresh Mix/Existing Song.flac") and not ex(I, "Fresh Mix/Existing Song.flac"), man)
check("new playlist written from the folder", ex(M, "Playlists/Fresh Mix.m3u") and "01 - New Song.flac" in open(M + "/Playlists/Fresh Mix.m3u").read())
check("existing playlist left alone", "Brand New" not in open(M + "/Playlists/Old Mix.m3u").read() and "Third Artist" not in open(M + "/Playlists/Old Mix.m3u").read())
check("emptied landing folders removed", not os.listdir(I), os.listdir(I))
s, j = real(d1, "PLACE 5"); check("the dry run cannot authorise a second real run", s == 400 and "already" in j["error"], (s, j))
u = subprocess.run([sys.executable, _os.path.join(REPO, "dewey.py"), "undo", "--manifest=" + qr + "/manifest.json", "--execute"], capture_output=True, text=True, env=dict(os.environ, MD_OUT=R + "/out"))
check("undo puts the duplicate back in _incoming", u.returncode == 0 and ex(I, "Fresh Mix/Existing Song.flac"), u.stdout[-400:] + u.stderr[-300:])
# --- drift between dry run and real run
d2, r = dry(); check("second dry run: 1 duplicate to set aside", r and "to place 0 | already in the library / arrive twice 1" in r["text"], r and r["text"][-500:])
shutil.copy(M + "/Loose Artist/Loose Album (2022)/01 - Loose Song.flac", I + "/Late Arrival.flac")      # something lands after the dry run
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=4", "-metadata", "artist=Late Artist", "-metadata", "title=Late Song", "-metadata", "album=Late Album", "-metadata", "track=1", "-metadata", "date=2023", I + "/Late Arrival.flac"])
s, j = real(d2, "PLACE 1"); check("drifted run starts (the tool decides)", s == 201, (s, j))
r = wait_job(j["job"]); check("drift: tool REFUSED, nothing moved", r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and ex(I, "Fresh Mix/Existing Song.flac") and ex(I, "Late Arrival.flac") and len(os.listdir(Q)) == 1, r["text"][-500:])
# --- m3u-update appends to an existing playlist (named by the user, backup kept)
os.makedirs(I + "/Old Mix", exist_ok=True)
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1100:duration=4", "-metadata", "artist=Fourth Artist", "-metadata", "title=Second New", "-metadata", "album=Fourth Album", "-metadata", "track=1", "-metadata", "date=2024", I + "/Old Mix/Second New.flac"])
d3, r = dry(**{"m3u-update": "Old Mix"}); check("dry run with m3u-update says it will append", r and "playlist 'Old Mix': append" in r["text"], r and r["text"][-600:])
n3 = eligible()["n"]; s, j = real(d3, "PLACE %d" % n3); check("real run with m3u-update starts", s == 201, (s, j))
r = wait_job(j["job"]); pl = open(M + "/Playlists/Old Mix.m3u").read()
check("existing playlist got the new track appended; backup kept", r["meta"]["state"] == "done" and "02 - Other Song.flac" in pl and "Second New" in pl, r["text"][-600:] + pl)
check("backup of the playlist in the new run's _playlists", glob.glob(Q + "/place-*/_playlists/Old Mix.m3u"), os.listdir(Q))
check("--m3u-update reached the tool unchanged", "--m3u-update=Old Mix" in r["text"])
# --- forged dry-run records
def forge(name, log=None, **kw):
    m = {"id": name, "cmd": "place", "title": "x", "args": [], "started": "2026-10-24 12:00:00", "t0": 0, "state": "done", "rc": 0,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "dry"}
    m.update(kw); json.dump(m, open("%s/out/web-jobs/%s.json" % (R, name), "w"))
    if log is not False:
        open("%s/out/web-jobs/%s.log" % (R, name), "w").write(log or "$ dewey place\n\nincoming files 3 | to place 2 | already in the library / arrive twice 1 | not placed 0\n" + "pad\n" * 9000 + "plan fingerprint: 0123456789abcdef\n\nDRY RUN\n")
    return name
for i, a in enumerate(["--incoming=/etc", "--quarantine=/tmp/x", "--navidrome-db=/x", "--playlist-dir=/x", "--music=/", "--execute", "--expect-plan=0123456789abcdef", "--plan=/etc/passwd", "--show=abc", "--workers=999", "--m3u-update="]):
    n = forge("20261024-1700%02d-place" % i, args=[a]); s, j = real(n, "PLACE 3"); check("forged option %s refused" % a, s == 400 and "not allowed" in j["error"], (s, j))
n = forge("20261024-170020-place", args=["--keep-similar", "--upgrade", "--m3u-update=A|B", "--workers=2", "--show=10"]); s, j = real(n, "PLACE 3")
check("all whitelisted options accepted; fingerprint found in the TAIL of a long log", s == 201, (s, j)); r = wait_job(j.get("job", "x"))
check("it passed the fingerprint from the log; the tool REFUSED (landing folder empty / plan differs), rc != 0", r and "--expect-plan=0123456789abcdef" in r["text"] and "REFUSED" in r["text"] and r["meta"]["state"] == "failed", r and r["text"][:700])
n = forge("20261024-170021-place", log=False); s, j = real(n, "PLACE 3"); check("no log refused", s == 400, (s, j))
n = forge("20261024-170022-place", log="incoming files 3 | to place 2 | already in the library / arrive twice 1 | not placed 0\n"); s, j = real(n, "PLACE 3"); check("no fingerprint refused", s == 400 and "what it would do" in j["error"], (s, j))
n = forge("20261024-170023-place", log="plan fingerprint: 0123456789abcdef\n"); s, j = real(n, "PLACE 3"); check("no counts refused", s == 400, (s, j))
n = forge("20261024-170024-place", log="incoming files 3 | to place 0 | already in the library / arrive twice 0 | not placed 3\nplan fingerprint: 0123456789abcdef\n"); s, j = real(n, "PLACE 0"); check("nothing to move refused", s == 400 and "nothing to do" in j["error"], (s, j))
n = forge("20261024-170025-place", log="incoming files 3 | to place 2 | already in the library / arrive twice 1 | not placed 0\nplan fingerprint: XYZ\n"); s, j = real(n, "PLACE 3"); check("malformed fingerprint refused", s == 400, (s, j))
n = forge("20261024-170026-place", cmd="apply"); s, j = real(n, "PLACE 3"); check("another command's dry run refused for place", s == 400, (s, j))
print("FAILS:", fails)
