import os as _os, subprocess, sys, json, shutil
REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
# The demo library must keep telling the story the README tells: one duplicate removable, one blocked by a playlist,
# one wide-gap pair held for approval, a landing folder with new music, a layout to fix, a file with no tags.
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:600]) if (extra and not cond) else ""))
    fails += 0 if cond else 1


env = dict(_os.environ, DEWEY_DEMO_DIR=R)
p = subprocess.run([sys.executable, _os.path.join(REPO, "demo", "make_demo.py")], env=env, capture_output=True, text=True)
check("demo library builds", p.returncode == 0 and "demo library ready" in p.stdout, p.stdout + p.stderr)
M, I, Q = R + "/media/Music", R + "/media/_incoming", R + "/media/_dedupe-quarantine"
env.update({"MD_MUSIC": M, "MD_OUT": R + "/out", "MD_QUAR": Q, "MD_INCOMING": I, "MD_REORG": R + "/media/_reorg", "MD_NDDB": R + "/navidrome-data/navidrome.db"})


def tool(*a):
    q = subprocess.run([sys.executable, _os.path.join(REPO, "dewey.py")] + list(a), env=env, capture_output=True, text=True)
    return q.returncode, q.stdout + q.stderr


rc, t = tool("scan")
check("scan finds 14 audio files, 2 exact groups and 1 wide-gap group", rc == 0 and "tracks 14 | exact 2 | variant 0 | wide-gap 1" in t, t[-600:])
check("one copy removable, one blocked by a Navidrome playlist", "removable 1 files" in t and "blocked by playlists 1" in t, t[-600:])
rc, t = tool("apply")
check("apply (dry run) would remove the stray copy of Stone Bridge only", rc == 0 and "would remove 1 files" in t and "REMOVE  Paper Lanterns/Slow River (2021)/02 - Stone Bridge.flac" in t and "DRY RUN" in t, t[-800:])
check("apply names the MP3 of Signal Fire as blocked by the Evening playlist", "BLOCKED" in t and "Downloads/Lumen Harbor - Signal Fire.mp3" in t and "in: Evening" in t, t[-800:])
check("the wide-gap pair is held, not removed", "Home Stretch" not in t.split("BLOCKED")[0], t[-800:])
rc, t = tool("place")
check("place (dry run): 5 new songs, 1 already in the library, 2 playlists", rc == 0 and "to place 5" in t and "already in the library / arrive twice 1" in t and "playlist 'Road Trip Mix': would create (2 tracks)" in t, t[-900:])
rc, t = tool("reorg")
check("reorg plans 9 moves and skips the file with no tags", rc == 0 and "to move 9" in t and "skipped 1" in t, t[-600:])
rc, t = tool("tagaudit")
check("tagaudit reports one file missing core tags", rc == 0 and "files missing title, artist or album: 1" in t, t[-600:])
# the dashboard endpoint (Home Assistant / Homepage): counts the demo library is known to hold
wenv = dict(env, MD_STATE=R + "/wstate", MD_QUEUE=R + "/wqueue", MD_WEBROOT=_os.path.join(REPO, "web"), MD_APP=_os.path.join(REPO, "dewey.py"))
_os.makedirs(R + "/wstate", exist_ok=True); _os.makedirs(R + "/wqueue", exist_ok=True)
q = subprocess.run([sys.executable, "-c", "import json,sys; sys.path.insert(0, sys.argv[1]); import webui; print('W=' + json.dumps(webui.api_widget()))", REPO],
                   env=wenv, capture_output=True, text=True)
try:
    w = json.loads([l for l in q.stdout.splitlines() if l.startswith("W=")][0][2:])
except Exception:
    w = {}
check("widget: artists and albums as the scan counts them, 13 FLAC and 1 MP3", (w.get("artists"), w.get("albums"), w.get("flac_files"), w.get("mp3_files"), w.get("files")) == (5, 5, 13, 1, 14), (w, q.stderr[-400:]))
check("widget: last_scan is an ISO time with an offset", bool(w.get("last_scan")) and ("+" in w["last_scan"][19:] or "-" in w["last_scan"][19:] or w["last_scan"].endswith("Z")), w)
files = sorted(_os.path.join(d, f) for d, _s, fs in _os.walk(M) for f in fs)
check("nothing in the demo library was changed by any dry run", len(files) == 17 and _os.path.exists(M + "/Misc/stone-bridge-copy.flac") and not _os.listdir(Q), files)
shutil.rmtree(R + "/media", ignore_errors=True)
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
