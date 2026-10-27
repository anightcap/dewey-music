import json, base64, time, sys, os, http.client, importlib.util, subprocess, glob, shutil, hashlib
import os as _os; REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
spec = importlib.util.spec_from_file_location("runner", _os.path.join(REPO, "runner.py")); rn = importlib.util.module_from_spec(spec); spec.loader.exec_module(rn)
AUTH = "Basic " + base64.b64encode(b"tester:test-password-1234").decode()
M = R + "/media/Music"; Q = R + "/media/_dedupe-quarantine"; I = R + "/media/_incoming"; RO = R + "/media/_reorg"
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
def code(): return rn.totp_code(open(os.path.join(R, "state", "totp.key")).read().strip(), int(time.time() // 30))
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
def real(d, confirm, cmd): return call("POST", "/api/runner/run", {"cmd": cmd, "dry_job": d, "confirm": confirm})
def elig(cmd): return call("GET", "/api/runner")[1]["eligible"].get(cmd)
def ex(root, rel): return os.path.exists(os.path.join(root, rel))
def man(run): return Q + "/" + run + "/manifest.json"
def tool(*a):
    env = dict(os.environ); env.update({"MD_MUSIC": M, "MD_OUT": R + "/out", "MD_QUAR": Q, "MD_REORG": RO})
    p = subprocess.run([sys.executable, _os.path.join(REPO, "dewey.py")] + list(a), capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr
def forge(name, log, **kw):
    m = {"id": name, "cmd": "undo", "title": "x", "args": [], "started": "2026-10-24 12:00:00", "t0": 0, "state": "done", "rc": 0,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "dry"}
    m.update(kw); json.dump(m, open("%s/out/web-jobs/%s.json" % (R, name), "w"))
    if log is not None: open("%s/out/web-jobs/%s.log" % (R, name), "w").write(log)
    return name

runs = sorted(os.listdir(Q)); apply_run = [r for r in runs if not r.startswith("place-")][0]; place_run = [r for r in runs if r.startswith("place-")][0]
reorg_run = sorted(os.listdir(RO))[0]
rman = RO + "/" + reorg_run + "/manifest.json"
s, j = call("POST", "/api/runner/arm", {"code": code()}); check("armed", s == 200 and j["armed"], (s, j))
st = call("GET", "/api/runner")[1]
check("runner offers undo (RESTORE {n}) and purge (DELETE {n})", "undo" in st["commands"] and "purge" in st["commands"] and st["confirm"]["undo"] == "RESTORE {n}" and st["confirm"]["purge"] == "DELETE {n}", st)
old_mix = open(M + "/Playlists/Old.m3u").read(); mix_after_reorg = open(M + "/Playlists/Mix.m3u").read()
check("start: apply run set the two lossy copies aside; Old.m3u rewritten", not ex(M, "Gamma/Done Album (2020)/01 - Fine.mp3") and "Fine.mp3" not in old_mix, old_mix)

# ===== tool-level guards (no runner) =====
os.makedirs("/tmp/evil-run", exist_ok=True); shutil.copy(man(apply_run), "/tmp/evil-run/manifest.json")
rc, out = tool("undo", "--manifest", "/tmp/evil-run/manifest.json", "--execute")
check("copied manifest in another folder refused (CLI, --execute)", rc != 0 and "REFUSED" in out and "Nothing was changed" in out and not ex(M, "Gamma/Done Album (2020)/01 - Fine.mp3"), out)
rc, out = tool("undo", "--manifest", "/tmp/evil-run/manifest.json")
check("... and in a dry run too", rc != 0 and "REFUSED" in out, out)
os.makedirs(Q + "/zz-fake", exist_ok=True)
json.dump({"run": "zz", "music_root": "/somewhere/else", "quarantine": Q + "/zz-fake", "files": [], "playlists": [], "dirs": []}, open(Q + "/zz-fake/manifest.json", "w"))
rc, out = tool("undo", "--manifest", Q + "/zz-fake/manifest.json")
check("manifest for a different library refused", rc != 0 and "belongs to the library /somewhere/else" in out, out)
shutil.rmtree(Q + "/zz-fake")
rc, out = tool("undo", "--manifest", man(apply_run))
check("dry run prints the summary line + fingerprint at the top", rc == 0 and "undo quarantine run %s: 2 file(s) to put back | 1 playlist(s) | skipped 0" % apply_run in out.split("\n\n")[0] + out and "plan fingerprint: " in out and "DRY RUN" in out, out)
fp = out.split("plan fingerprint: ")[1][:16]
rc, out = tool("undo", "--manifest", man(apply_run), "--execute", "--expect-plan", "0123456789abcdef")
check("wrong fingerprint refused, nothing moved", rc != 0 and "REFUSED" in out and not ex(M, "Gamma/Done Album (2020)/01 - Fine.mp3"), out)

# ===== undo of the apply (quarantine) run through the console and the runner =====
d1, r = job("undo", manifest=man(apply_run))
check("web dry run done", r and r["meta"]["state"] == "done" and "2 file(s) to put back | 1 playlist(s)" in r["text"], r and r["text"][-600:])
el = elig("undo")
check("eligible: n=2, kind quarantine, 1 playlist, fingerprint, the manifest in its args", el and el["job"] == d1 and el["n"] == 2 and el["kind"] == "quarantine" and el["playlists"] == 1 and el["fp"] == fp and el["args"] == ["--manifest=" + man(apply_run)], el)
for bad in ("RESTORE", "RESTORE 1", "restore 2", "RESTORE 2 ", ""):
    s, j = real(d1, bad, "undo"); check("wrong phrase %r refused" % bad, s == 400 and "RESTORE 2" in j["error"], (s, j))
check("nothing restored yet", not ex(M, "Gamma/Done Album (2020)/01 - Fine.mp3"))
s, j = real(d1, "RESTORE 2", "undo"); check("real undo starts", s == 201 and j.get("job"), (s, j))
r = wait_job(j["job"]); t = r["text"]
check("real undo finished OK through the runner", r["meta"]["state"] == "done" and r["meta"]["runner"], t[-700:])
check("argv has --manifest, --execute, --expect-plan=<fp> and no --plan", "--manifest=" + man(apply_run) in t and "--execute" in t and "--expect-plan=" + fp in t and "--plan=" not in t, t[:400])
check("both lossy copies are back", ex(M, "Gamma/Done Album (2020)/01 - Fine.mp3") and ex(M, "Delta/Single (2015)/01 - Song.mp3"))
check("playlist Old.m3u restored from its backup", open(M + "/Playlists/Old.m3u").read() != old_mix and "Fine.mp3" in open(M + "/Playlists/Old.m3u").read(), open(M + "/Playlists/Old.m3u").read())
check("run folder (manifest) is still there", ex(Q, apply_run + "/manifest.json"))
s, j = real(d1, "RESTORE 2", "undo"); check("the same dry run cannot be used twice", s == 400 and "already authorised" in j["error"], (s, j))

# ===== undo again: nothing left =====
with open(M + "/Playlists/Old.m3u", "a") as fh: fh.write("# my edit after the undo\n")
d2, r = job("undo", manifest=man(apply_run))
check("second dry run: 0 files to put back", r and "0 file(s) to put back | 0 playlist(s) | skipped 2" in r["text"], r and r["text"][:900])
check("nothing is offered for a real run", elig("undo") is None, elig("undo"))
s, j = real(d2, "RESTORE 0", "undo"); check("real call refused: nothing to do", s == 400 and "nothing to do" in j["error"], (s, j))
check("playlist edit made after the first undo is untouched", open(M + "/Playlists/Old.m3u").read().endswith("# my edit after the undo\n"))
rc, out = tool("undo", "--manifest", man(apply_run), "--execute")
check("CLI second undo: nothing to undo, playlist not overwritten", rc == 0 and "nothing to undo" in out and open(M + "/Playlists/Old.m3u").read().endswith("# my edit after the undo\n"), out)

# ===== undo of the place run, with drift =====
d3, r = job("undo", manifest=man(place_run))
check("place undo dry run: 1 file", r and "undo place run" in r["text"] and "1 file(s) to put back | 0 playlist(s)" in r["text"], r and r["text"][:600])
el = elig("undo"); check("eligible n=1 kind place", el and el["n"] == 1 and el["kind"] == "place", el)
os.makedirs(I + "/Fresh Mix", exist_ok=True); open(I + "/Fresh Mix/Fine.flac", "w").write("someone dropped a file here")
s, j = real(d3, "RESTORE 1", "undo"); r = wait_job(j["job"]) if s == 201 else None
check("drift (the spot in _incoming got taken) -> the TOOL refuses, nothing moved", s == 201 and r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and ex(Q, place_run + "/Fresh Mix/Fine.flac"), (s, j, r and r["text"][-500:]))
os.unlink(I + "/Fresh Mix/Fine.flac")
d3, r = job("undo", manifest=man(place_run)); s, j = real(d3, "RESTORE 1", "undo"); r = wait_job(j["job"])
check("fresh dry run + real: file back in _incoming", r["meta"]["state"] == "done" and ex(I, "Fresh Mix/Fine.flac") and not ex(Q, place_run + "/Fresh Mix/Fine.flac"), r["text"][-500:])

# ===== undo of the reorg run, with drift =====
before = {p: 1 for p in ("Alpha Band/First Album/a1.flac", "Alpha Band/First Album/a2.flac", "Alpha Band/First Album/a3.flac", "Alpha Band/First Album/a1.lrc", "Beta Group/Second Album/b1.flac", "Beta Group/Second Album/b2.flac")}
check("before: the layout run has moved the files", all(not ex(M, p) for p in before) and ex(M, "Alpha Band/First Album (2001)/01 - Opening.flac"))
rd1, r = job("undo", manifest=rman)
check("reorg undo dry run: 6 files, 1 playlist", r and "undo reorg run %s: 6 file(s) to put back | 1 playlist(s) | skipped 0" % reorg_run in r["text"], r and r["text"][:700])
el = elig("undo"); check("eligible n=6 kind reorg", el and el["n"] == 6 and el["kind"] == "reorg" and el["playlists"] == 1, el)
os.makedirs(M + "/Alpha Band/First Album", exist_ok=True); open(M + "/Alpha Band/First Album/a1.flac", "w").write("squatter")
s, j = real(rd1, "RESTORE 6", "undo"); r = wait_job(j["job"]) if s == 201 else None
check("drift (an original path got occupied) -> the tool refuses, nothing moved", s == 201 and r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and ex(M, "Alpha Band/First Album (2001)/01 - Opening.flac"), (s, j, r and r["text"][-500:]))
os.unlink(M + "/Alpha Band/First Album/a1.flac"); os.rmdir(M + "/Alpha Band/First Album")
rd2, r = job("undo", manifest=rman); s, j = real(rd2, "RESTORE 6", "undo"); r = wait_job(j["job"])
check("fresh dry run + real: finished OK", r["meta"]["state"] == "done" and "moved 6 files back" in r["text"], r["text"][-700:])
check("every original path is back", all(ex(M, p) for p in before), [p for p in before if not ex(M, p)])
check("the new layout folders are gone", not ex(M, "Alpha Band/First Album (2001)") and not ex(M, "Beta Group/Second Album (2010)"))
check("playlist Mix.m3u is back to the original", "../Alpha Band/First Album/a2.flac" in open(M + "/Playlists/Mix.m3u").read() and open(M + "/Playlists/Mix.m3u").read() != mix_after_reorg)
check("the tool says what to do about Beets", "beets-remap" in r["text"])

# ===== forged requests straight at the runner =====
good_log = "$ dewey undo\n\nundo quarantine run x: 2 file(s) to put back | 1 playlist(s) | skipped 0\nplan fingerprint: 0123456789abcdef\n"
for name, args, why in (("20261024-190001-undo", ["--manifest=/tmp/evil-run/manifest.json"], "outside"),
                        ("20261024-190002-undo", ["--manifest=" + Q + "/../../../../tmp/evil-run/manifest.json"], "dotdot"),
                        ("20261024-190003-undo", ["--manifest=" + Q + "/" + apply_run + "/manifest.json", "--execute"], "extra --execute"),
                        ("20261024-190004-undo", [], "no manifest"),
                        ("20261024-190005-undo", ["--manifest=" + Q + "/" + apply_run + "/manifest.json", "--manifest=/tmp/evil-run/manifest.json"], "two manifests"),
                        ("20261024-190006-undo", ["--manifest=" + Q + "/" + apply_run], "folder not file"),
                        ("20261024-190007-undo", ["--manifest=" + Q + "/" + apply_run + "/_playlists/Old.m3u"], "other file"),
                        ("20261024-190008-undo", ["--manifest=" + Q + "/nonexistent/manifest.json"], "missing"),
                        ("20261024-190009-undo", ["--manifest=" + Q + "/" + apply_run + "/manifest.json", "--music=/"], "extra --music")):
    n = forge(name, good_log, args=args); s, j = real(n, "RESTORE 2", "undo"); check("forged undo (%s) refused" % why, s == 400 and ("manifest.json that sits inside" in j["error"] or "not allowed" in j["error"]), (s, j))
os.makedirs("/tmp/evil-run2", exist_ok=True); shutil.copy("/tmp/evil-run/manifest.json", "/tmp/evil-run2/manifest.json")
if os.path.lexists(Q + "/linkrun"): os.unlink(Q + "/linkrun")
os.symlink("/tmp/evil-run2", Q + "/linkrun")
n = forge("20261024-190010-undo", good_log, args=["--manifest=" + Q + "/linkrun/manifest.json"]); s, j = real(n, "RESTORE 2", "undo")
check("forged undo through a symlinked run folder refused", s == 400 and "sits inside" in j["error"], (s, j))
os.unlink(Q + "/linkrun")
n = forge("20261024-190011-undo", good_log, args=["--manifest=" + rman]); s, j = real(n, "RESTORE 2", "undo"); r = wait_job(j["job"]) if s == 201 else None
check("a valid manifest path with a made-up fingerprint: the tool refuses (nothing to restore / plan differs)", s == 201 and r["meta"]["state"] == "failed" and "REFUSED" in r["text"], (s, j, r and r["text"][-400:]))

# ===== purge =====
def mkrun(name, days, nfiles=3, size=1000):
    d = Q + "/" + name; os.makedirs(d, exist_ok=True)
    for i in range(nfiles): open(d + "/f%d.flac" % i, "wb").write(b"x" * size)
    t = time.time() - days * 86400; os.utime(d, (t, t))
for n_, dys in (("20260901-000000", 54), ("20260915-000000", 40), ("20261001-000000", 10)): mkrun(n_, dys)
os.makedirs("/tmp/evil-purge/precious", exist_ok=True); open("/tmp/evil-purge/precious/keep.txt", "w").write("do not delete")
t_old = time.time() - 90 * 86400; os.utime("/tmp/evil-purge", (t_old, t_old))
if os.path.lexists(Q + "/linkold"): os.unlink(Q + "/linkold")
os.symlink("/tmp/evil-purge", Q + "/linkold")
open(Q + "/stray.txt", "w").write("a file, not a run"); os.utime(Q + "/stray.txt", (t_old, t_old))
for run in os.listdir(Q):                                    # the runs the tool made are minutes old: they stay
    pass
pd, r = job("purge", **{"older-than": 30})
check("purge dry run: 2 runs, 6 files, summary + fingerprint", r and "purge plan: 2 run(s) to delete (5.9 KB, 6 file(s)) | keeping" in r["text"] and "plan fingerprint: " in r["text"], r and r["text"][:900])
check("the symlink and the stray file are not listed as runs", "linkold" not in r["text"] and "stray" not in r["text"], r["text"])
check("dry run deleted nothing", ex(Q, "20260901-000000/f0.flac") and ex(Q, "20260915-000000/f0.flac"))
el = elig("purge"); check("eligible n=2 with the limit in its args", el and el["n"] == 2 and el["files"] == 6 and el["days"] == 30 and el["args"] == ["--older-than=30"], el)
for bad in ("DELETE", "DELETE 3", "delete 2", "RESTORE 2", ""):
    s, j = real(pd, bad, "purge"); check("wrong phrase %r refused" % bad, s == 400 and "DELETE 2" in j["error"], (s, j))
# drift: another run ages past the limit after the dry run
t2 = time.time() - 45 * 86400; os.utime(Q + "/20261001-000000", (t2, t2))
s, j = real(pd, "DELETE 2", "purge"); r = wait_job(j["job"]) if s == 201 else None
check("drift (a third run aged past the limit) -> the tool refuses, NOTHING deleted", s == 201 and r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and ex(Q, "20260901-000000/f0.flac") and ex(Q, "20260915-000000/f0.flac") and ex(Q, "20261001-000000/f0.flac"), (s, j, r and r["text"][-500:]))
pd, r = job("purge", **{"older-than": 30})
check("fresh dry run: now 3 runs", r and "purge plan: 3 run(s) to delete" in r["text"], r and r["text"][:400])
el = elig("purge"); check("eligible n=3", el and el["n"] == 3, el)
s, j = real(pd, "DELETE 3", "purge"); r = wait_job(j["job"]); t = r["text"]
check("real purge finished OK through the runner", r["meta"]["state"] == "done" and r["meta"]["runner"] and "purged 3 run(s)" in t, t[-600:])
check("argv: --older-than=30 --execute --expect-plan=<fp>", "--older-than=30" in t and "--execute" in t and "--expect-plan=" in t, t[:300])
check("the 3 old runs are gone", not any(ex(Q, x) for x in ("20260901-000000", "20260915-000000", "20261001-000000")))
check("the runs the tool made today are still there (manifests intact)", ex(Q, apply_run + "/manifest.json") and ex(Q, place_run + "/manifest.json"))
check("symlink target and stray file untouched", ex("/tmp/evil-purge", "precious/keep.txt") and os.path.exists(Q + "/stray.txt") and os.path.islink(Q + "/linkold"))
check("the reorg root is never purged", ex(RO, reorg_run + "/manifest.json"))
s, j = real(pd, "DELETE 3", "purge"); check("dry run can't be reused", s == 400 and "already authorised" in j["error"], (s, j))
pd, r = job("purge", **{"older-than": 30}); check("nothing left: dry run says 0", r and "purge plan: 0 run(s) to delete" in r["text"], r and r["text"][:300])
check("... and nothing is offered", elig("purge") is None)
s, j = real(pd, "DELETE 0", "purge"); check("real call refused: nothing to do", s == 400 and "nothing to do" in j["error"], (s, j))
# limit floor
mkrun("20260101-000000", 200)
for lim in (1, 6):
    pd, r = job("purge", **{"older-than": lim}); check("dry run with %d days works (and lists the run)" % lim, r and r["meta"]["state"] == "done" and "purge plan: " in r["text"], r and r["text"][:300])
    check("... but a real run is not offered for %d days" % lim, elig("purge") is None, elig("purge"))
    s, j = real(pd, "DELETE 1", "purge"); check("... and refused if asked for anyway (%d days)" % lim, s == 400, (s, j))
    check("... nothing deleted", ex(Q, "20260101-000000/f0.flac"))
n = forge("20261024-190020-purge", "purge plan: 1 run(s) to delete (3.0 KB, 3 file(s)) | keeping 2 | older than 0 days\nplan fingerprint: 0123456789abcdef\n", cmd="purge", args=["--older-than=0"])
s, j = real(n, "DELETE 1", "purge"); check("forged --older-than=0 refused by the runner itself", s == 400 and "not allowed" in j["error"], (s, j))
for a in ("--older-than=-1", "--older-than=7;rm", "--older-than=", "--quarantine=/", "--execute", "--older-than=6"):
    n = forge("20261024-1900%02d-purge" % (30 + len(a)), "purge plan: 1 run(s) to delete (3.0 KB, 3 file(s)) | keeping 2 | older than 7 days\nplan fingerprint: 0123456789abcdef\n", cmd="purge", args=[a])
    s, j = real(n, "DELETE 1", "purge"); check("forged purge option %s refused" % a, s == 400 and "not allowed" in j["error"], (s, j))
n = forge("20261024-190090-purge", "purge plan: 1 run(s) to delete (3.0 KB, 3 file(s)) | keeping 2 | older than 7 days\nplan fingerprint: 0123456789abcdef\n", cmd="purge", args=["--older-than=7", "--older-than=7"])
s, j = real(n, "DELETE 1", "purge"); check("two limits refused", s == 400 and "only the age limit" in j["error"], (s, j))
n = forge("20261024-190091-purge", "purge plan: 1 run(s) to delete (3.0 KB, 3 file(s)) | keeping 2 | older than 30 days\nplan fingerprint: 0123456789abcdef\n", cmd="purge", args=["--older-than=30"])
s, j = real(n, "DELETE 1", "purge"); r = wait_job(j["job"]) if s == 201 else None
check("forged fingerprint with a valid limit: the tool refuses, the 200-day run survives", s == 201 and r["meta"]["state"] == "failed" and "REFUSED" in r["text"] and ex(Q, "20260101-000000/f0.flac"), (s, j, r and r["text"][-400:]))
shutil.rmtree("/tmp/evil-run", ignore_errors=True); shutil.rmtree("/tmp/evil-run2", ignore_errors=True); shutil.rmtree("/tmp/evil-purge", ignore_errors=True)
print("\nFAILS:", fails)
