import json, base64, sys, os, http.client, importlib.util, re, time
import os as _os
REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
# The console's own login page: a session cookie instead of the browser's Basic pop-up.
AUTH = "Basic " + base64.b64encode(b"tester:test-password-1234").decode()
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:500]) if (extra and not cond) else ""))
    fails += 0 if cond else 1


def req(method, path, body=None, headers=None, https=True):
    c = http.client.HTTPConnection("127.0.0.1", 18099, timeout=30)
    h = {"Host": "127.0.0.1:18099"}
    if https:
        h["X-Forwarded-Proto"] = "https"
    h.update(headers or {})
    if method == "POST":
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse(); d = r.read()
    hd = dict((k.lower(), v) for k, v in r.getheaders())
    cookies = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
    try:
        j = json.loads(d)
    except Exception:
        j = d.decode("utf-8", "replace")
    return r.status, j, hd, cookies


WEB = {"X-MD-Web": "1"}
NAV = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Dest": "document"}

s, j, hd, ck = req("GET", "/", headers=NAV)
check("a browser opening the console without signing in gets the login page, not a pop-up",
      s == 401 and "www-authenticate" not in hd and isinstance(j, str) and 'id="f"' in j and "Sign in" in j and "<script src=\"/login.js\">" in j, (s, hd, str(j)[:200]))
s, j, hd, ck = req("GET", "/#/run", headers=NAV)
check("any page address shows it (the part after # is kept by the browser)", s == 401 and "Sign in" in str(j))
s, j, hd, ck = req("GET", "/login.css")
s2, j2, hd2, _ = req("GET", "/login.js")
check("the login page's own style and script load without signing in", s == 200 and s2 == 200 and "text/css" in hd["content-type"] and "javascript" in hd2["content-type"] and len(j) > 500, (s, s2))
s, j, hd, ck = req("GET", "/api/status")
check("a script (curl, a monitor) with no login is still offered the standard Basic challenge", s == 401 and "Basic" in hd.get("www-authenticate", ""), (s, hd))
s, j, hd, ck = req("GET", "/api/status", headers=dict(WEB, **{"Sec-Fetch-Dest": "empty"}))
check("the page's own requests get a plain 401 with no challenge, so no browser pop-up", s == 401 and "www-authenticate" not in hd, (s, hd))
s, j, hd, ck = req("GET", "/api/status", headers={"Authorization": AUTH})
check("Basic login still works for scripts", s == 200 and "version" in j, (s, str(j)[:100]))
s, j, hd, ck = req("GET", "/out/plan.json", headers=NAV)
check("files under /out/ are never answered with the login page", s == 401 and not str(j).lstrip().startswith("<"), (s, str(j)[:80]))

# --- signing in
s, j, hd, ck = req("POST", "/api/login", {"user": "tester", "password": "test-password-1234"})
check("login without the X-MD-Web header is refused", s == 403 and not ck, (s, j))
s, j, hd, ck = req("POST", "/api/login", {"user": "tester", "password": "test-password-1234"}, dict(WEB, Origin="https://evil.example"))
check("login from another origin is refused", s == 403 and not ck, (s, j))
s, j, hd, ck = req("POST", "/api/login", {"user": "tester", "password": "wrong-one"}, WEB)
check("a wrong password is refused and sets no cookie", s == 401 and "wrong" in j["error"] and not ck, (s, j, ck))
s, j, hd, ck = req("POST", "/api/login", {"user": "nobody", "password": "test-password-1234"}, WEB)
check("a wrong user name gets the same answer (does not say which half was wrong)", s == 401 and j["error"] == "wrong user name or password", (s, j))
s, j, hd, ck = req("POST", "/api/login", {"user": "tester", "password": "test-password-1234"}, WEB)
cookie = ck[0] if ck else ""
tok = re.match(r"dewey_session=([^;]+)", cookie)
check("the right login sets an HttpOnly, SameSite=Strict, Secure session cookie with no expiry date",
      s == 200 and j.get("user") == "tester" and bool(tok) and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
      and "Max-Age" not in cookie and "Expires" not in cookie and len(tok.group(1)) >= 40, (s, j, cookie))
H = {"Cookie": "dewey_session=" + (tok.group(1) if tok else "")}
s, j, hd, ck = req("POST", "/api/login", {"user": "tester", "password": "test-password-1234"}, WEB, https=False)
check("over plain http the cookie is set without the Secure flag (so the lab setup still works)", s == 200 and ck and "Secure" not in ck[0], ck)

s, j, hd, ck = req("GET", "/api/status", headers=H)
check("the cookie opens the console's data", s == 200 and "version" in j, (s, str(j)[:100]))
s, j, hd, ck = req("GET", "/", headers=dict(NAV, **H))
check("a page request with the cookie gets the console, not the login page", s == 200 and "Dewey" in j and 'id="f"' not in j, (s, str(j)[:120]))
s, j, hd, ck = req("GET", "/api/session", headers=H)
check("/api/session says who is signed in", s == 200 and j["user"] == "tester" and j["cookie"] is True and j["login"] is True, (s, j))
s, j, hd, ck = req("GET", "/api/session", headers={"Authorization": AUTH})
check("a Basic client has no cookie session", s == 200 and j["cookie"] is False and j["user"] == "tester", (s, j))
s, j, hd, ck = req("POST", "/api/runner/disarm", {}, dict(H, **WEB))
check("changing things with the cookie works (same X-MD-Web rule as before)", s == 200, (s, str(j)[:120]))
s, j, hd, ck = req("POST", "/api/runner/disarm", {}, dict(H))
check("a cookie request without the X-MD-Web header is still refused (forms on other sites cannot send it)", s == 403, (s, j))
s, j, hd, ck = req("POST", "/api/runner/disarm", {}, dict(H, **dict(WEB, Origin="https://evil.example")))
check("a cookie request from another origin is refused", s == 403, (s, j))
s, j, hd, ck = req("GET", "/api/status", headers={"Cookie": "dewey_session=" + "A" * 43})
check("a made-up cookie is not accepted", s == 401, (s, j))
s, j, hd, ck = req("GET", "/api/status", headers={"Cookie": "dewey_session=" + "A" * 5000})
check("an oversized cookie is not accepted either", s == 401, s)

s, j, hd, ck = req("POST", "/api/logout", {}, dict(H, **WEB))
check("sign out clears the cookie", s == 200 and ck and "Max-Age=0" in ck[0], (s, ck))
s, j, hd, ck = req("GET", "/api/status", headers=H)
check("the signed-out cookie no longer works (the server forgot it)", s == 401, (s, j))
s, j, hd, ck = req("POST", "/api/logout", {}, WEB)
check("signing out with no session is harmless", s == 200, (s, j))

# --- session timing, tested on the class itself (the real limits are minutes and hours)
os.environ.update({"MD_OUT": R + "/out", "MD_WEBROOT": REPO + "/web", "MD_WEB_USER": "tester", "MD_WEB_PASS": "x" * 12, "MD_WEB_PORT": "18998",
                   "MD_STATE": R + "/state", "MD_QUEUE": R + "/queue", "MD_MUSIC": R + "/media/Music", "MD_RULES": R + "/rules.txt", "MD_NDDB": R + "/nd/navidrome.db"})
sp = importlib.util.spec_from_file_location("webui_t", os.path.join(REPO, "webui.py")); wu = importlib.util.module_from_spec(sp); sp.loader.exec_module(wu)
S = wu.Sessions()
t = S.create("tester")
check("a new session is valid", S.get(t) == "tester")
k = S._key(t)
S.items[k]["last"] -= S.IDLE + 5
check("a session unused for longer than the idle limit ends", S.get(t) == "" and S.count() == 0)
t = S.create("tester"); k = S._key(t)
S.items[k]["last"] -= S.IDLE - 60
S.get(t, touch=False)
check("a background poll (touch=False) does not extend it", abs(S.items[k]["last"] - (time.time() - (S.IDLE - 60))) < 5)
S.get(t, touch=True)
check("real use does extend it", S.items[k]["last"] > time.time() - 5)
S.items[k]["t0"] -= S.MAX + 5
check("past the overall limit it ends when no job is running", S.get(t, busy=lambda: False) == "")
t = S.create("tester"); k = S._key(t)
S.items[k]["t0"] -= S.MAX + 5
check("past the overall limit it is NOT cut off while a job is running", S.get(t, busy=lambda: True) == "tester")
check("...and ends right after the job finishes", S.get(t, busy=lambda: False) == "")
check("limits are 30 minutes idle and 8 hours overall by default", S.IDLE == 1800 and S.MAX == 28800, (S.IDLE, S.MAX))
for i in range(S.LIMIT + 10):
    S.create("tester")
check("the number of live sessions is capped", S.count() <= S.LIMIT, S.count())
check("a session token is stored only as a hash", all(len(x) == 64 for x in S.items))

# --- lock-out (last: it locks the login for a minute)
codes = []
for i in range(6):
    s, j, hd, ck = req("POST", "/api/login", {"user": "tester", "password": "wrong-%d" % i}, WEB)
    codes.append(s)
check("five different wrong passwords lock the login", 429 in codes, codes)
s, j, hd, ck = req("POST", "/api/login", {"user": "tester", "password": "test-password-1234"}, WEB)
check("while locked even the right password is refused, with the wait time", s == 429 and j.get("locked_for", 0) > 0 and not ck, (s, j))
s, j, hd, ck = req("GET", "/api/status", headers={"Authorization": AUTH})
check("the same lock-out applies to Basic clients", s == 429, (s, j))
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
