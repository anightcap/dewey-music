#!/usr/bin/env python3
"""Build a small demo library to try Dewey on, with no music of yours involved.

Every "song" is eight seconds of a generated tone (the duplicate check needs about seven) (ffmpeg), tagged with made-up artists, so there is nothing to
license and nothing to lose. The library is deliberately messy: it has copies of the same song, files whose names
do not follow the layout, a playlist, missing cover art and a file with no tags, and a landing folder
(_incoming) with new music waiting to be filed.

    python3 demo/make_demo.py            builds demo/media/Music, demo/media/_incoming and a stand-in demo/navidrome-data/navidrome.db
    python3 demo/make_demo.py --clean    deletes demo/media, demo/navidrome-data and demo/out and builds it again

Needs ffmpeg on the PATH. See demo/README.md for what to run on it."""
import hashlib, os, shutil, sqlite3, subprocess, sys

HERE = os.environ.get("DEWEY_DEMO_DIR") or os.path.dirname(os.path.abspath(__file__))   # DEWEY_DEMO_DIR: build somewhere else (the tests do)
MEDIA = os.path.join(HERE, "media")
MUSIC = os.path.join(MEDIA, "Music")
INCOMING = os.path.join(MEDIA, "_incoming")


def tone(path, freq, secs, artist, title, album, track, year, codec="flac", tags=True, extra=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=%d:duration=%s" % (freq, secs)]
    if codec == "mp3":
        cmd += ["-codec:a", "libmp3lame", "-b:a", "128k"]
    if tags:
        for k, v in (("artist", artist), ("album_artist", artist), ("title", title), ("album", album), ("track", track), ("date", year)):
            if v:
                cmd += ["-metadata", "%s=%s" % (k, v)]
    for k, v in (extra or {}).items():
        cmd += ["-metadata", "%s=%s" % (k, v)]
    subprocess.run(cmd + [path], check=True)


def main():
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is needed to generate the demo audio (apt install ffmpeg / brew install ffmpeg)")
    if "--clean" in sys.argv:
        for d in ("media", "navidrome-data", "out"):
            shutil.rmtree(os.path.join(HERE, d), ignore_errors=True)
    if os.path.exists(MUSIC):
        sys.exit("demo/media already exists; run with --clean to build it again")
    os.makedirs(os.path.join(HERE, "navidrome-data"), exist_ok=True)
    os.makedirs(os.path.join(MEDIA, "_dedupe-quarantine"), exist_ok=True)
    os.makedirs(os.path.join(MEDIA, "_reorg"), exist_ok=True)

    # 1. Lumen Harbor: four songs whose file names say nothing (the layout plan will rename them),
    #    and the same recording stored a second time as a lower-quality MP3 in a Downloads folder.
    a = "Lumen Harbor"; al = "Quiet Signals"
    for i, (f, t, hz) in enumerate([("track1.flac", "Signal Fire", 330), ("track2.flac", "Low Tide", 349), ("track3.flac", "Paper Moon", 392), ("track4.flac", "Last Ferry", 440)], 1):
        tone(os.path.join(MUSIC, a, al, f), hz, 8, a, t, al, i, "2019")
    with open(os.path.join(MUSIC, a, al, "cover.jpg"), "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0demo-cover\xff\xd9")
    tone(os.path.join(MUSIC, "Downloads", "Lumen Harbor - Signal Fire.mp3"), 330, 8, a, "Signal Fire", al, 1, "2019", codec="mp3")

    # 2. Paper Lanterns: tidy folders already in the right layout; one song is stored twice (an exact copy).
    a = "Paper Lanterns"; al = "Slow River"
    for i, (t, hz) in enumerate([("Morning Mist", 262), ("Stone Bridge", 294), ("Open Water", 311)], 1):
        tone(os.path.join(MUSIC, a, "%s (2021)" % al, "%02d - %s.flac" % (i, t)), hz, 8, a, t, al, i, "2021")
    os.makedirs(os.path.join(MUSIC, "Misc"), exist_ok=True)
    shutil.copy(os.path.join(MUSIC, a, "%s (2021)" % al, "02 - Stone Bridge.flac"), os.path.join(MUSIC, "Misc", "stone-bridge-copy.flac"))
    with open(os.path.join(MUSIC, a, "%s (2021)" % al, "cover.jpg"), "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0demo-cover\xff\xd9")

    # 3. Velvet Static: no cover art, and one file with no tags at all (Dewey lists it but never files it from its name).
    a = "Velvet Static"; al = "Dial Tone"
    tone(os.path.join(MUSIC, a, al, "01 Hum.flac"), 233, 8, a, "Hum", al, 1, "2016")
    tone(os.path.join(MUSIC, a, al, "02 Buzz.flac"), 247, 8, a, "Buzz", al, 2, "2016")
    tone(os.path.join(MUSIC, "Misc", "mystery-recording.flac"), 523, 8, None, None, None, None, None, tags=False)

    # 4. A song that exists twice with lengths a few seconds apart (a longer fade): Dewey lists it for your decision, it is never applied by itself.
    a = "Harbor Lights"; al = "Long Way Home"
    tone(os.path.join(MUSIC, a, "%s (2012)" % al, "01 - Home Stretch.flac"), 370, 8, a, "Home Stretch", al, 1, "2012")
    tone(os.path.join(MUSIC, a, "Singles", "Home Stretch (long fade).flac"), 370, 12, a, "Home Stretch", "Singles", 1, "2012")

    # A playlist that points at one of the duplicates, so you can see playlists being kept intact.
    os.makedirs(os.path.join(MUSIC, "Playlists"), exist_ok=True)
    with open(os.path.join(MUSIC, "Playlists", "Evening.m3u"), "w") as fh:
        fh.write("#EXTM3U\n../Misc/stone-bridge-copy.flac\n../Lumen Harbor/Quiet Signals/track2.flac\n../Velvet Static/Dial Tone/01 Hum.flac\n")

    # New music waiting in the landing folder: a new album, a song the library already has, and a folder that becomes a playlist.
    a = "Neon Tide"; al = "Afterglow"
    for i, (t, hz) in enumerate([("Coastline", 415), ("Sodium Light", 466), ("Static Bloom", 494)], 1):
        tone(os.path.join(INCOMING, "%s - %s (2024)" % (a, al), "%02d %s.flac" % (i, t)), hz, 8, a, t, al, i, "2024")
    shutil.copy(os.path.join(MUSIC, "Paper Lanterns", "Slow River (2021)", "01 - Morning Mist.flac"), os.path.join(INCOMING, "Neon Tide - Afterglow (2024)", "extra - Morning Mist.flac"))
    tone(os.path.join(INCOMING, "Road Trip Mix", "Highway Hymn.flac"), 554, 8, "The Mile Markers", "Highway Hymn", "Singles", 1, "2023")
    tone(os.path.join(INCOMING, "Road Trip Mix", "Gas Station Waltz.flac"), 587, 8, "The Mile Markers", "Gas Station Waltz", "Singles", 2, "2023")

    # A tiny stand-in for Navidrome's database, so you can see playlist protection at work. A real install uses Navidrome's own navidrome.db.
    # It says: the MP3 copy of "Signal Fire" sits in a Navidrome playlist, and the stray copy of "Stone Bridge" is starred and played.
    db = os.path.join(HERE, "navidrome-data", "navidrome.db")
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.executescript("""create table media_file(id text primary key, path text);
        create table annotation(item_id text, item_type text, play_count integer, starred integer, rating integer);
        create table playlist(id text primary key, name text);
        create table playlist_tracks(playlist_id text, media_file_id text);""")
    ids = {}
    for root, _dirs, files in os.walk(MUSIC):
        for f in files:
            if f.lower().endswith((".flac", ".mp3")):
                rel = os.path.relpath(os.path.join(root, f), MUSIC).replace(os.sep, "/")
                ids[rel] = hashlib.md5(rel.encode()).hexdigest()[:12]
                con.execute("insert into media_file values(?,?)", (ids[rel], rel))
    con.execute("insert into playlist values('pl1','Evening')")
    con.execute("insert into playlist_tracks values('pl1',?)", (ids["Downloads/Lumen Harbor - Signal Fire.mp3"],))
    con.execute("insert into playlist_tracks values('pl1',?)", (ids["Velvet Static/Dial Tone/01 Hum.flac"],))
    con.execute("insert into annotation values(?,?,?,?,?)", (ids["Misc/stone-bridge-copy.flac"], "media_file", 12, 1, 5))
    con.commit(); con.close()

    n_music = sum(len(f) for _, _, f in os.walk(MUSIC))
    n_in = sum(len(f) for _, _, f in os.walk(INCOMING))
    print("demo library ready: %d files in demo/media/Music, %d in demo/media/_incoming" % (n_music, n_in))
    print("next: see demo/README.md")


if __name__ == "__main__":
    main()
