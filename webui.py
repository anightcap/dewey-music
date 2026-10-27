#!/usr/bin/env python3
"""Dewey web console - phase 2 (reads, dry runs, and real runs only through the separate runner container).

A small web server (Python standard library only, no pip packages) that sits on top of the Dewey
command line tool:

  * shows everything the tool has written to out/ (duplicate groups, new-music plan, reorg plan, tag audit,
    quarantine runs, rules, logs) in a browsable form, with audio preview;
  * launches the tool's READ-ONLY commands as background jobs (scan, reorg, tagaudit, compare, nd-missing, and the
    DRY RUNS of apply / place / reorg-apply / playlistfix / beets-remap / undo / purge) and streams their log.

This program can not change the library: it refuses any job that carries --execute, only runs commands from a fixed
list with validated options, and is meant to run in a container that mounts the library read-only. A real run is
only ever done by runner.py in its own container, after an authenticator-app code has armed it; this server just
passes the request on through a shared queue folder (MD_QUEUE=/queue) and shows the runner's answer and log.

Environment (all optional; the defaults match the docker-compose.yml next to this file):
  MD_MUSIC=/media/Music   MD_OUT=/out   MD_RULES=/rules.txt   MD_QUAR=/media/_dedupe-quarantine
  MD_INCOMING=/media/_incoming   MD_REORG=/media/_reorg   MD_NDDB=/navidrome-data/navidrome.db
  MD_BEETSDB=/beets/library.db   MD_CRED=/navidrome.cred   MD_APP=/app/dewey.py   MD_WEBROOT=/app/web
  MD_WEB_PORT=8099   MD_WEB_HOST=0.0.0.0   MD_WEB_USER / MD_WEB_PASS  (set both to require a login)
  MD_WEB_USER_2 / MD_WEB_PASS_2 ... _9   one more login per person (the History shows who started a job)
  MD_WIDGET_TOKEN=<24+ random characters>   lets a dashboard (Homepage) read GET /api/widget, a few counts, and nothing else
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.environ.get

MUSIC = ENV("MD_MUSIC", "/media/Music")
MEDIA = os.path.dirname(MUSIC.rstrip("/")) or "/"
OUT = ENV("MD_OUT", "/out")
RULES = ENV("MD_RULES", "/rules.txt")
QUAR = ENV("MD_QUAR", os.path.join(MEDIA, "_dedupe-quarantine"))
INCOMING = ENV("MD_INCOMING", os.path.join(MEDIA, "_incoming"))
REORG_ROOT = ENV("MD_REORG", os.path.join(MEDIA, "_reorg"))
NDDB = ENV("MD_NDDB", "/navidrome-data/navidrome.db")
BEETSDB = ENV("MD_BEETSDB", "/beets/library.db")
CRED = ENV("MD_CRED", "/navidrome.cred")
APP = ENV("MD_APP", os.path.join(HERE, "dewey.py"))
WEBROOT = ENV("MD_WEBROOT", os.path.join(HERE, "web"))
PORT = int(ENV("MD_WEB_PORT", "8099"))
HOST = ENV("MD_WEB_HOST", "0.0.0.0")
def _load_users():
    """MD_WEB_USER / MD_WEB_PASS, then MD_WEB_USER_2 / MD_WEB_PASS_2 ... up to _9: one login per person (a pair with only one half is ignored with a note)"""
    users = []
    for i in range(1, 10):
        suf = "" if i == 1 else "_%d" % i
        u, pw = ENV("MD_WEB_USER" + suf, ""), ENV("MD_WEB_PASS" + suf, "")
        if bool(u) != bool(pw):
            sys.stderr.write("ignoring MD_WEB_USER%s / MD_WEB_PASS%s: both are needed\n" % (suf, suf))
        elif u:
            if any(u == x[0] for x in users):
                sys.stderr.write("ignoring MD_WEB_USER%s: the name %r is already used\n" % (suf, u))
            else:
                users.append((u, pw))
    return users


USERS = _load_users()
LOGIN_ON = bool(USERS)
JOBS_DIR = os.path.join(OUT, "web-jobs")
QUEUE = ENV("MD_QUEUE", "/queue")                          # request folder shared with the runner container (a Docker volume); the only way writes are ever asked for
# security settings (all optional, normally set in web.env next to dewey.sh, or in .env with Docker Compose)
COMPOSE_INSTALL = ENV("MD_INSTALL", "").strip().lower() == "compose"
CFG_FILE = ".env" if COMPOSE_INSTALL else "web.env"                  # where the MD_ settings live, for the hints below
APPLY_HINT = "run  docker compose up -d" if COMPOSE_INSTALL else "run  .\\dewey.ps1 web"
RUNNER_HINT = "docker compose --profile runner up -d" if COMPOSE_INSTALL else ".\\dewey.ps1 runner"
BIND_INFO = ENV("MD_WEB_BIND", "").strip()                 # what dewey.sh published the port on (127.0.0.1 = loopback only); shown on the Security panel
ALLOWED_HOSTS = set(x.strip().lower() for x in ENV("MD_WEB_HOSTS", "").split(",") if x.strip())    # host names this console answers to
WIDGET_TOKEN = ENV("MD_WIDGET_TOKEN", "").strip()                  # read-only dashboard token: valid for GET /api/widget only
if WIDGET_TOKEN and len(WIDGET_TOKEN) < 24:
    sys.stderr.write("ignoring MD_WIDGET_TOKEN: it must be at least 24 characters\n")
    WIDGET_TOKEN = ""
ALLOW_PLAIN_PW = ENV("MD_WEB_ALLOW_PLAIN_PASSWORD", "") == "1"   # lab use only: accept the Navidrome password over plain http

AUDIO_EXT = (".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aac", ".ape", ".wv", ".aif", ".aiff")
AUDIO_MIME = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".ogg": "audio/ogg",
              ".oga": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac", ".aif": "audio/aiff",
              ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma", ".ape": "audio/x-ape", ".wv": "audio/x-wavpack"}
ART_NAMES = ("cover", "folder", "front")
ART_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# files in out/ that may be opened or downloaded from the browser
OUT_FILES = ("report.html", "reorg-report.html", "dupes.csv", "plan.json", "inventory.json", "unmatched.csv", "filehealth.csv",
             "reorg-plan.csv", "reorg-plan.json", "reorg-moves.tsv", "tagaudit.csv", "tagaudit-albums.csv", "place-plan.csv",
             "incoming-map.tsv", "nd-missing.csv", "beets-remap.csv", "redirects.tsv", "scan.log")


def read_version():
    try:
        with open(APP, encoding="utf-8") as fh:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(20000), re.M)
            return m.group(1) if m else "?"
    except OSError:
        return "?"


VERSION = read_version()
WEB_VERSION = "1.10.1 (phase 2: real runs through the runner: playlist fix, remove duplicates, file new music, folder layout, Beets, undo, purge)"


# ---------------------------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------------------------
class Cache:
    """loads a file once and reloads it only when its size or mtime changes"""

    def __init__(self):
        self._d, self._lock = {}, threading.Lock()

    def get(self, path, loader):
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            ent = self._d.get(path)
        if ent and ent[0] == key:
            return ent[1]
        val = loader(path)
        with self._lock:
            self._d[path] = (key, val)
        return val


CACHE = Cache()


def load_json(path):
    def ld(p):
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    return CACHE.get(path, ld)


def load_csv(path):
    def ld(p):
        with open(p, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    return CACHE.get(path, ld)


def out(name):
    return os.path.join(OUT, name)


def mtime_iso(path):
    try:
        return dt.datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def age_days(path):
    try:
        return round((time.time() - os.stat(path).st_mtime) / 86400.0, 2)
    except OSError:
        return None


def safe_rel(rel):
    """a relative path that stays inside its root (no .., no absolute, no NUL)"""
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        return None
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or not parts:
        return None
    return "/".join(parts)


def resolve_under(root, rel):
    rel = safe_rel(rel)
    if rel is None:
        return None
    full = os.path.realpath(os.path.join(root, rel))
    rootr = os.path.realpath(root)
    if full != rootr and not full.startswith(rootr + os.sep):
        return None
    return full


def paginate(items, page, size):
    size = max(1, min(int(size or 40), 500))
    page = max(1, int(page or 1))
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    return {"total": total, "page": page, "pages": pages, "size": size, "items": items[(page - 1) * size: page * size]}


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def is_readonly_mount(path):
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        return None


# ---------------------------------------------------------------------------------------------------------------------
# console settings (out/web-settings.json): the only thing the console itself ever writes, and only inside out/
# ---------------------------------------------------------------------------------------------------------------------
SETTINGS_PATH = os.path.join(OUT, "web-settings.json")
SETTING_DEFAULTS = {"workers": 4, "tolerance": None, "wide_tolerance": None}
GLOBAL_OPTS = {"workers": "workers", "tolerance": "tolerance", "wide-tolerance": "wide_tolerance"}   # job option -> setting
STARTED = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_settings():
    s = dict(SETTING_DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        s.update(clean_settings(d, strict=False))
    except (OSError, ValueError, TypeError):
        pass
    return s


def clean_settings(d, strict=True):
    """validated settings; unknown keys are ignored. strict=True raises ValueError with a message for the user."""
    res = {}
    if not isinstance(d, dict):
        raise ValueError("settings must be an object")
    if "workers" in d:
        w = d["workers"]
        if w in (None, ""):
            res["workers"] = SETTING_DEFAULTS["workers"]
        else:
            try:
                w = int(w)
            except (TypeError, ValueError):
                if strict:
                    raise ValueError("Parallel workers must be a whole number")
                w = SETTING_DEFAULTS["workers"]
            if not 1 <= w <= 16:
                if strict:
                    raise ValueError("Parallel workers must be between 1 and 16")
                w = SETTING_DEFAULTS["workers"]
            res["workers"] = w
    for k, label in (("tolerance", "Tolerance"), ("wide_tolerance", "Wide tolerance")):
        if k in d:
            v = d[k]
            if v in (None, ""):
                res[k] = None
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                if strict:
                    raise ValueError("%s must be a number of seconds (or blank to use rules.txt)" % label)
                res[k] = None
                continue
            if not 0 < v <= 60:
                if strict:
                    raise ValueError("%s must be between 0 and 60 seconds" % label)
                res[k] = None
                continue
            res[k] = round(v, 3)
    return res


def save_settings(d):
    new = clean_settings(d)
    cur = load_settings()
    cur.update(new)
    if cur["tolerance"] is not None and cur["wide_tolerance"] is not None and cur["wide_tolerance"] < cur["tolerance"]:
        raise ValueError("Wide tolerance must be at least as large as Tolerance")
    if not os.path.isdir(OUT) or not os.access(OUT, os.W_OK):
        raise ValueError("the out folder is not writable from this container")
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1)
    os.replace(tmp, SETTINGS_PATH)
    return cur


def rules_tolerances():
    """strict / wide tolerance as rules.txt sets them (None = the tool's own default of 3 s / 6 s)"""
    res = {"tolerance": None, "wide_tolerance": None}
    try:
        with open(RULES, encoding="utf-8-sig") as fh:
            for line in fh:
                m = re.match(r"^(tolerance|wide-tolerance):\s*([0-9.]+)", line.strip())
                if m:
                    res[m.group(1).replace("-", "_")] = float(m.group(2))
    except (OSError, ValueError):
        pass
    return res


def api_settings():
    st = load_settings()
    rt = rules_tolerances()
    return {"settings": st, "defaults": SETTING_DEFAULTS, "rules": rt, "tool_defaults": {"tolerance": 3.0, "wide_tolerance": 6.0},
            "path": SETTINGS_PATH, "writable": os.path.isdir(OUT) and os.access(OUT, os.W_OK)}


# ---------------------------------------------------------------------------------------------------------------------
# data: duplicate groups
# ---------------------------------------------------------------------------------------------------------------------
def group_status(g):
    roles = [m["role"] for m in g["members"]]
    if "remove" in roles:
        return "removable"
    if "blocked" in roles:
        return "blocked"
    if "review" in roles:
        return "review"
    return "keep"


def group_summary(g):
    ms = g["members"]
    roles = {}
    for m in ms:
        roles[m["role"]] = roles.get(m["role"], 0) + 1
    reclaim = sum(m.get("size") or 0 for m in ms if m["role"] in ("remove", "blocked"))
    return {
        "id": g["id"], "artist": g["artist"], "title": g["title"], "kind": g["kind"], "note": g.get("note", ""),
        "status": group_status(g), "n": len(ms), "roles": roles, "reclaim": reclaim,
        "plays": sum(m.get("plays") or 0 for m in ms), "starred": any(m.get("starred") for m in ms),
        "playlists": sorted(set(p for m in ms for p in (m.get("pl_nd") or []) + (m.get("pl_m3u") or []))),
        "label": any(m.get("adv_mismatch") for m in ms),
        "fmts": sorted(set(m.get("fmt", "") for m in ms)),
    }


def load_plan():
    return load_json(out("plan.json"))


def api_plan_info():
    plan = load_plan()
    if plan is None:
        return {"exists": False}
    groups = plan["groups"]
    kinds, status = {}, {}
    reclaim = 0
    for g in groups:
        kinds[g["kind"]] = kinds.get(g["kind"], 0) + 1
        s = group_status(g)
        status[s] = status.get(s, 0) + 1
        reclaim += sum(m.get("size") or 0 for m in g["members"] if m["role"] == "remove")
    # has the library changed since this scan? (any real apply / reorg-apply / place / undo run after the plan was written)
    changed = []
    pm = os.stat(out("plan.json")).st_mtime
    logd = out("logs")
    if os.path.isdir(logd):
        for n in sorted(os.listdir(logd)):
            m = re.match(r"^(apply|reorg-apply|place|undo|purge)-(\d{8}-\d{6})-run\.txt$", n)
            if m:
                try:
                    if dt.datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").timestamp() > pm + 1:
                        changed.append(n)
                except ValueError:
                    pass
    # the files are the truth: a planned removal whose file is no longer in the library has already happened
    gone, checked = 0, 0
    for g in groups:
        rem = [m for m in g["members"] if m["role"] == "remove"]
        if rem and checked < 6000:
            checked += len(rem)
            if all(not os.path.exists(os.path.join(MUSIC, m["path"])) for m in rem):
                gone += 1
    return {"exists": True, "version": plan.get("version"), "generated": plan.get("generated"), "file_mtime": mtime_iso(out("plan.json")), "age_days": age_days(out("plan.json")),
            "tolerance": plan.get("tolerance"), "wide_tolerance": plan.get("wide_tolerance"), "navidrome_checked": plan.get("navidrome_checked"),
            "groups": len(groups), "kinds": kinds, "status": status, "removable_bytes": reclaim, "removable_h": human(reclaim),
            "stale_by": changed, "gone": gone, "removable_open": max(0, status.get("removable", 0) - gone)}


def api_groups(q):
    plan = load_plan()
    if plan is None:
        return {"exists": False, "items": [], "total": 0, "page": 1, "pages": 1, "size": 40}
    kind = q.get("kind", [""])[0]
    status = q.get("status", [""])[0]
    text = q.get("q", [""])[0].strip().lower()
    flag = q.get("flag", [""])[0]
    sort = q.get("sort", ["artist"])[0]
    rows = []
    for g in plan["groups"]:
        if kind and g["kind"] != kind:
            continue
        s = group_summary(g)
        if status and s["status"] != status:
            continue
        if flag == "plays" and not s["plays"]:
            continue
        if flag == "starred" and not s["starred"]:
            continue
        if flag == "playlists" and not s["playlists"]:
            continue
        if flag == "label" and not s["label"]:
            continue
        if text:
            hay = (g["artist"] + " " + g["title"] + " " + " ".join(m["path"] for m in g["members"])).lower()
            if text not in hay:
                continue
        rows.append(s)
    if sort == "size":
        rows.sort(key=lambda r: (-r["reclaim"], r["artist"].lower(), r["title"].lower()))
    elif sort == "title":
        rows.sort(key=lambda r: (r["title"].lower(), r["artist"].lower()))
    elif sort == "plays":
        rows.sort(key=lambda r: (-r["plays"], r["artist"].lower()))
    else:
        rows.sort(key=lambda r: (r["artist"].lower(), r["title"].lower()))
    res = paginate(rows, q.get("page", ["1"])[0], q.get("size", ["40"])[0])
    res["exists"] = True
    by_id = dict((g["id"], g) for g in plan["groups"])
    for r in res["items"]:
        rem = [m for m in by_id[r["id"]]["members"] if m["role"] == "remove"]
        r["gone"] = bool(rem) and all(not os.path.exists(os.path.join(MUSIC, m["path"])) for m in rem)
    return res


def api_group(gid):
    plan = load_plan()
    if plan is None:
        return None
    for g in plan["groups"]:
        if g["id"] == gid:
            g = json.loads(json.dumps(g))
            for m in g["members"]:
                full = resolve_under(MUSIC, m["path"])
                m["exists"] = bool(full and os.path.isfile(full))
                m["size_h"] = human(m.get("size"))
            g["status"] = group_status(g)
            return g
    return None


# ---------------------------------------------------------------------------------------------------------------------
# data: reorg plan, place plan, tag audit
# ---------------------------------------------------------------------------------------------------------------------
LABEL_RX = re.compile(r"[\(\[]\s*(explicit|clean|censored)\b[^\)\]]*[\)\]]", re.I)
NUM_SUFFIX_RX = re.compile(r"\s\(\d+\)$")
YEAR_RX = re.compile(r"\s*\((\d{4})\)$")


def explain_change(it):
    """plain-language reason for a planned move or rename. Plans from before this version carry a reason only when the
    name was suffixed, so the rest is worked out here from the two paths."""
    if it.get("reason"):
        return it["reason"]
    a, b = it.get("path") or "", it.get("to") or ""
    if not b or a == b:
        return ""
    pa, pb = a.split("/"), b.split("/")
    bits = []
    if len(pa) >= 3 and len(pb) >= 3:
        if pa[0] != pb[0]:
            t = "artist folder '%s' becomes '%s' (it follows the artist tag" % (pa[0], pb[0])
            if "_" in pb[0] and "_" not in pa[0]:
                t += "; a character a folder name cannot hold, like *, is written as _"
            bits.append(t + ")")
        if pa[1] != pb[1]:
            ya, yb = YEAR_RX.search(pa[1]), YEAR_RX.search(pb[1])
            if YEAR_RX.sub("", pa[1]) == YEAR_RX.sub("", pb[1]):
                if ya and not yb:
                    bits.append("the year (%s) is dropped from the album folder because the tags have no year" % ya.group(1))
                elif yb and not ya:
                    bits.append("the year (%s) is added to the album folder" % yb.group(1))
                else:
                    bits.append("the album folder year changes to match the tags")
            else:
                bits.append("album folder '%s' becomes '%s' (it follows the album tag)" % (pa[1], pb[1]))
    if pa[-1] != pb[-1]:
        fa, ea = posixpath.splitext(pa[-1])
        fb, eb = posixpath.splitext(pb[-1])
        ma = LABEL_RX.search(fa)
        if ma and not LABEL_RX.search(fb) and LABEL_RX.sub("", fa).strip() == fb.strip():
            bits.append("the (%s) label is dropped from the file name: the title tag has no label and no other copy needs the plain name any more"
                        % ma.group(1).capitalize())
        elif NUM_SUFFIX_RX.sub("", fa) == fb:
            bits.append("the numbered suffix is dropped because the other copy that used this name is gone")
        elif re.sub(r"^\d+\s*-?\s*", "", fa) == re.sub(r"^\d+\s*-?\s*", "", fb):
            bits.append("the track number is written as NN - Title")
        elif ea.lower() != eb.lower() and fa == fb:
            bits.append("the extension follows the real file type")
        else:
            bits.append("the file name is rebuilt from the tags (NN - Title)")
    note = {"single": "no album tag, so it is filed as a single",
            "names": "tags are missing, so the names come from the file and folder names (fix the tags, then re-plan)",
            "rule": "set by a reorg-set line in rules.txt"}.get(it.get("src"))
    if note:
        bits.append(note)
    txt = "; ".join(bits)
    return (txt[:1].upper() + txt[1:]) if txt else "path rebuilt from the tags"


def change_state(it):
    """what is true on disk right now for one planned change"""
    a = os.path.exists(os.path.join(MUSIC, it["path"]))
    b = os.path.exists(os.path.join(MUSIC, it["to"])) if it.get("to") else False
    if a and not b:
        return "pending"        # still where the plan found it
    if b and not a:
        return "done"           # already at the planned place
    if a and b:
        return "both"           # something else sits at the target
    return "gone"


_rs_cache = {"k": None, "t": 0.0, "v": None}


def reorg_states(plan):
    try:
        k = os.stat(out("reorg-plan.json")).st_mtime_ns
    except OSError:
        return {}, {}
    now = time.time()
    if _rs_cache["k"] == k and now - _rs_cache["t"] < 20 and _rs_cache["v"]:
        return _rs_cache["v"]
    states, counts, n = {}, {}, 0
    for it in plan["items"]:
        if it["action"] in ("move", "rename"):
            n += 1
            if n > 20000:
                break
            st = change_state(it)
            states[it["path"]] = st
            counts[st] = counts.get(st, 0) + 1
    _rs_cache.update({"k": k, "t": now, "v": (states, counts)})
    return states, counts


def api_reorg(q):
    plan = load_json(out("reorg-plan.json"))
    if plan is None:
        return {"exists": False}
    action = q.get("action", [""])[0]
    want = q.get("state", [""])[0]
    text = q.get("q", [""])[0].strip().lower()
    states, scounts = reorg_states(plan)
    counts = {}
    for it in plan["items"]:
        counts[it["action"]] = counts.get(it["action"], 0) + 1
    rows = []
    for it in plan["items"]:
        if action and it["action"] != action:
            continue
        if not action and it["action"] == "ok" and not text:
            continue                      # default view: only what would change
        st = states.get(it["path"], "")
        if want and st != want:
            continue
        reason = explain_change(it) if it["action"] in ("move", "rename") else it.get("reason", "")
        if text and text not in (it["path"] + " " + (it.get("to") or "") + " " + reason).lower():
            continue
        rows.append({"path": it["path"], "to": it.get("to"), "action": it["action"], "reason": reason, "state": st,
                     "size": it.get("size"), "artist": it.get("artist"), "album": it.get("album"), "title": it.get("title")})
    res = paginate(rows, q.get("page", ["1"])[0], q.get("size", ["50"])[0])
    res.update({"exists": True, "generated": plan.get("generated"), "version": plan.get("version"), "summary": plan.get("summary", {}),
                "counts": counts, "state_counts": scounts, "file_mtime": mtime_iso(out("reorg-plan.json"))})
    return res


def list_incoming():
    """folders in the landing area with their audio file counts (live, read-only)"""
    res = []
    if not os.path.isdir(INCOMING):
        return {"exists": False, "folders": [], "files": 0}
    total = 0
    for dp, dn, fn in os.walk(INCOMING):
        dn[:] = [d for d in dn if d.lower() != "@eadir"]
        n = sum(1 for f in fn if f.lower().endswith(AUDIO_EXT))
        if n:
            rel = os.path.relpath(dp, INCOMING).replace(os.sep, "/")
            res.append({"folder": rel, "files": n})
            total += n
    res.sort(key=lambda r: r["folder"].lower())
    return {"exists": True, "folders": res, "files": total}


def api_place(q):
    rows = load_csv(out("place-plan.csv"))
    pending = load_json(out("place-pending.json"))
    inc = list_incoming()
    mp = out("incoming-map.tsv")
    mapn = 0
    try:
        with open(mp, encoding="utf-8") as fh:
            mapn = sum(1 for _ in fh)
    except OSError:
        pass
    info = {"incoming": inc, "map_lines": mapn, "pending": None, "exists": rows is not None, "plan_mtime": mtime_iso(out("place-plan.csv"))}
    if pending:
        info["pending"] = {"playlists": [p.get("name") for p in (pending.get("playlists") or [])]}
    if rows is None:
        return info
    status = q.get("status", [""])[0]
    pl = q.get("playlist", [""])[0]
    text = q.get("q", [""])[0].strip().lower()
    by_status, by_pl = {}, {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        d = by_pl.setdefault(r["playlist"], {"playlist": r["playlist"], "place": 0, "duplicate": 0, "held": 0, "upgrade": 0})
        d[r["status"]] = d.get(r["status"], 0) + 1
        if "upgrade:" in (r.get("note") or ""):
            d["upgrade"] += 1
    sel = []
    for r in rows:
        if status == "upgrade":
            if "upgrade:" not in (r.get("note") or ""):
                continue
        elif status and r["status"] != status:
            continue
        if pl and r["playlist"] != pl:
            continue
        if text and text not in (r["incoming_file"] + " " + r["library_path"] + " " + (r.get("note") or "")).lower():
            continue
        sel.append({"playlist": r["playlist"], "incoming": r["incoming_file"], "status": r["status"], "library": r["library_path"], "note": r.get("note", "")})
    res = paginate(sel, q.get("page", ["1"])[0], q.get("size", ["50"])[0])
    info.update(res)
    info["by_status"] = by_status
    info["upgrades"] = sum(d["upgrade"] for d in by_pl.values())
    info["playlists"] = sorted(by_pl.values(), key=lambda d: d["playlist"].lower())
    return info


def tag_rows():
    return load_csv(out("tagaudit.csv"))


def api_tagaudit(q):
    rows = tag_rows()
    if rows is None:
        return {"exists": False}
    lst = q.get("list", [""])[0]
    text = q.get("q", [""])[0].strip().lower()
    cover, lyr, miss = {}, {}, {}
    core_missing = no_lyrics = no_cover = errors = 0
    by_artist = {}
    fmts = {}
    CORE = ("title", "artist", "album", "albumartist", "track", "year")
    for r in rows:
        cover[r["cover_art"]] = cover.get(r["cover_art"], 0) + 1
        lyr[r["lyrics"]] = lyr.get(r["lyrics"], 0) + 1
        fmts[r["format"]] = fmts.get(r["format"], 0) + 1
        tags = (r.get("missing_tags") or "").split()
        for t in tags:
            miss[t] = miss.get(t, 0) + 1
        if any(t in CORE for t in tags):
            core_missing += 1
        if r["lyrics"] == "none":
            no_lyrics += 1
            a = r["path"].split("/")[0]
            d = by_artist.setdefault(a, [0, 0])
            d[1] += 1
        a = r["path"].split("/")[0]
        by_artist.setdefault(a, [0, 0])[0] += 1
        if r["cover_art"] == "none":
            no_cover += 1
        if r.get("error"):
            errors += 1
    info = {"exists": True, "files": len(rows), "cover": cover, "lyrics": lyr, "missing_tags": miss, "formats": fmts, "core_missing": core_missing,
            "no_lyrics": no_lyrics, "no_cover": no_cover, "errors": errors, "mtime": mtime_iso(out("tagaudit.csv")),
            "age_days": age_days(out("tagaudit.csv"))}
    info["artists_no_lyrics"] = sorted(
        [{"artist": a, "tracks": v[0], "no_lyrics": v[1], "share": round(100.0 * v[1] / v[0], 1)} for a, v in by_artist.items() if v[1]],
        key=lambda d: (-d["no_lyrics"], d["artist"].lower()))[:100]
    if lst:
        def pick(r):
            tags = (r.get("missing_tags") or "").split()
            if lst == "core":
                return any(t in CORE for t in tags)
            if lst == "nocover":
                return r["cover_art"] == "none"
            if lst == "folderonly":
                return r["cover_art"] == "folder"
            if lst == "embeddedonly":
                return r["cover_art"] == "embedded"
            if lst == "nolyrics":
                return r["lyrics"] == "none"
            if lst == "nogenre":
                return "genre" in tags
            if lst == "errors":
                return bool(r.get("error"))
            return False
        sel = []
        for r in rows:
            if not pick(r):
                continue
            if text and text not in r["path"].lower():
                continue
            sel.append({"path": r["path"], "format": r["format"], "missing": r.get("missing_tags", ""), "cover": r["cover_art"],
                        "folder_art": r.get("folder_art_file", ""), "lyrics": r["lyrics"], "error": r.get("error", "")})
        info.update(paginate(sel, q.get("page", ["1"])[0], q.get("size", ["50"])[0]))
        info["list"] = lst
    return info


# ---------------------------------------------------------------------------------------------------------------------
# data: quarantine / reorg runs, rules, logs, search
# ---------------------------------------------------------------------------------------------------------------------
def list_runs():
    runs = []
    for root, kind_default in ((QUAR, "apply"), (REORG_ROOT, "reorg")):
        if not os.path.isdir(root):
            continue
        for n in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, n)
            mf = os.path.join(d, "manifest.json")
            if not os.path.isdir(d):
                continue
            has = os.path.isfile(mf)
            if not has and kind_default != "apply":
                # a reorg run that was stopped has only its journal
                if not any(f.endswith(".jsonl") for f in os.listdir(d)):
                    continue
            man = load_json(mf) if has else None
            kind = (man or {}).get("kind") or ("place" if n.startswith("place-") else kind_default)
            files = len((man or {}).get("files", [])) if man else None
            runs.append({"run": n, "root": os.path.basename(root), "kind": kind, "manifest": has, "files": files,
                         "when": mtime_iso(d), "age_days": age_days(d), "container_manifest": mf,
                         "playlists": len((man or {}).get("playlists", [])) if man else None})
    runs.sort(key=lambda r: r["when"] or "", reverse=True)
    return runs


_size_cache = {}


def run_size(rd):
    """total bytes under a run folder (cached for a minute)"""
    now = time.time()
    c = _size_cache.get(rd)
    if c and now - c[0] < 60:
        return c[1]
    total = 0
    for dp, dn, fn in os.walk(rd):
        for f in fn:
            try:
                total += os.stat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    _size_cache[rd] = (now, total)
    return total


def api_quarantine(q):
    runs = list_runs()
    return {"runs": runs, "quarantine": QUAR, "reorg_root": REORG_ROOT, "exists": os.path.isdir(QUAR) or os.path.isdir(REORG_ROOT)}


def api_run_size(root, run):
    base = QUAR if root == os.path.basename(QUAR) else REORG_ROOT if root == os.path.basename(REORG_ROOT) else None
    if base is None or safe_rel(run) is None or "/" in run or not os.path.isdir(os.path.join(base, run)):
        return None
    b = run_size(os.path.join(base, run))
    return {"bytes": b, "size_h": human(b), "reorg": base == REORG_ROOT}


def api_run(run, root, q):
    base = QUAR if root == os.path.basename(QUAR) else REORG_ROOT if root == os.path.basename(REORG_ROOT) else None
    if base is None or safe_rel(run) is None or "/" in run:
        return None
    mf = os.path.join(base, run, "manifest.json")
    man = load_json(mf)
    if man is None:
        return None
    kind = man.get("kind") or ("place" if run.startswith("place-") else "apply")
    text = q.get("q", [""])[0].strip().lower()
    files = []
    for f in man.get("files", []):
        if kind == "apply":
            rec = {"label": f.get("path"), "to": f.get("winner"), "size": f.get("size"), "play": "%s/%s" % (run, f.get("path")) if base == QUAR else None}
        elif kind == "place":
            rec = {"label": f.get("from"), "to": None, "size": None, "play": "%s/%s" % (run, f.get("to")) if base == QUAR else None}
        else:
            rec = {"label": f.get("from"), "to": f.get("to"), "size": None, "play": None, "k": f.get("kind")}
        if text and text not in ("%s %s" % (rec["label"], rec["to"])).lower():
            continue
        files.append(rec)
    res = paginate(files, q.get("page", ["1"])[0], q.get("size", ["50"])[0])
    res.update({"run": run, "kind": kind, "root": root, "manifest": mf, "playlists": man.get("playlists", []), "dirs": len(man.get("dirs", [])),
                "all_files": len(man.get("files", []))})
    return res


RULE_HELP = {
    "prefer-edit": "For one song, which version survives when several edits exist",
    "prefer-order": "Default order when a song has several edits",
    "keep-both": "Never touch this song; every copy stays",
    "keep-both-id": "Same, using the group id from the report",
    "keep-file": "This exact file always wins its group",
    "protect-artist": "Leave this artist's duplicates alone",
    "protect-path": "Files under this folder are never removed (shown as BLOCKED)",
    "skip-path": "Do not scan this folder",
    "tolerance": "Seconds apart that still count as the same recording",
    "wide-tolerance": "Wider gap shown as a WIDE-GAP group that needs approval",
    "clean-explicit": "keep-both (default) or dedupe when one copy is labeled Explicit/Clean",
    "dedupe-labels": "Dedupe the label differences for just this song",
    "audio-check": "on (default) or off: compare the decoded audio before removing a pair",
    "keep-played": "Keep the copy that has Navidrome plays",
    "keep-album": "The artist's own album beats a compilation copy",
    "place-upgrade": "A higher-quality incoming copy (FLAC over MP3) is added next to the library copy",
    "reorg-skip": "Leave this folder where it is",
    "reorg-various": "This album belongs under Various Artists",
    "reorg-unknown-album": "Songs with no album tag go to Artist/<name>/",
    "reorg-artist": "Always use this folder spelling for an artist",
}


def api_rules():
    try:
        with open(RULES, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError:
        return {"exists": False, "text": "", "rules": []}
    rules = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(#\s*)?([a-z][a-z-]*):\s*(.*)$", s)
        if m and m.group(2) in RULE_HELP:
            rules.append({"line": i, "key": m.group(2), "value": m.group(3), "active": not m.group(1), "help": RULE_HELP[m.group(2)]})
    return {"exists": True, "text": text, "rules": rules, "mtime": mtime_iso(RULES), "path": RULES}


def api_logs():
    """every log in one list: the tool's own run logs (out/logs, written by apply / place / reorg-apply / playlistfix /
    beets-remap / undo / purge, dry runs included) and the jobs started from this console (out/web-jobs)"""
    items = []
    d = out("logs")
    tool = 0
    logged = {}
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), reverse=True)[:400]:
            if n.endswith(".txt"):
                p = os.path.join(d, n)
                m = re.match(r"^([a-z-]+)-(\d{8}-\d{6})-(run|dry)\.txt$", n)
                tool += 1
                if m:
                    try:
                        logged.setdefault(m.group(1), []).append(dt.datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").timestamp())
                    except ValueError:
                        pass
                items.append({"id": n, "source": "tool", "cmd": m.group(1) if m else "", "real": bool(m and m.group(3) == "run"),
                              "when": mtime_iso(p), "size": os.stat(p).st_size, "title": n, "state": ""})
    web = 0
    for j in JOBS.list(300):
        try:                                     # a console job the tool also logged itself is listed once, as the run log
            t0 = dt.datetime.strptime(j.get("started") or "", "%Y-%m-%d %H:%M:%S").timestamp()
            if any(abs(t0 - t) <= 15 for t in logged.get(j.get("cmd"), [])):
                continue
        except ValueError:
            pass
        web += 1
        try:
            size = os.stat(JOBS.log_path(j["id"])).st_size
        except OSError:
            size = 0
        items.append({"id": j["id"], "source": "console", "cmd": j.get("cmd", ""), "real": False, "when": j.get("started"), "size": size,
                      "title": j.get("title", j["id"]), "state": j.get("state", ""), "args": " ".join(j.get("args") or [])})
    items.sort(key=lambda x: x["when"] or "", reverse=True)
    return {"items": items, "dir": d, "dir_exists": os.path.isdir(d), "tool_count": tool, "console_count": web}


def nd_endpoint():
    """host and port of the Navidrome URL in navidrome.cred (never the user name or password)"""
    try:
        with open(CRED, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line.lower().startswith("url") and "=" in line:
                    u = urlparse(line.split("=", 1)[1].strip().strip('"').strip("'"))
                    if u.hostname:
                        return u.hostname, u.port or (443 if u.scheme == "https" else 80), "%s://%s:%s" % (u.scheme or "http", u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError:
        pass
    return None, None, None


def cred_keys():
    """which settings navidrome.cred holds (url / user / password): only the names, never a value"""
    keys = set()
    try:
        with open(CRED, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if v.strip().strip('"').strip("'"):
                        keys.add(k.strip().lower())
    except OSError:
        pass
    return keys


def cred_has_password():
    return "password" in cred_keys()


def api_security(req):
    """how exposed the console is, in plain words, with the exact fix for each weak spot (req = the request handler)"""
    rows = []

    def add(name, level, detail, fix=""):
        rows.append({"name": name, "level": level, "detail": detail, "fix": fix})

    login = LOGIN_ON
    gs = GUARD.stats()
    if login:
        add("Console login", "ok", "required: %s from %s" % ("a user name and password" if len(USERS) == 1 else "%d separate accounts (%s)" % (len(USERS), ", ".join(u for u, _ in USERS)), CFG_FILE))
    else:
        add("Console login", "bad", "none: anyone who can reach this port can open the console",
            "Set MD_WEB_USER=... and MD_WEB_PASS=... (a long random password) in %s, then %s" % (CFG_FILE, APPLY_HINT))
    if login:
        if gs["locked_for"]:
            add("Wrong logins", "bad", "login is locked for %d more seconds after repeated wrong passwords" % gs["locked_for"])
        elif gs["last_hour"]:
            add("Wrong logins", "warn", "%d wrong login(s) in the last hour, %d in the last day (details: bash dewey.sh web logs)" % (gs["last_hour"], gs["last_day"]))
        else:
            add("Wrong logins", "ok", "none in the last day. After 5 different wrong passwords in 10 minutes the login locks for a minute, then longer.")
    b = BIND_INFO
    if b in ("127.0.0.1", "localhost", "::1"):
        add("Who can reach the port", "ok", "the NAS itself only (127.0.0.1). Other devices get in through Tailscale Serve, which adds HTTPS.")
    elif b:
        add("Who can reach the port", "warn", "published on %s only" % b, "Use MD_WEB_BIND=127.0.0.1 and reach the console through Tailscale Serve.")
    else:
        add("Who can reach the port", "warn", "every device on your network can open the plain-http port (and read anything this console shows)",
            "Once the https address works, add  MD_WEB_BIND=127.0.0.1  to %s and %s . Plain http then stops answering." % (CFG_FILE, APPLY_HINT))
    names = req._names()
    seen = names[-1] if names else ""
    if ALLOWED_HOSTS:
        add("Host names", "ok", "answers only to: " + ", ".join(sorted(ALLOWED_HOSTS)) + " (and localhost)")
    else:
        add("Host names", "info", "answers to any host name. Limiting it blocks tricks where a web page makes your browser talk to the NAS under another name.",
            ("Add  MD_WEB_HOSTS=%s  to %s (comma separated for several)." % (seen.rsplit(":", 1)[0], CFG_FILE)) if seen else "")
    if cred_has_password():
        add("Navidrome password", "info", "stored in navidrome.cred (never crosses the network, but sits in that file)")
    else:
        add("Navidrome password", "ok", "not stored. The Fix playlists card asks for it each run and only accepts it from an https page.")
    if WIDGET_TOKEN:
        add("Dashboard widget", "info", "a token (MD_WIDGET_TOKEN) lets a dashboard such as Homepage read /api/widget: a dozen counts, no names or paths, nothing else. "
            "It works from any network the container is on, so keep that network private to the NAS (see MD_WEB_NETWORK).")
    else:
        add("Dashboard widget", "ok", "off: no MD_WIDGET_TOKEN is set, so no token can read anything")
    rs = runner_state()
    if not rs:
        add("Write access (runner)", "ok", "off: the runner container is not running, so nothing can change the library from the browser. Real runs use dewey.ps1.",
            "To allow a few real runs from this console: " + RUNNER_HINT)
    else:
        left = max(0, int(rs.get("armed_until", 0) - time.time() + 0.999))
        cmds = ", ".join(rs.get("commands") or []) or "none"
        if left:
            add("Write access (runner)", "warn", "ARMED for %d more minutes: a real run of (%s) can be started from this console. Disarm it on the Run page when you are done." % (-(-left // 60), cmds))
        else:
            add("Write access (runner)", "ok", "the runner is up but NOT armed. Starting a real run needs a 6-digit code from your authenticator app (checked only inside the runner, "
                "so the console login alone cannot enable it), a finished dry run of the same job, and a typed confirmation. Real runs allowed: %s." % cmds)
    add("Docker access", "ok" if not os.path.exists("/var/run/docker.sock") else "bad",
        "this console has no access to Docker: it cannot start anything outside its own fixed list of read-only and dry-run jobs"
        if not os.path.exists("/var/run/docker.sock") else "the Docker socket is mounted into this container: that is root access to the NAS",
        "" if not os.path.exists("/var/run/docker.sock") else "Remove the docker.sock mount from the console's docker run.")
    if os.path.isdir(MUSIC):
        ro = is_readonly_mount(MUSIC)
        add("Library", "ok" if ro else "bad" if ro is False else "warn",
            "mounted read-only: the console cannot change a music file" if ro else "WRITABLE from this console" if ro is False else "could not tell whether it is read-only")
    proxied = bool(req.headers.get("X-Forwarded-Proto") or req.headers.get("Tailscale-User-Login") or req.headers.get("X-Forwarded-Host"))
    return {"rows": rows, "request": {"host": seen, "proxied": proxied, "forwarded_https": (req.headers.get("X-Forwarded-Proto") or "").lower() == "https"}}


def api_diagnostics():
    """what this container can see, so a missing log or run is explained on screen instead of guessed at"""
    checks = []

    def add(group, name, level, detail, path=""):
        checks.append({"group": group, "name": name, "level": level, "detail": detail, "path": path})

    # library + out
    if os.path.isdir(MUSIC):
        ro = is_readonly_mount(MUSIC)
        add("Folders", "Music library", "ok" if ro else "bad" if ro is False else "warn",
            "mounted read-only" if ro else "WRITABLE: the console must see the library read-only" if ro is False else "could not tell whether it is read-only", MUSIC)
    else:
        add("Folders", "Music library", "bad", "folder not found", MUSIC)
    if os.path.isdir(OUT):
        try:
            du = shutil.disk_usage(OUT)
            free = "%s free" % human(du.free)
        except OSError:
            free = ""
        add("Folders", "Output folder (plans, reports, settings)", "ok" if os.access(OUT, os.W_OK) else "bad",
            ("writable, " if os.access(OUT, os.W_OK) else "NOT writable, ") + free, OUT)
    else:
        add("Folders", "Output folder (plans, reports, settings)", "bad", "folder not found", OUT)
    d = out("logs")
    if os.path.isdir(d):
        try:
            names = os.listdir(d)
        except OSError as e:
            names = None
            add("Folders", "Run logs (out/logs)", "bad", "this container cannot read the folder (%s)" % (e.strerror or e), d)
        if names is not None:
            txt = sorted(f for f in names if f.endswith(".txt"))
            if txt:
                add("Folders", "Run logs (out/logs)", "ok", "%d log file(s), newest %s" % (len(txt), txt[-1]), d)
            elif names:
                add("Folders", "Run logs (out/logs)", "warn", "no .txt logs, but %d other entries: %s" % (len(names), ", ".join(sorted(names)[:6])), d)
            else:
                add("Folders", "Run logs (out/logs)", "warn",
                    "the folder is empty. Every real or dry run of apply, place, reorg-apply, playlistfix, beets-remap, undo or purge writes a log here. "
                    "(The out\\logs folder on the PC is only a copy of each run's newest log, so it can hold files this one does not.)", d)
    else:
        add("Folders", "Run logs (out/logs)", "info", "the folder does not exist yet: the first run that writes a log creates it", d)
    for label, p in (("Quarantine (duplicates and place runs)", QUAR), ("Reorg runs", REORG_ROOT)):
        if os.path.isdir(p):
            names = sorted(os.listdir(p), reverse=True)
            names = [n for n in names if os.path.isdir(os.path.join(p, n))]
            add("Folders", label, "ok" if names else "info", ("%d run folder(s): " % len(names) + ", ".join(names[:8]) + (" ..." if len(names) > 8 else "")) if names else "no run folders", p)
        else:
            add("Folders", label, "info", "folder does not exist (nothing has been set aside yet)", p)
    inc = list_incoming()
    add("Folders", "Landing folder (_incoming)", "ok" if inc["exists"] else "info", ("%d audio file(s) waiting" % inc["files"]) if inc["exists"] else "folder does not exist", INCOMING)
    # databases + Navidrome
    if os.path.isfile(NDDB):
        db_sz = os.stat(NDDB).st_size
        wal_sz = os.stat(NDDB + "-wal").st_size if os.path.isfile(NDDB + "-wal") else 0
        big = db_sz + wal_sz > 1024 ** 3
        tip = ": at this size the copy is most of their run time." if big else "."
        if wal_sz > 256 * 1024 ** 2:
            tip += " The write-ahead log alone is %s, which usually means Navidrome has not folded it back into the database; restarting the Navidrome container normally does." % human(wal_sz)
        add("Connections", "Navidrome database (read-only copy)", "warn" if big else "ok",
            "database %s + write-ahead log %s. Every job that reads Navidrome (scan, Fix playlists) copies both first%s" % (human(db_sz), human(wal_sz), tip), NDDB)
    else:
        add("Connections", "Navidrome database (read-only copy)", "warn", "not mounted: plays, stars and playlists are not available to scans", NDDB)
    add("Connections", "Beets library.db", "ok" if os.path.isfile(BEETSDB) else "warn",
        human(os.stat(BEETSDB).st_size) if os.path.isfile(BEETSDB) else "not mounted: the Beets remap dry run is unavailable", BEETSDB)
    if os.path.isfile(CRED):
        host, port, url = nd_endpoint()
        if "user" not in cred_keys():
            add("Connections", "Navidrome login file", "warn", "navidrome.cred has no user= line: add  user=<your Navidrome user name>", CRED)
        if not host:
            add("Connections", "Navidrome login file", "warn", "navidrome.cred has no url= line", CRED)
        else:
            try:
                socket.create_connection((host, port), timeout=3).close()
                add("Connections", "Can this container reach Navidrome?", "ok", "yes: %s answers (the playlist-fix dry run can work)" % url)
                add("Connections", "Navidrome password", "ok" if cred_has_password() else "info",
                    "stored in navidrome.cred: playlist jobs log in on their own" if cred_has_password()
                    else "not stored (as intended): the Fix playlists card asks for it each run and never saves it")
            except OSError as e:
                add("Connections", "Can this container reach Navidrome?", "bad",
                    "no connection to %s (%s). If Navidrome runs on the same NAS, the container may need host networking: add  --network host  to the web console's docker run in dewey.sh." % (url, e.strerror or e))
    else:
        add("Connections", "Navidrome login file", "warn", "navidrome.cred is not mounted: the playlist-fix dry run is unavailable", CRED)
    # rules, login, phase 2
    if os.path.isfile(RULES):
        add("Console", "rules.txt", "ok", "%s, last changed %s" % (human(os.stat(RULES).st_size), mtime_iso(RULES)), RULES)
    else:
        add("Console", "rules.txt", "bad", "not found", RULES)
    add("Console", "Login", "ok" if LOGIN_ON else "warn",
        ("required (%d account%s)" % (len(USERS), "" if len(USERS) == 1 else "s")) if LOGIN_ON else "none: anyone on the LAN can open this console (it can only read). Set MD_WEB_USER and MD_WEB_PASS in %s to require one." % CFG_FILE)
    add("Console", "Docker socket", "bad" if os.path.exists("/var/run/docker.sock") else "info",
        "mounted: remove it, this console never needs it" if os.path.exists("/var/run/docker.sock") else "not mounted (good: this is what keeps the console unable to start anything of its own)")
    add("Console", "Versions", "info", "tool %s, web console %s, Python %s, started %s" % (VERSION, WEB_VERSION, sys.version.split()[0], STARTED))
    return {"checks": checks}


def read_tail(path, limit=400000):
    size = os.stat(path).st_size
    with open(path, "rb") as fh:
        if size > limit:
            fh.seek(size - limit)
        data = fh.read()
    t = data.decode("utf-8-sig", "replace")
    if size > limit:
        t = "... (earlier output cut, %s total)\n" % human(size) + t
    return t


_index = {"t": 0, "paths": []}
_index_lock = threading.Lock()


def library_paths():
    """sorted list of audio paths relative to the library root (for the compare picker); cached for 5 minutes"""
    with _index_lock:
        if time.time() - _index["t"] < 300 and _index["paths"]:
            return _index["paths"]
    paths = None
    rows = tag_rows()
    if rows:
        paths = [r["path"] for r in rows]
    if not paths:
        plan = load_json(out("reorg-plan.json"))
        if plan:
            paths = [it["path"] for it in plan["items"]]
    if not paths:
        paths = []
        for dp, dn, fn in os.walk(MUSIC):
            dn[:] = [d for d in dn if d.lower() not in ("@eadir", "playlists", "_dedupe-quarantine")]
            for f in fn:
                if f.lower().endswith(AUDIO_EXT):
                    paths.append(os.path.relpath(os.path.join(dp, f), MUSIC).replace(os.sep, "/"))
    paths.sort(key=str.lower)
    with _index_lock:
        _index["t"], _index["paths"] = time.time(), paths
    return paths


def api_search(q):
    text = q.get("q", [""])[0].strip().lower()
    if len(text) < 2:
        return {"items": []}
    words = text.split()
    res = []
    for p in library_paths():
        lp = p.lower()
        if all(w in lp for w in words):
            res.append(p)
            if len(res) >= 25:
                break
    return {"items": res}


# ---------------------------------------------------------------------------------------------------------------------
# status / dashboard
# ---------------------------------------------------------------------------------------------------------------------
def api_status():
    inv = load_json(out("inventory.json"))
    plan = api_plan_info()
    reorg = load_json(out("reorg-plan.json"))
    ta = tag_rows()
    runs = list_runs()
    inc = list_incoming()
    cur = JOBS.current_info()
    s = {
        "version": VERSION, "web_version": WEB_VERSION, "install": "compose" if COMPOSE_INSTALL else "launcher", "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "library": {"path": MUSIC, "exists": os.path.isdir(MUSIC), "readonly_mount": is_readonly_mount(MUSIC) if os.path.isdir(MUSIC) else None},
        "out_writable": os.access(OUT, os.W_OK), "write_enabled": False, "runner": runner_brief(),
        "navidrome_db": os.path.isfile(NDDB), "beets_db": os.path.isfile(BEETSDB), "cred": os.path.isfile(CRED),
        "inventory": inv, "inventory_mtime": mtime_iso(out("inventory.json")), "inventory_age": age_days(out("inventory.json")),
        "plan": plan,
        "reorg": None, "tagaudit": None,
        "incoming": {"files": inc["files"], "folders": len(inc["folders"]), "exists": inc["exists"]},
        "place_pending": os.path.isfile(out("place-pending.json")),
        "place_plan": mtime_iso(out("place-plan.csv")),
        "quarantine": {"runs": len(runs), "oldest_days": max([r["age_days"] or 0 for r in runs], default=0),
                       "place_or_apply": len([r for r in runs if r["kind"] in ("apply", "place")]), "reorg": len([r for r in runs if r["kind"] == "reorg"])},
        "job": cur,
        "recent_jobs": JOBS.list(8),
        "settings": load_settings(),
    }
    if reorg:
        _st, sc = reorg_states(reorg)
        s["reorg"] = {"summary": reorg.get("summary", {}), "generated": reorg.get("generated"), "age_days": age_days(out("reorg-plan.json")),
                      "pending": sc.get("pending", 0), "done": sc.get("done", 0), "other": sc.get("both", 0) + sc.get("gone", 0)}
    if ta is not None:
        miss_core = sum(1 for r in ta if any(t in ("title", "artist", "album", "albumartist", "track", "year") for t in (r.get("missing_tags") or "").split()))
        s["tagaudit"] = {"files": len(ta), "core_missing": miss_core, "no_cover": sum(1 for r in ta if r["cover_art"] == "none"),
                         "no_lyrics": sum(1 for r in ta if r["lyrics"] == "none"), "age_days": age_days(out("tagaudit.csv")), "mtime": mtime_iso(out("tagaudit.csv"))}
    return s


_WIDGET = {"t": 0.0, "v": None}
_WIDGET_LOCK = threading.Lock()


def api_widget():
    """a few flat numbers for a dashboard tile (Homepage customapi). Counts only: no paths, names or file contents.
    Cached for 60 s because the plan check looks at the disk."""
    with _WIDGET_LOCK:
        if _WIDGET["v"] is not None and time.time() - _WIDGET["t"] < 60:
            return _WIDGET["v"]
        s = api_status()
        inv, p, ta, ro_, q = s.get("inventory") or {}, s.get("plan") or {}, s.get("tagaudit") or {}, s.get("reorg") or {}, s.get("quarantine") or {}
        cur = s.get("job")
        v = {
            "files": inv.get("files"),
            "bytes": inv.get("bytes"),
            "duplicate_groups_open": p.get("removable_open") if p.get("exists", True) and p else None,
            "space_to_free_bytes": p.get("removable_bytes") if p else None,
            "scan_age_days": p.get("age_days") if p else None,
            "waiting_in_incoming": (s.get("incoming") or {}).get("files"),
            "layout_changes_pending": ro_.get("pending") if ro_ else None,
            "tags_missing": ta.get("core_missing") if ta else None,
            "no_cover_art": ta.get("no_cover") if ta else None,
            "no_lyrics": ta.get("no_lyrics") if ta else None,
            "quarantine_runs": q.get("runs"),
            "job": ("%s running" % cur.get("cmd")) if cur else "idle",
            "library_read_only": bool((s.get("library") or {}).get("readonly_mount")),
            "version": s.get("version"),
        }
        _WIDGET["v"], _WIDGET["t"] = v, time.time()
        return v


# ---------------------------------------------------------------------------------------------------------------------
# the runner: a separate container that alone can change the library, and only while armed with an authenticator-app code.
# This console never changes anything itself: it drops a small request file into the shared queue and shows the answer.
# ---------------------------------------------------------------------------------------------------------------------
RUNNER_STATE = os.path.join(QUEUE, "state.json")
RUNNER_FRESH_S = 8
DRY_MAX_AGE_S = 6 * 3600


def runner_state():
    """what the runner last published (it rewrites it every second); None when it is not running"""
    try:
        if time.time() - os.stat(RUNNER_STATE).st_mtime > RUNNER_FRESH_S:
            return None
        with open(RUNNER_STATE, encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def runner_running_job():
    st = runner_state()
    return (st or {}).get("running_job") or None


def runner_call(kind, timeout=10.0, **kw):
    """send one request to the runner and wait for its answer; raises JobError with the runner's own words when it refuses"""
    if not runner_state():
        raise JobError("the runner is not running, so nothing can be changed from here. Start it with  " + RUNNER_HINT)
    rid = secrets.token_hex(12)
    req = {"id": rid, "type": kind, "time": time.time()}
    req.update(kw)
    rd, sd = os.path.join(QUEUE, "req"), os.path.join(QUEUE, "res")
    try:
        os.makedirs(rd, exist_ok=True)
        tmp = os.path.join(rd, rid + ".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # may carry a password: owner only
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(req, fh)
        os.replace(tmp, os.path.join(rd, rid + ".json"))
    except OSError as e:
        raise JobError("could not reach the runner's queue: %s" % e)
    req = None
    end = time.time() + timeout
    rp = os.path.join(sd, rid + ".json")
    while time.time() < end:
        if os.path.isfile(rp):
            try:
                with open(rp, encoding="utf-8") as fh:
                    res = json.load(fh)
                os.unlink(rp)
            except (OSError, ValueError):
                time.sleep(0.1)
                continue
            if not res.get("ok"):
                raise JobError(str(res.get("error") or "the runner refused"))
            return res
        time.sleep(0.15)
    try:
        os.unlink(os.path.join(rd, rid + ".json"))               # never leave an unanswered request (or its password) lying around
    except OSError:
        pass
    raise JobError("the runner did not answer in time")


_PLANSHA = {}
PLAN_FILE_OF_NEED = {"plan": "plan.json", "reorg_plan": "reorg-plan.json"}      # which file in out/ a command's "needs" entry means


def plan_file_for(cmd):
    """the plan file a command's real run is tied to (None when it has none): plan.json for the duplicate commands, reorg-plan.json for the layout"""
    for n in (CMD_BY_ID.get(cmd) or {}).get("needs", []):
        if n in PLAN_FILE_OF_NEED:
            return PLAN_FILE_OF_NEED[n]
    return None


def plan_sha(name="plan.json"):
    """sha-256 of out/<name> (cached by size and time); a real run is only allowed on the very plan its dry run was made from"""
    p = out(name)
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    c = _PLANSHA.get(name)
    if not c or c[0] != key:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        c = _PLANSHA[name] = (key, h.hexdigest())
    return c[1]


SUMMARY_RE = re.compile(r"selection: ([0-9]+) groups \| would remove ([0-9]+) files \(([^)]*)\)")
PLACE_RE = re.compile(r"incoming files ([0-9]+) \| to place ([0-9]+) \| already in the library / arrive twice ([0-9]+) \| not placed ([0-9]+)")
FP_RE = re.compile(r"plan fingerprint: ([0-9a-f]{16})\b")
REORG_RE = re.compile(r"\| selected ([0-9]+) \(([^)]*)\) \| skipped ([0-9]+)")
UNDO_RE = re.compile(r"undo (quarantine|place|reorg) run (\S+): ([0-9]+) file\(s\) to put back \| ([0-9]+) playlist\(s\) \| skipped ([0-9]+)")
PURGE_RE = re.compile(r"purge plan: ([0-9]+) run\(s\) to delete \(([^,]*), ([0-9]+) file\(s\)\) \| keeping ([0-9]+) \| older than ([0-9]+) days")
BEETS_RE = re.compile(r"Beets items ([0-9]+) \| already correct ([0-9]+) \| to repoint ([0-9]+) \| file not found anywhere ([0-9]+) \| outside (\S+) ([0-9]+)")
BEETS_ART_RE = re.compile(r"album art paths: ok ([0-9]+) \| to repoint ([0-9]+) \| art file not found ([0-9]+)")


def dry_summary(cmd, jid):
    """what a finished dry run said it would do, for the confirmation text: apply {groups, n, size}; place {n, place, dup, held, fp};
    reorg-apply {n, size, skipped}; beets-remap {n, items, art, missing, outside, fp};
    undo {n, kind, run, playlists, skipped, fp}; purge {n, size, files, keeping, days, fp}.
    The summary is among the first lines of its log, place's fingerprint among the last. None when the log does not say."""
    try:
        with open(os.path.join(OUT, "web-jobs", jid + ".log"), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    head, tail = data[:30000].decode("utf-8", "replace"), data[-30000:].decode("utf-8", "replace")
    if cmd == "apply":
        m = SUMMARY_RE.search(head)
        return {"groups": int(m.group(1)), "n": int(m.group(2)), "size": m.group(3)} if m else None
    if cmd == "place":
        m, f = PLACE_RE.search(head), FP_RE.findall(tail)
        if not m or not f:
            return None
        return {"n": int(m.group(2)) + int(m.group(3)), "place": int(m.group(2)), "dup": int(m.group(3)), "held": int(m.group(4)), "fp": f[-1]}
    if cmd == "reorg-apply":
        m = REORG_RE.search(head)
        return {"n": int(m.group(1)), "size": m.group(2), "skipped": int(m.group(3))} if m else None
    if cmd == "beets-remap":
        m, a, f = BEETS_RE.search(head), BEETS_ART_RE.search(head), FP_RE.findall(head + tail)
        if not m or not a or not f:
            return None
        return {"n": int(m.group(3)) + int(a.group(2)), "items": int(m.group(3)), "art": int(a.group(2)), "missing": int(m.group(4)),
                "outside": int(m.group(6)), "fp": f[-1]}
    if cmd == "undo":
        m, f = UNDO_RE.search(head), FP_RE.findall(head)
        if not m or not f:
            return None
        return {"n": int(m.group(3)), "kind": m.group(1), "run": m.group(2), "playlists": int(m.group(4)), "skipped": int(m.group(5)), "fp": f[-1]}
    if cmd == "purge":
        m, f = PURGE_RE.search(head), FP_RE.findall(head)
        if not m or not f:
            return None
        return {"n": int(m.group(1)), "size": m.group(2), "files": int(m.group(3)), "keeping": int(m.group(4)), "days": int(m.group(5)), "fp": f[-1]}
    return None


def runner_info():
    """runner status for the page: armed or not, and which finished dry run (if any) a real run could be based on"""
    st = runner_state()
    if not st:
        return {"available": False, "queue": os.path.isdir(QUEUE), "login": LOGIN_ON}
    now = time.time()
    armed_left = max(0, int(st.get("armed_until", 0) - now + 0.999))
    used = set(st.get("used_dry") or [])
    eligible = {}
    for cmd in st.get("commands") or []:
        for m in JOBS.list(60):
            if m.get("cmd") != cmd or m.get("mode") != "dry" or m.get("runner"):
                continue
            # only the NEWEST dry run of a card can be run for real: when that one is not usable (still running, failed, used, would do nothing,
            # plan changed since), nothing is offered, rather than an older dry run the person may no longer have in mind
            if m.get("state") != "done" or m.get("rc") != 0:
                break
            needs_plan = (st.get("needs_plan") or {}).get(cmd, True)
            sha = plan_sha((st.get("plan_file") or {}).get(cmd) or "plan.json") if needs_plan else None      # the file the runner will check, by its own list
            if m["id"] in used or (needs_plan and (not sha or m.get("plan_sha") != sha)):
                break
            try:
                age = (dt.datetime.now() - dt.datetime.strptime(m.get("finished") or "", "%Y-%m-%d %H:%M:%S")).total_seconds()
            except ValueError:
                break
            if age > DRY_MAX_AGE_S:
                break
            if cmd == "purge":
                lim = [a for a in (m.get("args") or []) if a.startswith("--older-than=")]
                if lim and not lim[0][len("--older-than="):].isdigit() or lim and int(lim[0][len("--older-than="):]) < 7:
                    break                           # the runner never purges anything younger than 7 days
            rec = {"job": m["id"], "args": m.get("args") or [], "finished": m.get("finished"), "age_minutes": int(age // 60)}
            if "{n}" in (st.get("confirm") or {}).get(cmd, ""):
                sm = dry_summary(cmd, m["id"])          # the file count is part of the phrase typed to confirm
                if not sm or not sm["n"]:
                    break
                rec.update(sm)
            eligible[cmd] = rec
            break
    return {"available": True, "armed": armed_left > 0, "armed_left": armed_left, "arm_minutes": st.get("arm_minutes"), "locked_for": st.get("locked_for", 0),
            "commands": st.get("commands") or [], "confirm": st.get("confirm") or {}, "cred_password": bool(st.get("cred_password")),
            "running_job": st.get("running_job"), "eligible": eligible, "login": LOGIN_ON, "dry_max_hours": DRY_MAX_AGE_S // 3600}


def runner_brief():
    st = runner_state()
    if not st:
        return {"available": False}
    left = max(0, int(st.get("armed_until", 0) - time.time() + 0.999))
    return {"available": True, "armed": left > 0, "armed_left": left, "running_job": st.get("running_job")}


# ---------------------------------------------------------------------------------------------------------------------
# jobs: the only way anything is executed from here. Fixed command list, validated options, no --execute (the runner does real runs).
# ---------------------------------------------------------------------------------------------------------------------
def O(name, typ="str", label=None, help="", default=None, choices=None, multi=False, placeholder=""):
    # "global": the option falls back to the value on the Settings page when it is left blank
    return {"name": name, "type": typ, "label": label or name, "help": help, "default": default, "choices": choices, "multi": multi,
            "placeholder": placeholder, "global": name in GLOBAL_OPTS}


KIND_CHOICES = [
    {"value": "exact", "label": "exact", "help": "same song, same length (within the tolerance)"},
    {"value": "variant", "label": "variant", "help": "same song, different edit (radio edit, single...): the plain album version is kept"},
    {"value": "wide", "label": "wide gap", "help": "lengths differ by a little more than the tolerance. Held for your approval: ticking this IS that approval"},
]
COMMANDS = [
    {"id": "scan", "title": "Scan for duplicates", "group": "Find & clean", "kind": "read",
     "desc": "Reads every file, groups the same song, decodes pairs to check the audio, and writes the duplicate plan. Changes nothing. Takes a few minutes; faster the second time.",
     "opts": [O("workers", "int", "Parallel workers"), O("tolerance", "float", "Tolerance (s)", "Seconds apart that still count as the same recording (rules.txt default 3)"),
              O("wide-tolerance", "float", "Wide tolerance (s)")]},
    {"id": "apply", "title": "Remove duplicates", "group": "Find & clean", "kind": "dry", "needs": ["plan"],
     "desc": "Dry run: shows which copies would go to the quarantine and which are blocked. Nothing is moved. A real run (armed, from this card) moves the losers into the quarantine: nothing is deleted, and it can be undone.",
     "opts": [O("artist", "str", "Only artist containing"), O("group", "str", "Only group id(s)", "comma separated", multi=True),
              O("kinds", "set", "Group kinds", default=["exact", "variant"], choices=KIND_CHOICES),
              O("exact-only", "bool", "Exact groups only"), O("limit", "int", "Limit")]},
    {"id": "playlistfix", "title": "Fix playlists that block a removal", "group": "Find & clean", "kind": "dry", "needs": ["plan", "cred", "navidrome_db"], "login": True,
     "desc": "Dry run: which Navidrome playlists point at a copy about to be removed, and what would change. Talks to Navidrome (needs navidrome.cred).",
     "opts": [O("artist", "str", "Only artist containing"), O("group", "str", "Only group id(s)", "comma separated", multi=True),
              O("kinds", "set", "Group kinds", default=["exact", "variant"], choices=KIND_CHOICES), O("exact-only", "bool", "Exact groups only"), O("limit", "int", "Limit"),
              O("playlist", "str", "Only playlist(s)", "comma separated", multi=True), O("no-stars", "bool", "Do not carry stars over")]},
    {"id": "compare", "title": "Compare two files", "group": "Find & clean", "kind": "read", "positional": ["file_a", "file_b"],
     "desc": "Decodes two files and says whether it is the same audio (only a label differs), a clean/explicit pair, or a different recording.",
     "opts": []},
    {"id": "place", "title": "File new music", "group": "New music", "kind": "dry",
     "desc": "Dry run: reads the _incoming landing folder and shows where each song would be filed, which are duplicates and which are upgrades. A real run (armed, from this card) files the new songs into the library, sets duplicates aside in the quarantine (nothing is deleted, it can be undone) and writes playlists for new folders.",
     "opts": [O("workers", "int", "Parallel workers"), O("show", "int", "Rows to list", default=40), O("keep-similar", "bool", "Keep same song with different length"),
              O("upgrade", "bool", "Add higher-quality copies (FLAC over MP3)", "also set by place-upgrade in rules.txt"),
              O("m3u-update", "str", "Add new tracks to existing playlists", "playlist names, separated by |  (an existing playlist is otherwise left alone)")]},
    {"id": "tagaudit", "title": "Tag, art and lyrics audit", "group": "Library health", "kind": "read",
     "desc": "Lists files and albums missing tags, cover art or lyrics. Read-only.",
     "opts": [O("workers", "int", "Parallel workers")]},
    {"id": "nd-missing", "title": "Explain Navidrome's Missing Files", "group": "Library health", "kind": "read", "needs": ["navidrome_db"],
     "desc": "Sorts Navidrome's Missing Files rows into moved / gone / still on disk, and whether any carry plays, stars or playlists.",
     "opts": []},
    {"id": "reorg", "title": "Plan the folder layout", "group": "Layout", "kind": "read",
     "desc": "Plans the Artist/Album (YYYY)/NN - Title layout for the whole library. Writes the plan only.",
     "opts": [O("workers", "int", "Parallel workers")]},
    {"id": "reorg-apply", "title": "Apply the folder layout", "group": "Layout", "kind": "dry", "needs": ["reorg_plan"],
     "desc": "Dry run: which files the reorg plan would rename or move, and which .m3u playlists would be repointed. Nothing is moved. A real run (armed, from this card) moves exactly that, in batches you choose with the options: Navidrome's database is backed up first, nothing is deleted, and Undo puts every file back.",
     "opts": [O("artist", "str", "Only artist folders containing"), O("album", "str", "Only album folders containing"), O("limit", "int", "About this many files"),
              O("show", "int", "Rows to list", default=400)]},
    {"id": "beets-remap", "title": "Repoint Beets after a reorg", "group": "Layout", "kind": "dry", "needs": ["beets_db"],
     "desc": "Dry run: how many Beets rows would be repointed at the reorganised files. Works on a copy. A real run (armed, from this card) edits the paths in Beets' library.db after backing it up; no music file is touched. Pause the Beets task first.",
     "opts": []},
    {"id": "undo", "title": "Undo a run", "group": "Quarantine", "kind": "dry", "needs": [], "special": "manifest",
     "desc": "Dry run: what restoring a quarantine, place or reorg run would put back. A real run (armed, from this card) puts exactly those files back; a run that was already undone has nothing left to restore. It does not touch Navidrome or Beets: rescan Navidrome afterwards, and repoint Beets if the run was a layout run.",
     "opts": []},
    {"id": "purge", "title": "Purge old quarantine", "group": "Quarantine", "kind": "dry",
     "desc": "Dry run: which quarantine runs are old enough to be deleted permanently. A real run (armed, from this card) deletes exactly those runs and cannot be undone: the files in them are gone for good, and so is the manifest that Undo needs. A real run needs an age limit of at least 7 days.",
     "opts": [O("older-than", "int", "Older than (days)", default=30)]},
]
CMD_BY_ID = dict((c["id"], c) for c in COMMANDS)


def need_ok(c):
    miss = []
    for n in c.get("needs", []):
        if n == "plan" and not os.path.isfile(out("plan.json")):
            miss.append("no plan yet: run Scan first")
        if n == "reorg_plan" and not os.path.isfile(out("reorg-plan.json")):
            miss.append("no reorg plan yet: run Plan the folder layout first")
        if n == "cred" and not os.path.isfile(CRED):
            miss.append("navidrome.cred is not mounted in this container")
        if n == "navidrome_db" and not os.path.isfile(NDDB):
            miss.append("navidrome.db is not mounted in this container")
        if n == "beets_db" and not os.path.isfile(BEETSDB):
            miss.append("the Beets library.db is not mounted in this container")
    return miss


def api_commands():
    res = []
    st = load_settings()
    rt = rules_tolerances()
    tool = {"tolerance": 3.0, "wide_tolerance": 6.0}
    for c in COMMANDS:
        d = dict(c)
        d["blocked"] = need_ok(c)
        if c.get("login"):
            ck = cred_keys()
            # stored False = the card must ask for the password; missing = lines navidrome.cred still needs
            d["login"] = {"stored": "password" in ck, "missing": [k for k in ("url", "user") if k not in ck], "plain_ok": ALLOW_PLAIN_PW}
        opts = []
        for o in c["opts"]:
            o = dict(o)
            if o.get("global"):
                key = GLOBAL_OPTS[o["name"]]
                if st.get(key) is not None:
                    o["global_value"], o["global_from"] = st[key], "settings"
                elif rt.get(key) is not None:
                    o["global_value"], o["global_from"] = rt[key], "rules.txt"
                elif key in tool:
                    o["global_value"], o["global_from"] = tool[key], "tool default"
            opts.append(o)
        d["opts"] = opts
        res.append(d)
    return res


class JobError(Exception):
    pass


def build_argv(cmd, params):
    """validated command line for a job; raises JobError for anything not on the list"""
    c = CMD_BY_ID.get(cmd)
    if not c:
        raise JobError("unknown command")
    if not isinstance(params, dict):
        params = {}
    miss = need_ok(c)
    if miss:
        raise JobError("; ".join(miss))
    argv = [cmd]
    st = load_settings()
    for o in c["opts"]:
        v = params.get(o["name"])
        if v in (None, "", False) and o.get("global") and st.get(GLOBAL_OPTS[o["name"]]) is not None:
            v = st[GLOBAL_OPTS[o["name"]]]          # left blank on the card: use the global setting
        if v in (None, "", False):
            continue
        flag = "--" + o["name"]
        t = o["type"]
        if t == "bool":
            if v is True or str(v).lower() in ("1", "true", "yes", "on"):
                argv.append(flag)
        elif t == "set":
            items = v if isinstance(v, list) else [x for x in re.split(r"[,\s]+", str(v)) if x]
            items = [str(x).strip().lower() for x in items]
            allowed = [ch["value"] for ch in (o.get("choices") or [])]
            if not items or any(x not in allowed for x in items):
                raise JobError("%s: tick at least one of %s" % (o["label"], ", ".join(allowed)))
            argv.append("%s=%s" % (flag, ",".join(x for x in allowed if x in items)))
        elif t in ("int", "float"):
            try:
                n = int(v) if t == "int" else float(v)
            except (TypeError, ValueError):
                raise JobError("%s must be a number" % o["label"])
            if n < 0 or n > 100000:
                raise JobError("%s is out of range" % o["label"])
            argv.append("%s=%s" % (flag, n))
        else:
            vals = [x.strip() for x in re.split(r"[,\n]", str(v))] if o["multi"] else [str(v).strip()]
            for x in vals:
                if not x:
                    continue
                if len(x) > 300 or "\x00" in x or "\n" in x:
                    raise JobError("%s has an invalid value" % o["label"])
                argv.append("%s=%s" % (flag, x))
    if c.get("positional"):
        argv.append("--")                      # file names go after '--' so they can never be read as options
        for key in c["positional"]:
            rel = safe_rel(params.get(key))
            if rel is None or not os.path.isfile(os.path.join(MUSIC, rel)):
                raise JobError("%s: pick a file inside the library" % key)
            if not rel.lower().endswith(AUDIO_EXT):
                raise JobError("%s is not an audio file" % key)
            argv.append(rel)
    if c.get("special") == "manifest":
        man = str(params.get("manifest") or "")
        ok = False
        for base in (QUAR, REORG_ROOT):
            full = resolve_under(base, os.path.relpath(man, base)) if man.startswith(base.rstrip("/") + "/") else None
            if full and os.path.basename(full) == "manifest.json" and os.path.isfile(full):
                ok = True
        if not ok:
            raise JobError("pick a quarantine or reorg run that has a manifest")
        argv.append("--manifest=" + man)
    # belt and braces: nothing in this phase may carry --execute (or any restore / write switch)
    for a in argv:
        if a.startswith("--execute") or a.startswith("--restore") or a.startswith("--bump"):
            raise JobError("the console itself never runs with --execute: a real run goes through the runner (arm it, then use Run for real on the card)")
    return argv


DISK_RE = re.compile(r"^(sd[a-z]+|sata\d+|sas\d+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+)$")      # whole disks, not partitions or md/dm layers


class LoadSampler(threading.Thread):
    """While a job runs: how busy is the whole NAS (CPU, the busiest disk, load average)? It reads /proc, which a container
    shares with the NAS, every two seconds. This is what answers 'can I raise the number of workers?'"""
    INTERVAL = 2.0

    def __init__(self, meta):
        threading.Thread.__init__(self, daemon=True)
        self.meta = meta
        self.halt = threading.Event()
        self.samples = []

    @staticmethod
    def _cpu():
        try:
            with open("/proc/stat") as fh:
                v = [int(x) for x in fh.readline().split()[1:]]
            return sum(v), v[3] + (v[4] if len(v) > 4 else 0), v[4] if len(v) > 4 else 0       # total, idle(+iowait), iowait
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _disks():
        res = {}
        try:
            with open("/proc/diskstats") as fh:
                for line in fh:
                    p = line.split()
                    if len(p) > 12 and DISK_RE.match(p[2]):
                        res[p[2]] = int(p[12])            # milliseconds this disk has had work queued
        except (OSError, ValueError):
            pass
        return res

    def run(self):
        c0, d0, t0 = self._cpu(), self._disks(), time.time()
        while not self.halt.wait(self.INTERVAL):
            c1, d1, t1 = self._cpu(), self._disks(), time.time()
            dt_s = max(t1 - t0, 0.001)
            s = {}
            if c0 and c1 and c1[0] > c0[0]:
                tot = float(c1[0] - c0[0])
                s["cpu"] = round(100.0 * (1.0 - (c1[1] - c0[1]) / tot), 1)
                s["wait"] = round(100.0 * (c1[2] - c0[2]) / tot, 1)
            busy = [min(100.0, 100.0 * (d1[k] - d0[k]) / (dt_s * 1000.0)) for k in d1 if k in d0]
            if busy:
                s["disk"] = round(max(busy), 1)
            try:
                with open("/proc/loadavg") as fh:
                    s["load"] = float(fh.read().split()[0])
            except (OSError, ValueError, IndexError):
                pass
            if s:
                self.samples.append(s)
                self.meta["live"] = s
            c0, d0, t0 = c1, d1, t1

    def finish(self):
        self.halt.set()
        self.join(timeout=3)
        self.meta.pop("live", None)
        rows = self.samples
        if not rows:
            return None

        def stat(k):
            v = [r[k] for r in rows if k in r]
            return (round(sum(v) / len(v), 1), round(max(v), 1)) if v else None
        out = {"samples": len(rows)}
        for k, name in (("cpu", "cpu"), ("wait", "wait"), ("disk", "disk"), ("load", "load")):
            r = stat(k)
            if r:
                out[name + "_avg"], out[name + "_max"] = r
        return out


class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.sampler = None
        self.cur = None            # meta dict of the running job
        os.makedirs(JOBS_DIR, exist_ok=True) if os.path.isdir(OUT) else None
        threading.Thread(target=self._beat, daemon=True).start()

    def _beat(self):
        """while a console job runs, say so to the runner (it will not start a real run alongside one)"""
        while True:
            time.sleep(4)
            if self.cur is not None:
                self.mark_busy()

    @staticmethod
    def clear_busy():
        try:
            os.unlink(os.path.join(QUEUE, "web-busy"))
        except OSError:
            pass

    @staticmethod
    def mark_busy():
        try:
            if os.path.isdir(QUEUE):
                with open(os.path.join(QUEUE, "web-busy"), "w") as fh:
                    fh.write(str(time.time()))
        except OSError:
            pass

    # --- persistence -------------------------------------------------------------------------------------------
    def meta_path(self, jid):
        return os.path.join(JOBS_DIR, jid + ".json")

    def log_path(self, jid):
        return os.path.join(JOBS_DIR, jid + ".log")

    def save(self, meta):
        tmp = self.meta_path(meta["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        os.replace(tmp, self.meta_path(meta["id"]))

    def load(self, jid):
        if not re.match(r"^[0-9]{8}-[0-9]{6}-[a-z-]+$", jid or ""):
            return None
        try:
            with open(self.meta_path(jid), encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            return None
        if m.get("state") == "running" and (not self.cur or self.cur["id"] != jid):
            if not (m.get("runner") and runner_running_job() == jid):       # a real run belongs to the runner, which says whether it is still going
                # The job may have finished between reading its record above and looking at self.cur just now (a job that ends
                # sets "done" on disk BEFORE it clears the running marker), so read the record once more before calling it
                # interrupted. Without this a dry run of under a second was sometimes shown as "interrupted" and the page stopped following it.
                try:
                    with open(self.meta_path(jid), encoding="utf-8") as fh:
                        m2 = json.load(fh)
                except (OSError, ValueError):
                    m2 = m
                if m2.get("state") != "running":
                    return m2
                m["state"] = "interrupted"          # the web server (or the runner) restarted while it ran
        return m

    def list(self, n=30):
        res = []
        if not os.path.isdir(JOBS_DIR):
            return res
        names = sorted([f[:-5] for f in os.listdir(JOBS_DIR) if f.endswith(".json")], reverse=True)[:n]
        for jid in names:
            m = self.load(jid)
            if m:
                res.append(m)
        return res

    def current_info(self):
        with self.lock:
            if self.cur:
                return dict(self.cur)
        rj = runner_running_job()                  # a real run started through the runner counts as the current job too
        return self.load(rj) if rj else None

    # --- run ----------------------------------------------------------------------------------------------------
    def start(self, cmd, params, secure=False, by=""):
        argv = build_argv(cmd, params)
        # a Navidrome password typed on the card: handed to this one job through its environment. It is never put on the
        # command line, in the job record, or in any log, and it is not kept after the request.
        nd_pass = None
        if CMD_BY_ID[cmd].get("login"):
            ck = cred_keys()
            gap = [k for k in ("url", "user") if k not in ck]
            if gap:
                raise JobError("navidrome.cred has no %s= line. It needs  url=http://<your-navidrome-address>:4533  and  user=<your Navidrome user>  "
                               "(then run .\\dewey.ps1 web again so the console picks it up)." % " or ".join(gap))
        if CMD_BY_ID[cmd].get("login") and not cred_has_password():
            pw = params.get("nd_password") if isinstance(params, dict) else None
            if not isinstance(pw, str) or not pw:
                raise JobError("Type the Navidrome password into the box on this card first. It is used for this run only and is never saved.")
            if len(pw) > 200 or "\x00" in pw or "\n" in pw or "\r" in pw:
                raise JobError("that password cannot be used (too long, or it contains a line break)")
            if not secure and not ALLOW_PLAIN_PW:
                raise JobError("For safety the Navidrome password is only accepted from a page opened over https, because over plain http it "
                               "would cross your network readable. Open the console at its https address (Settings > Security explains how), "
                               "or store password= in navidrome.cred.")
            nd_pass = pw
        with self.lock:
            if self.cur is not None:
                raise JobError("another job is running (%s). Wait for it to finish or cancel it." % self.cur["cmd"])
            self.mark_busy()                           # first say we are about to run, then look at the runner (so the two cannot both start)
            if runner_running_job():
                self.clear_busy()
                raise JobError("a real run is in progress (started through the runner). Wait for it to finish or cancel it.")
            now = dt.datetime.now()
            jid = "%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), cmd)
            os.makedirs(JOBS_DIR, exist_ok=True)
            meta = {"id": jid, "cmd": cmd, "title": CMD_BY_ID[cmd]["title"], "args": argv[1:], "started": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "t0": time.time(), "state": "running", "rc": None, "finished": None, "mode": CMD_BY_ID[cmd]["kind"]}
            if by and len(USERS) > 1:
                meta["by"] = by
            if plan_file_for(cmd):
                meta["plan_sha"] = plan_sha(plan_file_for(cmd))          # the runner only accepts a real run on the exact plan this dry run was made from
            full = [sys.executable, "-u", APP] + argv
            env = dict(os.environ)
            env.update({"PYTHONUNBUFFERED": "1", "MD_MUSIC": MUSIC, "MD_OUT": OUT, "MD_RULES": RULES, "MD_QUAR": QUAR, "MD_NDDB": NDDB,
                        "MD_INCOMING": INCOMING, "MD_REORG": REORG_ROOT, "MD_BEETSDB": BEETSDB, "MD_ND_CRED": CRED})
            env.pop("MD_ND_PASS", None)                # never inherit one from the container's own environment
            if nd_pass:
                env["MD_ND_PASS"] = nd_pass
            logf = open(self.log_path(jid), "wb")
            logf.write(("$ dewey %s\n\n" % " ".join(argv)).encode("utf-8"))
            logf.flush()
            try:
                self.proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, env=env,
                                             start_new_session=True, cwd=OUT if os.path.isdir(OUT) else None)
            except OSError as e:
                logf.close()
                self.clear_busy()
                raise JobError("could not start: %s" % e)
            self.cur = meta
            self.save(meta)
            self.sampler = LoadSampler(meta)
            self.sampler.start()
        threading.Thread(target=self._wait, args=(self.proc, logf, meta), daemon=True).start()
        return meta

    def _wait(self, proc, logf, meta):
        rc = proc.wait()
        try:
            logf.close()
        except OSError:
            pass
        sampler, load = self.sampler, None
        if sampler:
            try:
                load = sampler.finish()
            except Exception:
                load = None
        with self.lock:
            if load:
                meta["load"] = load
            meta["rc"] = rc
            if meta.get("cancelled"):
                meta["state"] = "cancelled"
            else:
                meta["state"] = "done" if rc == 0 else "failed"
            meta["finished"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta["seconds"] = round(time.time() - meta["t0"], 1)
            self.save(meta)
            self.cur = None
            self.proc = None
            self.clear_busy()

    def cancel(self, jid, by=""):
        m = self.load(jid)
        if m and m.get("runner") and m.get("state") == "running":
            runner_call("cancel", job=jid, by=by)         # a real run: the runner stops it
            return
        with self.lock:
            if not self.cur or self.cur["id"] != jid or not self.proc:
                raise JobError("that job is not running")
            self.cur["cancelled"] = True
            proc = self.proc
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        def killer():
            time.sleep(6)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        threading.Thread(target=killer, daemon=True).start()

    def log_chunk(self, jid, offset):
        meta = self.load(jid)
        if meta is None:
            return None
        p = self.log_path(jid)
        try:
            size = os.stat(p).st_size
        except OSError:
            size = 0
        offset = max(0, min(int(offset or 0), size))
        text = ""
        if size > offset:
            with open(p, "rb") as fh:
                fh.seek(offset)
                data = fh.read(400000)
            # do not cut a UTF-8 sequence in half: drop up to 3 trailing bytes until the chunk decodes
            txt, used = None, len(data)
            for cut in range(4):
                try:
                    txt = data[:len(data) - cut].decode("utf-8")
                    used = len(data) - cut
                    break
                except UnicodeDecodeError:
                    continue
            if txt is None:
                txt, used = data.decode("utf-8", "replace"), len(data)
            text = txt
            offset += used
        meta = dict(meta)
        meta.pop("t0", None)
        with self.lock:
            if self.cur and self.cur.get("id") == jid and self.cur.get("live"):
                meta["live"] = dict(self.cur["live"])          # how busy the NAS is right now (not kept on disk)
        return {"text": text, "offset": offset, "meta": meta, "more": offset < size}


JOBS = JobManager()


# ---------------------------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------------------------
class LoginGuard(object):
    """Slows down password guessing. Five DIFFERENT wrong logins within ten minutes lock the login for a minute, doubling
    each time up to fifteen minutes; while it is locked even the right password is refused. The same wrong login repeated
    (a browser re-sending an old saved password on every page request) counts once."""
    WINDOW, MAX_BAD, BASE_LOCK, MAX_LOCK = 600, 5, 60, 900

    def __init__(self):
        self.lock = threading.Lock()
        self.recent = {}            # hash of a wrong Authorization header -> time
        self.events = []            # times of every counted wrong login (for the Security panel)
        self.locked_until = 0.0
        self.strikes = 0

    def locked_for(self):
        with self.lock:
            return max(0, int(self.locked_until - time.time() + 0.999))

    def fail(self, header):
        key = hashlib.sha256(header.encode("utf-8", "replace")).hexdigest()[:16]
        now = time.time()
        with self.lock:
            self.recent = dict((k, t) for k, t in self.recent.items() if now - t < self.WINDOW)
            if key in self.recent:
                return 0
            self.recent[key] = now
            self.events = [t for t in self.events if now - t < 86400] + [now]
            if len(self.recent) >= self.MAX_BAD:
                self.strikes += 1
                secs = min(self.MAX_LOCK, self.BASE_LOCK * (2 ** (self.strikes - 1)))
                self.locked_until = now + secs
                self.recent = {}
                return secs
            return 0

    def ok(self):
        with self.lock:
            if time.time() >= self.locked_until:
                self.strikes = 0
                self.recent = {}

    def stats(self):
        now = time.time()
        with self.lock:
            return {"last_hour": sum(1 for t in self.events if now - t < 3600), "last_day": sum(1 for t in self.events if now - t < 86400),
                    "locked_for": max(0, int(self.locked_until - now + 0.999))}


GUARD = LoginGuard()
CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Handler(BaseHTTPRequestHandler):
    server_version = "dewey-web/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # The standard library logs the raw request line and quotes the client's own bytes in its error messages
    # ("Bad request version ('...')"). A client that sends a body where the request line belongs would get that text,
    # passwords included, copied into the container log. So only a well-formed "METHOD /path HTTP/1.x" is logged, and
    # without its query string; anything else is logged as a size only.
    _REQLINE = re.compile(r"^([A-Z]{3,10}) (/[^\s?]{0,200})(?:\?\S*)? HTTP/1\.[01]$")

    def log_request(self, code="-", size="-"):
        m = self._REQLINE.match(getattr(self, "requestline", "") or "")
        shown = "%s %s" % (m.group(1), m.group(2)) if m else "(malformed request line, %d bytes)" % len(getattr(self, "requestline", "") or "")
        self.log_message('"%s" %s %s', shown, str(code), str(size))

    def log_error(self, fmt, *args):
        code = args[0] if args and isinstance(args[0], int) else "-"
        self.log_message("error %s (details withheld)", code)

    def end_headers(self):
        # headers every response carries. The generated reports under /out/ keep their own inline styles and scripts, so
        # the strict content policy is left off those.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if not (getattr(self, "path", "") or "").startswith("/out/"):  # no .path yet when the request line itself was malformed
            self.send_header("Content-Security-Policy", CSP)
        BaseHTTPRequestHandler.end_headers(self)

    # -- plumbing ----------------------------------------------------------------------------------------------
    def _names(self):
        """the host names this request was addressed to (Host, plus X-Forwarded-Host when a proxy such as Tailscale Serve sets it)"""
        return [x for x in ((self.headers.get("Host") or "").lower(), (self.headers.get("X-Forwarded-Host") or "").lower()) if x]

    def _host_ok(self):
        if not ALLOWED_HOSTS:
            return True
        for n in self._names():
            bare = n.rsplit(":", 1)[0] if not n.endswith("]") else n
            if n in ALLOWED_HOSTS or bare in ALLOWED_HOSTS or bare in ("localhost", "127.0.0.1"):
                return True
        return False

    def _via_https(self):
        """True when the browser loaded this page over https (Origin / Referer scheme, or the proxy said so)"""
        org, ref = self.headers.get("Origin") or "", self.headers.get("Referer") or ""
        return (urlparse(org).scheme == "https" or (not org and urlparse(ref).scheme == "https")
                or (self.headers.get("X-Forwarded-Proto") or "").lower() == "https")

    def _auth_state(self):
        """(login correct, an Authorization header was sent); self._user is the account that logged in"""
        self._user = ""
        if not LOGIN_ON:
            return True, False
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
            except Exception:
                return False, True
            hit = ""
            for name, pw in USERS:                # every account is compared, so the time taken does not say which one exists
                if hmac.compare_digest(u.encode("utf-8"), name.encode("utf-8")) & hmac.compare_digest(p.encode("utf-8"), pw.encode("utf-8")):
                    hit = name
            self._user = hit
            return bool(hit), True
        return False, bool(h)

    def _auth_ok(self):
        return self._auth_state()[0]

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _widget_gate(self):
        """GET /api/widget with 'Authorization: Bearer <MD_WIDGET_TOKEN>': True = let it through (it can read nothing else),
        False = a Bearer token that is wrong or not enabled (answered here), None = not a widget request"""
        if self.command not in ("GET", "HEAD") or unquote(urlparse(self.path).path) != "/api/widget":
            return None
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return None
        tok = h[7:].strip()
        if WIDGET_TOKEN and hmac.compare_digest(tok.encode("utf-8"), WIDGET_TOKEN.encode("utf-8")):
            return True
        GUARD.fail(h)
        sys.stderr.write("WIDGET TOKEN REFUSED from %s%s\n" % (self.client_address[0], "" if WIDGET_TOKEN else " (MD_WIDGET_TOKEN is not set)"))
        self._send(401, {"error": "bad widget token"})
        return False

    def _gate(self):
        w = self._widget_gate()
        if w is not None:
            return w
        if not self._host_ok():
            sys.stderr.write("REFUSED host name %r (not in MD_WEB_HOSTS) from %s\n" % (self.headers.get("Host"), self.client_address[0]))
            self._send(421, {"error": "this console does not answer to that host name (see MD_WEB_HOSTS in %s)" % CFG_FILE})
            return False
        login = LOGIN_ON
        left = GUARD.locked_for() if login else 0
        if left:
            self._send(429, {"error": "too many wrong logins: try again in %d s" % left}, extra={"Retry-After": str(left)})
            return False
        ok, sent = self._auth_state()
        if not ok:
            if sent:
                secs = GUARD.fail(self.headers.get("Authorization", ""))
                who = self.headers.get("Tailscale-User-Login") or "-"
                sys.stderr.write("LOGIN FAILED from %s (tailscale user %s)%s\n" % (self.client_address[0], who, " - login locked for %d s" % secs if secs else ""))
            self._send(401, {"error": "login required"}, extra={"WWW-Authenticate": 'Basic realm="Dewey"'})
            return False
        if login:
            GUARD.ok()
        return True

    def do_HEAD(self):
        self.do_GET()

    # -- GET -----------------------------------------------------------------------------------------------------
    def do_GET(self):
        if not self._gate():
            return
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path[5:], q)
            if path.startswith("/out/"):
                return self._out_file(path[5:])
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:                       # never leave the browser hanging
            sys.stderr.write("ERROR %s: %r\n" % (self.path, e))
            try:
                self._err(500, "server error: %s" % e)
            except Exception:
                pass

    def _api_get(self, p, q):
        if p == "status":
            return self._send(200, api_status())
        if p == "widget":
            return self._send(200, api_widget())
        if p == "security":
            return self._send(200, api_security(self))
        if p == "commands":
            return self._send(200, api_commands())
        if p == "runner":
            return self._send(200, runner_info())
        if p == "plan":
            return self._send(200, api_plan_info())
        if p == "groups":
            return self._send(200, api_groups(q))
        if p.startswith("groups/"):
            g = api_group(p[7:])
            return self._send(200, g) if g else self._err(404, "no such group")
        if p == "reorg":
            return self._send(200, api_reorg(q))
        if p == "place":
            return self._send(200, api_place(q))
        if p == "tagaudit":
            return self._send(200, api_tagaudit(q))
        if p == "quarantine":
            return self._send(200, api_quarantine(q))
        if p.startswith("quarantine/"):
            parts = p.split("/")
            if len(parts) == 4 and parts[1] == "size":
                r = api_run_size(parts[2], parts[3])
                return self._send(200, r) if r else self._err(404, "no such run")
            if len(parts) == 3:
                r = api_run(parts[2], parts[1], q)
                return self._send(200, r) if r else self._err(404, "no such run")
        if p == "rules":
            return self._send(200, api_rules())
        if p == "logs":
            return self._send(200, api_logs())
        if p == "settings":
            return self._send(200, api_settings())
        if p == "diagnostics":
            return self._send(200, api_diagnostics())
        if p.startswith("logs/"):
            name = p[5:]
            fp = resolve_under(out("logs"), name)
            if not fp or not os.path.isfile(fp) or not name.endswith(".txt"):
                return self._err(404, "no such log")
            return self._send(200, read_tail(fp), "text/plain; charset=utf-8")
        if p == "search":
            return self._send(200, api_search(q))
        if p == "jobs":
            return self._send(200, {"current": JOBS.current_info(), "jobs": JOBS.list(40)})
        if p.startswith("jobs/") and p.endswith("/log"):
            r = JOBS.log_chunk(p[5:-4], q.get("offset", ["0"])[0])
            return self._send(200, r) if r else self._err(404, "no such job")
        if p == "audio":
            return self._audio(q)
        if p == "art":
            return self._art(q)
        return self._err(404, "unknown endpoint")

    # -- POST ----------------------------------------------------------------------------------------------------
    def do_POST(self):
        if not self._gate():
            return
        # a custom header a plain cross-site form cannot send, plus a same-origin check
        if self.headers.get("X-MD-Web") != "1":
            return self._err(403, "missing X-MD-Web header")
        org = self.headers.get("Origin")
        if org and urlparse(org).netloc.lower() not in self._names():
            return self._err(403, "cross-origin request refused")
        secure = self._via_https()
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100000:
            return self._err(413, "too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._err(400, "bad JSON")
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/jobs":
                meta = JOBS.start(str(body.get("cmd")), body.get("params") or {}, secure=secure, by=getattr(self, "_user", ""))
                m = dict(meta)
                m.pop("t0", None)
                return self._send(201, m)
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                JOBS.cancel(path[10:-7], by=getattr(self, "_user", ""))
                return self._send(200, {"ok": True})
            if path in ("/api/runner/arm", "/api/runner/run", "/api/runner/disarm"):
                by = getattr(self, "_user", "") or "console"
                if path != "/api/runner/disarm":
                    # a code or a password is in this request: only with a console login, and only over https
                    if not LOGIN_ON:
                        raise JobError("write access needs a console login: set MD_WEB_USER and MD_WEB_PASS in %s first" % CFG_FILE)
                    if not secure and not ALLOW_PLAIN_PW:
                        raise JobError("write access can only be switched on from a page opened over https (Settings > Security explains how), "
                                       "because over plain http the code would cross your network readable")
                if path == "/api/runner/disarm":
                    runner_call("disarm", by=by)
                    return self._send(200, runner_info())
                if path == "/api/runner/arm":
                    runner_call("arm", code=str(body.get("code") or "").strip(), by=by)
                    return self._send(200, runner_info())
                nd = body.get("nd_password")
                r = runner_call("job", cmd=str(body.get("cmd") or ""), dry_job=str(body.get("dry_job") or ""), confirm=str(body.get("confirm") or ""),
                                nd_pass=nd if isinstance(nd, str) and nd else None, by=by, timeout=15.0)
                nd = None
                return self._send(201, {"job": r.get("job")})
            if path == "/api/settings":
                save_settings(body)
                return self._send(200, api_settings())
        except JobError as e:
            return self._err(409 if ("running" in str(e) or "in progress" in str(e)) else 400, str(e))
        except ValueError as e:
            return self._err(400, str(e))
        return self._err(404, "unknown endpoint")

    # -- files ---------------------------------------------------------------------------------------------------
    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        fp = resolve_under(WEBROOT, path.lstrip("/"))
        if not fp or not os.path.isfile(fp):
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(fp, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _out_file(self, name):
        if name not in OUT_FILES:
            return self._err(404, "not available")
        fp = os.path.join(OUT, name)
        if not os.path.isfile(fp):
            return self._err(404, "not found (run the command that writes it first)")
        ctype = {".html": "text/html; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".json": "application/json", ".tsv": "text/plain; charset=utf-8",
                 ".log": "text/plain; charset=utf-8"}.get(os.path.splitext(name)[1], "application/octet-stream")
        with open(fp, "rb") as fh:
            data = fh.read()
        extra = {} if name.endswith(".html") else {"Content-Disposition": 'attachment; filename="%s"' % name} if self.path.endswith("?dl=1") else {}
        self._send(200, data, ctype, extra)

    def _root_for(self, q):
        root = q.get("root", ["music"])[0]
        return {"music": MUSIC, "quarantine": QUAR, "incoming": INCOMING}.get(root)

    def _audio(self, q):
        root = self._root_for(q)
        rel = q.get("p", [""])[0]
        fp = resolve_under(root, rel) if root else None
        if not fp or not os.path.isfile(fp) or not fp.lower().endswith(AUDIO_EXT):
            return self._err(404, "no such audio file")
        size = os.stat(fp).st_size
        ctype = AUDIO_MIME.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                if m.group(1) == "" and m.group(2):
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1) or 0)
                    if m.group(2):
                        end = min(size - 1, int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(fp, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _art(self, q):
        root = self._root_for(q)
        rel = q.get("p", [""])[0]
        fp = resolve_under(root, rel) if root else None
        if not fp:
            return self._send(204, b"", "image/jpeg")
        d = fp if os.path.isdir(fp) else os.path.dirname(fp)
        try:
            names = os.listdir(d)
        except OSError:
            return self._send(204, b"", "image/jpeg")
        for base in ART_NAMES:
            for ext in ART_EXTS:
                for n in names:
                    if n.lower() == base + ext:
                        with open(os.path.join(d, n), "rb") as fh:
                            data = fh.read()
                        return self._send(200, data, mimetypes.guess_type(n)[0] or "image/jpeg", {"Cache-Control": "private, max-age=3600"})
        return self._send(204, b"", "image/jpeg")        # no cover image: the page shows a placeholder


def main():
    for d in (OUT,):
        if not os.path.isdir(d):
            print("WARNING: %s does not exist - mount the out folder" % d)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("Dewey web %s (tool %s) on http://%s:%d | library %s (%s) | out %s | login %s" % (
        WEB_VERSION, VERSION, HOST, PORT, MUSIC,
        {True: "mounted READ-ONLY", False: "WRITABLE - mount it :ro!", None: "?"}[is_readonly_mount(MUSIC) if os.path.isdir(MUSIC) else None],
        OUT, ("required (%d account%s)" % (len(USERS), "" if len(USERS) == 1 else "s")) if LOGIN_ON else "NOT set (LAN only!)"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
