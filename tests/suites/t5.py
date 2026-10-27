import json, base64, time, sys, http.client, threading
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
AUTH = "Basic " + base64.b64encode(b"tester:test-password-1234").decode()
def call(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", 18099, timeout=30)
    h = {"Host": "127.0.0.1:18099", "Authorization": AUTH, "X-Forwarded-Proto": "https"}
    if method == "POST": h["X-MD-Web"] = "1"; h["Content-Type"] = "application/json"
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse(); d = r.read()
    try: return r.status, json.loads(d)
    except Exception: return r.status, d.decode()
bad = []; stop = False; cur = {"id": None}
def poller():
    while not stop:
        jid = cur["id"]
        if jid:
            s, j = call("GET", "/api/jobs/%s/log?offset=0" % jid)
            if s == 200 and j["meta"]["state"] == "interrupted": bad.append((jid, time.time()))
        time.sleep(0.002)
ths = [threading.Thread(target=poller, daemon=True) for _ in range(6)]
[t.start() for t in ths]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
for i in range(N):
    s, j = call("POST", "/api/jobs", {"cmd": "apply", "params": {"kinds": ["exact", "variant"]}})
    if s != 201: time.sleep(0.3); continue
    cur["id"] = j["id"]
    for _ in range(100):
        s, m = call("GET", "/api/jobs/%s/log?offset=0" % j["id"])
        if m["meta"]["state"] not in ("running", "interrupted"): break
        time.sleep(0.02)
    time.sleep(0.15)
stop = True; time.sleep(0.3)
print("transient 'interrupted' readings:", len(bad), sorted(set(b[0] for b in bad))[:5])
