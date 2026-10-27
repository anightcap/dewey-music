#!/usr/bin/env python3
"""Dewey test runner: builds a throw-away rig for every suite, starts the runner and the console from it,
runs the suite, stops everything and prints a summary.  Needs Linux or macOS (or WSL), python3, ffmpeg and bash;
the browser tests also need node + playwright (use --no-ui to skip them).

  python3 tests/run_tests.py             everything
  python3 tests/run_tests.py --no-ui     only the API / engine suites
  python3 tests/run_tests.py console undo   only suites whose name contains one of these words

Nothing outside a temporary folder is touched, and no real music library or Navidrome is needed."""
import os, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PORT = 18099
PRE_REORG = "reorg --workers 1"          # builds the folder-layout plan in the messy-library rig

# (name, kind, rig, suite file, extra args, pre-step)
SUITES = [
    ("console basics",             "api", "rig0", "t01_console.py", [], None),
    ("arming lockout",             "api", "rig0", "t02.py", [], None),
    ("arming expiry",              "api", "rig1", "t03.py", [], None),
    ("remove duplicates (apply)",  "api", "rig2", "t4.py", [], None),
    ("no false 'interrupted'",     "api", "rig0", "t5.py", ["30"], None),
    ("file new music (place)",     "api", "rig3", "t6.py", [], None),
    ("folder layout (reorg)",      "api", "rig4", "t7.py", [], None),
    ("layout batches",             "api", "rig4", "t8.py", [], None),
    ("beets repoint",              "api", "rig5", "t9.py", [], None),
    ("undo and purge",             "api", "rig6", "t10.py", [], None),
    ("demo library",               "api", None,   "t11_demo.py", [], None),
    ("browser: playlist fix",      "ui",  "rig0", "ui.js", [], None),
    ("browser: remove duplicates", "ui",  "rig2", "ui_apply.js", [], None),
    ("browser: file new music",    "ui",  "rig3", "ui_place.js", [], None),
    ("browser: folder layout",     "ui",  "rig4", "ui_reorg.js", [], PRE_REORG),
    ("browser: box without reload", "ui", "rig4", "ui_noreload.js", [], PRE_REORG),
    ("browser: beets repoint",     "ui",  "rig5", "ui_beets.js", [], None),
    ("browser: undo and purge",    "ui",  "rig6", "ui_undo.js", [], None),
]


def stop_rig(rig_dir):
    for name in ("runner.pid", "web.pid"):
        try:
            os.kill(int(open(os.path.join(rig_dir, name)).read().strip()), signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.8)


def main():
    args = sys.argv[1:]
    no_ui = "--no-ui" in args
    words = [a.lower() for a in args if not a.startswith("--")]
    todo = [s for s in SUITES if not (no_ui and s[1] == "ui") and (not words or any(w in (s[0] + " " + s[3]).lower() for w in words))]
    tmp = os.environ.get("DEWEY_TEST_TMP") or tempfile.mkdtemp(prefix="dewey-tests-")
    os.makedirs(tmp, exist_ok=True)
    keep = os.environ.get("DEWEY_TEST_KEEP")
    tls_dir = os.path.join(tmp, "tls")
    results = []
    for n, (name, kind, rig, suite, extra, pre) in enumerate(todo, 1):
        rig_dir = os.path.join(tmp, "rig%02d" % n)
        env = dict(os.environ, DEWEY_REPO=REPO, DEWEY_TEST_TMP=tmp, TLS_DIR=tls_dir, NODE_PATH=os.environ.get("NODE_PATH", ""))
        print("\n=== %s  (%s, %s)" % (name, rig, suite), flush=True)
        stop_rig(rig_dir)
        s = subprocess.run(["bash", os.path.join(HERE, "rigs", rig + ".sh"), os.path.basename(rig_dir)], env=env, capture_output=True, text=True) if rig else None
        if rig is None:
            os.makedirs(rig_dir, exist_ok=True)
        if s is not None and s.returncode != 0:
            print("rig setup failed:\n" + s.stdout[-800:] + s.stderr[-800:]); results.append((name, False, "rig setup failed")); continue
        # the rig's own env.sh holds every MD_* setting the runner and console use
        rig_env = subprocess.run(["bash", "-c", ". %s/env.sh && env" % rig_dir], capture_output=True, text=True, env=env).stdout if rig else ""
        for line in rig_env.splitlines():
            if "=" in line and line.split("=", 1)[0].startswith("MD_"):
                env[line.split("=", 1)[0]] = line.split("=", 1)[1]
        if pre:
            subprocess.run([sys.executable, os.path.join(REPO, "dewey.py")] + pre.split(), cwd=rig_dir, env=env, capture_output=True, text=True)
        proxy = None
        try:
            if kind == "ui":
                if not os.path.exists(os.path.join(tls_dir, "c.pem")):
                    os.makedirs(tls_dir, exist_ok=True)
                    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=127.0.0.1",
                                    "-keyout", os.path.join(tls_dir, "k.pem"), "-out", os.path.join(tls_dir, "c.pem")], capture_output=True, check=True)
                proxy = subprocess.Popen(["node", os.path.join(HERE, "ui", "tlsproxy.js")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
                cmd = ["node", os.path.join(HERE, "ui", suite), rig_dir]
            else:
                cmd = [sys.executable, os.path.join(HERE, "suites", suite), rig_dir] if suite != "t5.py" else [sys.executable, os.path.join(HERE, "suites", suite)] + extra
            try:
                p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
                out, rc = p.stdout + p.stderr, p.returncode
            except subprocess.TimeoutExpired as e:
                out, rc = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), 1
                out += "\nTIMEOUT"
        finally:
            if proxy:
                proxy.terminate()
            stop_rig(rig_dir)
        lines = out.splitlines()
        fails = [l for l in lines if l.startswith("FAIL ")]
        npass = len([l for l in lines if l.startswith("PASS")])
        if suite == "t5.py":
            m = re.search(r"readings: (\d+)", out); ok = rc == 0 and m and m.group(1) == "0"
        elif kind == "ui":
            ok = rc == 0 and "Error" not in out and "TIMEOUT" not in out and not fails
        else:
            ok = rc == 0 and not fails and npass > 0
        print("  %s  (%d checks passed%s)" % ("OK" if ok else "FAILED", npass, (", %d failed" % len(fails)) if fails else ""))
        if not ok:
            print("\n".join("    " + l[:220] for l in (fails + lines[-12:])[:30]))
        results.append((name, bool(ok), ""))
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [r for r in results if not r[1]]
    print("\n%d of %d test groups passed" % (len(results) - len(bad), len(results)))
    for r in bad:
        print("  FAILED: " + r[0] + (" - " + r[2] if r[2] else ""))
    sys.exit(1 if bad or not results else 0)


if __name__ == "__main__":
    main()
