#!/usr/bin/env python3
"""
check_public.py -- stops personal or secret details from reaching the public repository.

    python3 tools/check_public.py              check this folder; exit code 1 if anything is found
    python3 tools/check_public.py --list       also print which files were checked
    python3 tools/check_public.py --install-hook   run this check automatically before every  git commit

What it looks for
  * generic patterns that never belong in a public tree: private network addresses, Windows/Linux home folders with a
    real user name, e-mail addresses, *.ts.net host names, tokens and keys, authenticator secrets
  * files that must stay on your own machine: .env, web.env, navidrome.cred, dewey.config.psd1, rules.txt, *.db, *.zip, out/, ...
  * YOUR OWN words: put one per line in  private-denylist.txt  (next to this script's parent folder; it is git-ignored, so it
    never leaves your machine). A line is a plain text (case-insensitive), or  re:<regular expression>.
    Good entries: your NAS address, your user names, your host name, family names, playlist names, artists you only
    use in rules.
  * when the folder is a git repository: a commit author e-mail that is not a no-reply address

It checks the files git would commit (tracked and new, minus .gitignore). Outside git, it checks every file except those
named in .gitignore. A line that is a deliberate example can end with  leak-ok  to be skipped.
Standard library only. Read-only: it never changes anything except with --install-hook.
"""
import fnmatch
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DENYLIST = os.environ.get("DEWEY_DENYLIST") or os.path.join(ROOT, "private-denylist.txt")

# example values that may appear in docs and examples
OK_IPS = {"192.168.1.50", "192.168.1.60", "192.168.1.10", "127.0.0.1", "0.0.0.0"}
OK_EMAILS = re.compile(r"(@example\.(com|org|net)$|^noreply@|@users\.noreply\.github\.com$)", re.I)
OK_HOMES = {"you", "user", "username", "name", "yourname", "your-name", "example", "me", "public", "default", "runner",
            "$user", "%username%", "<you>", "<user>", "<name>", "home", "x", "u"}

RULES = [
    ("private-ip", re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})\b")),
    ("tailnet-host", re.compile(r"\b[a-z0-9][a-z0-9-]*\.[a-z0-9-]+\.ts\.net\b", re.I)),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("windows-home", re.compile(r"[A-Za-z]:\\Users\\([^\\\s'\"<>|]+)", re.I)),
    ("unix-home", re.compile(r"(?<![A-Za-z0-9_.-])/(?:home|Users)/([^/\s'\"<>|:]+)")),
    ("token", re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,})\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("otp-secret", re.compile(r"secret=[A-Z2-7]{16,}", re.I)),
    ("password-value", re.compile(r"(?im)^\s*(?:password|MD_WEB_PASS(?:_\d)?)\s*=\s*(?!change-this|<|\$|\{|\s*$)(\S{4,})")),
]

# files that must never be committed, whatever their content
FORBIDDEN_NAMES = [".env", "web.env", "navidrome.cred", "dewey.config.psd1", "rules.txt", "sync-music.ignore.txt",
                   "private-denylist.txt", "totp.key", "BUILD-DIARY.md", "StackOverview*.html", "*.db", "*.db-*", "*.sqlite*",
                   "*.zip", "*.tgz", "*.tar", "*.tar.gz", "*.key", "*.pem", "*.m3u", "*.m3u8", "*.flac", "*.mp3", "*.m4a"]
FORBIDDEN_DIRS = {"out", "data", "state", "queue", "__pycache__", ".vscode", ".idea"}
SKIP_DIRS = {".git", "node_modules"}


def read_gitignore():
    pats = []
    p = os.path.join(ROOT, ".gitignore")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("!"):
                pats.append(line)
    return pats


def ignored(rel, pats):
    parts = rel.split("/")
    for pat in pats:
        if pat.endswith("/"):
            if pat[:-1] in parts[:-1]:
                return True
        elif fnmatch.fnmatch(parts[-1], pat) or fnmatch.fnmatch(rel, pat):
            return True
    return False


def file_list():
    """(files to check, how we found them)"""
    if os.path.isdir(os.path.join(ROOT, ".git")):
        try:
            out = subprocess.run(["git", "-C", ROOT, "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True, check=True).stdout
            return sorted(x for x in out.split("\n") if x and os.path.isfile(os.path.join(ROOT, x))), "git"
        except (OSError, subprocess.CalledProcessError):
            pass
    pats = read_gitignore()
    files = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), ROOT).replace(os.sep, "/")
            if not ignored(rel, pats):
                files.append(rel)
    return sorted(files), "folder"


def load_denylist():
    terms = []
    if os.path.exists(DENYLIST):
        for line in open(DENYLIST, encoding="utf-8", errors="replace"):
            line = line.rstrip("\n").strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("re:"):
                terms.append((line, re.compile(line[3:], re.I)))
            else:
                terms.append((line, re.compile(re.escape(line), re.I)))
    return terms


def main():
    show = "--list" in sys.argv
    if "--install-hook" in sys.argv:
        hooks = os.path.join(ROOT, ".git", "hooks")
        if not os.path.isdir(hooks):
            print("no .git folder here: run  git init  first")
            return 1
        hp = os.path.join(hooks, "pre-commit")
        with open(hp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\n# installed by tools/check_public.py\n"
                     "for py in python3 python py; do\n"
                     "  if command -v \"$py\" >/dev/null 2>&1 && \"$py\" -c 'import sys' >/dev/null 2>&1; then exec \"$py\" tools/check_public.py; fi\n"
                     "done\n"
                     "echo 'pre-commit: no working python found, so the leak check could not run' >&2\n"
                     "exit 1\n")
        os.chmod(hp, 0o755)
        print("pre-commit hook installed: every commit now runs tools/check_public.py first")
        return 0

    files, how = file_list()
    deny = load_denylist()
    findings = []

    for rel in files:
        name = os.path.basename(rel)
        parts = rel.split("/")
        if any(p in FORBIDDEN_DIRS for p in parts[:-1]):
            findings.append((rel, 0, "forbidden-folder", "files from this folder must stay on your machine"))
            continue
        if any(fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_NAMES):
            findings.append((rel, 0, "forbidden-file", "this file must stay on your machine (see .gitignore)"))
            continue
        path = os.path.join(ROOT, rel)
        try:
            raw = open(path, "rb").read()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            if len(raw) > 2_000_000:
                findings.append((rel, 0, "big-binary", "large binary file"))
            continue
        if show:
            print("  checked", rel)
        text = raw.decode("utf-8", errors="replace")
        for n, line in enumerate(text.split("\n"), 1):
            if "leak-ok" in line:
                continue
            for term, rx in deny:
                if rx.search(line):
                    findings.append((rel, n, "your-denylist", "matches %s" % term))
            for rule, rx in RULES:
                for m in rx.finditer(line):
                    val = m.group(0)
                    if rule == "private-ip" and val in OK_IPS:
                        continue
                    if rule == "email" and OK_EMAILS.search(val):
                        continue
                    if rule in ("windows-home", "unix-home") and m.group(1).lower() in OK_HOMES:
                        continue
                    if rule == "tailnet-host" and re.search(r"(your-tailnet|example)", val, re.I):
                        continue
                    findings.append((rel, n, rule, line.strip()[:110] if rule != "your-denylist" else "line hidden"))

    if how == "git":
        try:
            nm = subprocess.run(["git", "-C", ROOT, "config", "user.name"], capture_output=True, text=True).stdout.strip()
            for term, rx in deny:
                if nm and rx.search(nm):
                    findings.append(("(git config)", 0, "author-name", "commits would carry the name %r, which matches your denylist: use  git config user.name <a public name>" % nm))
            em = subprocess.run(["git", "-C", ROOT, "config", "user.email"], capture_output=True, text=True).stdout.strip()
            if em and not OK_EMAILS.search(em):
                findings.append(("(git config)", 0, "author-email", "commits would carry the address %s: use  git config user.email <id>+<name>@users.noreply.github.com" % em))
        except OSError:
            pass

    if not os.path.exists(os.path.join(ROOT, "LICENSE")):
        print("note: there is no LICENSE file yet (add one before the repository goes public)")
    if not deny:
        print("note: no private-denylist.txt, so only the generic patterns were checked (add your own names there)")
    if findings:
        print("\nFOUND %d problem(s) in %d file(s) (checked %d files, %s):" % (len(findings), len({f[0] for f in findings}), len(files), how))
        for rel, n, rule, what in findings:
            print("  %s%s  [%s]  %s" % (rel, (":%d" % n) if n else "", rule, what))
        print("\nFix these (or, for a deliberate example line, end it with  leak-ok ) and run the check again.")
        return 1
    print("clean: %d files checked (%s), nothing personal found" % (len(files), how))
    return 0


if __name__ == "__main__":
    sys.exit(main())
