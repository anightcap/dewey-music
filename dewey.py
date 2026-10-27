#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dewey -- keeps a music library in order: finds "like" tracks, reports them, and cleans
them up safely (quarantine, never delete); also plans the folder layout, files new music and audits tags.

Commands
  scan    Read-only. Walks the library, reads tags, finds duplicate groups and
          writes report.html / dupes.csv / plan.json / inventory.json to --out.
  apply   Reads plan.json. DRY RUN unless --execute. Moves "loser" files into a
          quarantine folder (outside the library), rewrites .m3u playlists that
          pointed at them, writes a manifest so everything can be undone.
  playlistfix
          Clears BLOCKED files: swaps the loser for the kept copy (same position)
          in the Navidrome playlists that hold it, via Navidrome's API (synced
          .m3u playlists: by rewriting the .m3u). DRY RUN unless --execute; backs
          every playlist up first; --restore puts them back. Login comes from
          navidrome.cred (url / user / password), never from the command line.
  reorg   Read-only. Works out where every file belongs in the one layout
          Artist/Album (YYYY)/NN - Title.ext (tags decide; nothing is guessed from
          file names) and writes reorg-plan.json / .csv / reorg-report.html.
  reorg-apply
          Moves the files of the reorg plan (DRY RUN unless --execute; --artist,
          --album and --limit pick a batch). Move-only: tags are never changed.
          Journals every move, backs up navidrome.db and the .m3u files first.
  tagaudit
          Read-only. Lists every file and album that lacks tags, cover art or lyrics
          (tagaudit.csv, tagaudit-albums.csv). Counts embedded pictures, cover/folder/front
          images in the album folder, embedded lyrics and .lrc/.txt files next to the song.
  compare Read-only. Compares two files by their decoded audio: same version, or a real clean/explicit pair?
          (scan uses the same check when two copies carry different Explicit/Clean labels.)
  undo    Puts a quarantine run (or a reorg run) back exactly where it came from.
  purge   Permanently deletes quarantine runs older than N days.

Matching tiers
  exact    same artist + same title (version labels like "Remastered" ignored),
           durations within --tolerance seconds. Different files, same song.
  wide     same as exact, but the copies differ by more than --tolerance and at
           most --wide-tolerance seconds (remasters, fade-outs). Reported; only
           applied when you approve it (apply --group ID, or --kinds ...,wide).
  variant  same song, different edit label: Radio Edit / Extended / Single...
           Which one survives is set by prefer-order (rules.txt).
  review   same artist + title but something is off (lengths disagree, generic
           title like "Intro", ...). Reported, never touched automatically.
  similar  titles almost match (Lovin' / Loving) and lengths match. Reported only.
  exempt   matched a keep-both / protect rule. Reported only.

Works on Python 3.8+. Needs: pip install mutagen   (the clean/explicit audio check also needs numpy + ffmpeg; without them it is skipped)
"""

import argparse
import atexit
import csv
import datetime as dt
import copy
import difflib
import getpass
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

try:
    import mutagen
except ImportError:  # pragma: no cover
    mutagen = None

VERSION = "1.12.8"
CACHE_RV = 2   # bump when read_one() output changes meaning

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav",
             ".wma", ".aac", ".ape", ".wv", ".aif", ".aiff"}
LOSSLESS_EXT = {".flac", ".wav", ".ape", ".wv", ".aif", ".aiff"}
PLAYLIST_EXT = {".m3u", ".m3u8"}
JUNK_EXT = {".jpg", ".jpeg", ".png", ".gif", ".nfo", ".lrc", ".txt", ".log",
            ".cue", ".ini", ".db"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
SKIP_DIRS = {"@eadir", "#recycle", ".git", "playlists", "_dedupe-quarantine"}
# what the file extension claims, for spotting files whose content is something else
EXT_FMT = {".flac": "flac", ".mp3": "mp3", ".m4a": "m4a", ".mp4": "m4a", ".ogg": "ogg",
           ".oga": "ogg", ".opus": "ogg", ".wav": "wav"}
LOSSLESS_REAL = {"flac", "wav", "ape"}

LAYOUT_RANK = {"year-album": 3, "artist-album": 2, "artist-only": 1, "loose": 0}
YEAR_DIR = re.compile(r"^\[(\d{4})\]\s+(.+)$")

# Titles that are too generic to trust as "same song" on name alone.
GENERIC_TITLES = {"intro", "outro", "interlude", "skit", "untitled", "prelude",
                  "overture", "finale", "reprise", "track", "bonus", "unknown",
                  "hidden track", "silence"}
BAD_ARTISTS = {"", "variousartists", "various", "unknown", "unknownartist", "va"}

# ---------------------------------------------------------------------------
# text normalisation
# ---------------------------------------------------------------------------
_APOS = re.compile(r"[’‘`´']")


def fold(s):
    """lowercase, accent-free, punctuation-free, single-spaced."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold().replace("&", " and ")
    s = _APOS.sub("", s)
    s = re.sub(r"[\W_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compact(s):
    return fold(s).replace(" ", "")


ARTIST_SPLIT = re.compile(
    r"\s*;\s*|\s+/\s+|\s*,\s*|\s+(?:feat\.?|ft\.?|featuring|with|vs\.?|x)\s+", re.I)


def primary_artist(a):
    parts = [p for p in ARTIST_SPLIT.split((a or "").strip()) if p and p.strip()]
    return parts[0] if parts else (a or "").strip()


def artist_key(a):
    k = compact(primary_artist(a))
    if k.startswith("the") and len(k) > 5:
        k = k[3:]
    return k


# --- version / descriptor parsing -----------------------------------------
FEAT_PAREN = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+[^\)\]]*[\)\]]", re.I)
FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)
SEG = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")

# words that make a bracket/suffix segment a *descriptor* rather than title text
IGNORE_WORDS = {"remaster", "remastered", "remasterd", "re", "digital", "digitally",
                "stereo", "mono", "deluxe", "edition", "version", "bonus", "track",
                "explicit", "clean", "censored", "original", "album", "anniversary",
                "expanded", "special", "collectors", "lp", "mix", "soundtrack",
                "motion", "picture", "and", "the", "ost", "mastered"}
DISTINCT_RX = re.compile(
    r"\b(live|acoustic|unplugged|demo|instrumental|karaoke|a ?cappella|acapella|"
    r"remix|rmx|rework|redo|bootleg|mashup|dub|vip|cover|tribute|orchestral|"
    r"piano|stripped|nightcore|sped|slowed|reverb|medley|reprise|club|vocal|"
    r"sessions?|rehearsal|skit|take \d+)\b")
HINT_RX = re.compile(
    r"\b(version|mix|edit|edited|remix|rmx|remaster\w*|live|acoustic|demo|"
    r"instrumental|karaoke|cappella|acapella|explicit|clean|deluxe|bonus|original|"
    r"single|album|radio|extended|mono|stereo|unplugged|rework|dub|vip|cover|"
    r"soundtrack|edition|sessions?|reprise|medley|mashup|bootleg|tribute|orchestral|"
    r"stripped|sped|slowed|nightcore|reverb|club|vocal|cut|skit|take \d+|ost|"
    r"short|long|full length|full version|7|12)\b")
EDIT_PATTERNS = [
    ("radio edit", re.compile(r"radio(?: edit| version| mix| cut)?")),
    ("single", re.compile(r"(?:7|7 inch|single)(?: version| edit| mix)?")),
    ("edit", re.compile(r"(?:edit|edited|edited version|short|short version|short edit)")),
    ("video", re.compile(r"video(?: version| edit| mix)")),
    ("extended", re.compile(
        r"(?:extended|long|full length|full)(?: version| mix| edit)?|12|12 inch(?: version| mix)?")),
]


def classify_segment(text):
    """-> None (part of the title) | ('edit', name) | ('ignore', None) | ('distinct', text)"""
    t = fold(text)
    if not t:
        return None
    if re.match(r"from\b", t):
        return ("ignore", None)
    if not HINT_RX.search(t):
        return None
    if DISTINCT_RX.search(t):
        return ("distinct", t)
    for name, rx in EDIT_PATTERNS:
        if rx.fullmatch(t):
            return ("edit", name)
    toks = t.split()
    if all(tok in IGNORE_WORDS or re.fullmatch(r"\d{4}", tok) for tok in toks):
        return ("ignore", None)
    return ("distinct", t)


def parse_title(raw):
    """-> (base_spaced, edit_token, distinct_tuple)"""
    s = (raw or "").replace("–", " - ").replace("—", " - ")
    s = FEAT_PAREN.sub("", s)
    edits, distinct = [], []

    def take(seg):
        c = classify_segment(seg)
        if c is None:
            return False
        if c[0] == "edit":
            edits.append(c[1])
        elif c[0] == "distinct":
            distinct.append(c[1])
        return True

    # trailing " - something" suffixes
    while " - " in s:
        head, tail = s.rsplit(" - ", 1)
        if take(tail):
            s = head
        else:
            break
    # bracketed segments
    def repl(m):
        return "" if take(m.group(1)) else m.group(0)
    s = SEG.sub(repl, s)
    s = FEAT_TAIL.sub("", s)
    base = fold(s)
    edit = "+".join(sorted(set(edits)))
    return base, edit, tuple(sorted(set(distinct)))


FEAT_CAP_PAREN = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]*)[\)\]]", re.I)
FEAT_CAP_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+(.*)$", re.I)
FEAT_NAME_SPLIT = re.compile(r"\s*(?:,|;|&|/|\band\b|\bx\b)\s*", re.I)


def feat_set(title, artist_tag):
    """the guest artists a song credits, as a set of compact names: from '(feat. A & B)' in the title and from the artists after the
    first one in the artist tag. Used to tell 'Into You (feat. Tamia)' from 'Into You (feat. Ashanti)': two recordings, not duplicates."""
    names = set()
    t = title or ""
    for m in FEAT_CAP_PAREN.finditer(t):
        names.update(compact(x) for x in FEAT_NAME_SPLIT.split(m.group(1)) if x.strip())
    t2 = FEAT_CAP_PAREN.sub("", t)
    m = FEAT_CAP_TAIL.search(t2)
    if m:
        names.update(compact(x) for x in FEAT_NAME_SPLIT.split(m.group(1)) if x.strip())
    parts = [p for p in ARTIST_SPLIT.split((artist_tag or "").strip()) if p and p.strip()]
    names.update(compact(x) for x in parts[1:])
    names.discard("")
    return frozenset(names)


def feat_conflict(a, b):
    """True when both credit guests and neither list contains the other ('feat. Tamia' vs 'feat. Ashanti'). One side crediting nobody
    is common tag sloppiness for the same recording, and 'A' vs 'A & B' is the same song credited fully or not, so neither counts."""
    return bool(a) and bool(b) and not (a <= b or b <= a)


def parse_filename(stem, folder_artist):
    """Fallback when tags are missing: 'NN - Artist - Title' / 'Title - Artist'."""
    s = re.sub(r"^\d{1,3}\s*[-._)]\s*", "", stem).strip()
    parts = [p.strip() for p in s.split(" - ")]
    if len(parts) == 1:
        return folder_artist or "", parts[0]
    fa = compact(folder_artist) if folder_artist else None
    if fa and compact(parts[-1]) == fa:
        return parts[-1], " - ".join(parts[:-1])
    if fa and compact(parts[0]) == fa:
        return parts[0], " - ".join(parts[1:])
    return parts[0], " - ".join(parts[1:])


# ---------------------------------------------------------------------------
# rules file
# ---------------------------------------------------------------------------
class Rules:
    def __init__(self):
        self.keep_both = set()        # (artist_key, base_key)
        self.keep_both_ids = set()
        self.prefer_edit = {}         # (artist_key, base_key) -> edit token to keep for that one song
        self.label_mode = "keep-both"  # clean/explicit labels differ -> leave both ("dedupe" = treat as duplicates)
        self.keep_album = False       # prefer the artist's own album over a compilation/soundtrack copy (ahead of playlists/plays)
        self.keep_played = False      # prefer the copy with Navidrome plays over the copy in more playlists (plays are not carried over)
        self.place_upgrade = False    # place: an incoming copy of HIGHER audio quality (FLAC over MP3) is added next to the library copy
        self.audio_check = True       # when labels differ, compare the decoded audio: identical audio = same version = plain duplicate
        self.dedupe_labels = set()    # (artist_key, base_key) songs where label differences should still be deduped
        self.keep_files = set()
        self.protect_paths = []
        self.protect_artists = set()
        self.skip_paths = []
        self.prefer_order = ["", "radio edit", "single", "edit", "video", "extended"]
        self.tolerance = None
        self.wide_tolerance = None
        self.reorg_skip = []          # path prefixes the reorg leaves where they are
        self.reorg_various = set()    # compact(album) of albums that belong under Various Artists
        self.reorg_unknown_album = ""  # folder name for files with no album tag ("" = leave them alone)
        self.reorg_artist = {}        # rg_key(alias) -> canonical folder name
        self.reorg_set = {}           # relative path -> {artist, album, year, title, track}: placed by hand, tags untouched
        self.reorg_names = True       # tag-less files in the [YYYY] Artist - Album/Title - Artist layout: use the names
        self.source = None

    def rank(self, token):
        first = (token or "").split("+")[0]
        try:
            return self.prefer_order.index(first)
        except ValueError:
            return len(self.prefer_order)


def load_rules(path):
    r = Rules()
    if not path or not os.path.exists(path):
        return r
    r.source = path
    with open(path, encoding="utf-8-sig") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                print("rules.txt line %d ignored (no key:): %s" % (n, line))
                continue
            key, val = [x.strip() for x in line.split(":", 1)]
            key = key.lower()
            if key == "keep-both" and "|" in val:
                a, t = [x.strip() for x in val.split("|", 1)]
                r.keep_both.add((artist_key(a), compact(parse_title(t)[0])))
            elif key == "clean-explicit":
                r.label_mode = "dedupe" if val.strip().lower().startswith("dedupe") else "keep-both"
            elif key == "keep-album":
                r.keep_album = val.strip().lower() in ("yes", "on", "true", "1")
            elif key == "keep-played":
                r.keep_played = val.strip().lower() in ("yes", "on", "true", "1")
            elif key == "place-upgrade":
                r.place_upgrade = val.strip().lower() in ("yes", "on", "true", "1")
            elif key == "audio-check":
                r.audio_check = val.strip().lower() not in ("off", "no", "false", "0")
            elif key == "dedupe-labels" and "|" in val:
                a, t = [x.strip() for x in val.split("|", 1)]
                r.dedupe_labels.add((artist_key(a), compact(parse_title(t)[0])))
            elif key == "keep-both-id":
                r.keep_both_ids.add(val.lower())
            elif key == "prefer-edit" and "|" in val and "=" in val:
                left, tok = val.rsplit("=", 1)
                a, t = [x.strip() for x in left.split("|", 1)]
                tok = tok.strip().lower()
                r.prefer_edit[(artist_key(a), compact(parse_title(t)[0]))] = (
                    "" if tok in ("plain", "album", "original") else tok)
            elif key == "keep-file":
                r.keep_files.add(val.replace("\\", "/"))
            elif key == "protect-path":
                r.protect_paths.append(val.replace("\\", "/"))
            elif key == "protect-artist":
                r.protect_artists.add(artist_key(val))
            elif key == "skip-path":
                r.skip_paths.append(val.replace("\\", "/"))
            elif key == "prefer-order":
                order = []
                for x in val.split(","):
                    x = x.strip().lower()
                    order.append("" if x in ("plain", "album", "original", "") else x)
                r.prefer_order = order
            elif key == "reorg-skip":
                r.reorg_skip.append(val.replace("\\", "/").strip("/"))
            elif key == "reorg-various":
                r.reorg_various.add(compact(val))
            elif key == "reorg-unknown-album":
                r.reorg_unknown_album = val
            elif key == "reorg-names":
                r.reorg_names = val.strip().lower() not in ("off", "no", "false", "0")
            elif key == "reorg-set" and "|" in val:
                f = [x.strip() for x in val.split("|")]
                f += [""] * (6 - len(f))
                try:
                    yr = int(f[3]) if f[3] else 0
                    tr = int(f[5]) if f[5] else 0
                except ValueError:
                    print("rules.txt line %d: reorg-set needs a number for year / track" % n)
                    continue
                r.reorg_set[f[0].replace("\\", "/").strip("/")] = {"artist": f[1], "album": f[2], "year": yr, "title": f[4], "track": tr}
            elif key == "reorg-artist" and "|" in val:
                alias, canon = [x.strip() for x in val.split("|", 1)]
                r.reorg_artist[rg_key(alias)] = canon
            elif key == "tolerance":
                r.tolerance = float(val)
            elif key == "wide-tolerance":
                r.wide_tolerance = float(val)
            else:
                print("rules.txt line %d: unknown key '%s'" % (n, key))
    return r


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------
def first_str(tags, *keys):
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v:
            if isinstance(v, (list, tuple)):
                v = v[0] if v else ""
            v = str(v).strip()
            if v:
                return v
    return ""


def sniff_format(path):
    """What the file really is, judged from its first bytes ('' if unsure)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
            if head[:3] == b"ID3" and len(head) >= 10:
                # a leading ID3 tag may sit in front of a FLAC as well as an MP3
                size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
                fh.seek(10 + size)
                nxt = fh.read(4)
                if nxt == b"fLaC":
                    return "flac"
                return "mp3"
    except OSError:
        return ""
    if head[:4] == b"fLaC":
        return "flac"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"MAC ":
        return "ape"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return ""


def open_as(path, fmt):
    """Open a file as the format its CONTENT says it is, whatever its name says."""
    if fmt == "mp3":
        from mutagen.mp3 import EasyMP3
        return EasyMP3(path)
    if fmt == "m4a":
        from mutagen.easymp4 import EasyMP4
        return EasyMP4(path)
    if fmt == "flac":
        from mutagen.flac import FLAC
        return FLAC(path)
    if fmt == "ogg":
        from mutagen.oggvorbis import OggVorbis
        try:
            return OggVorbis(path)
        except Exception:
            from mutagen.oggopus import OggOpus
            return OggOpus(path)
    if fmt == "wav":
        from mutagen.wave import WAVE
        return WAVE(path)
    return None


def flac_streaminfo_only(path):
    """FLAC whose tag blocks are damaged: read just the STREAMINFO block (length, bit depth, rate)."""
    import io
    from mutagen.flac import StreamInfo
    with open(path, "rb") as fh:
        head = fh.read(10)
        fh.seek(0)
        if head[:3] == b"ID3":
            size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
            fh.seek(10 + size)
        if fh.read(4) != b"fLaC":
            raise ValueError("not a FLAC stream")
        hdr = fh.read(4)
        if len(hdr) < 4 or (hdr[0] & 0x7F) != 0:
            raise ValueError("no STREAMINFO block")
        data = fh.read(int.from_bytes(hdr[1:4], "big"))
    return StreamInfo(io.BytesIO(data))


def read_one(path):
    out = {"duration": 0.0, "bitrate": 0, "sample_rate": 0, "bits": 0,
           "channels": 0, "codec": "", "artist": "", "albumartist": "",
           "title": "", "album": "", "year": 0, "track": 0, "disc": 0,
           "comp": False, "mbid_rec": "", "mbid_rel": "", "isrc": "",
           "acoustid": "", "error": "", "real_fmt": "", "mislabeled": "",
           "note": "", "rv": CACHE_RV}
    ext = os.path.splitext(path)[1].lower()
    real = sniff_format(path)
    claimed = EXT_FMT.get(ext, "")
    out["real_fmt"] = real
    if real and claimed and real != claimed:
        out["mislabeled"] = real           # e.g. MP3/AAC data inside a ".flac" file
    f, err = None, ""
    if out["mislabeled"]:
        # the name lies, so don't let mutagen trust it: open by content
        try:
            f = open_as(path, real)
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
    if f is None and not err:
        try:
            f = mutagen.File(path, easy=True)
        except Exception as e:  # corrupt / unsupported
            err = "%s: %s" % (type(e).__name__, e)
        if f is None and not err:
            err = "unreadable"
    if f is None and real and not out["mislabeled"] and err:
        try:                                # last chance: open by content
            f = open_as(path, real)
            err = ""
        except Exception:
            pass
    if f is None:
        if real == "flac":
            try:                            # tags damaged but the audio header is fine
                si = flac_streaminfo_only(path)
                info = si
                out["note"] = "tag blocks unreadable - length read from the FLAC header, tags ignored"
                out["duration"] = float(getattr(info, "length", 0) or 0)
                out["sample_rate"] = int(getattr(info, "sample_rate", 0) or 0)
                out["bits"] = int(getattr(info, "bits_per_sample", 0) or 0)
                out["channels"] = int(getattr(info, "channels", 0) or 0)
                out["codec"] = "flac"
                return out
            except Exception:
                pass
        out["error"] = err or "unreadable"
        return out
    info = f.info
    out["duration"] = float(getattr(info, "length", 0) or 0)
    out["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
    out["sample_rate"] = int(getattr(info, "sample_rate", 0) or 0)
    out["bits"] = int(getattr(info, "bits_per_sample", 0) or 0)
    out["channels"] = int(getattr(info, "channels", 0) or 0)
    out["codec"] = str(getattr(info, "codec", "") or "")
    tags = f.tags or {}
    out["artist"] = first_str(tags, "artist")
    out["albumartist"] = first_str(tags, "albumartist")
    out["title"] = first_str(tags, "title")
    out["album"] = first_str(tags, "album")
    m = re.search(r"\d{4}", first_str(tags, "date", "originaldate", "year"))
    out["year"] = int(m.group(0)) if m else 0
    m = re.match(r"\d+", first_str(tags, "tracknumber"))
    out["track"] = int(m.group(0)) if m else 0
    m = re.match(r"\d+", first_str(tags, "discnumber"))
    out["disc"] = int(m.group(0)) if m else 0
    out["comp"] = first_str(tags, "compilation") in ("1", "true", "True")
    out["mbid_rec"] = first_str(tags, "musicbrainz_trackid")
    out["mbid_rel"] = first_str(tags, "musicbrainz_releasetrackid")
    out["isrc"] = first_str(tags, "isrc")
    out["acoustid"] = first_str(tags, "acoustid_id")
    return out


def layout_of(rel):
    parts = rel.split("/")
    if len(parts) == 1:
        return "loose"
    if YEAR_DIR.match(parts[0]):
        return "year-album"
    if len(parts) == 2:
        return "artist-only"
    return "artist-album"


def folder_artist_of(rel):
    parts = rel.split("/")
    if len(parts) == 1:
        return ""
    m = YEAR_DIR.match(parts[0])
    if m:
        return m.group(2).split(" - ")[0].split(";")[0].strip()
    return parts[0]


class Track:
    pass


def make_track(rel, d, rules):
    t = Track()
    t.rel = rel
    t.ext = os.path.splitext(rel)[1].lower()
    t.size = d["size"]
    t.mtime = d["mtime"]
    t.duration = d["duration"]
    t.layout = layout_of(rel)
    stem = os.path.splitext(os.path.basename(rel))[0]
    fa = folder_artist_of(rel)
    artist = d["artist"] or d["albumartist"]
    title = d["title"]
    t.tags_missing = not (artist and title)
    if t.tags_missing:
        fa_guess, fn_title = parse_filename(stem, fa)
        artist = artist or fa_guess
        title = title or fn_title
    t.artist = artist
    t.title = title
    t.album = d["album"]
    t.albumartist = d["albumartist"]
    t.tag_artist = d["artist"]          # the raw tags (reorg never falls back to file names)
    t.tag_title = d["title"]
    t.feat = feat_set(d["title"], d["artist"])
    t.comp_tag = bool(d["comp"])
    t.disc = d["disc"]
    t.real_fmt = d.get("real_fmt", "")
    t.year = d["year"]
    t.track = d["track"]
    t.comp = bool(d["comp"]) or bool(re.search(
        r"\b(greatest hits|essential|best of|soundtrack|now that|compilation|collection)\b",
        (d["album"] or "").lower())) or fold(d["albumartist"]) == "various artists"
    t.mbid_rec = d["mbid_rec"]
    t.acoustid = d["acoustid"]
    real = d.get("real_fmt", "")
    claimed_fmt = EXT_FMT.get(t.ext, "")
    # judged fresh from the sniffed format and the extension (an older cache entry could hold a stale flag)
    t.mislabeled = real if (real and claimed_fmt and real != claimed_fmt) else ""
    t.note = d.get("note", "")
    if real:
        # judge by what is inside the file, not by what its name claims
        lossless = real in LOSSLESS_REAL or (real == "m4a" and d["codec"].lower() == "alac")
        shown = real.upper()
    else:
        lossless = t.ext in LOSSLESS_EXT or (t.ext in (".m4a", ".mp4") and d["codec"].lower() == "alac")
        shown = t.ext[1:].upper()
    t.lossless = lossless
    if lossless:
        t.quality = (1, d["bits"], d["sample_rate"], 0)
        t.fmt = "%s %s/%.4g" % (shown, d["bits"] or "?", (d["sample_rate"] or 0) / 1000.0)
    else:
        t.quality = (0, 0, 0, int(d["bitrate"] / 1000))
        t.fmt = "%s %dk" % (shown, int(d["bitrate"] / 1000))
    if t.mislabeled:
        t.fmt += " (named %s)" % t.ext
    t.has_lrc = bool(d.get("has_lrc"))
    t.error = d["error"]
    t.completeness = sum(1 for x in (d["album"], d["albumartist"], d["year"], d["mbid_rec"], d["track"]) if x)
    base, edit, distinct = parse_title(title)
    t.base = base
    t.base_key = base.replace(" ", "")
    t.edit_key = edit
    t.distinct_key = "|".join(distinct)
    t.artist_key = artist_key(artist)
    t.matchable = bool(t.artist_key and t.base_key and t.artist_key not in BAD_ARTISTS
                       and t.duration > 0 and not t.error)
    t.generic = base in GENERIC_TITLES or bool(re.fullmatch(r"track \d+", base))
    t.plays = 0
    t.starred = False
    t.rating = 0
    t.nd_id = None
    t.pl_nd = set()
    t.pl_m3u = set()
    return t


def scan_library(music, cache_path, workers, rules, log):
    entries = []
    leftovers = []     # hidden audio files (download fragments etc.) - reported, never scanned
    lrc = set()
    t0 = time.time()
    skip_prefixes = tuple(p.strip("/") + "/" for p in rules.skip_paths)
    for root, dirs, files in os.walk(music):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        rel_root = os.path.relpath(root, music).replace(os.sep, "/")
        if rel_root == ".":
            rel_root = ""
        if skip_prefixes and (rel_root + "/").startswith(skip_prefixes):
            dirs[:] = []
            continue
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            rel = (rel_root + "/" + name) if rel_root else name
            if ext == ".lrc":
                lrc.add(rel)
                continue
            if ext not in AUDIO_EXT or name.startswith("._"):
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            if name.startswith("."):
                leftovers.append((rel, st.st_size, st.st_mtime))
                continue
            entries.append((rel, st.st_size, st.st_mtime))
    log("found %d audio files (%.1fs)%s" % (len(entries), time.time() - t0,
        ("; %d hidden leftover files set aside" % len(leftovers)) if leftovers else ""))

    con = sqlite3.connect(cache_path)
    con.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, data TEXT)")
    cached = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT path,size,mtime,data FROM files")}
    todo, results = [], {}
    for rel, size, mtime in entries:
        c = cached.get(rel)
        if c and c[0] == size and abs(c[1] - mtime) < 0.001:
            d = json.loads(c[2])
            # entries from before v1.1 are fine unless they failed (the reader got smarter about those)
            if d.get("rv") == CACHE_RV or (not d.get("error") and d.get("duration", 0) > 0):
                results[rel] = d
                continue
        todo.append((rel, size, mtime))
    log("%d cached, %d to read" % (len(results), len(todo)))
    if todo and mutagen is None:
        sys.exit("mutagen is not installed (pip install mutagen)")

    def work(item):
        rel, size, mtime = item
        d = read_one(os.path.join(music, rel))
        d["size"], d["mtime"] = size, mtime
        return rel, d

    done = 0
    t0 = time.time()
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            batch = []
            for rel, d in ex.map(work, todo):
                results[rel] = d
                batch.append((rel, d["size"], d["mtime"], json.dumps(d)))
                done += 1
                if len(batch) >= 500:
                    con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", batch)
                    con.commit()
                    batch = []
                if done % 1000 == 0:
                    rate = done / max(time.time() - t0, 0.001)
                    log("  read %d / %d (%.0f files/s)" % (done, len(todo), rate))
            if batch:
                con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", batch)
                con.commit()
    live = set(results)
    stale = [p for p in cached if p not in live]
    if stale:
        con.executemany("DELETE FROM files WHERE path=?", [(p,) for p in stale])
        con.commit()
    con.close()
    for rel, d in results.items():
        stem = os.path.splitext(rel)[0]
        d["has_lrc"] = (stem + ".lrc") in lrc
    return results, leftovers


# ---------------------------------------------------------------------------
# playlists
# ---------------------------------------------------------------------------
def read_text_guess(path):
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
        enc = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        enc = "latin-1"
    return text, enc, bom, ("\r\n" in text)


def resolve_entry(line, m3u_rel_dir, music):
    p = line.strip().replace("\\", "/")
    if not p or p.startswith("#"):
        return None
    mp = music.replace("\\", "/").rstrip("/") + "/"
    if posixpath.isabs(p) or re.match(r"^[A-Za-z]:/", p):
        for pref in (mp, "/music/", "/volume1/Media/Music/", "/media/Music/"):
            if p.startswith(pref):
                return p[len(pref):]
        return None
    full = posixpath.normpath(posixpath.join(m3u_rel_dir, p))
    if full.startswith(".."):
        return None
    return full


def find_playlists(music, extra_dirs):
    out = []
    dirs = [os.path.join(music, "Playlists")] + list(extra_dirs or [])
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if os.path.splitext(name)[1].lower() in PLAYLIST_EXT:
                out.append(os.path.join(d, name))
    return out


def index_m3u(music, playlists):
    refs = defaultdict(set)
    for pth in playlists:
        text = read_text_guess(pth)[0]
        rel_dir = os.path.relpath(os.path.dirname(pth), music).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        name = os.path.splitext(os.path.basename(pth))[0]
        for line in text.splitlines():
            r = resolve_entry(line, rel_dir, music)
            if r:
                refs[r].add(name)
    return refs


def rewrite_m3u(path, music, mapping, keep_dups=False):
    """mapping: loser_rel -> winner_rel. Returns (new_text_info or None, replaced, dropped).
    keep_dups=True (reorg: pure moves) never drops an entry, so intentional repeats in a playlist survive."""
    text, enc, bom, crlf = read_text_guess(path)
    rel_dir = os.path.relpath(os.path.dirname(path), music).replace(os.sep, "/")
    rel_dir = "" if rel_dir == "." else rel_dir
    lines = text.splitlines()
    resolved = [resolve_entry(l, rel_dir, music) for l in lines]
    present = {r for r in resolved if r}
    out, replaced, dropped = [], 0, 0
    pending = []  # buffered #EXTINF / comment lines preceding an entry
    for line, r in zip(lines, resolved):
        if r is None:
            if line.strip().startswith("#EXTINF") or (line.strip().startswith("#") and pending):
                pending.append(line)
            else:
                out.extend(pending)
                pending = []
                out.append(line)
            continue
        if r in mapping:
            w = mapping[r]
            if w in present and not keep_dups:
                dropped += 1          # winner already in this playlist: just drop the loser
                pending = []
                continue
            present.add(w)
            newp = posixpath.relpath(w, rel_dir or ".")
            lstem = posixpath.splitext(posixpath.basename(r))[0]
            wstem = posixpath.splitext(posixpath.basename(w))[0]
            for pi, pline in enumerate(pending):
                if pline.startswith("#EXTINF") and pline.endswith("," + lstem):
                    pending[pi] = pline[:-len(lstem)] + wstem
            out.extend(pending)
            pending = []
            out.append(newp)
            replaced += 1
        else:
            out.extend(pending)
            pending = []
            out.append(line)
    out.extend(pending)
    if not (replaced or dropped):
        return None, 0, 0
    nl = "\r\n" if crlf else "\n"
    body = nl.join(out) + nl
    data = body.encode("utf-8" if enc == "utf-8" else "latin-1")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return data, replaced, dropped


# ---------------------------------------------------------------------------
# Navidrome (read-only copy of its sqlite db)
# ---------------------------------------------------------------------------
_ND_SNAP = {}


def _nd_snap_cleanup():
    for d in list(_ND_SNAP.values()):
        shutil.rmtree(d, ignore_errors=True)
    _ND_SNAP.clear()


atexit.register(_nd_snap_cleanup)


def nd_connect(db_path, log=None):
    """Read-only connection to a private copy of navidrome.db. The copy (database + its write-ahead log, which is where
    Navidrome's newest data lives) is made ONCE per run and shared by everything in the run that reads Navidrome: on a big
    library it is gigabytes, and copying it used to be most of a playlist-fix or scan. The -shm file is not copied (SQLite
    rebuilds it), and the connection is read-only, so closing it does not write the log back into the copy."""
    d = _ND_SNAP.get(db_path)
    if d is None:
        d = tempfile.mkdtemp(prefix="md_nd_")
        _ND_SNAP[db_path] = d
        base, t0, parts = os.path.basename(db_path), time.time(), []
        for suf in ("", "-wal"):
            src = db_path + suf
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(d, base + suf))
                parts.append(("database" if not suf else "write-ahead log", os.path.getsize(src)))
        secs = max(time.time() - t0, 0.001)
        total = sum(s for _, s in parts)
        if log:
            log("Navidrome: copied %s in %.0f s (%.0f MB/s)" % (
                " + ".join("%s %.0f MB" % (n, s / 1048576.0) for n, s in parts), secs, total / 1048576.0 / secs))
    path = os.path.join(d, os.path.basename(db_path))
    try:
        return sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(path)), uri=True)
    except sqlite3.Error:
        return sqlite3.connect(path)


def load_navidrome(db_path, rel_index, log):
    info, playlists = {}, defaultdict(set)
    try:
        con = nd_connect(db_path, log)

        def cols(table):
            return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]

        mf = cols("media_file")
        if "path" not in mf or "id" not in mf:
            log("Navidrome: media_file has no id/path column (%s) - skipping" % mf)
            return info, playlists, False
        id2rel = {}
        for nid, p in con.execute("SELECT id, path FROM media_file"):
            parts = (p or "").replace("\\", "/").lstrip("/").split("/")
            for i in range(len(parts)):
                c = "/".join(parts[i:])
                if c in rel_index:
                    id2rel[nid] = c
                    break
        total = con.execute("SELECT COUNT(*) FROM media_file").fetchone()[0]
        log("Navidrome: mapped %d of its %d tracks to files on disk" % (len(id2rel), total))
        if not id2rel:
            log("Navidrome: NONE of its tracks matched a file under the music folder (wrong database, or a path "
                "layout this tool can't map). Playlist protection is OFF - not using this database.")
            con.close()
            return {}, defaultdict(set), False
        if len(id2rel) < 0.9 * total:
            log("Navidrome: WARNING - %d tracks in its database have no matching file (deleted files, or a stale "
                "database copy). Playlist protection only covers the ones that matched." % (total - len(id2rel)))
        for nid, rel in id2rel.items():
            info[rel] = {"id": nid, "plays": 0, "starred": False, "rating": 0}
        try:
            ac = cols("annotation")
            if {"item_id", "item_type", "play_count"} <= set(ac):
                sel = "SELECT item_id, SUM(play_count), %s, %s FROM annotation WHERE item_type='media_file' GROUP BY item_id" % (
                    "MAX(starred)" if "starred" in ac else "0",
                    "MAX(rating)" if "rating" in ac else "0")
                for iid, pc, st, rt in con.execute(sel):
                    rel = id2rel.get(iid)
                    if rel:
                        info[rel]["plays"] = int(pc or 0)
                        info[rel]["starred"] = bool(st)
                        info[rel]["rating"] = int(rt or 0)
        except sqlite3.Error as e:
            log("Navidrome: could not read play counts (%s)" % e)
        try:
            pc = cols("playlist")
            ptc = cols("playlist_tracks")
            if "id" in pc and "name" in pc and {"playlist_id", "media_file_id"} <= set(ptc):
                names = {pid: n for pid, n in con.execute("SELECT id, name FROM playlist")}
                n_links = 0
                for pid, mid in con.execute("SELECT playlist_id, media_file_id FROM playlist_tracks"):
                    rel = id2rel.get(mid)
                    if rel and pid in names:
                        playlists[rel].add(names[pid])
                        n_links += 1
                log("Navidrome: %d playlist entries point at library files" % n_links)
        except sqlite3.Error as e:
            log("Navidrome: could not read playlists (%s)" % e)
        con.close()
        return info, playlists, True
    except sqlite3.Error as e:
        log("Navidrome: database read failed (%s) - continuing without it" % e)
        return info, playlists, False


# ---------------------------------------------------------------------------
# audio comparison -- used ONLY when two copies of a song carry different (or missing) Explicit/Clean labels.
# A label is just text in a file name, so it cannot say which version the audio really is. A clean edit changes the
# audio (words muted / bleeped), so two copies whose decoded audio matches throughout are the same version and are
# ordinary duplicates. Anything else (a local difference, a different master, an unreadable file) keeps both copies.
# Needs ffmpeg + numpy; without them the check is skipped and both copies stay, exactly as before.
# ---------------------------------------------------------------------------
AUDIO = {"check": None}      # set by scan: callable(kept_track, other_track) -> {"verdict": ..., "why": ...}


class AudioUnavailable(Exception):
    pass


def _audio_np():
    try:
        import numpy as np
    except ImportError:
        raise AudioUnavailable("numpy is not installed in the image (rebuild it: MD_REBUILD=1)")
    if not shutil.which("ffmpeg"):
        raise AudioUnavailable("ffmpeg is not installed in the image (rebuild it: MD_REBUILD=1)")
    return np


A_SR, A_WIN, A_HOP = 16000, 1024, 512            # 16 kHz mono, 32 ms frames
A_FRAME_S = A_HOP / float(A_SR)
A_DEV_DB = 4.0                                    # a frame counts as 'different' when its band profile is this many dB off
A_SAME_RUN = 2                                    # ...and 3 such frames in a row (~96 ms, a short word) is a real difference


def _decode_mono(path, np):
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(A_SR),
                        "-f", "f32le", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if p.returncode != 0 or len(p.stdout) < A_SR * 4:
        raise ValueError("could not decode %s" % os.path.basename(path))
    return np.frombuffer(p.stdout, dtype=np.float32)


def _band_db(x, np, nb=14, fmin=150.0, fmax=6000.0):
    """frames x bands of log energy (dB), 150 Hz-6 kHz: lossy encodes and different dithers barely move this range."""
    n = 1 + (len(x) - A_WIN) // A_HOP
    idx = np.arange(A_WIN)[None, :] + A_HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(A_WIN), axis=1)) ** 2
    freqs = np.fft.rfftfreq(A_WIN, 1.0 / A_SR)
    edges = np.geomspace(fmin, fmax, nb + 1)
    cols = [spec[:, (freqs >= lo) & (freqs < hi)].sum(axis=1) for lo, hi in zip(edges[:-1], edges[1:])]
    return 10.0 * np.log10(np.stack(cols, axis=1) + 1e-10)


def _fmt_t(frames):
    sec = int(frames * A_FRAME_S)
    return "%d:%02d" % (sec // 60, sec % 60)


def _xcorr_align(xa, xb, np, max_s=2.0):
    """Shift the two signals into line at sample level (cross-correlation of 20 s from the middle)."""
    n = min(len(xa), len(xb))
    seg = min(n, 20 * A_SR)
    mid = max(0, n // 2 - seg // 2)
    a = xa[mid:mid + seg] - xa[mid:mid + seg].mean()
    b = xb[mid:mid + seg] - xb[mid:mid + seg].mean()
    nfft = 1 << (2 * seg - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(b, nfft)), nfft)
    m = min(int(max_s * A_SR), seg - 1)
    cc = np.concatenate((c[-m:], c[:m + 1]))          # lags -m .. +m ; lag > 0: a[n + lag] matches b[n]
    lag = int(np.argmax(cc)) - m
    if lag > 0:
        xa = xa[lag:]
    elif lag < 0:
        xb = xb[-lag:]
    n = min(len(xa), len(xb))
    return xa[:n], xb[:n], lag


def compare_audio(path_a, path_b, np=None):
    """Return {"verdict": same|local|broad|error, "why": text, ...}. Only 'same' is ever acted on."""
    np = np or _audio_np()
    xa, xb = _decode_mono(path_a, np), _decode_mono(path_b, np)
    la, lb = len(xa) / float(A_SR), len(xb) / float(A_SR)
    if abs(la - lb) > 1.5:
        return {"verdict": "broad", "why": "lengths differ by %.1fs" % abs(la - lb)}
    xa, xb, lag_samples = _xcorr_align(xa, xb, np)     # different rips often start a few ms (or a few hundred) apart
    A, B = _band_db(xa, np), _band_db(xb, np)
    best = None
    for lag in range(-2, 3):                            # tiny residual from the frame grid
        a = A[max(lag, 0):]
        b = B[max(-lag, 0):]
        n = min(len(a), len(b))
        if n < 200:
            continue
        sc = float(np.abs(a[:n] - b[:n]).mean())
        if best is None or sc < best[0]:
            best = (sc, lag)
    if best is None:
        return {"verdict": "error", "why": "too little audio to compare (needs about 7 seconds)"}
    lag = best[1]
    a, b = A[max(lag, 0):], B[max(-lag, 0):]
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    loud = (a.mean(axis=1) > a.mean(axis=1).max() - 40) | (b.mean(axis=1) > b.mean(axis=1).max() - 40)
    loud[:10] = False
    loud[-10:] = False                                  # ignore the very start / end (fades, padding)
    if loud.sum() < 100:
        return {"verdict": "error", "why": "too little audio to compare"}
    off = np.median((a - b)[loud], axis=0)              # constant gain / tone differences between rips
    d = np.abs(a - b - off).mean(axis=1)
    d[~loud] = 0.0
    median_d = float(np.median(d[loud]))
    dev = d > A_DEV_DB
    # merge single-frame gaps, then find the longest run
    run = best_run = start = best_start = 0
    gap = 0
    for i, v in enumerate(dev):
        if v:
            if run == 0:
                start = i
            run += 1
            gap = 0
        elif run and gap < 1:
            gap += 1
            run += 1
        else:
            if run - gap > best_run:
                best_run, best_start = run - gap, start
            run = gap = 0
    if run - gap > best_run:
        best_run, best_start = run - gap, start
    dev_frac = float(dev[loud].mean())
    info = {"median_db": round(median_d, 2), "longest_run_ms": int(best_run * A_FRAME_S * 1000),
            "deviating_pct": round(dev_frac * 100, 2), "lag_ms": int(lag_samples * 1000 / A_SR)}
    if median_d <= 3.0 and best_run <= A_SAME_RUN:
        info.update(verdict="same", why="decoded audio matches throughout (median %.1f dB apart, longest mismatch %d ms)" % (
            median_d, info["longest_run_ms"]))
    elif median_d <= 3.0:
        info.update(verdict="local", why="same recording but %d ms of it differs around %s (a muted/bleeped word?)" % (
            info["longest_run_ms"], _fmt_t(best_start)))
    else:
        info.update(verdict="broad", why="audio differs broadly (median %.1f dB apart): different master or source" % median_d)
    return info


def make_audio_checker(music, out, log):
    """Cached comparer for scan. Results are keyed on both files' size+mtime, so a rescan only decodes new pairs."""
    cache_path = os.path.join(out, "audio-compare.json")
    cache = {}
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        pass
    st = {"off": None, "new": 0, "dirty": False}

    def check(a, b):
        key = "%s|%d|%d||%s|%d|%d" % (a.rel, a.size, int(a.mtime), b.rel, b.size, int(b.mtime))
        if key in cache:
            return cache[key]
        if st["off"]:
            return {"verdict": "unavailable", "why": st["off"]}
        try:
            np = _audio_np()
        except AudioUnavailable as e:
            st["off"] = str(e)
            log("audio check skipped: %s - copies with different labels stay as they are" % st["off"])
            return {"verdict": "unavailable", "why": st["off"]}
        try:
            res = compare_audio(os.path.join(music, a.rel), os.path.join(music, b.rel), np)
        except Exception as e:                      # unreadable / odd file: never guess
            res = {"verdict": "error", "why": "audio check failed (%s)" % str(e)[:80]}
        cache[key] = res
        st["new"] += 1
        st["dirty"] = True
        return res

    def finish():
        if st["dirty"]:
            try:
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh)
            except OSError:
                pass
        return st["new"], st["off"]

    return check, finish


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------
class UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def fmt_dur(sec):
    sec = int(round(sec))
    return "%d:%02d" % (sec // 60, sec % 60)


def keeper_parts(t, rules):
    """(label, value) pairs in ranking order; the first difference between two copies decides who is kept."""
    played = getattr(rules, "keep_played", False)
    album = getattr(rules, "keep_album", False)
    pl = ("in more playlists", max(len(t.pl_nd), len(t.pl_m3u)))
    pp = ("more plays", t.plays)
    notcomp = ("not a compilation/soundtrack", 0 if t.comp else 1)
    parts = [
        ("a keep-file rule", 1 if t.rel in rules.keep_files else 0),
        ("higher audio quality", t.quality),
        ("starred/rated in Navidrome", (10 if t.starred else 0) + int(t.rating or 0)),
    ]
    if album:
        parts.append(("on the artist's own album (not a compilation/soundtrack)", notcomp[1]))
    parts += [pp, pl] if played else [pl, pp]
    parts += [
        ("tidier folder layout", LAYOUT_RANK[t.layout]),
        notcomp,
        ("more complete tags", t.completeness + (1 if t.has_lrc else 0)),
        ("earlier release", -(t.year or 9999)),
        ("larger file", t.size),
        ("original file name (not a '(2)' copy)", 0 if re.search(r" \(\d\)\.[A-Za-z0-9]+$", t.rel) else 1),
    ]
    return parts


def keeper_key(t, rules):
    return tuple(v for _, v in keeper_parts(t, rules))


def why_kept(winner, loser, rules):
    for (label, x), (_, y) in zip(keeper_parts(winner, rules), keeper_parts(loser, rules)):
        if x != y:
            return label
    return "tie -> path order"


EXPL_RX = re.compile(r"[\(\[]\s*(explicit|clean|censored)\b[^\)\]]*[\)\]]", re.I)


def adv_label(rel, title):
    """'explicit' / 'clean' / 'censored' when the file name or title says so, else ''."""
    m = EXPL_RX.search(rel or "") or EXPL_RX.search(title or "")
    return m.group(1).lower() if m else ""


def group_id(artist_k, base_k, distinct_k):
    return hashlib.sha1(("%s|%s|%s" % (artist_k, base_k, distinct_k)).encode("utf-8")).hexdigest()[:8]


def build_groups(tracks, rules, tol, nd_available, wide=None, peel=False):
    wide = max(wide if wide is not None else tol, tol)
    uf = UF(len(tracks))
    by_key = defaultdict(list)
    by_mbid = defaultdict(list)
    by_acoust = defaultdict(list)
    for i, t in enumerate(tracks):
        if not t.matchable:
            continue
        by_key[(t.artist_key, t.base_key, t.distinct_key)].append(i)
        if t.mbid_rec:
            by_mbid[(t.artist_key, t.mbid_rec)].append(i)
        if t.acoustid:
            by_acoust[(t.artist_key, t.acoustid)].append(i)
    for lst in by_key.values():
        for j in lst[1:]:
            uf.union(lst[0], j)
    for table in (by_mbid, by_acoust):
        for lst in table.values():
            for j in lst[1:]:
                if abs(tracks[j].duration - tracks[lst[0]].duration) <= wide:
                    uf.union(lst[0], j)
    comps = defaultdict(list)
    for i, t in enumerate(tracks):
        if t.matchable:
            comps[uf.find(i)].append(t)

    groups, grouped = [], set()
    for members in comps.values():
        if len(members) < 2:
            continue
        for m in members:
            grouped.add(m.rel)
        if peel:
            groups.extend(classify_with_peel(members, rules, tol, nd_available, wide))
        else:
            groups.append(classify_group(members, rules, tol, nd_available, wide))

    groups.extend(similar_groups(tracks, grouped, tol))
    groups.sort(key=lambda g: (g["artist"].lower(), g["title"].lower(), g["id"]))
    return groups


def classify_group(members, rules, tol, nd_available, wide=None, id_suffix=""):
    wide = max(wide if wide is not None else tol, tol)
    members = sorted(members, key=lambda t: t.rel)
    ak = Counter(m.artist_key for m in members).most_common(1)[0][0]
    bk = Counter(m.base_key for m in members).most_common(1)[0][0]
    dk = Counter(m.distinct_key for m in members).most_common(1)[0][0]
    base_gid = group_id(ak, bk, dk)
    gid = group_id(ak, bk, dk + id_suffix) if id_suffix else base_gid
    names = Counter(primary_artist(m.artist) for m in members)
    artist = max(names, key=lambda n: (names[n], sum(ord(c) > 127 for c in n), len(n)))
    plain = [m for m in members if not m.edit_key] or members
    title_disp = min((m.title for m in plain), key=lambda s: (len(s), s))
    g = {"id": gid, "artist": artist, "title": title_disp, "kind": None, "note": "", "members": []}

    def entry(t, role, reason="", winner=None, blocked_by=None):
        return {
            "path": t.rel, "role": role, "reason": reason, "winner": winner,
            "blocked_by": blocked_by or [], "size": t.size, "mtime": t.mtime,
            "duration": round(t.duration, 1), "album": t.album, "year": t.year,
            "fmt": t.fmt, "edit": t.edit_key, "layout": t.layout, "plays": t.plays,
            "starred": t.starred, "pl_nd": sorted(t.pl_nd), "pl_m3u": sorted(t.pl_m3u),
            "title_tag": t.title, "artist_tag": t.artist, "tags_missing": t.tags_missing,
        }

    # exemptions
    if gid in rules.keep_both_ids or base_gid in rules.keep_both_ids or (ak, bk) in rules.keep_both or ak in rules.protect_artists:
        g["kind"] = "exempt"
        g["note"] = "matched a keep-both / protect-artist rule"
        g["members"] = [entry(t, "keep", "exempt by rule") for t in members]
        return g

    by_edit = defaultdict(list)
    for t in members:
        by_edit[t.edit_key].append(t)
    clusters = {}
    for tok, lst in by_edit.items():
        lst = sorted(lst, key=lambda t: t.duration)
        cl = [[lst[0]]]
        for t in lst[1:]:
            if t.duration - cl[-1][-1].duration <= wide:
                cl[-1].append(t)
            else:
                cl.append([t])
        clusters[tok] = cl

    generic = any(t.generic for t in members)
    conflict = [tok for tok, cl in clusters.items() if len(cl) > 1]
    # different guest artists ('feat. Tamia' / 'feat. Ashanti'): two recordings of the song, never a duplicate pair
    feat_pair = next(((a, b) for i, a in enumerate(members) for b in members[i + 1:]
                      if feat_conflict(getattr(a, "feat", frozenset()), getattr(b, "feat", frozenset()))), None)
    if generic or conflict or feat_pair:
        g["kind"] = "review"
        if generic:
            g["note"] = "generic title (%s) - same name does not mean same song" % members[0].base
        elif feat_pair:
            g["note"] = "different featured artists (%s vs %s) - different versions of the song, not duplicates" % (
                ", ".join(sorted(feat_pair[0].feat)), ", ".join(sorted(feat_pair[1].feat)))
        else:
            lens = ", ".join(sorted({fmt_dur(t.duration) for t in members}, key=lambda s: (len(s), s)))
            g["note"] = "same title but lengths disagree (%s) - could be different recordings" % lens
        g["members"] = [entry(t, "review", g["note"]) for t in members]
        return g

    # one recording per edit - but is the length spread within the strict tolerance, or only the wide one?
    spread = max(max(t.duration for t in cl[0]) - min(t.duration for t in cl[0]) for cl in clusters.values())
    is_wide = spread > tol + 1e-9

    # choose which edit-cluster survives
    lossless_forced = False
    if len(clusters) == 1:
        g["kind"] = "exact"
        keep_tok = next(iter(clusters))
    else:
        g["kind"] = "variant"
        keep_tok = min(clusters, key=lambda tok: (rules.rank(tok), tok))
        pref = rules.prefer_edit.get((ak, bk))
        if pref is not None and pref in clusters:
            keep_tok = pref
        labels = ", ".join("'%s'" % (tok or "plain/album") for tok in sorted(clusters, key=lambda x: rules.rank(x)))
        g["note"] = "different edits: %s -> keeping '%s'" % (labels, keep_tok or "plain/album")
        # A lossy copy never beats a lossless one, whatever its version: if the preferred version exists only as MP3 (or another lossy
        # file) and another version exists as FLAC (or another lossless file), the lossless version is the one that stays and the
        # lossy copy goes to the quarantine like any other loser. (Version preference only decides between files of the same kind.)
        def has_lossless(tok):
            return any(t.lossless for t in clusters[tok][0])
        if not has_lossless(keep_tok):
            lossless_toks = [tok for tok in clusters if has_lossless(tok)]
            if lossless_toks:
                was = keep_tok
                lossless_forced = True
                keep_tok = min(lossless_toks, key=lambda tok: (rules.rank(tok), tok))
                g["note"] = "different edits: %s -> keeping '%s'. LOSSLESS RULE: '%s' exists only as a lossy file, so the lossless '%s' is kept instead" % (
                    labels, keep_tok or "plain/album", was or "plain/album", keep_tok or "plain/album")

    if is_wide:
        g["note"] = ("WIDE GAP: copies differ by up to %.1fs (strict tolerance is %gs) - usually remaster or fade-out "
                     "differences, but check the lengths. Not removed unless you approve it "
                     "(apply --group %s, or --kinds wide). %s" % (spread, tol, gid, g["note"])).strip()
        g["kind"] = "wide"

    keep_cluster = clusters[keep_tok][0]
    ranked = sorted(keep_cluster, key=lambda t: t.rel)
    ranked = sorted(ranked, key=lambda t: keeper_key(t, rules), reverse=True)
    winner = ranked[0]
    forced = [t for t in keep_cluster if t.rel in rules.keep_files]
    g["members"].append(entry(winner, "keep", describe_keeper(winner, ranked, rules)))
    for t in forced:
        if t is not winner:
            g["members"].append(entry(t, "keep", "keep-file rule"))
    for t in members:
        if t is winner or t in forced:
            continue
        if t in keep_cluster:
            reason = "duplicate copy - kept copy wins on: %s" % why_kept(winner, t, rules)
        elif lossless_forced and not t.lossless:
            reason = "lossy copy (%s): a lossless copy of this song is kept ('%s')" % (t.fmt, keep_tok or "plain/album")
        else:
            reason = "non-preferred version '%s' (kept '%s')" % (t.edit_key or "plain/album", keep_tok or "plain/album")
        role, blocked = "remove", []
        # playlist safety: a Navidrome playlist that holds the loser but not the winner
        blocked = sorted(set(t.pl_nd) - set(winner.pl_nd))
        if blocked:
            role = "blocked"
            reason += " | BLOCKED: in Navidrome playlist(s) the kept file is not in"
        if (t.starred and not winner.starred) or (int(t.rating or 0) > int(winner.rating or 0)):
            role = "blocked"
            blocked = blocked + ["starred/rated in Navidrome (kept copy is not)"]
            reason += " | BLOCKED: starred/rated here but not on the kept copy"
        for pp in rules.protect_paths:
            if t.rel.startswith(pp.strip("/") + "/") or t.rel == pp:
                role = "blocked"
                if "protect-path rule" not in blocked:
                    blocked = blocked + ["protect-path rule"]
                reason += " | BLOCKED: protect-path rule"
        wl, tl = adv_label(winner.rel, winner.title), adv_label(t.rel, t.title)
        mismatch = (wl != tl) and bool(wl or tl)
        if mismatch:
            reason += " | label differs: kept=%s, this=%s" % (wl or "unlabeled", tl or "unlabeled")
            if rules.label_mode == "keep-both" and (ak, bk) not in rules.dedupe_labels:
                ac = AUDIO["check"](winner, t) if AUDIO["check"] else None
                if ac and ac.get("verdict") == "same":
                    reason += " | AUDIO CHECK: %s - same version, only the label differs, so this is a plain duplicate" % ac["why"]
                else:
                    role, blocked = "review", []
                    reason += " | LEFT ALONE: clean/explicit may differ, so both stay"
                    if ac and ac.get("why"):
                        reason += " (audio check: %s)" % ac["why"]
        elif g["kind"] == "exact" and role in ("remove", "blocked") and AUDIO["check"] and (ak, bk) not in rules.dedupe_labels:
            # no label to go by (both unlabeled, or both labeled alike): one of them may still be the clean edit, or a
            # cover/cast recording filed under the same title. Same recording = plain duplicate; anything else stays.
            ac = AUDIO["check"](winner, t)
            if ac and ac.get("verdict") in ("local", "broad", "error"):
                role, blocked = "review", []
                reason += " | LEFT ALONE: audio check says this is not the same version (%s)" % ac.get("why", "")
            elif ac and ac.get("verdict") == "same":
                reason += " | audio check: same recording"
        e = entry(t, role, reason, winner=winner.rel, blocked_by=blocked)
        e["adv_mismatch"] = mismatch
        g["members"].append(e)
    order = {"keep": 0, "remove": 1, "blocked": 2}
    g["members"].sort(key=lambda m: (order.get(m["role"], 3), m["path"]))
    return g


def classify_with_peel(members, rules, tol, nd_available, wide):
    """Like classify_group, but a song whose copies come in different lengths is no longer skipped as a whole.
    Copies of the SAME length (within the wide tolerance) are a plain duplicate set among themselves, so they are
    grouped and deduplicated; the copies of a different length (another master or edit) are left alone."""
    g = classify_group(members, rules, tol, nd_available, wide)
    if g["kind"] != "review" or not g["note"].startswith("same title but lengths disagree"):
        return [g]
    wide = max(wide if wide is not None else tol, tol)
    by_edit = defaultdict(list)
    for t in members:
        by_edit[t.edit_key].append(t)
    sets = []
    for tok, lst in by_edit.items():
        lst = sorted(lst, key=lambda t: t.duration)
        cl = [[lst[0]]]
        for t in lst[1:]:
            if t.duration - cl[-1][-1].duration <= wide:
                cl[-1].append(t)
            else:
                cl.append([t])
        for c in cl:
            if len(c) >= 2:
                sets.append((tok, c))
    if not sets:
        return [g]
    peeled = set()
    out = []
    for tok, c in sets:
        sub = classify_group(c, rules, tol, nd_available, wide, id_suffix="#%s#%d" % (tok, int(round(c[0].duration))))
        others = sorted({fmt_dur(t.duration) for t in members if t not in c})
        if others and sub["kind"] in ("exact", "wide"):
            sub["note"] = ("%s Same song also exists in other length(s) (%s): those copies are left alone." % (
                sub["note"], ", ".join(others))).strip()
        out.append(sub)
        peeled.update(t.rel for t in c)
    rest = [t for t in members if t.rel not in peeled]
    if len(rest) >= 2:
        left = classify_group(rest, rules, tol, nd_available, wide)
        if left["kind"] != "review":          # the rest never gets acted on: its lengths already disagreed
            left["kind"] = "review"
            left["note"] = "same title but lengths disagree - could be different recordings"
            for m in left["members"]:
                m["role"], m["reason"], m["winner"], m["blocked_by"] = "review", left["note"], None, []
        out.append(left)
    return out


def similar_groups(tracks, grouped, tol):
    by_artist = defaultdict(list)
    for t in tracks:
        if t.matchable and not t.generic:
            by_artist[t.artist_key].append(t)
    out, seen = [], set()
    for ak, lst in by_artist.items():
        lst = sorted(lst, key=lambda t: t.duration)
        n = len(lst)
        for i in range(n):
            a = lst[i]
            for j in range(i + 1, n):
                b = lst[j]
                if b.duration - a.duration > tol:
                    break
                if a.base_key == b.base_key or a.distinct_key != b.distinct_key:
                    continue
                if len(a.base) < 4 or len(b.base) < 4:
                    continue
                if re.findall(r"\d+", a.base) != re.findall(r"\d+", b.base):
                    continue
                sm = difflib.SequenceMatcher(None, a.base, b.base)
                if sm.real_quick_ratio() < 0.88 or sm.quick_ratio() < 0.88 or sm.ratio() < 0.88:
                    continue
                key = tuple(sorted((a.rel, b.rel)))
                if key in seen:
                    continue
                seen.add(key)
                gid = group_id(ak, a.base_key + "~" + b.base_key, "")
                members = []
                for t in (a, b):
                    members.append({
                        "path": t.rel, "role": "review", "reason": "similar title, same length",
                        "winner": None, "blocked_by": [], "size": t.size, "mtime": t.mtime,
                        "duration": round(t.duration, 1), "album": t.album, "year": t.year,
                        "fmt": t.fmt, "edit": t.edit_key, "layout": t.layout, "plays": t.plays,
                        "starred": t.starred, "pl_nd": sorted(t.pl_nd), "pl_m3u": sorted(t.pl_m3u),
                        "title_tag": t.title, "artist_tag": t.artist, "tags_missing": t.tags_missing})
                out.append({"id": gid, "artist": primary_artist(a.artist), "title": a.title + "  ~  " + b.title,
                            "kind": "similar", "note": "titles almost match and lengths agree - eyeball it",
                            "members": members})
    return out


def describe_keeper(winner, ranked, rules):
    bits = []
    if winner.rel in rules.keep_files:
        bits.append("keep-file rule")
    if len(ranked) > 1:
        bits.append(why_kept(winner, ranked[1], rules))
    return "kept: " + ", ".join(bits) if bits else "kept"


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#0d0f14;--s:#13161d;--s2:#1a1e28;--b:#252a38;--t:#c8d0e0;--d:#6b7590;--g:#4ade80;--a:#fbbf24;--r:#f87171;--bl:#60a5fa;--p:#a78bfa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.5 system-ui,sans-serif;padding:28px 36px}
h1{font:600 22px ui-monospace,monospace;color:#e8edf5;margin:0 0 4px}h2{font:600 13px ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--d);margin:30px 0 10px}
.sub{color:var(--d);font-size:12px;font-family:ui-monospace,monospace}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}.card{background:var(--s);border:1px solid var(--b);border-radius:8px;padding:12px 16px;min-width:140px}
.card b{display:block;font:600 22px ui-monospace,monospace;color:#e8edf5}.card span{font-size:11px;color:var(--d);text-transform:uppercase;letter-spacing:.08em}
table{border-collapse:collapse;width:100%;font-size:12.5px}td,th{padding:5px 8px;border-bottom:1px solid var(--b);text-align:left;vertical-align:top}
th{color:var(--d);font:11px ui-monospace,monospace;text-transform:uppercase;letter-spacing:.08em}
details{background:var(--s);border:1px solid var(--b);border-radius:8px;margin:8px 0}summary{cursor:pointer;padding:9px 14px;font-weight:600}
summary small{color:var(--d);font-weight:400;margin-left:8px}details>div{padding:0 14px 10px}
.gh td{background:var(--s2);font-weight:600;border-top:8px solid var(--s)}
.badge{display:inline-block;font:600 10px ui-monospace,monospace;padding:1px 7px;border-radius:3px;letter-spacing:.06em}
.keep{background:rgba(74,222,128,.14);color:var(--g)}.remove{background:rgba(248,113,113,.14);color:var(--r)}
.blocked{background:rgba(251,191,36,.14);color:var(--a)}.review{background:rgba(96,165,250,.14);color:var(--bl)}
.k-exact{color:var(--g)}.k-wide{color:var(--y,#d4a017)}.k-variant{color:var(--p)}.k-review,.k-similar{color:var(--bl)}.k-exempt{color:var(--d)}
.path{font-family:ui-monospace,monospace;font-size:11.5px;word-break:break-all}.dim{color:var(--d)}
input{background:var(--s);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;width:320px;font:13px ui-monospace,monospace}
.note{color:var(--d);font-size:12px;padding:2px 0 6px}
"""

JS = """
const q=document.getElementById('q');q.addEventListener('input',()=>{const v=q.value.toLowerCase();
document.querySelectorAll('details.artist').forEach(d=>{const m=!v||d.dataset.s.includes(v);d.style.display=m?'':'none';if(v&&m)d.open=true;});});
"""


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f PB" % n


def summarize(groups):
    s = {"groups": Counter(g["kind"] for g in groups), "remove_files": 0, "remove_bytes": 0,
         "blocked_files": 0, "playlists_touched": set(), "keep_files": 0,
         "wide_files": 0, "wide_bytes": 0, "left_alone_files": 0}
    for g in groups:
        wide = g["kind"] == "wide"
        for m in g["members"]:
            if m["role"] == "remove":
                if wide:
                    s["wide_files"] += 1
                    s["wide_bytes"] += m["size"]
                    continue
                s["remove_files"] += 1
                s["remove_bytes"] += m["size"]
                s["playlists_touched"].update(m["pl_m3u"])
                s["playlists_touched"].update(m["pl_nd"])
            elif m["role"] == "blocked":
                if not wide:
                    s["blocked_files"] += 1
            elif m["role"] == "review" and m.get("adv_mismatch"):
                s["left_alone_files"] += 1
    return s


def inventory(tracks, music_entries_count):
    inv = {"files": len(tracks), "bytes": sum(t.size for t in tracks)}
    inv["by_ext"] = dict(Counter(t.ext for t in tracks))
    inv["by_layout"] = dict(Counter(t.layout for t in tracks))
    inv["lossless"] = sum(1 for t in tracks if t.lossless)
    inv["lossy"] = sum(1 for t in tracks if not t.lossless)
    inv["tags_missing"] = sum(1 for t in tracks if t.tags_missing)
    inv["unreadable"] = sum(1 for t in tracks if t.error)
    inv["unmatchable"] = sum(1 for t in tracks if not t.matchable)
    inv["mislabeled"] = sum(1 for t in tracks if t.mislabeled)
    inv["artists"] = len({t.artist_key for t in tracks if t.artist_key})
    inv["albums"] = len({(t.artist_key, compact(t.album)) for t in tracks if t.album})
    return inv


def why_unmatchable(t):
    if t.error:
        return "unreadable: %s" % t.error
    if not t.artist_key or not t.base_key:
        return "no artist/title in tags or file name"
    if t.artist_key in BAD_ARTISTS:
        return "artist tag is '%s' - too generic to match on" % t.artist
    if t.duration <= 0:
        return "no duration"
    return "not matchable"


def health_rows(tracks, leftovers):
    rows = []
    for t in tracks:
        if t.mislabeled:
            rows.append(("mislabeled", t.rel, t.size, "named %s but the content is %s (%s) - treated as that format" % (
                t.ext, t.mislabeled.upper(), t.fmt)))
        elif t.note:
            rows.append(("damaged-tags", t.rel, t.size, t.note))
    for rel, size, _m in leftovers:
        rows.append(("leftover", rel, size, "hidden audio file - probably a half-finished download; not scanned"))
    return rows


def write_reports(out, groups, tracks, inv, plan_meta, leftovers=()):
    s = summarize(groups)
    health = health_rows(tracks, leftovers)
    by_artist = defaultdict(list)
    for g in groups:
        by_artist[g["artist"] or "(unknown)"].append(g)
    h = []
    A = h.append
    A("<!doctype html><meta charset=utf-8><title>Dewey report</title><style>%s</style>" % CSS)
    A("<h1>Dewey report</h1><div class=sub>generated %s &middot; v%s &middot; library %s &middot; tolerance %ss (wide %ss) &middot; navidrome db: %s</div>" % (
        plan_meta["generated"], VERSION, html.escape(plan_meta["music_root"]), plan_meta["tolerance"],
        plan_meta.get("wide_tolerance", plan_meta["tolerance"]),
        "yes" if plan_meta["navidrome_checked"] else "NOT used"))
    A("<div class=cards>")
    for label, val in (("tracks scanned", "{:,}".format(inv["files"])), ("library size", human(inv["bytes"])),
                       ("exact dup groups", s["groups"]["exact"]), ("variant groups", s["groups"]["variant"]),
                       ("wide-gap groups (need your OK)", s["groups"]["wide"]),
                       ("review / similar", s["groups"]["review"] + s["groups"]["similar"]),
                       ("files removable", "{:,}".format(s["remove_files"])), ("space reclaimable", human(s["remove_bytes"])),
                       ("blocked by playlists", s["blocked_files"]), ("playlists touched", len(s["playlists_touched"]))):
        A("<div class=card><b>%s</b><span>%s</span></div>" % (val, label))
    A("</div>")
    A("<h2>Library evaluation</h2><table><tr><th>layout</th><th>files</th><th>meaning</th></tr>")
    meaning = {"year-album": "[YYYY] Artist - Album/track", "artist-album": "Artist/Album/track",
               "artist-only": "Artist/track", "loose": "file sitting directly in the library root"}
    for k in ("year-album", "artist-album", "artist-only", "loose"):
        A("<tr><td>%s</td><td>%s</td><td class=dim>%s</td></tr>" % (k, "{:,}".format(inv["by_layout"].get(k, 0)), meaning[k]))
    A("</table><p class=sub>formats: %s &middot; lossless %s / lossy %s &middot; artists %s &middot; albums %s &middot; tags missing (filename used) %s &middot; unreadable %s &middot; not matchable %s</p>" % (
        ", ".join("%s %s" % (k, v) for k, v in sorted(inv["by_ext"].items())), "{:,}".format(inv["lossless"]),
        "{:,}".format(inv["lossy"]), "{:,}".format(inv["artists"]), "{:,}".format(inv["albums"]),
        inv["tags_missing"], inv["unreadable"], inv["unmatchable"]))
    if health:
        kinds = Counter(r[0] for r in health)
        A("<details><summary>%d files need a look<small>%s &middot; listed in filehealth.csv</small></summary><div><table>" % (
            len(health), " &middot; ".join("%d %s" % (v, k) for k, v in sorted(kinds.items()))))
        for kind, rel, size, detail in health[:300]:
            A("<tr><td class=dim>%s</td><td class=path>%s</td><td class=dim>%s</td><td class=dim>%s</td></tr>" % (
                kind, html.escape(rel), human(size), html.escape(detail)))
        if len(health) > 300:
            A("<tr><td colspan=4 class=dim>... %d more in filehealth.csv</td></tr>" % (len(health) - 300))
        A("</table></div></details>")
    bad = [t for t in tracks if not t.matchable]
    if bad:
        A("<details><summary>%d files could not be checked for duplicates<small>listed in unmatched.csv</small></summary><div><table>" % len(bad))
        for t in bad[:300]:
            A("<tr><td class=path>%s</td><td class=dim>%s</td></tr>" % (html.escape(t.rel), html.escape(why_unmatchable(t))))
        if len(bad) > 300:
            A("<tr><td colspan=2 class=dim>... %d more in unmatched.csv</td></tr>" % (len(bad) - 300))
        A("</table></div></details>")
    A("<h2>Duplicate groups by artist</h2><input id=q placeholder='filter: artist, title, album, path...'>")
    colgroup = "<colgroup><col style='width:84px'><col style='width:36%'><col style='width:17%'><col style='width:84px'><col style='width:52px'><col style='width:70px'><col></colgroup>"
    for artist in sorted(by_artist, key=str.lower):
        gs = by_artist[artist]
        rem = sum(1 for g in gs if g["kind"] != "wide" for m in g["members"] if m["role"] == "remove")
        blk = sum(1 for g in gs if g["kind"] != "wide" for m in g["members"] if m["role"] == "blocked")
        rev = sum(1 for g in gs if g["kind"] in ("review", "similar"))
        wid = sum(1 for g in gs if g["kind"] == "wide")
        hay = " ".join([artist] + [g["title"] for g in gs] + [m["path"] for g in gs for m in g["members"]]).lower()
        A("<details class=artist data-s='%s'><summary>%s<small>%d groups &middot; %d removable &middot; %d blocked &middot; %d to review%s</small></summary><div><table style='table-layout:fixed'>%s" % (
            html.escape(hay, quote=True), html.escape(artist), len(gs), rem, blk, rev,
            (" &middot; %d wide-gap" % wid) if wid else "", colgroup))
        for g in gs:
            A("<tr class=gh><td colspan=7><span class='k-%s'>%s</span> &nbsp;%s <span class=dim>[%s]</span><div class=note>%s</div></td></tr>" % (
                g["kind"], g["kind"].upper(), html.escape(g["title"]), g["id"], html.escape(g["note"])))
            for m in g["members"]:
                pls = pl_names(m)
                A("<tr><td><span class='badge %s'>%s</span></td><td class=path>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td>%s plays</td><td class=dim>%s</td></tr>" % (
                    m["role"], m["role"].upper(), html.escape(m["path"]), html.escape(m["album"] or ""),
                    (" (%s)" % m["year"]) if m["year"] else "", m["fmt"], fmt_dur(m["duration"]), m["plays"],
                    html.escape(m["reason"]) + ((" &middot; playlists: " + html.escape(", ".join(pls))) if pls else "")))
        A("</table></div></details>")
    A("<script>%s</script>" % JS)
    with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(h))

    with open(os.path.join(out, "dupes.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["group_id", "kind", "role", "artist", "title", "album", "year", "format", "seconds",
                    "plays", "playlists", "reason", "path", "keeper_path"])
        for g in groups:
            for m in g["members"]:
                w.writerow([g["id"], g["kind"], m["role"], g["artist"], g["title"], m["album"], m["year"], m["fmt"],
                            m["duration"], m["plays"], "; ".join(pl_names(m)),
                            m["reason"], m["path"], m["winner"] or ""])
    with open(os.path.join(out, "unmatched.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "size", "artist_tag", "title_tag", "seconds", "reason"])
        for t in tracks:
            if not t.matchable:
                w.writerow([t.rel, t.size, t.artist, t.title, round(t.duration, 1), why_unmatchable(t)])
    with open(os.path.join(out, "filehealth.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "path", "size", "detail"])
        for row in health:
            w.writerow(row)
    plan = dict(plan_meta)
    plan["groups"] = groups
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(out, "inventory.json"), "w", encoding="utf-8") as fh:
        json.dump(inv, fh, indent=1)
    return s


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def make_logger(out):
    path = os.path.join(out, "scan.log")

    def log(msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    return log


def pl_names(m):
    """Playlist names for the report: Navidrome playlists as-is, .m3u-only files tagged '(.m3u)'.
    Only Navidrome playlists can block a removal; an .m3u file is just rewritten."""
    nd = set(m["pl_nd"])
    return sorted(nd) + sorted("%s (.m3u)" % x for x in set(m["pl_m3u"]) if x not in nd)


def cmd_scan(a):
    music = os.path.abspath(a.music)
    if not os.path.isdir(music):
        sys.exit("music folder not found: %s" % music)
    os.makedirs(a.out, exist_ok=True)
    try:
        os.remove(os.path.join(a.out, "scan.log"))
    except OSError:
        pass
    log = make_logger(a.out)
    rules = load_rules(a.rules)
    tol = a.tolerance if a.tolerance is not None else (rules.tolerance if rules.tolerance else 3.0)
    wide = a.wide_tolerance if a.wide_tolerance is not None else (rules.wide_tolerance if rules.wide_tolerance else 6.0)
    wide = max(wide, tol)
    log("Dewey %s scanning %s (rules: %s)" % (VERSION, music, rules.source or "none"))
    raw, leftovers = scan_library(music, os.path.join(a.out, "cache.db"), a.workers, rules, log)
    tracks = [make_track(rel, d, rules) for rel, d in sorted(raw.items())]
    rel_index = {t.rel for t in tracks}

    # playlists (M3U)
    pls = find_playlists(music, a.playlist_dir)
    refs = index_m3u(music, pls)
    log("%d playlist files, %d distinct library files referenced" % (len(pls), len(refs)))
    for t in tracks:
        t.pl_m3u = refs.get(t.rel, set())

    # Navidrome
    nd_ok = False
    if a.navidrome_db and os.path.exists(a.navidrome_db):
        info, nd_pl, nd_ok = load_navidrome(a.navidrome_db, rel_index, log)
        for t in tracks:
            i = info.get(t.rel)
            if i:
                t.nd_id, t.plays, t.starred, t.rating = i["id"], i["plays"], i["starred"], i["rating"]
            t.pl_nd = nd_pl.get(t.rel, set())
    elif a.navidrome_db:
        log("WARNING: Navidrome db not found at %s - playlist/play-count protection limited to .m3u files" % a.navidrome_db)

    inv = inventory(tracks, len(raw))
    log("building duplicate groups...")
    finish_audio = None
    if rules.audio_check:
        AUDIO["check"], finish_audio = make_audio_checker(music, a.out, log)
    try:
        groups = build_groups(tracks, rules, tol, nd_ok, wide, peel=True)
    finally:
        AUDIO["check"] = None
    if finish_audio:
        n_new, off = finish_audio()
        if n_new:
            log("audio check: compared %d pair(s) of copies that are about to be treated as duplicates" % n_new)
    meta = {"version": VERSION, "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "music_root": music, "tolerance": tol, "wide_tolerance": wide, "navidrome_checked": nd_ok,
            "playlist_files": [os.path.relpath(p, music).replace(os.sep, "/") for p in pls],
            "rules": rules.source}
    s = write_reports(a.out, groups, tracks, inv, meta, leftovers)
    log("-" * 60)
    log("tracks %d | exact %d | variant %d | wide-gap %d | review %d | similar %d | exempt %d" % (
        len(tracks), s["groups"]["exact"], s["groups"]["variant"], s["groups"]["wide"], s["groups"]["review"],
        s["groups"]["similar"], s["groups"]["exempt"]))
    log("removable %d files (%s) | blocked by playlists %d | playlists touched %d" % (
        s["remove_files"], human(s["remove_bytes"]), s["blocked_files"], len(s["playlists_touched"])))
    log("held for your OK (wide-gap groups): %d files (%s) | clean/explicit left alone: %d files" % (
        s["wide_files"], human(s["wide_bytes"]), s["left_alone_files"]))
    nh = sum(1 for t in tracks if t.mislabeled)
    if nh or leftovers or any(t.note for t in tracks):
        log("file health: %d mislabeled (content differs from extension) | %d with damaged tags | %d hidden leftovers -> filehealth.csv" % (
            nh, sum(1 for t in tracks if t.note and not t.mislabeled), len(leftovers)))
    log("layout: %s" % ", ".join("%s=%d" % kv for kv in sorted(inv["by_layout"].items())))
    log("report: %s" % os.path.join(a.out, "report.html"))
    log("nothing was changed. next: review the report, edit rules.txt, then 'apply' (dry run first).")


def select_for_apply(plan, a):
    # PowerShell turns an unquoted  exact,variant  into "exact variant", so accept commas and/or spaces
    kinds = {"exact"} if a.exact_only else set(k for k in re.split(r"[,\s]+", a.kinds.lower()) if k)
    bad = kinds - {"exact", "variant", "wide", "review", "similar", "exempt"}
    if bad:
        sys.exit("unknown group kind(s) in --kinds: %s  (use exact, variant, wide)" % ", ".join(sorted(bad)))
    if a.group:
        kinds = kinds | {"wide"}      # naming a group yourself is the approval a wide-gap group needs
    want_artist = compact(a.artist) if a.artist else None
    want_ids = set(x.lower() for x in (a.group or []))
    sel = []
    for g in plan["groups"]:
        if g["kind"] not in kinds:
            continue
        if want_ids and g["id"].lower() not in want_ids:
            continue
        if want_artist and want_artist not in compact(g["artist"]):
            continue
        if any(m["role"] in ("remove", "blocked") for m in g["members"]):
            sel.append(g)
    if a.limit:
        sel = sel[:a.limit]
    return sel


def cmd_apply(a):
    plan_path = a.plan or os.path.join(a.out, "plan.json")
    if not os.path.exists(plan_path):
        sys.exit("no plan at %s - run 'scan' first" % plan_path)
    plan = json.load(open(plan_path, encoding="utf-8"))
    music = os.path.abspath(a.music or plan["music_root"])
    if not plan.get("navidrome_checked") and not a.no_navidrome_check:
        sys.exit("This plan was built WITHOUT the Navidrome database, so Navidrome-native playlists could not be\n"
                 "checked. Re-run scan with --navidrome-db, or pass --no-navidrome-check if you accept that risk.")
    sel = select_for_apply(plan, a)
    removes, blocked, skipped = [], [], []
    for g in sel:
        by_path = {m["path"]: m for m in g["members"]}
        for m in g["members"]:
            if m["role"] == "blocked":
                blocked.append((g, m))
            elif m["role"] == "remove":
                w = m["winner"]
                abs_l, abs_w = os.path.join(music, m["path"]), os.path.join(music, w or "")
                try:
                    st = os.stat(abs_l)
                except OSError:
                    skipped.append((g, m, "file is gone"))
                    continue
                if st.st_size != m["size"] or abs(st.st_mtime - m["mtime"]) > 0.001:
                    skipped.append((g, m, "file changed since the scan - re-run scan"))
                    continue
                if not w or not os.path.exists(abs_w):
                    skipped.append((g, m, "keeper file is missing - refusing"))
                    continue
                if os.path.samefile(abs_l, abs_w):
                    skipped.append((g, m, "loser and keeper are the same file"))
                    continue
                removes.append((g, m))
    print("plan generated %s | selection: %d groups | would remove %d files (%s) | blocked %d | skipped %d" % (
        plan["generated"], len(sel), len(removes), human(sum(m["size"] for _, m in removes)), len(blocked), len(skipped)))
    last = None
    for g, m in removes:
        if g["id"] != last:
            print("\n[%s] %-7s %s - %s" % (g["id"], g["kind"], g["artist"], g["title"]))
            for k in g["members"]:
                if k["role"] == "keep":
                    print("   KEEP    %s   (%s, %s)" % (k["path"], k["fmt"], fmt_dur(k["duration"])))
            last = g["id"]
        print("   REMOVE  %s   (%s, %s)  %s" % (m["path"], m["fmt"], fmt_dur(m["duration"]), m["reason"]))
    if blocked:
        print("\nBLOCKED (a Navidrome playlist holds the loser but not the keeper - untouched this run):")
        for g, m in blocked[:50]:
            print("   %s  [%s]  in: %s" % (m["path"], g["id"], ", ".join(m["blocked_by"])))
        if len(blocked) > 50:
            print("   ... and %d more (see report.html)" % (len(blocked) - 50))
    for g, m, why in skipped:
        print("SKIPPED %s : %s" % (m["path"], why))

    mapping = {m["path"]: m["winner"] for _, m in removes}
    pls = find_playlists(music, a.playlist_dir)
    touched = []
    for p in pls:
        data, rep, drp = rewrite_m3u(p, music, mapping)
        if data is not None:
            touched.append((p, data, rep, drp))
    if touched:
        print("\nplaylist files that will be rewritten (mtime preserved, backup kept):")
        for p, _, rep, drp in touched:
            print("   %s  (%d repointed, %d duplicate entries dropped)" % (os.path.basename(p), rep, drp))
    if not a.execute:
        print("\nDRY RUN - nothing changed. Add --execute to do it.")
        return
    if a.expect_remove is not None and a.expect_remove != len(removes):
        sys.exit("REFUSED: the dry run you confirmed would remove %d file(s) but this run would remove %d (a file changed, moved or went missing "
                 "since). Nothing was changed. Do a fresh dry run and confirm that one." % (a.expect_remove, len(removes)))
    if not removes:
        print("nothing to do.")
        return

    run_id = time.strftime("%Y%m%d-%H%M%S")
    qroot = os.path.join(a.quarantine, run_id)
    os.makedirs(qroot, exist_ok=True)
    manifest = {"run": run_id, "music_root": music, "quarantine": qroot, "files": [], "playlists": [], "dirs": []}

    for p, data, rep, drp in touched:
        bk = os.path.join(qroot, "_playlists", os.path.basename(p))
        os.makedirs(os.path.dirname(bk), exist_ok=True)
        st = os.stat(p)
        shutil.copy2(p, bk)
        with open(p, "wb") as fh:
            fh.write(data)
        if not a.bump_m3u_mtime:
            os.utime(p, (st.st_atime, st.st_mtime))
        manifest["playlists"].append({"path": os.path.relpath(p, music).replace(os.sep, "/"), "backup": bk,
                                      "mtime": st.st_mtime, "atime": st.st_atime})

    moved = 0
    mpath = os.path.join(qroot, "manifest.json")
    save_json_atomic(mpath, manifest)            # the playlists are already rewritten: record them before any file moves
    redir = open(os.path.join(a.out, "redirects.tsv"), "a", encoding="utf-8", buffering=1)
    try:
        for g, m in removes:
            src = os.path.join(music, m["path"])
            dest = os.path.join(qroot, m["path"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            side = []
            stem = os.path.splitext(src)[0]
            for ext in (".lrc",):
                if os.path.exists(stem + ext):
                    d2 = os.path.splitext(dest)[0] + ext
                    shutil.move(stem + ext, d2)
                    side.append(os.path.splitext(m["path"])[0] + ext)
            manifest["files"].append({"path": m["path"], "winner": m["winner"], "group": g["id"],
                                      "size": m["size"], "sidecars": side})
            redir.write("%s\t%s\t%s\n" % (os.path.basename(m["path"]), m["path"], m["winner"]))
            moved += 1
            prune_upwards(os.path.dirname(src), music, qroot, manifest)
            # keep the manifest current, so a stopped run can still be undone (every file at first, then every 25th)
            if len(manifest["files"]) <= 500 or len(manifest["files"]) % 25 == 0:
                save_json_atomic(mpath, manifest)
    finally:
        redir.close()
        save_json_atomic(mpath, manifest)
    print("\nmoved %d files to %s" % (moved, qroot))
    print("manifest: %s" % os.path.join(qroot, "manifest.json"))
    print("next: Navidrome rescan; 'beet update -M' on the NAS Beets library (always with -M); a library scan in any other app that indexes the music.")
    print("undo with:  undo --manifest %s --execute" % os.path.join(qroot, "manifest.json"))


def save_json_atomic(path, obj):
    """write a JSON file so that a stop at any moment leaves either the old or the new file, never half of one"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def prune_upwards(d, music, qroot, manifest):
    music = os.path.abspath(music)
    d = os.path.abspath(d)
    while d != music and d.startswith(music + os.sep):
        try:
            entries = os.listdir(d)
        except OSError:
            return
        for e in entries:
            p = os.path.join(d, e)
            if e.lower() == "@eadir":
                continue
            if os.path.isdir(p):
                return
            if os.path.splitext(e)[1].lower() not in JUNK_EXT and e.lower() not in JUNK_NAMES:
                return
            if os.path.splitext(e)[1].lower() in AUDIO_EXT:
                return
        # only junk (cover art, nfo, @eaDir ...) left -> move it aside, drop the folder
        rel = os.path.relpath(d, music).replace(os.sep, "/")
        for e in os.listdir(d):
            src = os.path.join(d, e)
            dest = os.path.join(qroot, rel, e)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
        os.rmdir(d)
        manifest["dirs"].append(rel)
        d = os.path.dirname(d)


def makedirs_like(path):
    """os.makedirs, but new folders copy owner+mode from the nearest existing parent."""
    missing = []
    d = path
    while d and not os.path.isdir(d):
        missing.append(d)
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    try:
        ref = os.stat(d)
    except OSError:
        ref = None
    for m in reversed(missing):
        os.mkdir(m)
        if ref is not None:
            try:
                os.chown(m, ref.st_uid, ref.st_gid)
                os.chmod(m, ref.st_mode & 0o7777)
            except (OSError, AttributeError):
                pass


def load_manifest(path):
    """manifest.json of a run, its folder, or (after a crash) the journal of a reorg run."""
    if os.path.isdir(path):
        mj = os.path.join(path, "manifest.json")
        path = mj if os.path.exists(mj) else os.path.join(path, REORG_JOURNAL)
    if path.endswith(".jsonl"):
        recs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        head = next((r for r in recs if r.get("kind") == "header"), None)
        if not head:
            sys.exit("%s has no header line - cannot tell which library it belongs to" % path)
        nb = os.path.join(head["run_dir"], "navidrome.db.bak")
        return {"kind": "reorg", "run": head["run"], "music_root": head["music_root"], "run_dir": head["run_dir"],
                "files": [r for r in recs if r.get("kind") in ("audio", "lrc", "extra")],
                "playlists": [dict((k, v) for k, v in r.items() if k != "kind") for r in recs if r.get("kind") == "playlist"],
                "dirs": [r["rel"] for r in recs if r.get("kind") == "dir"],
                "navidrome_backup": nb if os.path.exists(nb) else ""}
    return json.load(open(path, encoding="utf-8"))


def undo_guard(a, man):
    """A manifest is trusted only when it describes the folder it sits in (and the library this tool is set up for). A manifest that
    points somewhere else - copied, edited, or forged - is refused before anything is read or moved."""
    mp = os.path.abspath(a.manifest)
    mdir = os.path.realpath(mp if os.path.isdir(mp) else os.path.dirname(mp))
    own = man.get("run_dir") if man.get("kind") == "reorg" else man.get("quarantine")
    if not own or os.path.realpath(own) != mdir:
        sys.exit("REFUSED: this manifest says its run folder is %s, but it is in %s. Nothing was changed." % (own, mdir))
    lib = getattr(a, "music", None)
    if lib and man.get("music_root") and os.path.realpath(man["music_root"]) != os.path.realpath(lib):
        sys.exit("REFUSED: this run belongs to the library %s, not %s. Nothing was changed." % (man.get("music_root"), lib))
    if lib and man.get("kind") == "place":           # a place run puts files back in the landing folder: it must be this library's own
        want = os.environ.get("MD_INCOMING") or os.path.join(os.path.dirname(os.path.abspath(lib)), "_incoming")
        if not man.get("incoming") or os.path.realpath(man["incoming"]) != os.path.realpath(want):
            sys.exit("REFUSED: this place run's landing folder is %s, not %s. Nothing was changed." % (man.get("incoming"), want))


def undo_fingerprint(lines):
    fh = hashlib.sha256()
    for ln in lines:
        fh.update((ln + "\n").encode("utf-8", "surrogateescape"))
    return fh.hexdigest()[:16]


def undo_proceed(a, kind, man, lines, nfiles, npl, nskip, shown, more=0):
    """The summary every undo prints (its first lines, so the console and the runner can read it), then: should the real run go on?
    The runner passes the fingerprint of the dry run you confirmed (--expect-plan); a different plan is refused."""
    print("undo %s run %s: %d file(s) to put back | %d playlist(s) | skipped %d" % (kind, man.get("run"), nfiles, npl, nskip))
    fp = undo_fingerprint(lines)
    print("plan fingerprint: %s" % fp)
    for sline in shown:
        print(sline)
    if more:
        print("   ... and %d more" % more)
    if not a.execute:
        return False
    if a.expect_plan is not None and a.expect_plan != fp:
        sys.exit("REFUSED: what this undo would do is not what the dry run you confirmed showed (the library or the run folder changed since). "
                 "Nothing was changed. Do a fresh dry run and confirm that one.")
    if nfiles == 0:
        print("nothing to undo.")
        return False
    return True


def undo_playlists_plan(man, restore_ok):
    """playlists are put back only when files are, and only from backups that are still there"""
    pl, miss = [], []
    for p in man.get("playlists", []):
        (pl if os.path.isfile(p.get("backup") or "") else miss).append(p)
    return (pl if restore_ok else []), miss


def undo_playlists_run(music, pl):
    for p in pl:
        dest = os.path.join(music, p["path"])
        shutil.copy2(p["backup"], dest)
        os.utime(dest, (p["atime"], p["mtime"]))


def undo_place(a, man):
    """A place run only sets duplicates aside (new files went straight into the library), so undoing it puts those files
    back in the _incoming landing folder, where 'place' can be run again for them (--keep-similar, --upgrade / place-upgrade).
    Nothing in the library or in the playlists changes."""
    incoming, qroot = man.get("incoming"), man.get("quarantine")
    files = man.get("files", [])
    todo, notes = [], []
    for f in files:
        src, dest = os.path.join(qroot, f["to"]), os.path.join(incoming, f["from"])
        if not os.path.exists(src):
            notes.append("   MISSING in the quarantine (already restored or purged): %s" % f["to"])
        elif os.path.exists(dest):
            notes.append("   SKIP (already in _incoming): %s" % f["from"])
        else:
            todo.append((src, dest, f))
    shown = notes[:100] + ["   would put back  %s" % f["from"] for _, _, f in todo[:200]]
    lines = ["place\t%s\t%s" % (src, dest) for src, dest, _ in todo]
    if not undo_proceed(a, "place", man, lines, len(todo), 0, len(notes), shown, max(0, len(todo) - 200)):
        if not a.execute:
            print("\n%d file(s) would go back to %s.\nDRY RUN - add --execute. Then run 'place' (add --keep-similar or --upgrade as needed)." % (len(todo), incoming))
        return
    done = 0
    for src, dest, f in todo:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        done += 1
    for dp, dn, fn in os.walk(qroot, topdown=False):       # tidy up folders the move left empty (never the run folder itself)
        if os.path.abspath(dp) != os.path.abspath(qroot) and not os.listdir(dp):
            try:
                os.rmdir(dp)
            except OSError:
                pass
    print("put back %d file(s) in %s. Next:  place   (dry run first; --keep-similar / --upgrade decide what happens to them)." % (done, incoming))


def cmd_undo(a):
    man = load_manifest(a.manifest)
    undo_guard(a, man)
    if man.get("kind") == "reorg":
        return undo_reorg(a, man)
    if man.get("kind") == "place":
        return undo_place(a, man)
    music, qroot = man["music_root"], man["quarantine"]
    todo, notes, lines = [], [], []
    for f in man["files"]:
        src, dest = os.path.join(qroot, f["path"]), os.path.join(music, f["path"])
        if os.path.exists(dest):
            notes.append("   SKIP (already exists): %s" % f["path"])
        elif not os.path.exists(src):
            notes.append("   MISSING in the quarantine (already restored or purged): %s" % f["path"])
        else:
            todo.append(f)
            lines.append("file\t%s\t%s" % (src, dest))
            for sc in f.get("sidecars", []):
                if os.path.exists(os.path.join(qroot, sc)):
                    lines.append("side\t%s\t%s" % (os.path.join(qroot, sc), os.path.join(music, sc)))
    dirmoves = []
    for rel in man.get("dirs", []):
        base = os.path.join(qroot, rel)
        if os.path.isdir(base):
            for e in sorted(os.listdir(base)):
                if not os.path.exists(os.path.join(music, rel, e)):
                    dirmoves.append((os.path.join(base, e), os.path.join(music, rel, e)))
                    lines.append("dir\t%s\t%s" % dirmoves[-1])
    pl, miss = undo_playlists_plan(man, bool(todo))
    for p in pl:
        lines.append("playlist\t%s\t%s" % (p["path"], p["backup"]))
    for p in miss:
        notes.append("   MISSING playlist backup (that playlist is left as it is): %s" % p.get("backup"))
    shown = notes[:100] + ["   would restore %s" % f["path"] for f in todo[:200]]
    if not undo_proceed(a, "quarantine", man, lines, len(todo), len(pl), len(notes), shown, max(0, len(todo) - 200)):
        if not a.execute:
            print("DRY RUN - add --execute")
        return
    restored = 0
    for f in todo:
        src, dest = os.path.join(qroot, f["path"]), os.path.join(music, f["path"])
        if os.path.exists(dest) or not os.path.exists(src):
            print("SKIP (changed since the plan): %s" % f["path"])
            continue
        makedirs_like(os.path.dirname(dest))
        shutil.move(src, dest)
        for sc in f.get("sidecars", []):
            s2 = os.path.join(qroot, sc)
            if os.path.exists(s2):
                shutil.move(s2, os.path.join(music, sc))
        restored += 1
    for src, dest in dirmoves:
        if os.path.exists(src) and not os.path.exists(dest):
            makedirs_like(os.path.dirname(dest))
            shutil.move(src, dest)
    undo_playlists_run(music, pl)
    print("restored %d files and %d playlists. Run a Navidrome rescan." % (restored, len(pl)))


def purge_plan(a):
    """every run folder directly inside the quarantine: its age, size and file count, and whether it is old enough to delete"""
    now = time.time()
    cutoff = now - a.older_than * 86400
    rows = []
    for run in sorted(os.listdir(a.quarantine)):
        p = os.path.join(a.quarantine, run)
        if os.path.islink(p) or not os.path.isdir(p):
            continue
        mt = os.path.getmtime(p)
        size, nfiles = 0, 0
        for r, _, fs in os.walk(p):
            for f in fs:
                try:
                    size += os.lstat(os.path.join(r, f)).st_size
                    nfiles += 1
                except OSError:
                    pass
        rows.append({"run": run, "path": p, "mtime": mt, "age": (now - mt) / 86400, "size": size, "files": nfiles,
                     "old": mt < cutoff, "manifest": os.path.isfile(os.path.join(p, "manifest.json"))})
    return rows


def cmd_purge(a):
    if not os.path.isdir(a.quarantine):
        sys.exit("no quarantine at %s" % a.quarantine)
    rows = purge_plan(a)
    dele = [r for r in rows if r["old"]]
    lines = ["%s\t%d\t%d\t%d" % (r["run"], int(r["mtime"]), r["files"], r["size"]) for r in dele]
    fp = undo_fingerprint(lines)
    print("purge plan: %d run(s) to delete (%s, %d file(s)) | keeping %d | older than %d days" % (
        len(dele), human(sum(r["size"] for r in dele)), sum(r["files"] for r in dele), len(rows) - len(dele), a.older_than))
    print("plan fingerprint: %s" % fp)
    for r in rows:
        print("%s  %5.1f days old  %9s  %6d files  %s%s" % (r["run"], r["age"], human(r["size"]), r["files"], "-> DELETE" if r["old"] else "keep",
                                                               "" if r["manifest"] else "  (no manifest)"))
    if not a.execute:
        print("DRY RUN - add --execute to delete the runs marked DELETE. A purge cannot be undone.")
        return
    if a.expect_plan is not None and a.expect_plan != fp:
        sys.exit("REFUSED: the runs a purge would delete are not the ones the dry run you confirmed showed (a run aged past the limit, or one "
                 "changed, since). Nothing was deleted. Do a fresh dry run and confirm that one.")
    if not dele:
        print("nothing to delete.")
        return
    qreal, gone, bad = os.path.realpath(a.quarantine), 0, 0
    for r in dele:
        if os.path.dirname(os.path.realpath(r["path"])) != qreal:
            print("SKIPPED (not directly inside the quarantine): %s" % r["run"])
            bad += 1
            continue
        try:
            shutil.rmtree(r["path"])
            print("deleted %s (%s, %d files)" % (r["run"], human(r["size"]), r["files"]))
            gone += 1
        except OSError as e:
            print("COULD NOT DELETE %s: %s" % (r["run"], e))
            bad += 1
    print("purged %d run(s)%s." % (gone, (", %d could not be deleted" % bad) if bad else ""))
    if bad:
        sys.exit(1)


class _Tee(object):
    """stdout that also writes to a log file."""
    def __init__(self, stream, fh):
        self._stream, self._fh = stream, fh

    def write(self, text):
        self._stream.write(text)
        try:
            self._fh.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self):
        self._stream.flush()
        try:
            self._fh.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def start_log(a):
    """apply / undo / purge keep their own log in out/logs/ (dry runs too) - an audit trail of what each run did."""
    out = getattr(a, "out", None) or os.environ.get("MD_OUT", "./out")
    try:
        d = os.path.join(out, "logs")
        os.makedirs(d, exist_ok=True)
        mode = "run" if getattr(a, "execute", False) else "dry"
        now = dt.datetime.now()
        path = os.path.join(d, "%s-%s-%s.txt" % (a.cmd, now.strftime("%Y%m%d-%H%M%S"), mode))
        fh = open(path, "w", encoding="utf-8-sig", buffering=1)
    except OSError:
        return
    fh.write("# Dewey %s | %s %s\n# %s\n\n" % (
        VERSION, a.cmd, " ".join(sys.argv[2:]), now.strftime("%Y-%m-%d %H:%M:%S")))
    sys.stdout = _Tee(sys.stdout, fh)


# ---------------------------------------------------------------------------
# playlistfix: point Navidrome playlists at the kept copy (Subsonic API)
# ---------------------------------------------------------------------------
STAR_TAG = "starred/rated"          # blocked_by entry that starts with this = starred/rated loser
NO_FIX_TAGS = ("protect-path",)     # blocked for reasons the fixer cannot resolve


class SubsonicError(Exception):
    pass


def aslist(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class Subsonic(object):
    """Minimal Subsonic/OpenSubsonic client (token auth). Never logs the URL, token or password."""
    API = "1.16.1"

    def __init__(self, base, user, password, timeout=60):
        self.base, self.user, self.password, self.timeout = base.rstrip("/"), user, password, timeout

    def call(self, endpoint, params=None, post=False):
        salt = os.urandom(6).hex()
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        q = [("u", self.user), ("t", token), ("s", salt), ("v", self.API), ("c", "dewey"), ("f", "json")]
        q += list(params or [])
        body = urllib.parse.urlencode(q).encode("utf-8")
        url = "%s/rest/%s.view" % (self.base, endpoint)
        try:
            if post:
                req = urllib.request.Request(url, data=body,
                                             headers={"Content-Type": "application/x-www-form-urlencoded"})
            else:
                req = urllib.request.Request(url + "?" + body.decode("ascii"))
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise SubsonicError("%s: HTTP %s" % (endpoint, e.code))
        except (urllib.error.URLError, OSError) as e:
            raise SubsonicError("cannot reach Navidrome at %s (%s)" % (self.base, getattr(e, "reason", e)))
        try:
            d = json.loads(raw.decode("utf-8"))["subsonic-response"]
        except (ValueError, KeyError):
            raise SubsonicError("%s: unexpected reply - is the URL really Navidrome's?" % endpoint)
        if d.get("status") != "ok":
            err = d.get("error") or {}
            raise SubsonicError("%s: %s (code %s)" % (endpoint, err.get("message", "failed"), err.get("code", "?")))
        return d


def load_nd_creds(a):
    """url / user / password from flags, env, or the credentials file (never typed into chat); a terminal prompts."""
    cfg = {}
    path = a.cred or os.environ.get("MD_ND_CRED") or "/navidrome.cred"
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip().lower()] = v.strip().strip('"').strip("'")
    env = os.environ.get
    url = a.url or env("MD_ND_URL") or cfg.get("url") or ""
    user = a.user or env("MD_ND_USER") or cfg.get("user") or ""
    pw = env("MD_ND_PASS") or cfg.get("password") or ""
    if url and user and not pw and sys.stdin.isatty():
        pw = getpass.getpass("Navidrome password for %s: " % user)
    if url and user and not pw:
        sys.exit("Navidrome password needed, and there is no terminal to ask on (navidrome.cred has no password= line).\n"
                 "From the web console: type it into the password box on the card (used for that run only, never saved).\n"
                 "From dewey.ps1 / dewey.sh: it asks you. Or, only if you accept a stored password, add a line\n"
                 "    password=<your password>\nto navidrome.cred and chmod 600 the file.")
    if not (url and user and pw):
        sys.exit("Navidrome login not found. Create navidrome.cred next to dewey.sh (the Compose install: in the config folder) with two lines:\n"
                 "    url=http://<your-navidrome-address>:4533\n    user=<your Navidrome user>\n"
                 "and you will be asked for the password each run (nothing stored). Only if you accept a stored password,\n"
                 "add a third line  password=<your password>  and chmod 600 the file.")
    return url, user, pw


def pf_slug(name):
    s = re.sub(r"[^\w.-]+", "_", name, flags=re.U).strip("_")
    return s[:60] or "playlist"


def pf_song_brief(e):
    return {"id": e.get("id"), "path": e.get("path"), "title": e.get("title"),
            "artist": e.get("artist"), "album": e.get("album")}


def pf_get_playlist(api, pid):
    d = api.call("getPlaylist", [("id", pid)])["playlist"]
    d["entry"] = aslist(d.get("entry"))
    return d


def pf_write_tracks(api, pl, ids):
    """replace a playlist's tracks (same id, so name/owner/sharing and the links client apps hold survive)"""
    params = [("playlistId", pl["id"])] + [("songId", i) for i in ids]
    api.call("createPlaylist", params, post=True)


def pf_song_state(api, sid):
    s = api.call("getSong", [("id", sid)])["song"]
    return bool(s.get("starred")), int(s.get("userRating") or 0)



def nd_read_tables(db_path, wanted):
    """read-only copy of navidrome.db -> {table: [row dicts]} for the tables/columns that exist"""
    out = {}
    con = nd_connect(db_path)
    try:
        for table, cols in wanted.items():
            have = [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
            use = [c for c in cols if c in have]
            rows = []
            if use:
                for r in con.execute("SELECT %s FROM %s" % (", ".join(use), table)):
                    rows.append(dict(zip(use, r)))
            out[table] = rows
    finally:
        con.close()
    return out


def pf_find_m3u(db_path, m3u_files, music):
    """the .m3u file on disk that a synced playlist was imported from (Navidrome stores its own view of the path)"""
    if not db_path:
        return None
    want = db_path.replace("\\", "/")
    best = None
    for f in m3u_files:
        rel = os.path.relpath(f, music).replace(os.sep, "/")
        if want == rel or want.endswith("/" + rel):
            if best is None or len(rel) > len(best[0]):
                best = (rel, f)
    return best[1] if best else None


def pf_scan_and_wait(api, tries=120):
    api.call("startScan")
    time.sleep(1)
    for _ in range(tries):
        st = api.call("getScanStatus")["scanStatus"]
        if not st.get("scanning"):
            return True
        time.sleep(1)
    return False


def cmd_playlistfix(a):
    if a.restore:
        return pf_restore(a)
    plan_path = a.plan or os.path.join(a.out, "plan.json")
    if not os.path.exists(plan_path):
        sys.exit("no plan at %s - run 'scan' first" % plan_path)
    plan = json.load(open(plan_path, encoding="utf-8"))
    music = os.path.abspath(a.music or plan["music_root"])
    if not a.nd_db or not os.path.exists(a.nd_db):
        sys.exit("Navidrome database not found (%s) - the fixer needs it to look up track ids." % a.nd_db)

    # -- 1. which BLOCKED files can the fixer help with?
    sel = select_for_apply(plan, a)
    targets, nofix = [], []
    for g in sel:
        for m in g["members"]:
            if m["role"] != "blocked" or not m.get("winner"):
                continue
            tags = list(m["blocked_by"])
            pls = [t for t in tags if not t.startswith(STAR_TAG) and not t.startswith(NO_FIX_TAGS)]
            star = any(t.startswith(STAR_TAG) for t in tags)
            if any(t.startswith(NO_FIX_TAGS) for t in tags):
                nofix.append((g, m))          # stays blocked whatever we do to the playlists
            elif pls or star:
                targets.append((g, m, pls, star))
    print("plan generated %s | selection: %d groups | BLOCKED files the fixer can help with: %d" % (
        plan["generated"], len(sel), len(targets)))
    for g, m in nofix:
        print("   cannot fix (%s): %s" % (", ".join(m["blocked_by"]) or "blocked", m["path"]))
    if not targets:
        print("nothing to do.")
        return

    # log in details first: a missing password should stop the run now, not after the slow database reading below
    url, user, pw = load_nd_creds(a)

    rels = set()
    for _, m, _, _ in targets:
        rels.add(m["path"])
        rels.add(m["winner"])
    t_db = time.time()
    nd_connect(a.nd_db, print).close()          # makes the one shared copy and says how big it is and how long the copy took
    info, _, ok = load_navidrome(a.nd_db, rels, lambda s: None)
    if not ok:
        sys.exit("Navidrome's database did not map any of these files - not using it, so nothing was changed.")
    print("read Navidrome's database in %.0f s in total" % (time.time() - t_db))
    by_id, skipped = {}, []
    for g, m, pls, star in targets:
        lid = (info.get(m["path"]) or {}).get("id")
        wid = (info.get(m["winner"]) or {}).get("id")
        if not lid or not wid:
            skipped.append((m["path"], "Navidrome does not know the loser or the kept copy yet (rescan Navidrome)"))
        elif not os.path.exists(os.path.join(music, m["winner"])):
            skipped.append((m["path"], "the kept copy is missing on disk - refusing"))
        else:
            by_id[lid] = {"loser": m["path"], "winner": m["winner"], "wid": wid, "group": g["id"],
                          "star": star, "pls": pls}
    for p, why in skipped:
        print("SKIPPED %s : %s" % (p, why))
    mapping = dict((t["loser"], t["winner"]) for t in by_id.values())

    t_db = time.time()
    dbt = nd_read_tables(a.nd_db, {
        "playlist": ["id", "name", "path", "sync", "rules", "owner_id"],
        "media_file": ["id", "path"]})
    print("read the playlist tables in %.0f s" % (time.time() - t_db))
    meta = dict((r["id"], r) for r in dbt["playlist"])
    file_of = dict((r["id"], r["path"]) for r in dbt["media_file"])

    # -- 2. log in and read the playlists
    api = Subsonic(url, user, pw)
    try:
        api.call("ping")
        lists = aslist((api.call("getPlaylists").get("playlists") or {}).get("playlist"))
    except SubsonicError as e:
        sys.exit("Navidrome: %s" % e)
    print("connected to Navidrome as %s: %d playlists visible" % (user, len(lists)))
    only = set(x.lower() for x in (a.playlist or []))
    m3u_files = find_playlists(music, a.playlist_dir)
    api_work, m3u_work, blocked_pl, seen_names, left_alone = [], {}, [], set(), 0
    for pl in lists:
        if only and pl["name"].lower() not in only:
            continue
        full = pf_get_playlist(api, pl["id"])
        ids = [e["id"] for e in full["entry"]]
        idset = set(ids)
        hits = [by_id[i] for i in ids if i in by_id]
        if not hits:
            continue
        if not only and all(t["wid"] in idset for t in hits):
            left_alone += 1          # already holds the kept copy: it does not block anything, so leave it exactly as it is
            continue
        seen_names.add(pl["name"])
        mt = meta.get(pl["id"], {})
        n_hit = sum(1 for i in ids if i in by_id)
        if mt.get("rules"):
            blocked_pl.append((pl["name"], "a smart playlist: its tracks come from rules, not from a list"))
        elif mt.get("sync") and mt.get("path"):
            f = pf_find_m3u(mt["path"], m3u_files, music)
            if not f:
                blocked_pl.append((pl["name"], "synced from %s but that .m3u was not found under the music folder" % mt["path"]))
            else:
                m3u_work[f] = pl["name"]
        elif pl.get("owner") and pl["owner"] != user and not a.all_users:
            blocked_pl.append((pl["name"], "belongs to '%s' (use --all-users to try as an admin, or --user to log in as them)" % pl["owner"]))
        else:
            present = set(ids)
            new, changes = [], []
            for pos, e in enumerate(full["entry"], 1):
                t = by_id.get(e["id"])
                if t is None:
                    new.append(e["id"])
                elif t["wid"] in present:
                    changes.append(("drop", pos, t))
                else:
                    present.add(t["wid"])
                    new.append(t["wid"])
                    changes.append(("replace", pos, t))
            api_work.append((full, ids, new, changes))
    if only:
        for n in sorted(only - set(p["name"].lower() for p in lists)):
            print("no playlist named '%s' is visible to %s" % (n, user))
    m3u_plans = []
    for f, name in sorted(m3u_work.items()):
        data, rep, drp = rewrite_m3u(f, music, mapping)
        if data is not None:
            m3u_plans.append((f, name, data, rep, drp))

    # -- 3. report
    n_rep = sum(1 for _, _, _, ch in api_work for c in ch if c[0] == "replace")
    n_drop = sum(1 for _, _, _, ch in api_work for c in ch if c[0] == "drop")
    for full, ids, new, changes in api_work:
        print("\nplaylist '%s'  (%d tracks -> %d)  [edited through Navidrome's API]" % (full["name"], len(ids), len(new)))
        for kind, pos, t in changes:
            if kind == "replace":
                print("   #%-4d replace  %s\n          with     %s" % (pos, t["loser"], t["winner"]))
            else:
                print("   #%-4d drop     %s   (the kept copy is already in this playlist)" % (pos, t["loser"]))
    for f, name, data, rep, drp in m3u_plans:
        print("\nplaylist '%s' is synced from %s: %d entries replaced, %d duplicates dropped  [the .m3u is rewritten, "
              "then Navidrome re-imports it]" % (name, os.path.relpath(f, music).replace(os.sep, "/"), rep, drp))
        n_rep += rep
        n_drop += drp
    for name, why in blocked_pl:
        print("\nNOT FIXABLE: playlist '%s' - %s" % (name, why))
    named = set(n for t in by_id.values() for n in t["pls"])
    handled = seen_names | set(n for n, _ in blocked_pl)
    unseen = sorted(named - handled) if not only else []
    if unseen:
        print("\nthe plan says these playlists hold blocked files but %s cannot see them (private to another user?): %s" % (
            user, ", ".join(unseen)))

    stars = []
    if not a.no_stars and not only:
        for lid, t in by_id.items():
            if not t["star"]:
                continue
            try:
                ls, lr = pf_song_state(api, lid)
                ws, wr = pf_song_state(api, t["wid"])
            except SubsonicError as e:
                print("could not read star/rating for %s: %s" % (t["loser"], e))
                continue
            need_star, need_rate = ls and not ws, lr > wr
            if need_star or need_rate:
                stars.append((t, need_star, lr if need_rate else 0))
            elif not (ls or lr):
                print("note: %s is starred/rated by another user, not %s; nothing to carry over" % (t["loser"], user))
        if stars:
            print("\nstar / rating carry-over (as %s):" % user)
            for t, ns, rt in stars:
                print("   %s  ->  %s" % (" + ".join(x for x in ("star" if ns else "", ("rating %d" % rt) if rt else "") if x), t["winner"]))
    elif only and any(t["star"] for t in by_id.values()):
        print("\n(star / rating carry-over is skipped when --playlist is used)")

    if left_alone:
        print("\n%d other playlist(s) hold one of these files but already hold the kept copy too - left untouched." % left_alone)
    print("\n%d replacements, %d duplicate entries dropped | %d playlists via the API, %d synced .m3u files, %d star/rating carry-overs" % (
        n_rep, n_drop, len(api_work), len(m3u_plans), len(stars)))
    if not a.execute:
        print("DRY RUN - nothing changed. Add --execute to do it.")
        return
    if not (api_work or m3u_plans or stars):
        print("nothing to do.")
        return

    # -- 4. back everything up first, then write
    run = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(a.out, "playlist-backups", run)
    os.makedirs(os.path.join(bdir, "m3u"), exist_ok=True)
    index = {"run": run, "playlists": [], "m3u": []}
    for n, (full, ids, new, changes) in enumerate(api_work, 1):
        fn = "%03d-%s.json" % (n, pf_slug(full["name"]))
        entries = []
        for e in full["entry"]:
            b = pf_song_brief(e)
            b["file"] = file_of.get(e["id"])          # the real path Navidrome has (the API's "path" is a display path)
            entries.append(b)
        with open(os.path.join(bdir, fn), "w", encoding="utf-8") as fh:
            json.dump({"id": full["id"], "name": full["name"], "comment": full.get("comment", ""),
                       "public": bool(full.get("public")), "owner": full.get("owner"),
                       "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "entry": entries}, fh, indent=1, ensure_ascii=False)
        index["playlists"].append(fn)
    for f, name, data, rep, drp in m3u_plans:
        shutil.copy2(f, os.path.join(bdir, "m3u", os.path.basename(f)))
        index["m3u"].append({"file": os.path.basename(f), "target": os.path.relpath(f, music).replace(os.sep, "/")})
    with open(os.path.join(bdir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1)
    print("\nplaylists backed up to %s" % bdir)

    failed = 0
    for full, ids, new, changes in api_work:
        try:
            pf_write_tracks(api, full, new)
        except SubsonicError as e:
            failed += 1
            print("   FAILED  %s: %s  (left unchanged)" % (full["name"], e))
            continue
        try:
            back = pf_get_playlist(api, full["id"])
            if [e["id"] for e in back["entry"]] != new:
                raise SubsonicError("Navidrome's copy does not match what was written")
            if (back["name"], back.get("comment", ""), bool(back.get("public"))) != (
                    full["name"], full.get("comment", ""), bool(full.get("public"))):
                api.call("updatePlaylist", [("playlistId", full["id"]), ("name", full["name"]),
                                            ("comment", full.get("comment", "")),
                                            ("public", "true" if full.get("public") else "false")], post=True)
            print("   ok      %s  (%d tracks)" % (full["name"], len(new)))
        except SubsonicError as e:
            failed += 1
            print("   FAILED  %s: %s" % (full["name"], e))
            try:
                pf_write_tracks(api, full, ids)
                print("           put the original tracks back")
            except SubsonicError as e2:
                print("           could NOT restore it (%s) - use the backup in %s" % (e2, bdir))
    if m3u_plans:
        for f, name, data, rep, drp in m3u_plans:
            with open(f, "wb") as fh:
                fh.write(data)                      # new mtime on purpose: that is what makes Navidrome re-import it
            print("   ok      rewrote %s" % os.path.relpath(f, music).replace(os.sep, "/"))
        try:
            done = pf_scan_and_wait(api)
        except SubsonicError as e:
            done = False
            print("   (could not start a Navidrome scan: %s)" % e)
        if not done:
            print("   NOTE: run a Navidrome scan so it re-imports the rewritten .m3u playlists")
        else:
            for f, name, data, rep, drp in m3u_plans:
                pls = [p for p in aslist((api.call("getPlaylists").get("playlists") or {}).get("playlist")) if p["name"] == name]
                ok_pl = bool(pls)
                if ok_pl:
                    have = set(e["id"] for e in pf_get_playlist(api, pls[0]["id"])["entry"])
                    ok_pl = not any(i in have for i in by_id)
                print("   %s  %s re-imported by Navidrome" % ("ok     " if ok_pl else "CHECK  ", name))
                if not ok_pl:
                    failed += 1
    for t, ns, rt in stars:
        try:
            if ns:
                api.call("star", [("id", t["wid"])], post=True)
            if rt:
                api.call("setRating", [("id", t["wid"]), ("rating", rt)], post=True)
            print("   ok      star/rating carried to %s" % t["winner"])
        except SubsonicError as e:
            failed += 1
            print("   FAILED  star/rating for %s: %s" % (t["winner"], e))
    print("\ndone%s. Next: run  scan  then  apply  - these files are no longer BLOCKED by playlists." % (
        " with %d problem(s)" % failed if failed else ""))
    print("undo with:  playlistfix --restore %s --execute" % bdir)


def pf_restore(a):
    path = a.restore
    idx_path = os.path.join(path, "index.json") if os.path.isdir(path) else None
    if idx_path and os.path.exists(idx_path):
        index = json.load(open(idx_path, encoding="utf-8"))
        files = [os.path.join(path, f) for f in index.get("playlists", [])]
        m3us = index.get("m3u", [])
    elif os.path.isfile(path):
        files, m3us, index = [path], [], {}
    else:
        sys.exit("no playlist backup found at %s" % path)
    saved = [json.load(open(f, encoding="utf-8")) for f in files]
    music = os.path.abspath(a.music) if a.music else None
    cur_by_file = {}
    cur_ids = set()
    if a.nd_db and os.path.exists(a.nd_db):
        for r in nd_read_tables(a.nd_db, {"media_file": ["id", "path"]})["media_file"]:
            cur_by_file[r["path"]] = r["id"]
            cur_ids.add(r["id"])
    url, user, pw = load_nd_creds(a)
    api = Subsonic(url, user, pw)
    try:
        api.call("ping")
        lists = dict((p["id"], p) for p in aslist((api.call("getPlaylists").get("playlists") or {}).get("playlist")))
    except SubsonicError as e:
        sys.exit("Navidrome: %s" % e)
    print("restoring %d playlist(s) and %d .m3u file(s) from %s as %s" % (len(saved), len(m3us), path, user))
    for b in saved:
        ids, unresolved = [], 0
        for e in b["entry"]:
            cur = e["id"] if e["id"] in cur_ids else cur_by_file.get(e.get("file") or "")
            if cur:
                ids.append(cur)
            else:
                unresolved += 1
        exists = b["id"] in lists
        print("   '%s': %d tracks%s%s" % (b["name"], len(ids),
              "" if exists else "  (playlist no longer exists - will be re-created)",
              ("  (%d tracks no longer in Navidrome - left out)" % unresolved) if unresolved else ""))
        if not a.execute:
            continue
        try:
            if exists:
                pf_write_tracks(api, {"id": b["id"]}, ids)
                api.call("updatePlaylist", [("playlistId", b["id"]), ("name", b["name"]), ("comment", b.get("comment", "")),
                                            ("public", "true" if b.get("public") else "false")], post=True)
            else:
                api.call("createPlaylist", [("name", b["name"])] + [("songId", i) for i in ids], post=True)
            print("   ok      %s" % b["name"])
        except SubsonicError as e:
            print("   FAILED  %s: %s" % (b["name"], e))
    for m in m3us:
        src = os.path.join(path, "m3u", m["file"])
        dst = os.path.join(music, m["target"]) if music else None
        print("   .m3u %s -> %s" % (m["file"], m["target"]))
        if a.execute and dst:
            shutil.copy2(src, dst)
            os.utime(dst, None)
            print("   ok      restored %s (Navidrome re-imports it on its next scan)" % m["target"])
    if not a.execute:
        print("DRY RUN - add --execute")
    elif m3us:
        try:
            pf_scan_and_wait(api)
        except SubsonicError:
            print("   (run a Navidrome scan so it re-imports the restored .m3u playlists)")



# ---------------------------------------------------------------------------
# reorg: one folder layout for the whole library
#     Artist/Album (YYYY)/NN - Title.ext        (multi-disc: D-NN, compilations: Various Artists/...)
# Move-only: tags are never changed. Every move is journalled so it can be undone.
# ---------------------------------------------------------------------------
LAYOUT_DOC = "Artist/Album (YYYY)/NN - Title.ext"
FOLDER_SPLIT = re.compile(r"\s*;\s*|\s+(?:feat\.?|ft\.?|featuring)\s+", re.I)
VARIOUS_KEYS = {"variousartists", "various", "va"}
REAL_EXT = {"flac": ".flac", "mp3": ".mp3", "m4a": ".m4a", "ogg": ".ogg", "wav": ".wav", "ape": ".ape"}
WIN_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}
ART_EXT = {".jpg", ".jpeg", ".png", ".gif", ".nfo", ".txt", ".log", ".cue"}   # album-level extras that travel with the album
REORG_JOURNAL = "journal.jsonl"
VA_NAME = "Various Artists"


def rg_clean(s, maxlen=110, maxbytes=180):
    """One path component, made safe for Linux + SMB/Windows. Same replacements Beets uses by default."""
    s = unicodedata.normalize("NFC", s or "")
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = re.sub(r"[\\/]", "_", s)
    s = re.sub(r'[<>:"?*|]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lstrip(". ")             # "...Baby One More Time" -> "Baby One More Time" (a leading dot would also hide the folder)
    if len(s) > maxlen:
        s = s[:maxlen]
    while len(s.encode("utf-8")) > maxbytes:        # CJK / emoji titles: Linux file names are limited to 255 BYTES
        s = s[:-1]
    s = s.rstrip(". ")
    if not s:
        s = "_"
    if s.lower() in WIN_RESERVED:
        s = "_" + s
    return s


def rg_primary(name):
    """First artist of a multi-artist tag. Splits on ';' and feat/ft only, so 'Earth, Wind & Fire' stays whole."""
    parts = [p.strip() for p in FOLDER_SPLIT.split((name or "").strip()) if p and p.strip()]
    return parts[0] if parts else (name or "").strip()


def rg_key(name):
    # accents, case and punctuation are ignored; a leading "The" is NOT (HIM / The HIM, Dream / The-Dream and Blaze / The Blaze
    # are different acts - use a reorg-artist rule when "Beatles" and "The Beatles" really are the same folder)
    return compact(name)


def rg_best(counter):
    """Most common spelling, but an all-lowercase spelling ("felix jaehn") loses to any other one ("Felix Jaehn");
    ties go alphabetically so the choice is deterministic."""
    def demoted(n):
        return n == n.lower() and any(m != n for m in counter)
    return max(counter, key=lambda n: (not demoted(n), counter[n], n))


def rg_has_collision(members):
    seen = {}
    for t, _it, _alb, _va in members:
        if t.track > 0:
            k = (max(t.disc, 1), t.track)
            ft = compact(t.tag_title)
            if k in seen and seen[k] != ft:
                return True
            seen[k] = ft
    return False


NAME_DIR_RX = re.compile(r"^\[(\d{4})\]\s+(.+?)\s+-\s+(.+)$")


def rg_from_names(rel):
    """Artist, album, year and title from the downloader layout  [YYYY] Artist(s) - Album/Title - Artist(s).ext
    Accepted only when the file name ends in the same artist string as the folder (a built-in cross-check);
    anything that does not fit exactly is left alone."""
    parts = rel.split("/")
    if len(parts) != 2:
        return None
    m = NAME_DIR_RX.match(parts[0])
    if not m:
        return None
    year, artists, album = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
    stem = os.path.splitext(parts[1])[0]
    cut = stem.rfind(" - ")
    if cut <= 0:
        return None
    title, tail = stem[:cut].strip(), stem[cut + 3:].strip()
    if not title or not compact(tail) or compact(tail) != compact(artists):
        return None
    album = EXPL_RX.sub("", album).strip() or album
    return {"artist": artists, "album": album, "year": year, "title": title}


def rg_from_placed(rel):
    """A tag-less file an earlier reorg already placed:  Artist/Album (YYYY)/Title - Artist.ext
    Same cross-check as the downloader layout: the file name must end in the folder's artist."""
    parts = rel.split("/")
    if len(parts) != 3:
        return None
    m = re.match(r"^(.+?)\s+\((\d{4})\)$", parts[1])
    if not m:
        return None
    stem = os.path.splitext(parts[2])[0]
    cut = stem.rfind(" - ")
    if cut <= 0:
        return None
    title, tail = stem[:cut].strip(), stem[cut + 3:].strip()
    if not title or compact(rg_primary(tail)) != compact(parts[0]):
        return None
    return {"artist": tail, "album": m.group(1).strip(), "year": int(m.group(2)), "title": title}


def reorg_history(out):
    """current path -> the path it had before any reorg run moved it (from out/reorg-moves.tsv); lets reorg-set rules keep
    matching a file after it has been moved."""
    origin = {}
    try:
        with open(os.path.join(out, "reorg-moves.tsv"), encoding="utf-8") as fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if len(f) >= 3 and f[1] and f[2]:
                    origin[f[2]] = origin.get(f[1], f[1])
    except OSError:
        pass
    return origin


def reorg_plan(tracks, rules, music=None, moved_from=None):
    """Work out where every file belongs. Pure function of the tags (and rules); touches no file."""
    items = []
    elig = []
    unk_paths = set()      # files with no album tag that a reorg-unknown-album rule files under one folder
    keep_names = set()     # tag-less files keep their file name: Navidrome identifies a file without a title tag by its name
    for t in tracks:
        it = {"path": t.rel, "to": "", "action": "skip", "reason": "", "size": t.size, "mtime": t.mtime,
              "artist": "", "album": "", "year": 0, "track": t.track, "disc": t.disc, "title": t.tag_title,
              "ext_fix": False, "lrc": bool(t.has_lrc)}
        items.append(it)
        skip = next((p for p in rules.reorg_skip if t.rel == p or t.rel.startswith(p.rstrip("/") + "/")), None)
        ov = rules.reorg_set.get(t.rel) or rules.reorg_set.get((moved_from or {}).get(t.rel, ""))
        if skip:
            it["reason"] = "reorg-skip rule: %s" % skip
            continue
        if t.error:
            it["reason"] = "unreadable: %s" % t.error
            continue
        if t.note and not ov:
            it["reason"] = "tag blocks are damaged - fix the tags first (%s)" % t.note
            continue
        tagless = not t.tag_title or not (t.tag_artist or t.albumartist)
        if ov or tagless:
            t = copy.copy(t)       # the plan reads the tags; the overrides below never leak into the scanned tracks
        if ov:
            if ov["artist"]:
                t.tag_artist, t.albumartist = ov["artist"], ""
            if ov["album"]:
                t.album = ov["album"]
            if ov["year"]:
                t.year = ov["year"]
            if ov["track"]:
                t.track = ov["track"]
            if ov["title"]:
                t.tag_title = ov["title"]
            if not t.tag_title:
                t.tag_title = posixpath.splitext(posixpath.basename(t.rel))[0]
            if not (t.tag_artist or t.albumartist):
                it["reason"] = "reorg-set rule: this file has no artist tag, so the rule needs an artist"
                continue
            if t.track <= 0 and compact(t.album) == compact(t.tag_title):
                t.track = 1        # the rule makes it a one-song release
            if tagless:
                keep_names.add(t.rel)
            it["src"] = "rule"
        elif tagless:
            nm = (rg_from_names(t.rel) or rg_from_placed(t.rel)) if rules.reorg_names else None
            if not nm:
                it["reason"] = ("artist/title tags are missing and the names do not follow [YYYY] Artist - Album/Title - Artist "
                                "(a reorg-set line in rules.txt can place it)")
                continue
            # only the gaps are filled: a tag that exists is never overruled by a folder name
            if not (t.tag_artist or t.albumartist):
                t.tag_artist = nm["artist"]
            if not t.tag_title:
                t.tag_title = nm["title"]
            if not t.album:
                t.album = nm["album"]
            if not t.year:
                t.year = nm["year"]
            keep_names.add(t.rel)
            it["src"] = "names"
        album = t.album.strip()
        if not album:
            if rules.reorg_unknown_album:
                album = rules.reorg_unknown_album
                unk_paths.add(t.rel)
            else:
                # a song with no album is a single: its own one-song release named after the song, the way the rest of
                # the library already files singles (Artist/Title (YYYY)/01 - Title.ext)
                album = t.tag_title
                t = copy.copy(t) if not (ov or tagless) else t
                t.track = 1
                it["src"] = it.get("src") or "single"
        it["track"], it["title"] = t.track, t.tag_title
        elig.append((t, it, album))

    # artist spellings: one folder per artist, spelled the way most of that artist's tracks spell it
    spell = defaultdict(Counter)
    for t, it, album in elig:
        spell[rg_key(rg_primary(t.albumartist or t.tag_artist))][rg_primary(t.albumartist or t.tag_artist)] += 1
    alias = {}
    for k, v in rules.reorg_artist.items():
        alias[k] = v
    canon_artist = {}
    for k, c in spell.items():
        canon_artist[k] = alias.get(k) or rg_best(c)

    groups = defaultdict(list)
    notes = {"va_albums": Counter(), "split_by_year": [], "spelling_merged": [], "scattered": []}
    for t, it, album in elig:
        pa = rg_primary(t.albumartist or t.tag_artist)
        va = bool(t.comp_tag) or compact(pa) in VARIOUS_KEYS or compact(album) in rules.reorg_various
        if va:
            ak, adisp = "various", VA_NAME
        else:
            ak = rg_key(pa)
            adisp = canon_artist.get(ak, pa)
            ak = rg_key(adisp)
        groups[(ak, compact(album) + ("#unk" if t.rel in unk_paths else ""))].append((t, it, album, va, adisp))
    for k, c in spell.items():
        if len(c) > 1:
            notes["spelling_merged"].append("%s  <-  %s" % (canon_artist[k], ", ".join(sorted(n for n in c if n != canon_artist[k]))))

    folders = {}      # lower-case 'parent|name' -> first spelling used, so SMB/Windows never sees two folders that differ only by case

    def folder(parent, name):
        k = (parent + "|" + name).lower()
        return folders.setdefault(k, name)

    for gk, mem in sorted(groups.items(), key=lambda kv: kv[0]):
        adisp = mem[0][4]
        va = mem[0][3]
        members = [(t, it, alb, va_) for t, it, alb, va_, _ in mem]
        parts = [members]
        years = Counter(t.year for t, _, _, _ in members if t.year)
        if len(years) >= 2 and rg_has_collision(members):
            dom = max(years, key=lambda y: (years[y], -y))
            by_year = defaultdict(list)
            for m in members:
                by_year[m[0].year or dom].append(m)
            parts = [by_year[y] for y in sorted(by_year)]
            notes["split_by_year"].append("%s / %s: %s" % (adisp, rg_best(Counter(m[2] for m in members)),
                                                             ", ".join(str(y) for y in sorted(by_year))))
        for sub in parts:
            alb_disp = rg_best(Counter(m[2] for m in sub))
            ys = Counter(m[0].year for m in sub if m[0].year)
            year = max(ys, key=lambda y: (ys[y], -y)) if ys else 0
            if va and len(ys) > 1 and ys[year] < 0.6 * sum(ys.values()):
                year = 0        # a compilation whose tracks carry their own recording years has no honest album year
            unk = all(m[0].rel in unk_paths for m in sub)
            if unk:
                year = 0        # the "no album" bucket has no year and no track numbers: its songs come from different releases
            discs = {m[0].disc for m in sub if m[0].disc > 0}
            multi = len(discs) > 1 or (max(discs) > 1 if discs else False)
            maxtrack = max([m[0].track for m in sub] + [0])
            width = 3 if maxtrack >= 100 else 2
            a_dir = folder("", rg_clean(adisp) or "Unknown Artist")
            al_name = rg_clean(alb_disp) or "Unknown Album"
            has_year = bool(re.search(r"\(\d{4}\)\s*$", al_name))     # the album title already ends in a year: don't add a second one
            al_dir = "%s (%d)" % (al_name, year) if year and not has_year else al_name
            if year and not has_year:
                # an album that already sits in an  Album (YYYY)  folder keeps that folder, so a later run (or one new
                # track with another year tag) never moves a whole album just because the year vote changed
                inc = Counter()
                for m in sub:
                    parts_ = posixpath.dirname(m[0].rel).split("/")
                    if len(parts_) == 2 and parts_[0].lower() == a_dir.lower():
                        mt = re.match(r"^(.*) \((\d{4})\)$", parts_[1])
                        if mt and mt.group(1).lower() == al_name.lower():
                            inc[parts_[1]] += 1
                if inc:
                    al_dir = inc.most_common(1)[0][0]
            al_dir = folder(a_dir, al_dir)
            if va:
                notes["va_albums"][al_dir] += len(sub)
            for t, it, alb, va_ in sub:
                title = rg_clean(t.tag_title, 120) or "Untitled"
                if va:
                    title = "%s - %s" % (rg_clean(rg_primary(t.tag_artist) or "Unknown Artist", 60), title)
                if t.rel in keep_names:
                    # no title tag: Navidrome identifies the file by its name, and a renamed file would lose its stars,
                    # ratings and playlist spots. It still moves into the right folder.
                    stem = rg_clean(posixpath.splitext(posixpath.basename(t.rel))[0], 170, 220) or title
                elif t.track > 0 and not unk:
                    num = ("%d-%0*d" % (t.disc or 1, width, t.track)) if multi else ("%0*d" % (width, t.track))
                    stem = "%s - %s" % (num, title)
                else:
                    stem = title
                stem = stem[:170]
                while len(stem.encode("utf-8")) > 220:
                    stem = stem[:-1]
                stem = stem.rstrip(". ")
                if t.mislabeled and t.real_fmt in REAL_EXT:
                    ext = REAL_EXT[t.real_fmt]
                    it["ext_fix"] = ext != t.ext
                else:
                    ext = t.ext.lower()
                it["to"] = "%s/%s/%s%s" % (a_dir, al_dir, stem, ext)
                it["artist"], it["album"], it["year"] = a_dir, al_dir, year

    # decide actions. Files that want the same name (same album, track and title: Explicit/Clean pairs, copies from the
    # old [YYYY] Artist - Album layout, unresolved duplicates): the one already in place keeps the plain name, then an
    # unlabeled copy before a labeled one, then path order. The others get a suffix - their own label
    # ("05 - Song (Explicit).flac") or a number ("05 - Song (2).flac") - so nothing is left behind in the old folders
    # and nothing is overwritten. Which of them to keep is the dedupe's job, not the reorg's.
    by_rel = dict((t.rel, t) for t in tracks)
    notes["suffixed"] = []
    claimed = {}

    def label_of(it):
        t = by_rel.get(it["path"])
        return adv_label(it["path"], t.tag_title if t else "")

    def with_suffix(to, suffix):
        root, ext = posixpath.splitext(to)
        return root + suffix + ext

    order = sorted((it for it in items if it["to"]),
                   key=lambda i: (i["to"] != i["path"], bool(label_of(i)), i["path"]))
    for it in order:
        key = it["to"].lower()
        if key in claimed and claimed[key] != it["path"]:
            owner = claimed[key]
            base, lab = it["to"], label_of(it)
            cands = [with_suffix(base, " (%s)" % lab.capitalize())] if lab else []
            cands += [with_suffix(base, " (%d)" % n) for n in range(2, 60)]
            pick = next((c for c in cands if claimed.get(c.lower(), it["path"]) == it["path"]), None)
            if pick is None:
                it["action"] = "held"
                it["reason"] = "same target as %s and no free suffix" % owner
                continue
            it["to"] = pick
            it["suffixed"] = True
            it["reason"] = "same album, track and title as %s: named %s so both stay" % (
                owner, posixpath.basename(pick))
            notes["suffixed"].append("%s  =>  %s   (plain name belongs to %s)" % (it["path"], pick, owner))
            key = pick.lower()
        claimed[key] = it["path"]
        if it["to"] == it["path"]:
            it["action"] = "ok"
        elif posixpath.dirname(it["to"]) == posixpath.dirname(it["path"]):
            it["action"] = "rename"
        else:
            it["action"] = "move"
        if it["ext_fix"]:
            it["reason"] = "extension fixed: the content is %s, not %s" % (
                REAL_EXT_NAME.get(posixpath.splitext(it["to"])[1], "?"), posixpath.splitext(it["path"])[1])
    paths = {i["path"].lower() for i in items}
    if music:
        for it in items:
            if it["action"] in ("move", "rename") and os.path.exists(os.path.join(music, it["to"])) \
                    and it["to"].lower() not in paths:
                it["action"] = "held"
                it["reason"] = "the target path is already taken by a file that is not part of this plan"
    # albums whose tracks are spread over several artist folders (compilations that are not flagged as such)
    spread = defaultdict(set)
    for it in items:
        if it["to"] and it["action"] != "held":
            spread[(compact(re.sub(r"\s*\(\d{4}\)$", "", it["album"])))].add(it["artist"])
    notes["scattered"] = sorted(
        ("%s (%d artist folders)" % (k, len(v)) for k, v in spread.items() if len(v) >= 4 and VA_NAME not in v))
    items.sort(key=lambda i: (i["to"] or "~" + i["path"]).lower())
    return items, notes


REAL_EXT_NAME = {".flac": "FLAC", ".mp3": "MP3", ".m4a": "M4A/AAC", ".ogg": "Ogg", ".wav": "WAV", ".ape": "APE"}


def reorg_summary(items):
    c = Counter(i["action"] for i in items)
    s = {"files": len(items), "ok": c["ok"], "move": c["move"], "rename": c["rename"], "held": c["held"],
         "skip": c["skip"], "ext_fix": sum(1 for i in items if i["ext_fix"] and i["action"] in ("move", "rename")),
         "loose": sum(1 for i in items if "/" not in i["path"] and i["action"] in ("move", "rename")),
         "suffixed": sum(1 for i in items if i.get("suffixed") and i["action"] != "held"),
         "from_names": sum(1 for i in items if i.get("src") == "names" and i["action"] in ("move", "rename", "ok")),
         "from_rule": sum(1 for i in items if i.get("src") == "rule" and i["action"] in ("move", "rename", "ok")),
         "singles": sum(1 for i in items if i.get("src") == "single" and i["action"] in ("move", "rename", "ok"))}
    s["bytes_moved"] = sum(i["size"] for i in items if i["action"] in ("move", "rename"))
    s["skip_reasons"] = Counter(re.sub(r"\(.*$", "", i["reason"]).strip() or i["reason"] for i in items if i["action"] == "skip")
    return s


def write_reorg_reports(out, items, notes, meta):
    s = reorg_summary(items)
    with open(os.path.join(out, "reorg-plan.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": VERSION, "generated": meta["generated"], "music_root": meta["music_root"],
                   "summary": {k: (dict(v) if isinstance(v, Counter) else v) for k, v in s.items()},
                   "items": items}, fh, ensure_ascii=False)
    with open(os.path.join(out, "reorg-plan.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["action", "from", "to", "reason", "artist_folder", "album_folder", "track", "title", "size", "source"])
        for i in items:
            w.writerow([i["action"], i["path"], i["to"], i["reason"], i["artist"], i["album"], i["track"], i["title"], i["size"], i.get("src", "tags")])
    h = []
    A = h.append
    A("<!doctype html><meta charset=utf-8><title>Dewey reorg plan</title><style>%s</style>" % CSS)
    A("<h1>reorg plan</h1><div class=sub>generated %s &middot; v%s &middot; library %s &middot; layout <b>%s</b> &middot; nothing has been moved</div>" % (
        meta["generated"], VERSION, html.escape(meta["music_root"]), LAYOUT_DOC))
    A("<div class=cards>")
    for label, val in (("files", "{:,}".format(s["files"])), ("already in place", "{:,}".format(s["ok"])),
                       ("to move", "{:,}".format(s["move"])), ("rename only", "{:,}".format(s["rename"])),
                       ("extension fixes", s["ext_fix"]), ("loose root files placed", s["loose"]),
                       ("named with a suffix", s["suffixed"]), ("placed from file names", s["from_names"]),
                       ("single-song releases", s["singles"]), ("held", s["held"]), ("skipped (tags / rules)", s["skip"]),
                       ("data moved", human(s["bytes_moved"]))):
        A("<div class=card><b>%s</b><span>%s</span></div>" % (val, label))
    A("</div>")
    held = [i for i in items if i["action"] == "held"]
    skipped = [i for i in items if i["action"] == "skip"]
    if held or skipped:
        A("<h2>Not moved - needs you</h2>")
        for title, rows in (("Held: the target is taken by a file outside the plan", held), ("Skipped: tags or rules", skipped)):
            if not rows:
                continue
            A("<details><summary>%s<small>%d files &middot; all listed in reorg-plan.csv</small></summary><div><table>" % (title, len(rows)))
            for i in rows[:400]:
                A("<tr><td class=path>%s</td><td class=dim>%s</td></tr>" % (html.escape(i["path"]), html.escape(i["reason"])))
            if len(rows) > 400:
                A("<tr><td colspan=2 class=dim>... and %d more (see reorg-plan.csv)</td></tr>" % (len(rows) - 400))
            A("</table></div></details>")
    A("<h2>Worth a look before you apply</h2>")
    for title, rows, hint in (
            ("Various Artists albums", ["%s  (%d tracks)" % (k, v) for k, v in sorted(notes["va_albums"].items())],
             "Compilation flag or 'Various Artists' album artist in the tags. Files are named NN - Artist - Title."),
            ("Named with a suffix: same album, track and title", sorted(notes.get("suffixed", [])),
             "Two or more files want the same name. The one already in place (or the unlabeled one) keeps the plain name; the others become '(Explicit)' / '(Clean)' or '(2)'. Nothing is overwritten and nothing is removed: decide which to keep with the dedupe afterwards."),
            ("Placed from file names (no tags in the file)", sorted(
                "%s  =>  %s" % (i["path"], i["to"]) for i in items if i.get("src") == "names" and i["action"] in ("move", "rename")),
             "These files have no artist or title tag. Each one sits in a [YYYY] Artist - Album folder and its file name ends in the same artist, "
             "which is the check that lets the tool trust the names. They keep their file names (so Navidrome keeps the stars, ratings and playlist spots), "
             "move into the right Artist/Album (YYYY) folder, and their tags stay empty. Turn this off with reorg-names: off."),
            ("Single-song releases (no album tag)", sorted(
                "%s  =>  %s" % (i["path"], i["to"]) for i in items if i.get("src") == "single" and i["action"] in ("move", "rename"))[:300],
             "No album tag, so each file becomes its own release named after the song, track 01, the way the rest of the library files singles. "
             "A title like 'Artist - Song (Official Video)' makes an ugly folder: fix those with a reorg-set line, or set reorg-unknown-album to gather them under one folder."),
            ("Placed by a reorg-set rule", sorted(
                "%s  =>  %s" % (i["path"], i["to"]) for i in items if i.get("src") == "rule" and i["action"] in ("move", "rename")),
             "Placed exactly as the rule says. The tags in the file are not touched."),
            ("Artist spellings merged", sorted(notes["spelling_merged"]),
             "The same artist was tagged with different spellings; all go to the most common one. Override with reorg-artist in rules.txt."),
            ("Albums split by year", notes["split_by_year"],
             "Same album title and artist, different years and overlapping track numbers: kept as separate releases."),
            ("Albums spread across 4+ artist folders", notes["scattered"],
             "Probably compilations that are not flagged as such. Add reorg-various: <album> to rules.txt to gather them under Various Artists.")):
        if rows:
            A("<details><summary>%s<small>%d</small></summary><div><div class=note>%s</div><table>" % (title, len(rows), html.escape(hint)))
            for r in rows[:300]:
                A("<tr><td class=path>%s</td></tr>" % html.escape(r))
            if len(rows) > 300:
                A("<tr><td class=dim>... and %d more</td></tr>" % (len(rows) - 300))
            A("</table></div></details>")
    A("<h2>Every change, by artist</h2><input id=q placeholder='filter by artist / album / file name'><div style='height:10px'></div>")
    by_artist = defaultdict(list)
    for i in items:
        if i["action"] in ("move", "rename"):
            by_artist[i["artist"]].append(i)
    for artist in sorted(by_artist, key=str.lower):
        rows = by_artist[artist]
        blob = " ".join((r["path"] + " " + r["to"]) for r in rows).lower()
        A("<details class=artist data-s=\"%s\"><summary>%s<small>%d files</small></summary><div><table>" % (
            html.escape(blob[:4000], quote=True), html.escape(artist), len(rows)))
        for r in rows:
            A("<tr><td class=path>%s</td><td class=dim>&rarr;</td><td class=path>%s</td><td class=dim>%s</td></tr>" % (
                html.escape(r["path"]), html.escape(r["to"]), html.escape(r["reason"])))
        A("</table></div></details>")
    A("<script>%s</script>" % JS)
    with open(os.path.join(out, "reorg-report.html"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(h))
    return s


def cmd_reorg(a):
    music = os.path.abspath(a.music)
    if not os.path.isdir(music):
        sys.exit("music folder not found: %s" % music)
    os.makedirs(a.out, exist_ok=True)
    log = make_logger(a.out)
    rules = load_rules(a.rules)
    log("Dewey %s reorg plan for %s (layout %s)" % (VERSION, music, LAYOUT_DOC))
    raw, leftovers = scan_library(music, os.path.join(a.out, "cache.db"), a.workers, rules, log)
    tracks = [make_track(rel, d, rules) for rel, d in sorted(raw.items())]
    items, notes = reorg_plan(tracks, rules, music, reorg_history(a.out))
    meta = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "music_root": music}
    s = write_reorg_reports(a.out, items, notes, meta)
    log("-" * 60)
    log("%d files | already in place %d | to move %d | rename only %d | named with a suffix %d | held %d | skipped %d" % (
        s["files"], s["ok"], s["move"], s["rename"], s["suffixed"], s["held"], s["skip"]))
    log("extension fixes %d | loose root files placed %d | data touched %s" % (s["ext_fix"], s["loose"], human(s["bytes_moved"])))
    log("placed from file names %d | single-song releases %d | placed by reorg-set %d" % (s["from_names"], s["singles"], s["from_rule"]))
    for why, n in s["skip_reasons"].most_common():
        log("   skipped %d: %s" % (n, why))
    if leftovers:
        log("%d hidden leftover download fragments are ignored (see filehealth.csv from a scan)" % len(leftovers))
    log("report: %s" % os.path.join(a.out, "reorg-report.html"))
    log("nothing was changed. next: read the report, then  reorg-apply --limit 50  (dry run), then --execute.")


def reorg_select(plan, music, a):
    """Moves from the saved plan that are still valid on disk, after the --artist / --album / --limit filters."""
    todo, skipped = [], []
    a.already_moved = 0
    want_artist = (a.artist or "").lower()
    want_album = (a.album or "").lower()
    items = [i for i in plan["items"] if i["action"] in ("move", "rename")]
    for it in items:
        if want_artist and want_artist not in it["artist"].lower():
            continue
        if want_album and want_album not in it["album"].lower():
            continue
        src, dst = os.path.join(music, it["path"]), os.path.join(music, it["to"])
        try:
            st = os.stat(src)
        except OSError:
            try:
                if os.stat(dst).st_size == it["size"]:
                    a.already_moved += 1            # an earlier batch of this same plan did it
                    continue
            except OSError:
                pass
            skipped.append((it, "file is gone"))
            continue
        if st.st_size != it["size"] or abs(st.st_mtime - it["mtime"]) > 0.001:
            skipped.append((it, "file changed since the plan - run reorg again"))
            continue
        if os.path.lexists(dst) and not os.path.samefile(src, dst):
            skipped.append((it, "target already exists (an earlier move in this plan may still have to go first - run reorg again)"))
            continue
        todo.append(it)
    if a.limit and len(todo) > a.limit:
        # finish the album that crosses the limit, so no album is left half moved
        cut = a.limit
        last = posixpath.dirname(todo[cut - 1]["to"])
        while cut < len(todo) and posixpath.dirname(todo[cut]["to"]) == last:
            cut += 1
        todo = todo[:cut]
    return todo, skipped


def nd_backup(db_path, dest):
    """Consistent single-file copy of the live Navidrome database (copy db+wal+shm, then VACUUM INTO)."""
    tmp = tempfile.mkdtemp(prefix="md_ndbak_")
    try:
        base = os.path.basename(db_path)
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suf):
                shutil.copy2(db_path + suf, os.path.join(tmp, base + suf))
        con = sqlite3.connect(os.path.join(tmp, base))
        try:
            con.execute("VACUUM INTO ?", (dest,))
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return os.path.getsize(dest)


def reorg_handover_extras(old_dir, music, moved_to_dirs, journal, log):
    """Cover art / nfo / cue left behind in a folder that lost all its audio travel to the album's new folder."""
    try:
        names = os.listdir(old_dir)
    except OSError:
        return
    if any(os.path.splitext(n)[1].lower() in AUDIO_EXT for n in names):
        return                      # audio still here: this folder stays as it is
    if not moved_to_dirs:
        return
    dest_dir = Counter(moved_to_dirs).most_common(1)[0][0]
    for n in sorted(names):
        p = os.path.join(old_dir, n)
        if not os.path.isfile(p) or os.path.splitext(n)[1].lower() not in ART_EXT:
            continue
        dst = os.path.join(music, dest_dir, n)
        if os.path.exists(dst):
            continue
        makedirs_like(os.path.dirname(dst))
        shutil.move(p, dst)
        journal({"from": os.path.relpath(p, music).replace(os.sep, "/"),
                 "to": os.path.relpath(dst, music).replace(os.sep, "/"), "kind": "extra"})


def cmd_reorg_apply(a):
    plan_path = a.plan or os.path.join(a.out, "reorg-plan.json")
    if not os.path.exists(plan_path):
        sys.exit("no plan at %s - run 'reorg' first" % plan_path)
    plan = json.load(open(plan_path, encoding="utf-8"))
    music = os.path.abspath(a.music or plan["music_root"])
    todo, skipped = reorg_select(plan, music, a)
    total = sum(1 for i in plan["items"] if i["action"] in ("move", "rename"))
    print("plan generated %s | %d changes in the plan | already moved by earlier batches %d | selected %d (%s) | skipped %d" % (
        plan["generated"], total, a.already_moved, len(todo), human(sum(i["size"] for i in todo)), len(skipped)))
    last = None
    for n, it in enumerate(todo):
        if n >= a.show:
            print("\n... and %d more changes (all of them are in reorg-plan.csv)" % (len(todo) - n))
            break
        d = posixpath.dirname(it["to"])
        if d != last:
            print("\n%s" % d)
            last = d
        print("   %-6s %s\n      ->  %s%s" % (it["action"].upper() if not it["ext_fix"] else "EXT", it["path"],
                                              posixpath.basename(it["to"]), ("   [" + it["reason"] + "]") if it["reason"] else ""))
    for it, why in skipped[:60]:
        print("SKIPPED %s : %s" % (it["path"], why))
    if len(skipped) > 60:
        print("... and %d more skipped" % (len(skipped) - 60))
    mapping = dict((i["path"], i["to"]) for i in todo)
    pls = find_playlists(music, a.playlist_dir)
    touched = []
    for p in pls:
        data, rep, drp = rewrite_m3u(p, music, mapping, keep_dups=True)
        if data is not None:
            touched.append((p, data, rep))
    if touched:
        print("\nplaylist files that will be rewritten (mtime preserved, backup kept):")
        for p, _d, rep in touched:
            print("   %s  (%d entries repointed)" % (os.path.basename(p), rep))
    if not a.execute:
        print("\nDRY RUN - nothing changed. Add --execute to do it.")
        return
    if a.expect_moves is not None and a.expect_moves != len(todo):
        sys.exit("REFUSED: the dry run you confirmed would move %d file(s) but this run would move %d (a file changed, moved or went missing "
                 "since). Nothing was changed. Do a fresh dry run and confirm that one." % (a.expect_moves, len(todo)))
    if not todo:
        print("nothing to do.")
        return

    reroot = a.reorg_root or os.path.join(os.path.dirname(music), "_reorg")
    base_id = time.strftime("%Y%m%d-%H%M%S")
    run_id, n_ = base_id, 1
    while os.path.exists(os.path.join(reroot, run_id)):
        n_ += 1
        run_id = "%s-%d" % (base_id, n_)
    rdir = os.path.join(reroot, run_id)
    os.makedirs(rdir)
    manifest = {"kind": "reorg", "run": run_id, "music_root": music, "run_dir": rdir, "files": [], "playlists": [],
                "dirs": [], "navidrome_backup": ""}
    if a.nd_db and os.path.exists(a.nd_db):
        try:
            n = nd_backup(a.nd_db, os.path.join(rdir, "navidrome.db.bak"))
            manifest["navidrome_backup"] = os.path.join(rdir, "navidrome.db.bak")
            print("\nNavidrome database backed up: %s (%s)" % (manifest["navidrome_backup"], human(n)))
        except Exception as e:                      # noqa: BLE001
            if not a.no_navidrome_backup:
                shutil.rmtree(rdir, ignore_errors=True)         # no half-made run folder left behind
                sys.exit("could not back up navidrome.db (%s). Nothing was moved. Fix that or pass --no-navidrome-backup." % e)
            print("WARNING: navidrome.db backup failed (%s) - continuing because of --no-navidrome-backup" % e)
    elif not a.no_navidrome_backup:
        shutil.rmtree(rdir, ignore_errors=True)
        sys.exit("navidrome.db not found, so it cannot be backed up first. Nothing was moved.\n"
                 "Set the Navidrome data folder (-NdData) or pass --no-navidrome-backup (it is also in the daily B2 backup).")

    jpath = os.path.join(rdir, REORG_JOURNAL)
    jfh = open(jpath, "a", encoding="utf-8", buffering=1)
    jfh.write(json.dumps({"kind": "header", "run": run_id, "music_root": music, "run_dir": rdir}) + "\n")

    def jwrite(rec):
        jfh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def journal(rec):
        jwrite(rec)
        manifest["files"].append(rec)

    cache = None
    cpath = os.path.join(a.out, "cache.db")
    if os.path.exists(cpath):
        try:
            cache = sqlite3.connect(cpath)
        except sqlite3.Error:
            cache = None
    moved, failed, touched_dirs = 0, 0, defaultdict(list)
    try:
        for it in todo:
            src, dst = os.path.join(music, it["path"]), os.path.join(music, it["to"])
            try:
                makedirs_like(os.path.dirname(dst))
                if os.path.lexists(dst) and not os.path.samefile(src, dst):
                    raise OSError("target exists")
                os.rename(src, dst)
            except OSError as e:
                failed += 1
                print("FAILED %s : %s" % (it["path"], e))
                continue
            rec = {"from": it["path"], "to": it["to"], "kind": "audio"}
            journal(rec)
            stem_s, stem_d = os.path.splitext(src)[0], os.path.splitext(dst)[0]
            if os.path.exists(stem_s + ".lrc") and not os.path.exists(stem_d + ".lrc"):
                os.rename(stem_s + ".lrc", stem_d + ".lrc")
                journal({"from": os.path.splitext(it["path"])[0] + ".lrc", "to": os.path.splitext(it["to"])[0] + ".lrc", "kind": "lrc"})
            if cache is not None:
                try:
                    cache.execute("UPDATE OR REPLACE files SET path=? WHERE path=?", (it["to"], it["path"]))
                except sqlite3.Error:
                    pass
            touched_dirs[os.path.dirname(src)].append(posixpath.dirname(it["to"]))
            moved += 1
            if moved % 500 == 0:
                print("   moved %d / %d" % (moved, len(todo)), flush=True)
                if cache is not None:
                    cache.commit()
        if cache is not None:
            cache.commit()
            cache.close()
        # playlists: back up, then rewrite entries that point at moved files
        done = dict((r["from"], r["to"]) for r in manifest["files"] if r["kind"] == "audio")
        for p in pls:
            data, rep, _drp = rewrite_m3u(p, music, done, keep_dups=True)
            if data is None:
                continue
            bk = os.path.join(rdir, "_playlists", os.path.basename(p))
            os.makedirs(os.path.dirname(bk), exist_ok=True)
            st = os.stat(p)
            shutil.copy2(p, bk)
            prec = {"path": os.path.relpath(p, music).replace(os.sep, "/"), "backup": bk,
                    "mtime": st.st_mtime, "atime": st.st_atime, "entries": rep}
            jwrite(dict(prec, kind="playlist"))         # written BEFORE the rewrite so a crash can still be undone
            with open(p, "wb") as fh:
                fh.write(data)
            if not a.bump_m3u_mtime:
                os.utime(p, (st.st_atime, st.st_mtime))
            manifest["playlists"].append(prec)
        # folders that lost their audio: extras go along with the album, what is left is set aside, empty folders go
        for old_dir, dests in sorted(touched_dirs.items(), key=lambda kv: -len(kv[0])):
            reorg_handover_extras(old_dir, music, dests, journal, print)
            n_before = len(manifest["dirs"])
            prune_upwards(old_dir, music, rdir + "/_leftovers", manifest)
            for rel in manifest["dirs"][n_before:]:
                jwrite({"kind": "dir", "rel": rel})
    finally:
        jfh.close()
        manifest["leftovers_dir"] = rdir + "/_leftovers"
        with open(os.path.join(rdir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(a.out, "reorg-moves.tsv"), "a", encoding="utf-8") as fh:
        for r in manifest["files"]:
            if r["kind"] == "audio":
                fh.write("%s\t%s\t%s\n" % (run_id, r["from"], r["to"]))
    print("\nmoved %d files%s. Playlists rewritten: %d. Folders removed: %d." % (
        moved, (" (%d FAILED)" % failed) if failed else "", len(manifest["playlists"]), len(manifest["dirs"])))
    print("run folder: %s" % rdir)
    print("next: Navidrome rescan (playlists, stars and play counts follow the files); run  scan  to refresh the report;")
    print("      a library scan in any other app that indexes the music. Beets: leave it until the reorg is finished, then rebuild it.")
    print("undo with:  undo --manifest %s --execute" % os.path.join(rdir, "manifest.json"))


def undo_reorg(a, man):
    music, rdir = man["music_root"], man["run_dir"]
    files = man["files"]
    # plan against what is there now, step by step in the order the moves will be undone (a path one move frees can be used by the next)
    added, removed, todo, notes = set(), set(), [], []

    def there(p):
        return p in added or (p not in removed and os.path.exists(p))
    for r in reversed(files):
        src, dst = os.path.join(music, r["to"]), os.path.join(music, r["from"])
        if not there(src):
            notes.append("   SKIP (not there any more): %s" % r["to"])
        elif there(dst):
            notes.append("   SKIP (original path is occupied): %s" % r["from"])
        else:
            todo.append(r)
            removed.add(src)
            added.add(dst)
    left = man.get("leftovers_dir", os.path.join(rdir, "_leftovers"))
    dirmoves = []
    for rel in man.get("dirs", []):
        base = os.path.join(left, rel)
        if os.path.isdir(base):
            for e in sorted(os.listdir(base)):
                if not os.path.exists(os.path.join(music, rel, e)):
                    dirmoves.append((os.path.join(base, e), os.path.join(music, rel, e)))
    pl, miss = undo_playlists_plan(man, bool(todo))
    for p in miss:
        notes.append("   MISSING playlist backup (that playlist is left as it is): %s" % p.get("backup"))
    lines = ["move\t%s\t%s" % (r["to"], r["from"]) for r in todo] + ["dir\t%s\t%s" % d for d in dirmoves] + \
            ["playlist\t%s\t%s" % (p["path"], p["backup"]) for p in pl]
    shown = notes[:100] + ["   would move back  %s" % r["to"] for r in todo[:200]]
    if not undo_proceed(a, "reorg", man, lines, len(todo), len(pl), len(notes), shown, max(0, len(todo) - 200)):
        if not a.execute:
            print("DRY RUN - add --execute")
        return
    back, skip = 0, 0
    for r in todo:
        src, dst = os.path.join(music, r["to"]), os.path.join(music, r["from"])
        if not os.path.exists(src):
            print("SKIP (not there any more): %s" % r["to"])
            skip += 1
            continue
        if os.path.exists(dst):
            print("SKIP (original path is occupied): %s" % r["from"])
            skip += 1
            continue
        makedirs_like(os.path.dirname(dst))
        os.rename(src, dst)
        back += 1
    # folders that were set aside (cover art, @eaDir ...) go back first so their parents exist
    for src, dest in dirmoves:
        if os.path.exists(src) and not os.path.exists(dest):
            makedirs_like(os.path.dirname(dest))
            shutil.move(src, dest)
    undo_playlists_run(music, pl)
    # new folders that are now empty (only @eaDir / nothing) are tidied away
    for d in sorted({os.path.dirname(os.path.join(music, r["to"])) for r in files}, key=len, reverse=True):
        reorg_rmdir_empty(d, music)
    print("moved %d files back%s and restored %d playlists. Run a Navidrome rescan." % (
        back, (" (%d skipped)" % skip) if skip else "", len(pl)))
    if back:
        print("Beets: its library.db still has the new paths. Run  beets-remap  (it follows the move history backwards) to point it at the old ones.")
    if man.get("navidrome_backup"):
        print("(a Navidrome database backup from before the run is at %s - only needed if Navidrome misbehaves)" % man["navidrome_backup"])


def reorg_rmdir_empty(d, music):
    music = os.path.abspath(music)
    d = os.path.abspath(d)
    while d != music and d.startswith(music + os.sep):
        try:
            names = [n for n in os.listdir(d) if n.lower() != "@eadir"]
        except OSError:
            return
        if names:
            return
        ea = os.path.join(d, "@eaDir")
        if os.path.isdir(ea):
            return                      # never delete Synology's own metadata folder; the folder just stays
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


# ---------------------------------------------------------------------------
# beets-remap: point the Beets library.db at the files' new paths after a reorg
# ---------------------------------------------------------------------------
def beets_history(out):
    """(forward, reverse) path maps from out/reorg-moves.tsv (relative to the library root); the last entry for a path wins."""
    fwd, rev = {}, {}
    try:
        with open(os.path.join(out, "reorg-moves.tsv"), encoding="utf-8") as fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if len(f) >= 3 and f[1] and f[2] and f[1] != f[2]:
                    fwd[f[1]] = f[2]
                    rev[f[2]] = f[1]
    except OSError:
        pass
    return fwd, rev


def chain_end(m, p):
    seen = {p}
    while p in m:
        p = m[p]
        if p in seen:
            break
        seen.add(p)
    return p


def beets_locate(rel, fwd, rev, exists):
    """Where the file Beets knows as `rel` is now, or None. Follows the move history forward, and backward when a run was undone."""
    new = chain_end(fwd, rel)
    if new != rel and exists(new):
        return new
    if exists(rel):
        return rel
    old = chain_end(rev, rel)
    if old != rel and exists(old):
        return old
    p, seen = rel, {rel}
    while p in fwd:                       # moved twice, one step undone: stop wherever the file really is
        p = fwd[p]
        if p in seen:
            break
        seen.add(p)
        if exists(p):
            return p
    return None


def beets_rel(pth, prefix):
    """(path relative to the library root, 'abs' | 'rel') or (None, None) when it points outside the library.
    Older Beets stores absolute paths, newer versions store them relative to the library directory."""
    if pth.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", pth):
        if pth.startswith(prefix + "/"):
            return pth[len(prefix) + 1:], "abs"
        return None, None
    return pth, "rel"


def cmd_beets_remap(a):
    music = os.path.abspath(a.music)
    db_path = a.beets_db
    if not db_path or not os.path.isfile(db_path):
        sys.exit("Beets database not found: %s  (the launcher mounts it at /beets; set MD_BEETS to the folder that holds library.db)" % db_path)
    prefix = a.beets_prefix.rstrip("/")
    fwd, rev = beets_history(a.out)
    print("beets-remap: %s  (library root as Beets sees it: %s, as seen here: %s)" % (db_path, prefix, music))
    print("move history: %d moves in reorg-moves.tsv" % len(fwd))

    tmp = tempfile.mkdtemp(prefix="md_beets_")
    try:
        work = os.path.join(tmp, "library.db")
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suf):
                shutil.copy2(db_path + suf, work + suf)
        con = sqlite3.connect(work)
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            sys.exit("the Beets database fails its integrity check; not touching it")
        rows = con.execute("SELECT id, path, album_id FROM items").fetchall()
        cache = {}

        def exists(rel):
            if rel not in cache:
                cache[rel] = os.path.isfile(os.path.join(music, rel))
            return cache[rel]

        def dec(raw):
            return os.fsdecode(raw) if isinstance(raw, bytes) else raw

        stats = Counter()
        changes, missing = [], []
        album_dirs = defaultdict(Counter)
        final_paths = set()
        for iid, raw, album_id in rows:
            pth = dec(raw)
            rel, style = beets_rel(pth, prefix)
            if rel is None:
                stats["outside"] += 1
                continue
            stats["style_" + style] += 1
            cur = beets_locate(rel, fwd, rev, exists)
            if cur is None:
                stats["missing"] += 1
                missing.append((iid, rel))
                continue
            final_paths.add(cur)
            if album_id:
                album_dirs[album_id][posixpath.dirname(cur)] += 1
            if cur == rel:
                stats["unchanged"] += 1
            else:
                stats["remapped"] += 1
                changes.append((iid, raw, rel, cur, style))
        art_changes, art_missing, art_ok = [], 0, 0
        for aid, raw in con.execute("SELECT id, artpath FROM albums WHERE artpath IS NOT NULL AND artpath != ''").fetchall():
            pth = dec(raw)
            rel, style = beets_rel(pth, prefix)
            if rel is None:
                continue
            if exists(rel):
                art_ok += 1
                continue
            hit = None
            for d, _ in album_dirs.get(aid, Counter()).most_common():
                cand = posixpath.join(d, posixpath.basename(rel))
                if exists(cand):
                    hit = cand
                    break
            if hit:
                art_changes.append((aid, raw, rel, hit, style))
            else:
                art_missing += 1
        con.close()

        on_disk = 0
        not_in_beets = []
        for root, dirs, files in os.walk(music):
            for name in files:
                if os.path.splitext(name)[1].lower() in AUDIO_EXT and not name.startswith("._"):
                    on_disk += 1
                    rel = os.path.relpath(os.path.join(root, name), music).replace(os.sep, "/")
                    if rel not in final_paths:
                        not_in_beets.append(rel)

        print("-" * 60)
        print("Beets items %d | already correct %d | to repoint %d | file not found anywhere %d | outside %s %d" % (
            len(rows), stats["unchanged"], stats["remapped"], stats["missing"], prefix, stats["outside"]))
        print("paths are stored %s (absolute %d, relative to the library folder %d)" % (
            "relative" if stats["style_rel"] and not stats["style_abs"] else "absolute" if stats["style_abs"] and not stats["style_rel"] else "mixed",
            stats["style_abs"], stats["style_rel"]))
        print("album art paths: ok %d | to repoint %d | art file not found %d" % (art_ok, len(art_changes), art_missing))
        print("audio files in the library %d | not in the Beets database %d (the next import will pick those up)" % (on_disk, len(not_in_beets)))
        for iid, raw, old, new, style in changes[:8]:
            print("   item %d: %s\n        -> %s" % (iid, old, new))
        if missing:
            print("items whose file is nowhere to be found (stale entries, e.g. duplicates the dedupe quarantined): %d" % len(missing))
            for iid, rel in missing[:10]:
                print("   item %d: %s" % (iid, rel))
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "beets-remap.csv"), "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["what", "id", "old", "new"])
            for iid, raw, old, new, style in changes:
                w.writerow(["repoint", iid, old, new])
            for aid, raw, old, new, style in art_changes:
                w.writerow(["art", aid, old, new])
            for iid, rel in missing:
                w.writerow(["not found", iid, rel, ""])
            for rel in not_in_beets:
                w.writerow(["not in beets", "", rel, ""])
        print("full list: %s" % os.path.join(a.out, "beets-remap.csv"))
        # a fingerprint of every row this run would change (which row, old path, new path): the web runner passes the dry run's one
        # back with --expect-plan, so a real run can only do exactly what the dry run showed
        fh_ = hashlib.sha256()
        for iid, raw, old, new, style in sorted(changes, key=lambda c: c[0]):
            fh_.update(("item\t%d\t%s\t%s\n" % (iid, old, new)).encode("utf-8", "surrogateescape"))
        for aid, raw, old, new, style in sorted(art_changes, key=lambda c: c[0]):
            fh_.update(("art\t%d\t%s\t%s\n" % (aid, old, new)).encode("utf-8", "surrogateescape"))
        fp = fh_.hexdigest()[:16]
        print("plan fingerprint: %s" % fp)
        if a.execute and a.expect_plan is not None and a.expect_plan != fp:
            sys.exit("REFUSED: what this run would change is not what the dry run you confirmed showed (Beets, the library or the move history "
                     "changed since). Nothing was changed. Do a fresh dry run and confirm that one.")

        if not changes and not art_changes:
            print("nothing to change.")
            return
        if not a.execute:
            print("DRY RUN - nothing changed. Stop the Beets task first, then add --execute.")
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = "%s.bak-reorg-%s" % (db_path, stamp)
        # take the write lock FIRST: if a Beets run is going, stop here with nothing written (not even a backup), and while the lock is held
        # nothing can write to the database, so the backup below is a consistent snapshot (VACUUM INTO from a second connection reads it
        # while this one holds the lock)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            con.close()
            sys.exit("the Beets database is locked (%s): a Beets run is still going. Stop it and try again; nothing was changed." % e)
        try:
            bk = sqlite3.connect(db_path, timeout=5)
            try:
                bk.execute("VACUUM INTO ?", (backup,))
            finally:
                bk.close()
        except Exception as e:                      # noqa: BLE001
            con.rollback()
            con.close()
            try:
                os.unlink(backup)
            except OSError:
                pass
            sys.exit("could not back up the Beets database (%s). Nothing was changed." % e)
        print("Beets database backed up: %s (%s)" % (backup, human(os.path.getsize(backup))))
        try:
            n = 0
            for iid, raw, old, new, style in changes:
                full = (prefix + "/" + new) if style == "abs" else new
                new_raw = os.fsencode(full) if isinstance(raw, bytes) else full
                cur = con.execute("UPDATE items SET path=? WHERE id=? AND path=?", (new_raw, iid, raw))
                n += cur.rowcount
            na = 0
            for aid, raw, old, new, style in art_changes:
                full = (prefix + "/" + new) if style == "abs" else new
                new_raw = os.fsencode(full) if isinstance(raw, bytes) else full
                cur = con.execute("UPDATE albums SET artpath=? WHERE id=? AND artpath=?", (new_raw, aid, raw))
                na += cur.rowcount
            if n != len(changes) or na != len(art_changes):
                raise RuntimeError("expected %d item and %d album rows, changed %d and %d" % (len(changes), len(art_changes), n, na))
            con.commit()
        except Exception as e:
            con.rollback()
            sys.exit("nothing was changed (%s)" % e)
        finally:
            con.close()
        con = sqlite3.connect(db_path)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        with open(os.path.join(a.out, "beets-remap-%s.tsv" % stamp), "w", encoding="utf-8") as fh:
            for iid, raw, old, new, style in changes:
                fh.write("item\t%d\t%s\t%s\n" % (iid, old, new))
            for aid, raw, old, new, style in art_changes:
                fh.write("art\t%d\t%s\t%s\n" % (aid, old, new))
        print("\nrepointed %d items and %d album art paths. database integrity: %s" % (n, na, ok))
        print("to go back: stop Beets, delete %s-wal and %s-shm if they exist, then copy %s over %s" % (db_path, db_path, backup, db_path))
        print("next: in the Beets container run  beet ls -p | head   and   beet ls -a | head   to see the new paths.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# nd-missing: what is behind Navidrome's "Missing Files" list (read-only)
# ---------------------------------------------------------------------------
def cmd_compare(a):
    """compare two library files by their decoded audio (read-only) - to see what the clean/explicit check would decide"""
    music = os.path.abspath(a.music)
    try:
        np = _audio_np()
    except AudioUnavailable as e:
        sys.exit("cannot compare: %s" % e)
    paths = []
    for rel in (a.file_a, a.file_b):
        full = rel if os.path.isabs(rel) else os.path.join(music, rel)
        if not os.path.isfile(full):
            sys.exit("file not found: %s (give the path relative to the Music folder)" % rel)
        paths.append(full)
    r = compare_audio(paths[0], paths[1], np)
    print("A: %s\nB: %s" % (a.file_a, a.file_b))
    print("verdict: %s - %s" % (r["verdict"].upper(), r["why"]))
    print("details: %s" % ", ".join("%s=%s" % kv for kv in sorted(r.items()) if kv[0] not in ("verdict", "why")))
    print({"same": "-> the same version of the song: only the label differs, so scan/apply treat the pair as a plain duplicate.",
           "local": "-> same recording, but part of it differs (a muted / bleeped / replaced word): a real clean-vs-explicit pair. Both stay.",
           "broad": "-> the audio differs broadly (different recording, master or length). Both stay.",
           "error": "-> could not be compared. Both stay."}.get(r["verdict"], ""))


# ---------------------------------------------------------------------------
# tagaudit: which files lack tags, cover art or lyrics (read-only)
# ---------------------------------------------------------------------------
ART_NAMES = ("cover", "folder", "front")        # Navidrome's default cover-art file names
ART_EXTS = (".jpg", ".jpeg", ".png", ".webp")
LYRIC_SIDECARS = (".lrc", ".txt")


def _audit_one(path):
    """What one file carries: text tags, embedded picture, embedded lyrics. Native frames, not 'easy' ones."""
    r = {"fmt": "", "error": "", "title": False, "artist": False, "album": False, "albumartist": False,
         "track": False, "year": False, "genre": False, "art": False, "lyrics": False, "art_known": True}
    real = sniff_format(path)
    r["fmt"] = real or os.path.splitext(path)[1].lower().lstrip(".")
    try:
        f = mutagen.File(path)
        if f is None and real:
            from mutagen.mp3 import MP3
            from mutagen.mp4 import MP4
            from mutagen.flac import FLAC
            f = {"mp3": MP3, "m4a": MP4, "flac": FLAC}.get(real, lambda p: None)(path)
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, e)
        return r
    if f is None:
        r["error"] = "unreadable"
        return r
    tags = f.tags
    name = type(f).__name__
    try:
        if name in ("MP3", "WAVE", "AIFF", "ID3FileType") or (tags is not None and type(tags).__name__ == "ID3"):
            if tags is None:
                return r
            def txt(*ids):
                for i in ids:
                    fr = tags.get(i)
                    if fr is not None and str(fr).strip():
                        return True
                return False
            r["title"], r["artist"], r["album"] = txt("TIT2"), txt("TPE1"), txt("TALB")
            r["albumartist"], r["track"] = txt("TPE2"), txt("TRCK")
            r["year"], r["genre"] = txt("TDRC", "TYER", "TDOR", "TORY"), txt("TCON")
            r["art"] = any(k.startswith("APIC") for k in tags.keys())
            def _lyr(fr):
                t = getattr(fr, "text", "")
                return bool(t if not isinstance(t, str) else t.strip())
            r["lyrics"] = any((k.startswith("USLT") or k.startswith("SYLT")) and _lyr(tags[k]) for k in tags.keys())
        elif name == "MP4":
            tg = tags or {}
            def has(k):
                v = tg.get(k)
                return bool(v) and any(str(x).strip() for x in (v if isinstance(v, list) else [v]))
            r["title"], r["artist"], r["album"] = has("\xa9nam"), has("\xa9ART"), has("\xa9alb")
            r["albumartist"], r["track"] = has("aART"), has("trkn")
            r["year"], r["genre"] = has("\xa9day"), has("\xa9gen")
            r["art"] = bool(tg.get("covr"))
            r["lyrics"] = has("\xa9lyr")
        elif name in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC"):
            tg = tags or {}
            def has(*ks):
                for k in ks:
                    try:
                        v = tg.get(k)
                    except Exception:
                        v = None
                    if v and any(str(x).strip() for x in v):
                        return True
                return False
            r["title"], r["artist"], r["album"] = has("title"), has("artist"), has("album")
            r["albumartist"] = has("albumartist", "album artist")
            r["track"] = has("tracknumber", "track")
            r["year"] = has("date", "year", "originaldate")
            r["genre"] = has("genre")
            r["lyrics"] = has("lyrics", "unsyncedlyrics", "syncedlyrics")
            if name == "FLAC":
                r["art"] = bool(getattr(f, "pictures", None))
            else:
                r["art"] = has("metadata_block_picture", "coverart")
        else:
            r["art_known"] = False             # wma / ape / wv ...: text tags only
            try:
                e = mutagen.File(path, easy=True)
                tg = e.tags or {}
                r["title"], r["artist"], r["album"] = bool(tg.get("title")), bool(tg.get("artist")), bool(tg.get("album"))
                r["albumartist"], r["track"] = bool(tg.get("albumartist")), bool(tg.get("tracknumber"))
                r["year"], r["genre"] = bool(tg.get("date")), bool(tg.get("genre"))
            except Exception:
                pass
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, e)
    return r


def cmd_tagaudit(a):
    """read-only: per file / per album, which tags, cover art and lyrics are missing"""
    music = os.path.abspath(a.music)
    if mutagen is None:
        sys.exit("mutagen is not installed (pip install mutagen)")
    os.makedirs(a.out, exist_ok=True)
    log = make_logger(a.out)
    t0 = time.time()
    files, dir_art, sidecar = [], {}, set()
    for root, dirs, fnames in os.walk(music):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        rel_root = os.path.relpath(root, music).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root
        for name in fnames:
            low = name.lower()
            stem, ext = os.path.splitext(low)
            rel = (rel_root + "/" + name) if rel_root else name
            if name.startswith("."):
                continue
            if ext in ART_EXTS and stem in ART_NAMES:
                dir_art.setdefault(rel_root, name)
            elif ext in LYRIC_SIDECARS:
                sidecar.add((rel_root + "/" + os.path.splitext(name)[0]).lower())
            elif ext in AUDIO_EXT:
                files.append(rel)
    log("tagaudit: found %d audio files (%.1fs); reading tags, pictures and lyrics" % (len(files), time.time() - t0))

    def work(rel):
        return rel, _audit_one(os.path.join(music, rel))

    res, done, t1 = {}, 0, time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for rel, r in ex.map(work, files):
            res[rel] = r
            done += 1
            if done % 2000 == 0:
                log("  read %d / %d (%.0f files/s)" % (done, len(files), done / max(time.time() - t1, 0.001)))

    TAGS = ("title", "artist", "album", "albumartist", "track", "year", "genre")
    rows, albums = [], {}
    c = Counter()
    for rel in sorted(res):
        r = res[rel]
        d = os.path.dirname(rel)
        folder_art = dir_art.get(d, "")
        miss = [k for k in TAGS if not r[k]] if not r["error"] else []
        art = ("embedded+folder" if r["art"] and folder_art else "embedded" if r["art"] else "folder" if folder_art else
               ("none" if r["art_known"] else "unknown"))
        side = ((d + "/" + os.path.splitext(os.path.basename(rel))[0]).lower() in sidecar)
        lyr = "embedded+sidecar" if r["lyrics"] and side else "embedded" if r["lyrics"] else "sidecar" if side else "none"
        rows.append([rel, r["fmt"], r["error"], " ".join(miss), art, folder_art, lyr])
        c["files"] += 1
        if r["error"]:
            c["unreadable"] += 1
        for k in miss:
            c["missing " + k] += 1
        if any(k in miss for k in ("title", "artist", "album")):
            c["missing a core tag (title/artist/album)"] += 1
        c["art " + art] += 1
        c["lyrics " + lyr] += 1
        A = albums.setdefault(d, Counter())
        A["tracks"] += 1
        A["core_missing"] += 1 if any(k in miss for k in ("title", "artist", "album")) else 0
        A["emb_art"] += 1 if r["art"] else 0
        A["lyrics"] += 0 if lyr == "none" else 1
    with open(os.path.join(a.out, "tagaudit.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "format", "error", "missing_tags", "cover_art", "folder_art_file", "lyrics"])
        w.writerows(rows)
    arows = []
    for d, A in sorted(albums.items()):
        fa = dir_art.get(d, "")
        art = "ok" if (fa or A["emb_art"] == A["tracks"]) else ("PARTIAL (some files only)" if A["emb_art"] else "NONE")
        arows.append([d, A["tracks"], A["core_missing"], art, fa, A["emb_art"], A["lyrics"], A["tracks"] - A["lyrics"]])
    with open(os.path.join(a.out, "tagaudit-albums.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["folder", "tracks", "tracks_missing_core_tags", "cover_art", "folder_art_file", "tracks_with_embedded_art",
                    "tracks_with_lyrics", "tracks_without_lyrics"])
        w.writerows(arows)
    n = c["files"] or 1
    print("\ntagaudit: %d audio files in %d folders (%.0fs)" % (c["files"], len(albums), time.time() - t0))
    print("  unreadable files: %d" % c["unreadable"])
    print("  missing tags:")
    for k in TAGS:
        print("    %-12s %6d  (%.1f%%)" % (k, c["missing " + k], 100.0 * c["missing " + k] / n))
    print("    files missing title, artist or album: %d" % c["missing a core tag (title/artist/album)"])
    print("  cover art (a file counts as covered by embedded art OR a cover/folder/front image in its folder):")
    for k in ("embedded+folder", "embedded", "folder", "none", "unknown"):
        if c["art " + k]:
            print("    %-16s %6d" % (k, c["art " + k]))
    nf = sum(1 for r in arows if r[3] == "NONE")
    print("    album folders with no art at all: %d of %d" % (nf, len(arows)))
    print("  lyrics (embedded tag or a .lrc / .txt next to the file):")
    for k in ("embedded+sidecar", "embedded", "sidecar", "none"):
        if c["lyrics " + k]:
            print("    %-16s %6d  (%.1f%%)" % (k, c["lyrics " + k], 100.0 * c["lyrics " + k] / n))
    print("\nwrote %s and %s" % (os.path.join(a.out, "tagaudit.csv"), os.path.join(a.out, "tagaudit-albums.csv")))
    print("nothing in the library was changed.")


def cmd_nd_missing(a):
    music = os.path.abspath(a.music)
    if not a.nd_db or not os.path.exists(a.nd_db):
        sys.exit("Navidrome database not found (%s) - the launcher mounts it at /navidrome-data; set MD_NDDATA." % a.nd_db)
    fwd, rev = beets_history(a.out)
    print("nd-missing: %s (read-only copy)" % a.nd_db)
    print("move history: %d moves in reorg-moves.tsv" % len(fwd))
    dbt = nd_read_tables(a.nd_db, {
        "media_file": ["id", "path", "title", "artist", "album", "album_artist", "missing", "updated_at", "pid"],
        "annotation": ["item_id", "item_type", "play_count", "starred", "rating"],
        "playlist_tracks": ["playlist_id", "media_file_id"],
        "playlist": ["id", "name"]})
    rows = dbt["media_file"]
    if not rows or "missing" not in rows[0]:
        sys.exit("this Navidrome database has no media_file.missing column - nothing to check.")
    disk = set()
    for root, dirs, names in os.walk(music):
        for n in names:
            if os.path.splitext(n)[1].lower() in AUDIO_EXT and not n.startswith("._"):
                disk.add(os.path.relpath(os.path.join(root, n), music).replace(os.sep, "/"))
    print("audio files on disk: %d" % len(disk))
    known = set(fwd) | set(fwd.values()) | disk

    def to_rel(path):
        parts = (path or "").replace("\\", "/").lstrip("/").split("/")
        for i in range(len(parts)):
            c = "/".join(parts[i:])
            if c in known:
                return c
        return None

    def truthy(v):
        return str(v).lower() in ("1", "true", "t")

    ann = {}
    for r in dbt.get("annotation", []):
        if r.get("item_type") != "media_file":
            continue
        d = ann.setdefault(r["item_id"], [0, 0, 0])
        d[0] += int(r.get("play_count") or 0)
        d[1] = max(d[1], int(r.get("starred") or 0))
        d[2] = max(d[2], int(r.get("rating") or 0))
    in_pl = defaultdict(set)
    plname = dict((r["id"], r.get("name")) for r in dbt.get("playlist", []))
    for r in dbt.get("playlist_tracks", []):
        in_pl[r["media_file_id"]].add(plname.get(r["playlist_id"]) or r["playlist_id"])

    def has_data(mid):
        d = ann.get(mid)
        return bool((d and (d[0] or d[1] or d[2])) or in_pl.get(mid))

    live = [r for r in rows if not truthy(r.get("missing"))]
    miss = [r for r in rows if truthy(r.get("missing"))]
    live_by_rel, live_by_key = {}, defaultdict(list)
    for r in live:
        rel = to_rel(r["path"])
        if rel:
            live_by_rel[rel] = r
        k = (compact(r.get("artist") or ""), compact(r.get("title") or ""))
        if k[1]:
            live_by_key[k].append(r)
    print("Navidrome tracks: %d | present %d | flagged missing %d" % (len(rows), len(live), len(miss)))

    cats = Counter()
    cat_data = Counter()
    twin_stat = Counter()
    out_rows = []
    for m in miss:
        old = to_rel(m["path"])
        cat, new, twin = "gone", "", None
        if old is not None and old in disk:
            cat, new = "still on disk", old
        elif old is not None:
            tgt = chain_end(fwd, old)
            if tgt != old and tgt in disk:
                cat, new = "moved by the reorg", tgt
                twin = live_by_rel.get(tgt)
        if twin is None:
            k = (compact(m.get("artist") or ""), compact(m.get("title") or ""))
            cand = live_by_key.get(k) or []
            if cand:
                twin = cand[0]
        d = has_data(m["id"])
        cats[cat] += 1
        if d:
            cat_data[cat] += 1
        if cat == "moved by the reorg":
            twin_stat["moved: new row exists" if live_by_rel.get(new) else "moved: no row for the new file"] += 1
        if d:
            twin_stat["data on missing row, a present twin exists" if twin else "data on missing row, no present twin"] += 1
            if twin and not has_data(twin["id"]):
                twin_stat["data on missing row, twin has none (would be lost on purge)"] += 1
        pl = ann.get(m["id"]) or [0, 0, 0]
        out_rows.append([cat, m["path"], new, m["id"], m.get("title") or "", m.get("artist") or "", m.get("album") or "",
                         pl[0], pl[1], pl[2], "; ".join(sorted(str(x) for x in in_pl.get(m["id"], ()))),
                         twin["id"] if twin else "", ("yes" if (twin and has_data(twin["id"])) else "no") if twin else "",
                         m.get("updated_at") or ""])
    print("-" * 60)
    for c in ("moved by the reorg", "gone", "still on disk"):
        print("  %-22s %6d   (with plays/stars/rating/playlists: %d)" % (c, cats[c], cat_data[c]))
    print("-" * 60)
    for k in sorted(twin_stat):
        print("  %-62s %6d" % (k, twin_stat[k]))
    stamps = Counter(str(m.get("updated_at") or "")[:16] for m in miss)
    if stamps:
        print("missing rows were last updated at (top): " + ", ".join("%s x%d" % kv for kv in stamps.most_common(3)))
    ex = [r for r in out_rows if r[0] == "moved by the reorg"][:5] + [r for r in out_rows if r[0] == "gone"][:5]
    for r in ex:
        print("   [%s] %s%s" % (r[0], r[1], (" -> " + r[2]) if r[2] else ""))
    path = os.path.join(a.out, "nd-missing.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "navidrome_path", "file_now", "id", "title", "artist", "album", "plays", "starred", "rating",
                    "playlists", "present_twin_id", "twin_has_data", "updated_at"])
        w.writerows(out_rows)
    print("full list: %s" % path)
    print("READ-ONLY - nothing was changed.")



# ---------------------------------------------------------------------------
# place: file newly arrived music (the _incoming landing folder) into the library layout
# ---------------------------------------------------------------------------
INCOMING_PREFIX = "_incoming/"
INCOMING_MAP = "incoming-map.tsv"


def place_clean_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".") or "playlist"


def place_prune_incoming(incoming):
    """remove folders in the landing area that hold nothing but cover art / junk (the PC keeps the originals)"""
    removed = 0
    for root, dirs, files in os.walk(incoming, topdown=False):
        if os.path.abspath(root) == os.path.abspath(incoming):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        keep = False
        for n in names:
            p = os.path.join(root, n)
            if n.lower() == "@eadir":
                continue
            if os.path.isdir(p):
                keep = True
                break
            ext = os.path.splitext(n)[1].lower()
            if ext in AUDIO_EXT or not (ext in JUNK_EXT or n.lower() in JUNK_NAMES):
                keep = True
                break
        if keep:
            continue
        for n in names:
            p = os.path.join(root, n)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            except OSError:
                keep = True
        if not keep:
            try:
                os.rmdir(root)
                removed += 1
            except OSError:
                pass
    return removed


PLACE_PENDING = "place-pending.json"


def place_load_pending(path):
    """playlist plans of a place run that was stopped before it wrote them: {name: [(stem, library path), ...]}"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return dict((p["name"], [tuple(e) for e in p["entries"]]) for p in data.get("playlists", []))
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def place_save_pending(path, pl_plan):
    save_json_atomic(path, {"playlists": [{"name": n, "entries": [list(e) for e in ents]}
                                          for n, _m3u, mode, ents in pl_plan if mode != "leave"]})


def place_finish_pending(a, music):
    """a run was stopped after every file had moved but before the playlists were written: write them now"""
    pend_path = os.path.join(a.out, PLACE_PENDING)
    prev = place_load_pending(pend_path)
    if not prev:
        return
    pl_dir = os.path.join(music, "Playlists")
    wrote = 0
    for name in sorted(prev, key=str.lower):
        m3u = os.path.join(pl_dir, place_clean_name(name) + ".m3u")
        entries = [e for e in prev[name] if os.path.exists(os.path.join(music, e[1]))]
        if os.path.exists(m3u) or not entries:
            continue                          # an existing playlist is never touched here
        os.makedirs(pl_dir, exist_ok=True)
        lines = ["#EXTM3U"]
        for stem, dest in entries:
            lines += ["#EXTINF:-1,%s" % stem, "../" + dest]
        with open(m3u, "wb") as fh:
            fh.write(("\n".join(lines) + "\n").encode("utf-8"))
        wrote += 1
        print("completed playlist %s (%d tracks) from the stopped run" % (os.path.basename(m3u), len(entries)))
    try:
        os.remove(pend_path)
    except OSError:
        pass
    print("playlists completed: %d" % wrote)


def cmd_place(a):
    music = os.path.abspath(a.music)
    if not os.path.isdir(music):
        sys.exit("music folder not found: %s" % music)
    incoming = os.path.abspath(a.incoming or os.path.join(os.path.dirname(music), "_incoming"))
    if not os.path.isdir(incoming):
        print("place: no landing folder at %s - nothing to do." % incoming)
        return
    os.makedirs(a.out, exist_ok=True)
    rules = load_rules(a.rules)
    tol = rules.tolerance if rules.tolerance else 3.0
    wide = max(rules.wide_tolerance if rules.wide_tolerance else 6.0, tol)

    def log(m):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

    print("place: landing folder %s -> library %s (layout %s)" % (incoming, music, LAYOUT_DOC))
    raw_in, _left = scan_library(incoming, os.path.join(a.out, "cache-incoming.db"), a.workers, rules, log)
    if not raw_in:
        print("nothing in the landing folder.")
        if a.execute and a.expect_plan:
            sys.exit("REFUSED: the plan you confirmed (fingerprint %s) was made when files were waiting in the landing folder, and it is empty now. "
                     "Nothing was changed. Do a fresh dry run." % a.expect_plan)
        if a.execute:
            place_finish_pending(a, music)
        elif place_load_pending(os.path.join(a.out, PLACE_PENDING)):
            print("note: an earlier place run was stopped before it finished. Run  place --execute  to complete its playlist(s).")
        return
    raw_lib, _ = scan_library(music, os.path.join(a.out, "cache.db"), a.workers, rules, log)
    lib = [make_track(rel, d, rules) for rel, d in sorted(raw_lib.items())]
    inc = []
    for rel, d in sorted(raw_in.items()):
        t = make_track(INCOMING_PREFIX + rel, d, rules)
        t.inc_rel = rel
        t.layout = "loose"                 # on a tie the copy already in the library wins
        if t.tags_missing:
            t.matchable = False            # no artist/title tags: never matched by a guess from the folder names
        inc.append(t)
    inc_by_path = dict((t.rel, t) for t in inc)

    # what the library already knows (playlists, plays) so that the keeper choice protects your data
    nd_ok = False
    rel_index = {t.rel for t in lib}
    pls = find_playlists(music, a.playlist_dir)
    refs = index_m3u(music, pls)
    for t in lib:
        t.pl_m3u = refs.get(t.rel, set())
    if a.navidrome_db and os.path.exists(a.navidrome_db):
        info, nd_pl, nd_ok = load_navidrome(a.navidrome_db, rel_index, log)
        for t in lib:
            i = info.get(t.rel)
            if i:
                t.nd_id, t.plays, t.starred, t.rating = i["id"], i["plays"], i["starred"], i["rating"]
            t.pl_nd = nd_pl.get(t.rel, set())

    # 1. duplicates on arrival: an incoming song that is already in the library (or arrives twice) is not placed again
    groups = build_groups(lib + inc, rules, tol, nd_ok, wide)
    skip = {}                 # incoming path -> the path it duplicates (library path or another incoming path)
    better = []               # incoming files that beat the library copy: still not added (the library copy keeps its plays/stars)
    upgrades = []             # (incoming path, library path): higher audio quality, added next to the library copy (place-upgrade)
    upgrade_on = bool(getattr(a, "upgrade", False) or rules.place_upgrade)
    qual = dict((t.rel, t.quality) for t in lib + inc)
    sim_note = {}             # incoming path -> why it counts as a copy of a library song although the lengths differ
    for g in groups:
        if g["kind"] not in ("exact", "variant", "review", "wide"):
            continue
        mem_in = [m for m in g["members"] if m["path"].startswith(INCOMING_PREFIX)]
        mem_lib = [m for m in g["members"] if not m["path"].startswith(INCOMING_PREFIX)]
        if not mem_in:
            continue
        if g["kind"] in ("review", "wide"):
            # same title and artist, but the lengths disagree (another master, a fade-out, a trimmed intro). The library
            # already has the song, so it is not added a second or third time; a generic title ("Intro") proves nothing
            if mem_lib and not a.keep_similar and "generic title" not in g["note"] and "different featured artists" not in g["note"]:
                for m in mem_in:
                    near = min(mem_lib, key=lambda x: abs(x["duration"] - m["duration"]))
                    skip[m["path"]] = near["path"]
                    sim_note[m["path"]] = "same song as in the library, length differs by %.1fs (%s here, %s there)" % (
                        abs(m["duration"] - near["duration"]), fmt_dur(m["duration"]), fmt_dur(near["duration"]))
            continue
        if g["kind"] == "exact" and mem_lib:
            # the same recording is already in the library: the library copy always stays the one, whatever the quality
            keep_lib = next((m for m in mem_lib if m["role"] == "keep"), mem_lib[0])["path"]
            up = None
            if upgrade_on:
                # place-upgrade: only a strictly higher AUDIO QUALITY counts (FLAC over MP3, 24-bit over 16-bit), never a tie-break
                lib_q = max(qual.get(m["path"], 0) for m in mem_lib)
                cand = [m for m in mem_in if m["role"] == "keep" and qual.get(m["path"], 0) > lib_q]
                if cand:
                    up = cand[0]["path"]
                    upgrades.append((up, keep_lib))
            for m in mem_in:
                if m["path"] == up:
                    continue                    # added next to the library copy; scan/playlistfix/apply retire the old one
                skip[m["path"]] = up or keep_lib
                if m["role"] == "keep" and not up:
                    better.append((m["path"], keep_lib))
            continue
        for m in mem_in:
            if m["role"] == "remove" and m.get("winner"):
                skip[m["path"]] = m["winner"]

    # 2. where everything else belongs (the same planner as the reorg, library included so albums and artists line up)
    placed_to = {}
    for _round in range(4):
        cand = [t for t in inc if t.rel not in skip]
        items, notes = reorg_plan(lib + cand, rules, music, reorg_history(a.out))
        placed_to = {}
        held = {}
        for it in items:
            if it["path"].startswith(INCOMING_PREFIX):
                if it["to"] and it["action"] in ("move", "rename", "ok"):
                    placed_to[it["path"]] = it["to"]
                else:
                    held[it["path"]] = it["reason"] or "not placed"
        # a duplicate whose kept copy could not be placed: place the duplicate instead
        undo = [p for p, w in skip.items() if w.startswith(INCOMING_PREFIX) and w not in placed_to]
        if not undo:
            break
        for p in undo:
            del skip[p]

    def resolve(p, depth=0):
        if not p.startswith(INCOMING_PREFIX):
            return p
        if p in placed_to:
            return placed_to[p]
        w = skip.get(p)
        return resolve(w, depth + 1) if w and depth < 8 else None

    rows = []
    up_lib = dict(upgrades)
    for t in inc:
        pl = t.inc_rel.split("/")[0] if "/" in t.inc_rel else ""
        if t.rel in skip:
            dest = resolve(t.rel)
            if dest:
                w_ = skip[t.rel]
                rows.append({"playlist": pl, "inc": t.inc_rel, "status": "duplicate", "dest": dest,
                             "why": ("also arrives as %s" % w_[len(INCOMING_PREFIX):]) if w_.startswith(INCOMING_PREFIX)
                             else sim_note.get(t.rel, "already in the library")})
                continue
        if t.rel in placed_to:
            rows.append({"playlist": pl, "inc": t.inc_rel, "status": "place", "dest": placed_to[t.rel],
                         "why": ("upgrade: higher audio quality than %s" % up_lib[t.rel]) if t.rel in up_lib else ""})
        else:
            rows.append({"playlist": pl, "inc": t.inc_rel, "status": "held", "dest": "",
                         "why": held.get(t.rel, "not placed")})
    by_status = Counter(r["status"] for r in rows)
    print("-" * 60)
    print("incoming files %d | to place %d | already in the library / arrive twice %d | not placed %d" % (
        len(rows), by_status["place"], by_status["duplicate"], by_status["held"]))
    pls_n = defaultdict(Counter)
    for r in rows:
        pls_n[r["playlist"] or "(loose files)"][r["status"]] += 1
    for name in sorted(pls_n, key=str.lower):
        c = pls_n[name]
        print("   %-44s %4d files: %d new, %d already have, %d not placed" % (
            name[:44], sum(c.values()), c["place"], c["duplicate"], c["held"]))
    shown = 0
    for r in rows:
        if r["status"] == "place" and shown < a.show:
            print("   PLACE  %s\n          -> %s" % (r["inc"], r["dest"]))
            shown += 1
    if by_status["place"] > shown:
        print("   ... and %d more (all are in place-plan.csv)" % (by_status["place"] - shown))
    for r in [x for x in rows if x["status"] == "duplicate"][:a.show]:
        print("   HAVE   %s\n          -> %s   (%s)" % (r["inc"], r["dest"], r["why"]))
    if by_status["duplicate"] > a.show:
        print("   ... and %d more duplicates" % (by_status["duplicate"] - a.show))
    for r in [x for x in rows if x["status"] == "held"][:100]:
        print("   NOT PLACED  %s : %s" % (r["inc"], r["why"]))
    if by_status["held"] > 100:
        print("   ... and %d more not placed" % (by_status["held"] - 100))
    ups = [(p, l) for p, l in upgrades if p in placed_to]
    if ups:
        print("upgrade: %d incoming file(s) have higher audio quality than the copy already in the library. They are ADDED next to it "
              "(place-upgrade). Afterwards run scan -> playlistfix -> apply: the lower-quality copy is retired, its playlist spots and stars follow." % len(ups))
        for p_, l_ in ups[:10]:
            print("   %s  upgrades  %s" % (p_.replace(INCOMING_PREFIX, ""), l_))
    if better:
        print("note: %d incoming file(s) look better than the copy already in the library (quality/tags). They are NOT added - "
              "the library copy keeps its plays, stars and playlist spots - and are set aside with the other duplicates "
              "if you place. Add 'place-upgrade: yes' to rules.txt (or use --upgrade) to add those with higher audio quality "
              "next to the library copy; otherwise swap one in by hand." % len(better))
    for b_, l_ in better[:10]:
        print("   %s  beats  %s" % (b_.replace(INCOMING_PREFIX, ""), l_))
    with open(os.path.join(a.out, "place-plan.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["playlist", "incoming_file", "status", "library_path", "note"])
        for r in rows:
            w.writerow([r["playlist"], r["inc"], r["status"], r["dest"], r["why"]])

    # 3. playlists: built from where each file ended up
    update = set(x.strip().lower() for x in re.split(r"[;|]", a.m3u_update or "") if x.strip())
    pl_dir = os.path.join(music, "Playlists")
    pl_plan = []
    for name in sorted(pls_n, key=str.lower):
        if name == "(loose files)":
            continue
        entries, seen = [], set()
        for r in sorted((x for x in rows if x["playlist"] == name and x["status"] in ("place", "duplicate")),
                        key=lambda x: x["inc"].lower()):
            if r["dest"] in seen:
                continue
            seen.add(r["dest"])
            entries.append((os.path.splitext(posixpath.basename(r["inc"]))[0], r["dest"]))
        m3u = os.path.join(pl_dir, place_clean_name(name) + ".m3u")
        if os.path.exists(m3u):
            mode = "append" if name.lower() in update else "leave"
        else:
            mode = "create"
        pl_plan.append((name, m3u, mode, entries))
    # an earlier --execute run that was stopped halfway leaves its playlist plan in place-pending.json: fold it in, so the playlist
    # is built from every song of the folder and not only from the ones that were still waiting in the landing folder
    pend_path = os.path.join(a.out, PLACE_PENDING)
    prev = place_load_pending(pend_path)
    if prev:
        have_names = set(n for n, _, _, _ in pl_plan)
        merged = []
        for name, m3u, mode, entries in pl_plan:
            if name in prev:
                got = set(d for _, d in prev[name])
                entries = list(prev[name]) + [e for e in entries if e[1] not in got]
            merged.append((name, m3u, mode, entries))
        for name in sorted(prev, key=str.lower):
            if name not in have_names:
                m3u = os.path.join(pl_dir, place_clean_name(name) + ".m3u")
                mode = ("append" if name.lower() in update else "leave") if os.path.exists(m3u) else "create"
                merged.append((name, m3u, mode, list(prev[name])))
        pl_plan = merged
        print("note: an earlier place run was stopped before it finished; its playlist(s) are completed: %s" %
              ", ".join(sorted(prev, key=str.lower)))
    for name, m3u, mode, entries in pl_plan:
        if mode == "leave":
            print("playlist '%s' already exists - left alone (Navidrome re-reads an .m3u that changes and would overwrite edits "
                  "made in Navidrome). Add --m3u-update \"%s\" to append the new tracks." % (name, name))
        else:
            print("playlist '%s': %s (%d tracks)" % (name, "would create" if mode == "create" and not a.execute
                                                         else mode, len(entries)))
    # a fingerprint of the whole plan (every file's fate and every playlist change): the web runner passes the one from the dry run
    # you confirmed as --expect-plan, so a real run only goes ahead on exactly the plan you read
    fp_src = ["%s\t%s\t%s" % (r["inc"], r["status"], r["dest"]) for r in rows]
    fp_src += ["PL\t%s\t%s\t%s" % (name, mode, ";".join("%s|%s" % e for e in entries)) for name, _m3u, mode, entries in pl_plan]
    fp = hashlib.sha256("\n".join(fp_src).encode("utf-8")).hexdigest()[:16]
    print("plan fingerprint: %s" % fp)
    if not a.execute:
        print("\nDRY RUN - nothing changed. Add --execute to do it.")
        return
    if a.expect_plan and a.expect_plan != fp:
        sys.exit("REFUSED: the plan you confirmed (fingerprint %s) is not the plan this run would carry out (%s): files arrived, changed or went "
                 "missing since the dry run. Nothing was changed. Do a fresh dry run and confirm that one." % (a.expect_plan, fp))
    if not rows and not prev:
        return
    place_save_pending(pend_path, pl_plan)

    run_id = time.strftime("%Y%m%d-%H%M%S")
    qroot = os.path.join(a.quarantine or os.path.join(os.path.dirname(music), "_dedupe-quarantine"), "place-" + run_id)
    done = Counter()
    mapf = open(os.path.join(a.out, INCOMING_MAP), "a", encoding="utf-8", buffering=1)      # one line per file, written at once
    manifest = {"run": run_id, "kind": "place", "incoming": incoming, "quarantine": qroot, "files": []}
    mpath = os.path.join(qroot, "manifest.json")
    try:
        for r in rows:
            src = os.path.join(incoming, r["inc"])
            key = r["inc"]
            if r["status"] == "place":
                dest = os.path.join(music, r["dest"])
                if os.path.exists(dest):
                    print("SKIP (target exists): %s" % r["dest"])
                    r["status"] = "held"
                    r["why"] = "target exists"
                    done["held"] += 1
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(src, dest)
                lrc = os.path.splitext(src)[0] + ".lrc"
                if os.path.exists(lrc):
                    d2 = os.path.splitext(dest)[0] + ".lrc"
                    if not os.path.exists(d2):
                        shutil.move(lrc, d2)
                mapf.write("%s\tplace\t%s\t%s\n" % (key, r["dest"], run_id))
                done["place"] += 1
            elif r["status"] == "duplicate":
                qdest = os.path.join(qroot, r["inc"])
                os.makedirs(os.path.dirname(qdest), exist_ok=True)
                shutil.move(src, qdest)
                manifest["files"].append({"from": r["inc"], "to": os.path.relpath(qdest, qroot).replace(os.sep, "/")})
                mapf.write("%s\tduplicate\t%s\t%s\n" % (key, r["dest"], run_id))
                done["duplicate"] += 1
                if len(manifest["files"]) <= 500 or len(manifest["files"]) % 25 == 0:
                    save_json_atomic(mpath, manifest)      # a stopped run can still be undone
    finally:
        mapf.close()
        if manifest["files"]:
            save_json_atomic(mpath, manifest)
    pruned = place_prune_incoming(incoming)

    ok_dest = {}
    for r in rows:
        ok_dest[r["inc"]] = r["status"]
    wrote = 0
    for name, m3u, mode, entries in pl_plan:
        entries = [e for e in entries if os.path.exists(os.path.join(music, e[1]))]
        if mode == "leave" or not entries:
            continue
        os.makedirs(pl_dir, exist_ok=True)
        if mode == "create":
            lines = ["#EXTM3U"]
            for stem, dest in entries:
                lines += ["#EXTINF:-1,%s" % stem, "../" + dest]
            with open(m3u, "wb") as fh:
                fh.write(("\n".join(lines) + "\n").encode("utf-8"))
            wrote += 1
            print("wrote playlist %s (%d tracks)" % (os.path.basename(m3u), len(entries)))
        else:
            text, enc, bom, crlf = read_text_guess(m3u)
            have = set(resolve_entry(l, "Playlists", music) for l in text.splitlines())
            add = [e for e in entries if e[1] not in have]
            if not add:
                continue
            nl = "\r\n" if crlf else "\n"
            body = text.rstrip("\r\n") + nl + nl.join("%s%s%s" % ("#EXTINF:-1,%s" % s, nl, "../" + d) for s, d in add) + nl
            data = body.encode("utf-8" if enc == "utf-8" else "latin-1")
            if bom:
                data = b"\xef\xbb\xbf" + data
            bk = os.path.join(qroot, "_playlists", os.path.basename(m3u))
            os.makedirs(os.path.dirname(bk), exist_ok=True)
            st_ = os.stat(m3u)
            shutil.copy2(m3u, bk)
            with open(m3u, "wb") as fh:
                fh.write(data)
            wrote += 1
            print("appended %d track(s) to %s (backup in %s)" % (len(add), os.path.basename(m3u), bk))
    try:
        os.remove(pend_path)                  # everything finished: nothing left to complete
    except OSError:
        pass
    print("\nplaced %d | duplicates set aside %d | not placed (left in the landing folder) %d | playlists written %d | empty landing folders removed %d" % (
        done["place"], done["duplicate"], by_status["held"] + done["held"], wrote, pruned))
    if done["duplicate"]:
        print("duplicates were moved to %s (nothing is deleted; 'purge' clears old runs)" % qroot)
    print("next: Navidrome rescan; a scheduled Beets import (if you run one) picks the new files up; a library scan in any other app that indexes the music.")


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="Dewey " + VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--music", default=env("MD_MUSIC"), help="library root (e.g. /media/Music)")
        p.add_argument("--out", default=env("MD_OUT", "./out"), help="where reports/plan/cache go")
        p.add_argument("--rules", default=env("MD_RULES"), help="rules.txt (exceptions)")
        p.add_argument("--playlist-dir", action="append", default=[], help="extra folder with .m3u files")
        p.add_argument("--quarantine", default=env("MD_QUAR"), help="quarantine root (outside the library)")

    s = sub.add_parser("scan", help="read-only scan + report")
    common(s)
    s.add_argument("--navidrome-db", default=env("MD_NDDB"))
    s.add_argument("--tolerance", type=float, default=None, help="max length difference in seconds for an automatic match (default 3)")
    s.add_argument("--wide-tolerance", type=float, default=None, help="copies up to this far apart become 'wide-gap' groups that need your OK (default 6)")
    s.add_argument("--workers", type=int, default=4)
    s.set_defaults(fn=cmd_scan)

    p = sub.add_parser("apply", help="quarantine losers (dry run unless --execute)")
    common(p)
    p.add_argument("--plan")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--artist", help="only groups whose artist contains this")
    p.add_argument("--group", action="append", help="only this group id (repeatable)")
    p.add_argument("--kinds", default="exact,variant", help="group kinds to act on (add 'wide' to include wide-gap groups)")
    p.add_argument("--exact-only", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-navidrome-check", action="store_true")
    p.add_argument("--expect-remove", type=int, default=None,
                   help="with --execute: stop before changing anything unless exactly this many files would be removed (the web runner passes the number from the dry run you confirmed)")
    p.add_argument("--bump-m3u-mtime", action="store_true",
                   help="touch rewritten .m3u files (makes Navidrome re-import them)")
    p.set_defaults(fn=cmd_apply)

    pf = sub.add_parser("playlistfix", help="point Navidrome playlists at the kept copy (dry run unless --execute)")
    common(pf)
    pf.add_argument("--plan")
    pf.add_argument("--execute", action="store_true")
    pf.add_argument("--artist", help="only groups whose artist contains this")
    pf.add_argument("--group", action="append", help="only this group id (repeatable)")
    pf.add_argument("--kinds", default="exact,variant", help="group kinds to act on (add 'wide' for wide-gap groups)")
    pf.add_argument("--exact-only", action="store_true")
    pf.add_argument("--limit", type=int, default=0)
    pf.add_argument("--playlist", action="append", help="only this playlist (by name, repeatable) - for a test run")
    pf.add_argument("--no-stars", action="store_true", help="do not carry stars/ratings over to the kept copy")
    pf.add_argument("--all-users", action="store_true", help="also try playlists that belong to other users (needs an admin login)")
    pf.add_argument("--url", help="Navidrome URL (default: navidrome.cred)")
    pf.add_argument("--user", help="Navidrome user (default: navidrome.cred)")
    pf.add_argument("--cred", help="credentials file (default /navidrome.cred)")
    pf.add_argument("--nd-db", default=env("MD_NDDB"), help="Navidrome database (read-only copy is used)")
    pf.add_argument("--restore", help="restore playlists from a playlist-backups folder or file instead")
    pf.set_defaults(fn=cmd_playlistfix)

    r = sub.add_parser("reorg", help="plan one folder layout for the whole library (read-only)")
    common(r)
    r.add_argument("--workers", type=int, default=4)
    r.set_defaults(fn=cmd_reorg)

    ra = sub.add_parser("reorg-apply", help="move files into the reorg layout (dry run unless --execute)")
    common(ra)
    ra.add_argument("--plan")
    ra.add_argument("--execute", action="store_true")
    ra.add_argument("--artist", help="only artist folders containing this")
    ra.add_argument("--album", help="only album folders containing this")
    ra.add_argument("--limit", type=int, default=0, help="about this many files (the album that crosses the limit is finished)")
    ra.add_argument("--show", type=int, default=400, help="how many changes the dry run lists (all are in reorg-plan.csv)")
    ra.add_argument("--nd-db", default=env("MD_NDDB"), help="Navidrome database, backed up before the first move")
    ra.add_argument("--no-navidrome-backup", action="store_true")
    ra.add_argument("--expect-moves", type=int, default=None,
                    help="with --execute: stop before changing anything unless exactly this many files would be moved (the web runner passes the number from the dry run you confirmed)")
    ra.add_argument("--reorg-root", default=env("MD_REORG"), help="where run folders go (default: _reorg next to the library)")
    ra.add_argument("--bump-m3u-mtime", action="store_true", help="touch rewritten .m3u files (Navidrome re-imports them)")
    ra.set_defaults(fn=cmd_reorg_apply)

    u = sub.add_parser("undo", help="restore a quarantine run or a reorg run")
    u.add_argument("--manifest", required=True)
    u.add_argument("--music", default=env("MD_MUSIC"), help="library root: a manifest for a different library is refused")
    u.add_argument("--expect-plan", default=None,
                   help="with --execute: stop before changing anything unless the plan's fingerprint (printed by the dry run) is exactly this "
                        "(the web runner passes the dry run you confirmed)")
    u.add_argument("--execute", action="store_true")
    u.set_defaults(fn=cmd_undo)

    b = sub.add_parser("beets-remap", help="point the Beets library.db at the files' new paths after a reorg (dry run unless --execute)")
    b.add_argument("--music", default=env("MD_MUSIC"), help="library root (e.g. /media/Music)")
    b.add_argument("--out", default=env("MD_OUT", "./out"))
    b.add_argument("--beets-db", default=env("MD_BEETSDB"), help="the Beets library.db")
    b.add_argument("--beets-prefix", default=env("MD_BEETSPREFIX", "/music"),
                   help="the library root as Beets sees it (the 'directory:' in its config.yaml)")
    b.add_argument("--expect-plan", default=None,
                   help="with --execute: stop before changing anything unless the plan's fingerprint (printed by the dry run) is exactly this "
                        "(the web runner passes the one from the dry run you confirmed)")
    b.add_argument("--execute", action="store_true")
    b.set_defaults(fn=cmd_beets_remap)

    nm = sub.add_parser("nd-missing", help="read-only: explain Navidrome's 'Missing Files' list (moved / gone / still on disk, and whether any carry plays, stars or playlists)")
    nm.add_argument("--music", default=env("MD_MUSIC"), help="library root (e.g. /media/Music)")
    nm.add_argument("--out", default=env("MD_OUT", "./out"))
    nm.add_argument("--nd-db", default=env("MD_NDDB"), help="Navidrome database (a read-only copy is used)")
    nm.set_defaults(fn=cmd_nd_missing)

    ta = sub.add_parser("tagaudit", help="read-only: list files and albums that lack tags, cover art or lyrics")
    ta.add_argument("--music", default=env("MD_MUSIC"), help="library root (e.g. /media/Music)")
    ta.add_argument("--out", default=env("MD_OUT", "./out"))
    ta.add_argument("--workers", type=int, default=4)
    ta.set_defaults(fn=cmd_tagaudit)

    cp = sub.add_parser("compare", help="read-only: compare two library files by their decoded audio (same version? clean vs explicit?)")
    cp.add_argument("file_a", help="path relative to the Music folder")
    cp.add_argument("file_b", help="path relative to the Music folder")
    cp.add_argument("--music", default=env("MD_MUSIC"), help="library root (e.g. /media/Music)")
    cp.set_defaults(fn=cmd_compare)

    pl = sub.add_parser("place", help="file newly arrived music from the _incoming landing folder into the library layout (dry run unless --execute)")
    common(pl)
    pl.add_argument("--incoming", default=env("MD_INCOMING"), help="landing folder (default: _incoming next to the music folder)")
    pl.add_argument("--navidrome-db", default=env("MD_NDDB"))
    pl.add_argument("--workers", type=int, default=4)
    pl.add_argument("--show", type=int, default=40, help="how many placements the dry run lists (all are in place-plan.csv)")
    pl.add_argument("--keep-similar", action="store_true",
                    help="also place incoming songs whose title/artist match a library song but whose length differs "
                         "(default: treated as the same song and set aside like any other duplicate)")
    pl.add_argument("--upgrade", action="store_true",
                    help="add an incoming copy of higher audio quality (FLAC over MP3) next to the library copy; same as 'place-upgrade: yes' in rules.txt")
    pl.add_argument("--expect-plan", default=None,
                    help="with --execute: stop before changing anything unless the plan's fingerprint (printed by the dry run) is exactly this (the web runner passes the one from the dry run you confirmed)")
    pl.add_argument("--m3u-update", default="", help="playlist names whose existing .m3u may get the new tracks appended (separate with |)")
    pl.add_argument("--execute", action="store_true")
    pl.set_defaults(fn=cmd_place)

    g = sub.add_parser("purge", help="delete old quarantine runs")
    g.add_argument("--quarantine", default=env("MD_QUAR"))
    g.add_argument("--older-than", type=int, default=30, help="days")
    g.add_argument("--expect-plan", default=None,
                   help="with --execute: delete nothing unless the list of runs to delete has exactly this fingerprint (printed by the dry run; "
                        "the web runner passes the dry run you confirmed)")
    g.add_argument("--execute", action="store_true")
    g.set_defaults(fn=cmd_purge)

    a = ap.parse_args()
    if a.cmd in ("scan", "reorg", "beets-remap", "nd-missing", "place", "compare", "tagaudit") and not a.music:
        sys.exit("--music is required")
    if a.cmd in ("apply", "purge") and not a.quarantine:
        a.quarantine = os.path.join(os.path.dirname(os.path.abspath(getattr(a, "music", None) or ".")), "_dedupe-quarantine")
    if a.cmd == "apply" and not getattr(a, "music", None):
        a.music = None
    if a.cmd in ("apply", "undo", "purge", "playlistfix", "reorg", "reorg-apply", "beets-remap", "nd-missing", "place"):
        start_log(a)
    a.fn(a)


if __name__ == "__main__":
    main()
