import os as _os, subprocess, sys, json, shutil, tempfile
REPO = _os.environ.get("DEWEY_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
R = sys.argv[1]
# Suggested rules: a scan writes suggested-rules.txt / suggestions.json for the groups waiting for a decision, and the
# lines it suggests really do settle those groups when they are put into rules.txt.
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:700]) if (extra and not cond) else ""))
    fails += 0 if cond else 1


sys.path.insert(0, _os.path.join(REPO, "demo"))
import make_demo  # noqa: E402

M, OUT, RULES = R + "/media/Music", R + "/out", R + "/rules.txt"
_os.makedirs(M, exist_ok=True)
_os.makedirs(R + "/media/_dedupe-quarantine", exist_ok=True)
A_DEF = "Test Artist/Real Album (1996)/1-06 - Shared Song (ft. Guest Person).flac"   # kept by default: own album, earlier release
A_OTHER = "Test Artist/Compilation Hits (Explicit Version) (1998)/02 - Shared Song.flac"
B1, B2 = "Other Artist/First (2001)/01 - Odd Length.flac", "Other Artist/Second (2003)/01 - Odd Length.flac"
# same recording filed on a compilation (folder says Explicit) and on the artist's own album (no label), 4 s apart
make_demo.tone(M + "/" + A_OTHER, 330, 12, "Test Artist", "Shared Song", "Compilation Hits (Explicit Version)", 2, "1998")
make_demo.tone(M + "/" + A_DEF, 330, 8, "Test Artist", "Shared Song", "Real Album", 6, "1996")
# same title, lengths far apart: never a duplicate pair, so only "keep every copy" can apply
make_demo.tone(M + "/" + B1, 440, 8, "Other Artist", "Odd Length", "First", 1, "2001")
make_demo.tone(M + "/" + B2, 440, 40, "Other Artist", "Odd Length", "Second", 1, "2003")
for d in (OUT,):
    _os.makedirs(d, exist_ok=True)
env = dict(_os.environ, MD_MUSIC=M, MD_OUT=OUT, MD_QUAR=R + "/media/_dedupe-quarantine", MD_RULES=RULES)
env.pop("MD_NDDB", None)


def tool(*a):
    q = subprocess.run([sys.executable, _os.path.join(REPO, "dewey.py")] + list(a), env=env, capture_output=True, text=True)
    return q.returncode, q.stdout + q.stderr


def scan():
    rc, t = tool("scan", "--tolerance", "5", "--wide-tolerance", "10")
    sug = json.load(open(OUT + "/suggestions.json", encoding="utf-8"))["groups"]
    return rc, t, {x["title"]: x for x in sug}


def lines(o):
    return o["lines"]


open(RULES, "w").write("")
rc, t, sug = scan()
check("scan runs and says it wrote suggested rules", rc == 0 and "suggested rules for" in t and "suggested-rules.txt" in t, t[-700:])
a, b = sug.get("Shared Song"), sug.get("Odd Length")
check("both groups waiting for a decision get suggestions", bool(a) and bool(b), list(sug))
txt = open(OUT + "/suggested-rules.txt", encoding="utf-8").read()
check("suggested-rules.txt has only comment lines, so pasting it whole changes nothing",
      all(l.startswith("#") or not l.strip() for l in txt.splitlines()) and "keep-both-id: " + a["group"] in txt.replace("# ", ""), txt[:600])
ids = [o["id"] for o in a["options"]] if a else []
check("label-mismatch group: keep both, treat as duplicates, keep the OTHER copy (never the one already kept)",
      ids == ["keep-both", "dedupe", "keep-file:" + A_OTHER], ids)
keep_opt = next((o for o in a["options"] if o["id"].startswith("keep-file:")), {})
check("keep-file option names the copy by its path and carries the dedupe-labels line it needs",
      lines(keep_opt) == ["keep-file: " + A_OTHER, "dedupe-labels: Test Artist | Shared Song"], lines(keep_opt))
check("the reason is shown in plain words", "label differs" in a["why"] and "audio check" in a["why"], a["why"])
check("far-apart lengths: only 'keep every copy' is offered, with the explanation",
      [o["id"] for o in b["options"]] == ["keep-both"] and "wide-tolerance" in b["note"], b)

# apply the keep-file suggestion, rescan: the other copy now wins and the kept copy becomes removable
open(RULES, "w").write("\n".join(lines(keep_opt)) + "\n")
rc, t, sug2 = scan()
check("after the suggested keep-file + dedupe-labels lines the song is settled: one copy removable, no suggestion left",
      "removable 1 files" in t and "Shared Song" not in sug2, t[-500:])
plan = json.load(open(OUT + "/plan.json", encoding="utf-8"))
roles = {}
for g in plan["groups"] if "groups" in plan else plan:
    if g.get("title") == "Shared Song":
        roles = {m["path"]: m["role"] for m in g["members"]}
check("the copy the rule names is kept, the default winner now goes", roles.get(A_OTHER) == "keep" and roles.get(A_DEF) == "remove", roles)

# a rule that is already in rules.txt is not suggested again
open(RULES, "w").write("keep-file: " + A_OTHER + "\n")
rc, t, sug3 = scan()
a3 = sug3.get("Shared Song")
ids3 = [o["id"] for o in a3["options"]] if a3 else []
check("with only keep-file set, 'treat as duplicates' is still offered and keep-file is not repeated",
      a3 is not None and "dedupe" in ids3 and not any(i == "keep-file:" + A_OTHER for i in ids3)
      and any(o["id"] == "keep-file:" + A_DEF and lines(o) == ["keep-file: " + A_DEF, "dedupe-labels: Test Artist | Shared Song"] for o in a3["options"]), a3)

# keep-both settles the far-apart pair
open(RULES, "w").write("keep-both-id: " + b["group"] + "\n")
rc, t, sug4 = scan()
check("keep-both-id removes the far-apart pair from the suggestions", "Odd Length" not in sug4, list(sug4))
open(RULES, "w").write("keep-both-id: " + b["group"] + "\nkeep-both-id: " + a["group"] + "\n")
rc, t, sug5 = scan()
check("with every group settled the suggestions file lists none", sug5 == {} and "0 group(s)" in open(OUT + "/suggested-rules.txt", encoding="utf-8").read(), sug5)
files = sorted(_os.path.join(d, f) for d, _s, fs in _os.walk(M) for f in fs)
check("scanning never changed the library", len(files) == 4 and not _os.listdir(R + "/media/_dedupe-quarantine"), files)
shutil.rmtree(R + "/media", ignore_errors=True)
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
