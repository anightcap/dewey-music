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
_, r = job("reorg"); check("layout plan written", r["meta"]["state"] == "done")
dA, r = dry(artist="Alpha"); dB, r = dry(artist="Beta")
el = eligible(); check("two dry runs: the console offers the NEWEST one (Beta, 4 files)", el and el["job"] == dB and el["n"] == 4 and "--artist=Beta" in el["args"], el)
dC, r = dry(artist="Nobody"); check("newest dry run selects nothing", "| selected 0 (" in r["text"], r["text"][:300])
check("then nothing is offered (not the older Beta one)", eligible() is None, eligible())
dB2, r = dry(artist="Beta"); el = eligible(); check("a fresh Beta dry run is offered again", el and el["job"] == dB2 and el["n"] == 4, el)
s, j = real(dB2, "MOVE 4"); r = wait_job(j["job"]); check("real run for Beta done", r["meta"]["state"] == "done" and ex(M, "Beta Group/Second Album (2010)/01 - Uno.flac"), r["text"][-300:])
check("after the real run nothing is offered, though older unused dry runs of the same plan exist", eligible() is None, eligible())
dA2, r = dry(artist="Alpha"); el = eligible(); check("Alpha dry run offered: 3 files, Beta's 4 are not counted", el and el["n"] == 3, el)
# a dry run that failed (bad option value the tool rejects) is the newest -> nothing offered
s, j = call("POST", "/api/jobs", {"cmd": "reorg-apply", "params": {"limit": "-5"}}); print("limit=-5 ->", s, str(j)[:120])
print("FAILS:", fails)
