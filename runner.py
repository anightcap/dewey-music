#!/usr/bin/env python3
"""Dewey runner: the only part of the suite that is ever allowed to change the library, and only while it is "armed".

What it is
  A small always-on container (dewey-runner), Python standard library only. It has the library mounted READ-WRITE and no network
  port, no Docker socket and no web server. The web console cannot do anything to the library itself: it drops a small request file
  into a queue folder (a Docker volume shared by the two containers, not a folder on the NAS share) and this program decides.

What it checks before it changes anything
  1. It is ARMED. Arming needs a 6-digit code from an authenticator app (TOTP, secret kept only in this container's own volume,
     so the console never sees it). Wrong codes lock arming for 10 minutes after 3 tries. The armed time ends by itself
     (MD_ARM_MINUTES, default 15) or when you disarm.
  2. The request names a DRY RUN of the same command that finished, with exit code 0, on a plan.json that has not changed since
     (checked by hash), within the last 6 hours. The real run uses the dry run's own recorded options plus --execute, so the options
     cannot differ from what you reviewed. A dry run can authorise one real run only.
     The runner reads plan.json ONCE, checks that exact copy against the dry run's hash, keeps it in its own state volume and runs the
     command on that copy (--plan=...): the plan cannot be swapped between the check and the run.
  2b. Commands that move files also carry what the dry run said it would do: apply the number of files it would move (you type it in
     the confirmation phrase, QUARANTINE 8, and the tool refuses to start if it would move a different number: --expect-remove);
     place a fingerprint of its whole plan (every file's fate and every playlist change: --expect-plan). Place has no plan.json, it
     plans from what is in the landing folder at that moment, so the fingerprint is what ties the real run to the dry run you read;
     reorg-apply the number of files it would move (MOVE 50, --expect-moves) and its own plan file, reorg-plan.json, which is hashed and
     snapshotted exactly like plan.json; beets-remap, which edits Beets' database and has no plan file either, a fingerprint of every row
     it would change (REPOINT 9000, --expect-plan); undo the number of files it would put back (RESTORE 12) and a fingerprint of every
     move, and only a manifest that sits directly inside a quarantine run folder or a reorg run folder is accepted; purge, the one command
     that deletes for good, the number of runs it would delete (DELETE 3) and a fingerprint of exactly which runs (name, age stamp, size,
     file count): a run that aged past the limit, or changed, after the dry run makes the tool refuse. Purge never takes a limit under 7 days.
  3. The command and every option are on this program's own fixed list (it does not trust the console's list).
  4. You typed the confirmation phrase. One real run at a time; nothing runs while a console job is running.

Commands:   python runner.py            run the loop (what the container does)
            python runner.py secret     print the authenticator-app secret (and the otpauth:// address)
            python runner.py newsecret  make a new secret (the old codes stop working)

Environment:  MD_QUEUE=/queue  MD_OUT=/out  MD_APP=/app/dewey.py  MD_STATE=/state  MD_ARM_MINUTES=15
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import struct
import subprocess
import sys
import time

QUEUE = os.environ.get("MD_QUEUE", "/queue")
OUT = os.environ.get("MD_OUT", "/out")
APP = os.environ.get("MD_APP", "/app/dewey.py")
STATE = os.environ.get("MD_STATE", "/state")
try:
    ARM_MIN = max(1, min(120, int(os.environ.get("MD_ARM_MINUTES", "15"))))
except ValueError:
    ARM_MIN = 15
RUNNER_VERSION = 1
JOBS_DIR = os.path.join(OUT, "web-jobs")
REQ_DIR = os.path.join(QUEUE, "req")
RES_DIR = os.path.join(QUEUE, "res")
DRY_MAX_AGE_S = 6 * 3600
WEB_BUSY_FRESH_S = 20
LOCK_AFTER, LOCK_SECONDS = 3, 600

ID_RE = re.compile(r"^[0-9a-z][0-9a-z-]{5,63}$")
JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z-]+$")


# ---------------------------------------------------------------------------------------------------------------------
# what a real run may be. One entry per command; slice 1 has just the Navidrome playlist fix.
# ---------------------------------------------------------------------------------------------------------------------
def _tok(*pats):
    return [re.compile(p) for p in pats]


_GROUP_TOKENS = [r"^--artist=[^\x00\n\r]{1,300}$", r"^--group=[^\x00\n\r]{1,300}$", r"^--kinds=(exact|variant|wide)(,(exact|variant|wide)){0,2}$",
                 r"^--exact-only$", r"^--limit=[0-9]{1,6}$"]
SUMMARY_RE = re.compile(r"selection: ([0-9]+) groups \| would remove ([0-9]+) files")
PLACE_RE = re.compile(r"incoming files ([0-9]+) \| to place ([0-9]+) \| already in the library / arrive twice ([0-9]+) \| not placed ([0-9]+)")
FP_RE = re.compile(r"plan fingerprint: ([0-9a-f]{16})\b")
REORG_RE = re.compile(r"\| selected ([0-9]+) \(([^)]*)\) \| skipped ([0-9]+)")
BEETS_RE = re.compile(r"Beets items ([0-9]+) \| already correct ([0-9]+) \| to repoint ([0-9]+) \| file not found anywhere ([0-9]+) \| outside \S+ ([0-9]+)")
UNDO_RE = re.compile(r"undo (?:quarantine|place|reorg) run \S+: ([0-9]+) file\(s\) to put back \| ([0-9]+) playlist\(s\) \| skipped ([0-9]+)")
PURGE_RE = re.compile(r"purge plan: ([0-9]+) run\(s\) to delete \(([^,]*), ([0-9]+) file\(s\)\) \| keeping ([0-9]+) \| older than ([0-9]+) days")
QUAR_ROOT = os.environ.get("MD_QUAR", "/media/_dedupe-quarantine")
REORG_ROOT = os.environ.get("MD_REORG") or os.path.join(os.path.dirname(os.environ.get("MD_MUSIC", "/media/Music").rstrip("/")), "_reorg")
BEETS_ART_RE = re.compile(r"album art paths: ok ([0-9]+) \| to repoint ([0-9]+) \| art file not found ([0-9]+)")

ALLOWED = {
    "playlistfix": {
        "confirm": "FIX PLAYLISTS",
        "title": "Fix playlists that block a removal",
        "needs_login": True,
        "tokens": _tok(*(_GROUP_TOKENS + [r"^--playlist=[^\x00\n\r]{1,300}$", r"^--no-stars$"])),
    },
    "apply": {
        # moves the losing copies into the quarantine (nothing is deleted) and rewrites the .m3u files that pointed at them
        "confirm": "QUARANTINE {n}",
        "title": "Move duplicates to the quarantine",
        "needs_login": False,
        "expect": "apply",                    # the dry run's "would remove N files" must be typed in the confirmation and is enforced by the tool
        "tokens": _tok(*_GROUP_TOKENS),
    },
    "place": {
        # files new music from the _incoming landing folder into the library, sets duplicates aside in the quarantine, writes new playlists
        "confirm": "PLACE {n}",
        "title": "File new music",
        "needs_login": False,
        "plan": False,                        # place plans from the landing folder itself (no plan.json); the fingerprint ties it to the dry run
        "expect": "place",
        "tokens": _tok(r"^--keep-similar$", r"^--upgrade$", r"^--m3u-update=[^\x00\n\r]{1,500}$", r"^--workers=[0-9]{1,2}$", r"^--show=[0-9]{1,4}$"),
    },
    "reorg-apply": {
        # renames files into the Artist/Album (YYYY)/NN - Title layout, rewrites the .m3u files that pointed at them, backs up Navidrome's database first
        "confirm": "MOVE {n}",
        "title": "Apply the folder layout",
        "needs_login": False,
        "plan_file": "reorg-plan.json",       # the layout plan (not the duplicate plan): checked by hash, kept as a copy, and the run uses that copy
        "expect": "reorg",
        "tokens": _tok(r"^--artist=[^\x00\n\r]{1,300}$", r"^--album=[^\x00\n\r]{1,300}$", r"^--limit=[0-9]{1,6}$", r"^--show=[0-9]{1,4}$"),
    },
    "beets-remap": {
        # points the Beets library.db at the files' new paths after a layout run (a database edit: no music file is touched)
        "confirm": "REPOINT {n}",
        "title": "Repoint Beets after a reorg",
        "needs_login": False,
        "plan": False,                        # no plan file: it works from Beets' database, the disk and reorg-moves.tsv; the fingerprint ties it to the dry run
        "expect": "beets",
        "tokens": [],                         # no options at all: which database and which library root are the container's own settings, never a request's
    },
    "undo": {
        # puts a quarantine run (or a place run, or a reorg run) back; the manifest must be one of the runner's own run folders (see manifest_ok)
        "confirm": "RESTORE {n}",
        "title": "Undo a run",
        "needs_login": False,
        "plan": False,
        "expect": "undo",
        "tokens": _tok(r"^--manifest=[^\x00\n\r]{1,500}$"),
        "check": "manifest",
    },
    "purge": {
        # DELETES old quarantine runs for good. The only command here that cannot be undone, so the strictest gate: the exact list of runs
        # is fingerprinted, the limit may not be under 7 days, and the phrase carries the number of runs
        "confirm": "DELETE {n}",
        "title": "Purge old quarantine runs",
        "needs_login": False,
        "plan": False,
        "expect": "purge",
        "tokens": _tok(r"^--older-than=(?:[7-9]|[1-9][0-9]{1,3})$"),
        "check": "purge",
    },
}


def manifest_ok(path):
    """a manifest a real undo may use: manifest.json directly inside ONE run folder of the quarantine or of the reorg root, nothing else"""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or os.path.basename(path) != "manifest.json":
        return False
    real = os.path.realpath(path)
    if not os.path.isfile(real) or os.path.basename(real) != "manifest.json":
        return False
    return any(os.path.dirname(os.path.dirname(real)) == os.path.realpath(root) for root in (QUAR_ROOT, REORG_ROOT))


def check_args(spec, args):
    """option checks that a pattern cannot express: undo takes exactly one manifest, purge at most one limit"""
    kind = spec.get("check")
    if kind == "manifest":
        if len(args) != 1 or not args[0].startswith("--manifest=") or not manifest_ok(args[0][len("--manifest="):]):
            return "undo needs a manifest.json that sits inside a quarantine or reorg run folder"
    elif kind == "purge":
        if len(args) > 1:
            return "purge takes only the age limit"
    return None


def log(msg):
    sys.stderr.write("%s runner: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# ---------------------------------------------------------------------------------------------------------------------
# TOTP (RFC 6238, SHA-1, 30 s, 6 digits): the second factor for arming
# ---------------------------------------------------------------------------------------------------------------------
def totp_code(secret_b32, step):
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    h = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def secret_path():
    return os.path.join(STATE, "totp.key")


def load_secret(create=True, force=False):
    p = secret_path()
    if not force:
        try:
            with open(p, encoding="ascii") as fh:
                s = fh.read().strip()
            if re.match(r"^[A-Z2-7]{32}$", s):
                return s
        except OSError:
            pass
    if not create:
        return None
    os.makedirs(STATE, exist_ok=True)
    s = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(s + "\n")
    os.replace(tmp, p)
    return s


def show_secret(s):
    print("Authenticator-app secret (type it in by hand: Add account > Enter a setup key; time-based, 6 digits, 30 s):")
    print("    " + " ".join(s[i:i + 4] for i in range(0, 32, 4)))
    label = re.sub(r"[^A-Za-z0-9._-]", "", os.environ.get("MD_LABEL", "")) or "console"
    print("or as an address: otpauth://totp/Dewey:%s?secret=%s&issuer=Dewey&digits=6&period=30" % (label, s))


# ---------------------------------------------------------------------------------------------------------------------
def atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def file_sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cred_has_password():
    try:
        with open(os.environ.get("MD_ND_CRED", "/navidrome.cred"), encoding="utf-8") as fh:
            return any(re.match(r"^\s*password\s*=\s*\S", ln) for ln in fh)
    except OSError:
        return False


class Runner:
    def __init__(self):
        self.secret = load_secret()
        self.armed_until = 0.0
        self.last_step = self._load_step()
        self.fails = 0
        self.locked_until = 0.0
        self.proc = None
        self.meta = None
        self.logf = None
        self.term_at = 0.0
        self.used_dry = self._load_used()
        for d in (REQ_DIR, RES_DIR, JOBS_DIR):
            os.makedirs(d, exist_ok=True)

    # -- small state ---------------------------------------------------------------------------------------------
    def _load_used(self):
        try:
            with open(os.path.join(STATE, "used-dry.json"), encoding="utf-8") as fh:
                return list(json.load(fh))[-200:]
        except (OSError, ValueError):
            return []

    def _load_step(self):
        try:
            with open(os.path.join(STATE, "last-step"), encoding="ascii") as fh:
                return int(fh.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _save_step(self):
        try:
            atomic_json(os.path.join(STATE, "last-step"), self.last_step)     # a used code stays used across a restart
        except OSError as e:
            log("could not save last-step: %s" % e)

    def _save_used(self):
        try:
            atomic_json(os.path.join(STATE, "used-dry.json"), self.used_dry[-200:])
        except OSError as e:
            log("could not save used-dry.json: %s" % e)

    def armed(self):
        return time.time() < self.armed_until

    def write_state(self):
        now = time.time()
        atomic_json(os.path.join(QUEUE, "state.json"), {
            "runner": RUNNER_VERSION, "time": now, "armed_until": self.armed_until if self.armed() else 0,
            "arm_minutes": ARM_MIN, "locked_for": max(0, int(self.locked_until - now)),
            "running_job": self.meta["id"] if self.meta else None, "commands": sorted(ALLOWED.keys()),
            "confirm": dict((k, v["confirm"]) for k, v in ALLOWED.items()), "needs_login": dict((k, v["needs_login"]) for k, v in ALLOWED.items()),
            "needs_plan": dict((k, v.get("plan", True)) for k, v in ALLOWED.items()),
            "plan_file": dict((k, v.get("plan_file", "plan.json")) for k, v in ALLOWED.items() if v.get("plan", True)),
            "cred_password": cred_has_password(), "used_dry": self.used_dry[-30:]})

    def reply(self, rid, ok, **kw):
        d = {"ok": ok, "time": time.time()}
        d.update(kw)
        try:
            atomic_json(os.path.join(RES_DIR, rid + ".json"), d)
        except OSError as e:
            log("could not write the reply: %s" % e)

    def web_busy(self):
        try:
            return time.time() - os.stat(os.path.join(QUEUE, "web-busy")).st_mtime < WEB_BUSY_FRESH_S
        except OSError:
            return False

    # -- requests ------------------------------------------------------------------------------------------------
    def poll(self):
        try:
            names = sorted(os.listdir(REQ_DIR))
        except OSError:
            return
        for n in names:
            if not n.endswith(".json") or n.endswith(".tmp"):
                continue
            p = os.path.join(REQ_DIR, n)
            try:
                st = os.lstat(p)
                if not os.path.isfile(p) or os.path.islink(p) or st.st_size > 20000:
                    os.unlink(p)
                    continue
                with open(p, encoding="utf-8") as fh:
                    raw = fh.read()
                os.unlink(p)                      # gone before anything is done with it (a request can carry a password)
                req = json.loads(raw)
            except (OSError, ValueError):
                try:
                    os.unlink(p)
                except OSError:
                    pass
                continue
            rid = req.get("id") if isinstance(req, dict) else None
            if not isinstance(rid, str) or not ID_RE.match(rid):
                continue
            try:
                self.handle(rid, req)
            except Exception as e:                 # a bad request must never stop the loop
                log("request %s failed: %r" % (rid, e))
                self.reply(rid, False, error="the runner could not handle that request")

    def handle(self, rid, req):
        t = req.get("type")
        by = str(req.get("by") or "")[:60]
        if t == "ping":
            return self.reply(rid, True)
        if t == "arm":
            return self.do_arm(rid, str(req.get("code") or ""), by)
        if t == "disarm":
            self.armed_until = 0.0
            log("disarmed by %s" % (by or "?"))
            return self.reply(rid, True)
        if t == "cancel":
            return self.do_cancel(rid, str(req.get("job") or ""), by)
        if t == "job":
            return self.do_job(rid, req, by)
        return self.reply(rid, False, error="unknown request")

    def do_arm(self, rid, code, by):
        now = time.time()
        if now < self.locked_until:
            return self.reply(rid, False, error="arming is locked after wrong codes: try again in %d s" % (self.locked_until - now), locked_for=int(self.locked_until - now))
        if not re.match(r"^[0-9]{6}$", code):
            return self.reply(rid, False, error="type the 6 digits from your authenticator app")
        step_now = int(now // 30)
        ok = False
        for d in (-1, 0, 1):
            step = step_now + d
            if step > self.last_step and hmac.compare_digest(totp_code(self.secret, step), code):
                self.last_step = step                      # a code works once
                self._save_step()
                ok = True
                break
        if not ok:
            self.fails += 1
            log("WRONG arming code from %s (%d/%d)" % (by or "?", self.fails, LOCK_AFTER))
            if self.fails >= LOCK_AFTER:
                self.fails = 0
                self.locked_until = now + LOCK_SECONDS
                return self.reply(rid, False, error="wrong code: arming is locked for %d minutes" % (LOCK_SECONDS // 60), locked_for=LOCK_SECONDS)
            return self.reply(rid, False, error="that code is not right (or was already used): wait for the next one and try again")
        self.fails = 0
        self.armed_until = now + ARM_MIN * 60
        log("ARMED for %d minutes by %s" % (ARM_MIN, by or "?"))
        return self.reply(rid, True, armed_until=self.armed_until)

    def do_cancel(self, rid, jid, by):
        if not self.proc or not self.meta or self.meta["id"] != jid:
            return self.reply(rid, False, error="that job is not running here")
        self.meta["cancelled"] = True
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        self.term_at = time.time()
        log("cancel requested by %s" % (by or "?"))
        return self.reply(rid, True)

    def do_job(self, rid, req, by):
        cmd = str(req.get("cmd") or "")
        spec = ALLOWED.get(cmd)
        if not spec:
            return self.reply(rid, False, error="the runner does not run %r" % cmd)
        if not self.armed():
            return self.reply(rid, False, error="write access is not armed (arm it first, with the code from your authenticator app)")
        if self.proc is not None:
            return self.reply(rid, False, error="a real run is already in progress")
        if self.web_busy():
            return self.reply(rid, False, error="a console job is running: wait for it to finish")
        dry = str(req.get("dry_job") or "")
        if not JOB_ID_RE.match(dry):
            return self.reply(rid, False, error="no dry run was named")
        if dry in self.used_dry:
            return self.reply(rid, False, error="that dry run already authorised a real run: do a fresh dry run")
        try:
            with open(os.path.join(JOBS_DIR, dry + ".json"), encoding="utf-8") as fh:
                dm = json.load(fh)
        except (OSError, ValueError):
            return self.reply(rid, False, error="that dry run was not found")
        if dm.get("cmd") != cmd or dm.get("mode") != "dry" or dm.get("state") != "done" or dm.get("rc") != 0 or dm.get("runner"):
            return self.reply(rid, False, error="that is not a finished dry run of %s" % cmd)
        try:
            fin = dt.datetime.strptime(dm.get("finished") or "", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return self.reply(rid, False, error="that dry run has no finish time")
        if (dt.datetime.now() - fin).total_seconds() > DRY_MAX_AGE_S:
            return self.reply(rid, False, error="that dry run is more than 6 hours old: do a fresh one")
        n, flags = None, []
        if spec.get("expect"):
            info = self.dry_info(spec["expect"], dry)
            if info is None:
                return self.reply(rid, False, error="the dry run's output does not say what it would do: do a fresh dry run")
            n, flags = info["n"], info["flags"]
            if n == 0:
                return self.reply(rid, False, error="that dry run would change nothing: there is nothing to do")
        need = spec["confirm"].replace("{n}", str(n)) if n is not None else spec["confirm"]
        if str(req.get("confirm") or "") != need:
            return self.reply(rid, False, error="type %s exactly to confirm" % need)
        plan_bytes, sha = None, None
        if spec.get("plan", True):
            # read the plan once: this exact copy is what gets checked AND what the command runs on
            try:
                with open(os.path.join(OUT, spec.get("plan_file", "plan.json")), "rb") as fh:
                    plan_bytes = fh.read()
            except OSError:
                return self.reply(rid, False, error="there is no %s" % spec.get("plan_file", "plan.json"))
            sha = hashlib.sha256(plan_bytes).hexdigest()
            if not dm.get("plan_sha") or dm["plan_sha"] != sha:
                return self.reply(rid, False, error="the plan changed since that dry run (a new %s?): do a fresh dry run" % ("layout plan" if spec.get("plan_file") else "scan"))
        args = dm.get("args")                 # the dry run's options (the console records the command line without the command name)
        if not isinstance(args, list) or len(args) > 40 or any(not isinstance(a, str) for a in args):
            return self.reply(rid, False, error="the dry run's options are not usable")
        for a in args:
            if not any(r.match(a) for r in spec["tokens"]):
                return self.reply(rid, False, error="option %r is not allowed for a real run" % a[:60])
        bad = check_args(spec, args)
        if bad:
            return self.reply(rid, False, error=bad)
        pw = req.get("nd_pass")
        if spec["needs_login"] and not cred_has_password():
            if not isinstance(pw, str) or not pw or len(pw) > 200 or any(c in pw for c in "\x00\r\n"):
                return self.reply(rid, False, error="the Navidrome password is needed (used for this run only)")
        else:
            pw = None
        argv = [cmd] + list(args) + ["--execute"]
        argv += flags
        self.start(rid, cmd, spec, argv, dry, by, sha, pw, plan_bytes)

    # -- running -------------------------------------------------------------------------------------------------
    def dry_info(self, kind, dry):
        """what the dry run said it would do: {"n": files (or database rows) that would change, "flags": the --expect-... option for the real run}, or None.
        The summary is among the first lines of the log (the layout dry run prints it before its long list), place's fingerprint is among the last."""
        try:
            with open(os.path.join(JOBS_DIR, dry + ".log"), "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        head, tail = data[:30000].decode("utf-8", "replace"), data[-30000:].decode("utf-8", "replace")
        if kind == "apply":
            m = SUMMARY_RE.search(head)
            return {"n": int(m.group(2)), "flags": ["--expect-remove=%d" % int(m.group(2))]} if m else None
        if kind == "place":
            m, f = PLACE_RE.search(head), FP_RE.findall(tail)
            if not m or not f:
                return None
            return {"n": int(m.group(2)) + int(m.group(3)), "flags": ["--expect-plan=" + f[-1]]}      # files that would move: placed + set aside
        if kind == "reorg":
            m = REORG_RE.search(head)
            return {"n": int(m.group(1)), "flags": ["--expect-moves=%d" % int(m.group(1))]} if m else None
        if kind == "beets":
            m, a, f = BEETS_RE.search(head), BEETS_ART_RE.search(head), FP_RE.findall(head + tail)
            if not m or not a or not f:
                return None
            return {"n": int(m.group(3)) + int(a.group(2)), "flags": ["--expect-plan=" + f[-1]]}      # rows that would change: items + album art paths
        if kind == "undo":
            m, f = UNDO_RE.search(head), FP_RE.findall(head)
            return {"n": int(m.group(1)), "flags": ["--expect-plan=" + f[-1]]} if m and f else None      # files that would be put back
        if kind == "purge":
            m, f = PURGE_RE.search(head), FP_RE.findall(head)
            return {"n": int(m.group(1)), "flags": ["--expect-plan=" + f[-1]]} if m and f else None      # runs that would be deleted
        return None

    def snapshot(self, jid, plan_bytes):
        """keep the exact plan this run uses (also an audit record); only the newest 40 are kept"""
        d = os.path.join(STATE, "plans")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, jid + "-plan.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(plan_bytes)
        try:
            old = sorted(os.listdir(d))[:-40]
            for n in old:
                os.unlink(os.path.join(d, n))
        except OSError:
            pass
        return path

    def start(self, rid, cmd, spec, argv, dry, by, sha, pw, plan_bytes):
        now = dt.datetime.now()
        jid = "%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), cmd)
        snap = None
        if plan_bytes is not None:
            try:
                snap = self.snapshot(jid, plan_bytes)
            except OSError as e:
                return self.reply(rid, False, error="could not keep a copy of the plan: %s" % e)
        if snap:
            argv = list(argv) + ["--plan=" + snap]
        meta = {"id": jid, "cmd": cmd, "title": spec["title"] + " (real run)", "args": argv, "started": now.strftime("%Y-%m-%d %H:%M:%S"),
                "t0": time.time(), "state": "running", "rc": None, "finished": None, "mode": "write", "runner": True, "dry_job": dry, "plan_sha": sha, "plan_copy": snap}
        if by:
            meta["by"] = by
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1"})
        env.pop("MD_ND_PASS", None)
        if pw:
            env["MD_ND_PASS"] = pw
        try:
            self.logf = open(os.path.join(JOBS_DIR, jid + ".log"), "wb")
            self.logf.write(("$ dewey %s\n(real run: write access armed, dry run %s, started by %s)\n\n" % (" ".join(argv), dry, by or "?")).encode("utf-8"))
            self.logf.flush()
            self.proc = subprocess.Popen([sys.executable, "-u", APP] + argv, stdin=subprocess.DEVNULL, stdout=self.logf, stderr=subprocess.STDOUT,
                                         env=env, start_new_session=True, cwd=OUT)
        except OSError as e:
            self.proc = None
            return self.reply(rid, False, error="could not start: %s" % e)
        finally:
            pw = None
            env.pop("MD_ND_PASS", None)
        self.meta = meta
        self.used_dry.append(dry)
        self._save_used()
        atomic_json(os.path.join(JOBS_DIR, jid + ".json"), meta)
        log("REAL RUN started: %s (by %s, dry run %s)" % (" ".join(argv), by or "?", dry))
        self.write_state()                          # say "busy" at once, so the console does not start a job beside it
        self.reply(rid, True, job=jid)

    def tick_job(self):
        if not self.proc:
            return
        rc = self.proc.poll()
        if rc is None:
            if self.meta.get("cancelled") and self.term_at and time.time() - self.term_at > 6:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            return
        try:
            self.logf.close()
        except (OSError, AttributeError):
            pass
        m = self.meta
        m["rc"] = rc
        m["state"] = "cancelled" if m.get("cancelled") else ("done" if rc == 0 else "failed")
        m["finished"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        m["seconds"] = round(time.time() - m["t0"], 1)
        try:
            atomic_json(os.path.join(JOBS_DIR, m["id"] + ".json"), m)
        except OSError as e:
            log("could not save the job record: %s" % e)
        log("real run %s finished: %s (exit %s)" % (m["id"], m["state"], rc))
        self.proc, self.meta, self.logf, self.term_at = None, None, None, 0.0

    def clean_replies(self):
        try:
            for n in os.listdir(RES_DIR):
                p = os.path.join(RES_DIR, n)
                if time.time() - os.stat(p).st_mtime > 600:
                    os.unlink(p)
        except OSError:
            pass

    def loop(self):
        log("up (version %d). Arming lasts %d minutes. Commands a real run may use: %s" % (RUNNER_VERSION, ARM_MIN, ", ".join(sorted(ALLOWED))))
        n = 0
        while True:
            try:
                self.poll()
                self.tick_job()
                self.write_state()
                n += 1
                if n % 60 == 0:
                    self.clean_replies()
            except Exception as e:
                log("loop error: %r" % e)
            time.sleep(1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "secret":
        show_secret(load_secret())
        return
    if len(sys.argv) > 1 and sys.argv[1] == "newsecret":
        show_secret(load_secret(force=True))
        print("The old secret no longer works. Restart the runner container (.\\dewey.ps1 runner) so it picks this one up.")
        return
    first = load_secret(create=False) is None
    r = Runner()
    if first:
        log("first start: made the authenticator secret. Show it with:  .\\dewey.ps1 runner secret")
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    r.loop()


if __name__ == "__main__":
    main()
