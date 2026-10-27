import json, base64, time, sys, os, http.client, importlib.util, subprocess, glob
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
spec = importlib.util.spec_from_file_location("runner", _os.path.join(REPO, "runner.py")); rn = importlib.util.module_from_spec(spec); spec.loader.exec_module(rn)
AUTH = "Basic " + base64.b64encode(b"tester:test-password-1234").decode()
PW = "S3cret-Nd-Pass-XYZ"


def call(method, path, body=None, https=True, auth=True):
    c = http.client.HTTPConnection("127.0.0.1", 18099, timeout=30)
    h = {"Host": "127.0.0.1:18099"}
    if auth:
        h["Authorization"] = AUTH
    if https:
        h["X-Forwarded-Proto"] = "https"
    if method == "POST":
        h["X-MD-Web"] = "1"; h["Content-Type"] = "application/json"
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse(); d = r.read()
    try:
        j = json.loads(d)
    except Exception:
        j = d.decode()
    return r.status, j


fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:400]) if (extra and not cond) else ""))
    if not cond:
        fails += 1


def code(delta=0):
    sec = open(os.path.join(R, "state", "totp.key")).read().strip()
    return rn.totp_code(sec, int(time.time() // 30) + delta)


def wait_job(jid, secs=40):
    end = time.time() + secs
    while time.time() < end:
        s, j = call("GET", "/api/jobs/%s/log?offset=0" % jid)
        if s == 200 and j["meta"]["state"] != "running":
            return j
        time.sleep(0.5)
    return None


def start_dry(**p):
    return call("POST", "/api/jobs", {"cmd": "playlistfix", "params": dict(p, nd_password=PW)})


def real(dry, confirm="FIX PLAYLISTS", pw=PW, cmd="playlistfix"):
    return call("POST", "/api/runner/run", {"cmd": cmd, "dry_job": dry, "confirm": confirm, "nd_password": pw})


s, j = call("GET", "/api/runner"); check("runner available, not armed", s == 200 and j["available"] and not j["armed"], j)
# --- arming
s, j = call("POST", "/api/runner/arm", {"code": code()}, https=False); check("arm refused over plain http", s == 400 and "https" in j["error"], (s, j))
s, j = call("POST", "/api/runner/arm", {"code": "abc"}); check("arm refuses non-digits", s == 400, (s, j))
s, j = call("POST", "/api/runner/arm", {"code": "000000"}); check("wrong code refused", s == 400 and "not right" in j["error"], (s, j))
c0 = code(); s, j = call("POST", "/api/runner/arm", {"code": c0}); check("right code arms", s == 200 and j["armed"] and 800 < j["armed_left"] <= 900, (s, j))
s, j = call("POST", "/api/runner/disarm"); check("disarm", s == 200 and not j["armed"], (s, j))
s, j = call("POST", "/api/runner/arm", {"code": c0}); check("same code cannot be reused", s == 400, (s, j))
s, j = call("POST", "/api/runner/arm", {"code": code(-1)}); check("older code cannot be used", s == 400, (s, j))
s, j = call("POST", "/api/runner/arm", {"code": code(1)}); check("next-step code arms (clock drift window)", s == 200 and j["armed"], (s, j))
# --- before any dry run
s, j = real("20261024-000000-playlistfix"); check("real run without a dry run refused", s == 400, (s, j))
# --- dry run
s, j = start_dry(kinds=["exact", "variant"], artist="Beatles"); check("dry run starts with plan_sha", s == 201 and j.get("plan_sha"), (s, j))
dry1 = j["id"]; r = wait_job(dry1); check("dry run finished", r and r["meta"]["state"] == "done", r and r["meta"])
check("dry run has plan_sha in saved meta", r["meta"].get("plan_sha"))
s, j = call("GET", "/api/runner"); check("dry run is eligible", dry1 == j["eligible"].get("playlistfix", {}).get("job"), j)
s, j = real(dry1, confirm="fix playlists"); check("wrong confirmation refused", s == 400 and "FIX PLAYLISTS" in j["error"], (s, j))
s, j = real(dry1, pw=""); check("password required (none stored)", s == 400 and "password" in j["error"].lower(), (s, j))
s, j = real(dry1); check("real run starts", s == 201 and j.get("job"), (s, j))
rj = j["job"]; r = wait_job(rj); check("real run done", r and r["meta"]["state"] == "done" and r["meta"]["mode"] == "write" and r["meta"]["runner"], r and r["meta"])
check("real run used the dry run's options + --execute", "--artist=Beatles" in r["text"] and "--execute" in r["text"] and "--kinds=exact,variant" in r["text"], r["text"])
check("password reached the tool by environment only", "password present: True" in r["text"] and PW not in r["text"], r["text"])
s, j = real(dry1); check("dry run cannot authorise a second real run", s == 400 and "already" in j["error"], (s, j))
s, j = call("GET", "/api/runner"); check("no eligible dry run after use", "playlistfix" not in j["eligible"], j)
hit = subprocess.run("grep -rl %s %s || true" % (PW, R), shell=True, capture_output=True, text=True).stdout.split()
check("password found in no file under the rig (out, queue, state)", not hit, hit)
check("request queue is empty", not glob.glob(R + "/queue/req/*"), glob.glob(R + "/queue/req/*"))
# --- plan changed after the dry run
s, j = start_dry(); dry2 = j["id"]; wait_job(dry2)
open(R + "/out/plan.json", "w").write('{"groups":[],"generated":"2026-10-24 11:00:00"}')
s, j = real(dry2); check("changed plan.json refused", s == 400 and "plan changed" in j["error"], (s, j))
s, j = start_dry(); dry3 = j["id"]; wait_job(dry3)


# --- forged dry meta
def forge(name, **kw):
    sha = subprocess.run("sha256sum %s/out/plan.json" % R, shell=True, capture_output=True, text=True).stdout.split()[0]
    m = {"id": name, "cmd": "playlistfix", "title": "x", "args": [], "started": "2026-10-24 12:00:00", "t0": 0, "state": "done", "rc": 0,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "dry", "plan_sha": sha}
    m.update(kw); json.dump(m, open("%s/out/web-jobs/%s.json" % (R, name), "w")); return name


n = forge("20261024-120001-playlistfix", args=["--execute"]); s, j = real(n); check("forged args with --execute refused", s == 400 and "not allowed" in j["error"], (s, j))
n = forge("20261024-120002-playlistfix", args=["--restore=/out/x"]); s, j = real(n); check("forged --restore refused", s == 400, (s, j))
n = forge("20261024-120003-playlistfix", args=["--artist=a\nb"]); s, j = real(n); check("newline in option refused", s == 400, (s, j))
n = forge("20261024-120004-scan", cmd="scan", args=[]); s, j = real(n, cmd="scan"); check("command outside the runner's list refused", s == 400 and "does not run" in j["error"], (s, j))
n = forge("20261024-120005-playlistfix", rc=1); s, j = real(n); check("failed dry run refused", s == 400, (s, j))
n = forge("20261024-120006-playlistfix", plan_sha="0" * 64); s, j = real(n); check("wrong plan hash refused", s == 400 and "plan changed" in j["error"], (s, j))
n = forge("20261024-120007-playlistfix", finished="2026-10-23 01:00:00"); s, j = real(n); check("old dry run refused", s == 400 and "6 hours" in j["error"], (s, j))
n = forge("20261024-120008-playlistfix", mode="write"); s, j = real(n); check("non-dry meta refused", s == 400, (s, j))
n = forge("20261024-120009-playlistfix", runner=True); s, j = real(n); check("a real run cannot be reused as a dry run", s == 400, (s, j))
s, j = real("../../etc/passwd"); check("path-like job id refused", s == 400, (s, j))
# garbage in the queue must not stop the runner
os.makedirs(R + "/queue/req", exist_ok=True)
open(R + "/queue/req/zz1.json", "w").write("{not json"); open(R + "/queue/req/zz2.json", "w").write("x" * 30000)
os.symlink("/etc/passwd", R + "/queue/req/zz3.json"); open(R + "/queue/req/zz4.json", "w").write('{"id":"../evil","type":"ping"}')
time.sleep(2.5); s, j = call("GET", "/api/runner"); check("runner survives garbage requests", s == 200 and j["available"], j)
check("garbage requests were cleared", not glob.glob(R + "/queue/req/*"), glob.glob(R + "/queue/req/*"))
# --- exclusion both ways + cancel
s, j = start_dry(limit=999); slow = j["id"]; time.sleep(1.2)
s, j2 = real(dry3); check("real run refused while a console job runs", s == 409 and "console job" in j2["error"], (s, j2))
call("POST", "/api/jobs/%s/cancel" % slow); wait_job(slow)
time.sleep(1)
s, j = start_dry(limit=999); slow2 = j["id"]; r = wait_job(slow2, 60)
s, j = real(slow2); check("slow real run starts", s == 201, (s, j)); rj = j.get("job"); time.sleep(1.5)
s, j = call("GET", "/api/status"); check("status shows the real run as current job", j["job"] and j["job"]["id"] == rj, j["job"])
s, j = start_dry(); check("console job refused while a real run runs", s == 409 and "real run" in j["error"], (s, j))
s, j = call("POST", "/api/jobs/%s/cancel" % rj); check("cancel a real run", s == 200, (s, j))
r = wait_job(rj, 20); check("real run reported cancelled", r and r["meta"]["state"] == "cancelled", r and r["meta"])
# --- disarm blocks
s, j = call("POST", "/api/runner/disarm"); s, j = start_dry(); d4 = j["id"]; wait_job(d4)
s, j = real(d4); check("real run refused when not armed", s == 400 and "not armed" in j["error"], (s, j))
s, j = call("GET", "/api/security"); check("security panel lists write access", any(r["name"].startswith("Write access") for r in j["rows"]), j)
s, j = call("GET", "/api/runner", auth=False); check("runner endpoint needs the login", s == 401, (s, j))
print("FAILS:", fails)
