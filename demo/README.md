# Try Dewey on a demo library

This builds a small, made-up music library so you can see what Dewey does **without pointing it at your own music**.
Every song is eight seconds of a generated tone with invented artists, so there is nothing to license and nothing to lose.

The library is deliberately messy:

| What is in it | What Dewey does with it |
|---|---|
| *Stone Bridge* stored twice (one copy starred and played in the stand-in Navidrome database) | `apply` keeps the starred copy and would set the other aside |
| *Signal Fire* as a FLAC and as a lower-quality MP3 in a `Downloads` folder; the MP3 is in a Navidrome playlist | The FLAC is kept, but the MP3 is **BLOCKED**: a playlist holds it and not the FLAC (`playlistfix` would carry the playlist over) |
| *Home Stretch* twice, 8 s and 12 s long | A **wide-gap** group: listed, never applied until you approve it by name |
| Files named `track1.flac`, `01 Hum.flac`, no year in the folder names | `reorg` plans `Artist/Album (YYYY)/NN - Title.ext` and rewrites the `.m3u` playlist to match |
| One file with no tags at all | Listed by `tagaudit`; never filed from its name |
| An `_incoming` folder: a new album, one song the library already has, and a folder (`Road Trip Mix`) that becomes a playlist | `place` files the new songs, recognises the one you already have, writes the playlists |

## 1. Build it

You need Python 3 and `ffmpeg` (and `mutagen` and `numpy`, which the tool uses: `pip install mutagen numpy`).

```sh
python3 demo/make_demo.py
```

This creates `demo/media/Music`, `demo/media/_incoming` and a stand-in `demo/navidrome-data/navidrome.db` (a real install uses Navidrome's own).
Run it with `--clean` to throw the demo away and build it again. All of it is git-ignored.

## 2. Look at it with the command line

Everything below is a **dry run or a read-only report**; nothing is changed. (Linux, macOS or WSL; on Windows without WSL use the Docker route.)

```sh
export MD_MUSIC=$PWD/demo/media/Music MD_OUT=$PWD/demo/out MD_QUAR=$PWD/demo/media/_dedupe-quarantine \
       MD_INCOMING=$PWD/demo/media/_incoming MD_REORG=$PWD/demo/media/_reorg MD_NDDB=$PWD/demo/navidrome-data/navidrome.db

python3 dewey.py scan        # 14 files: 2 exact groups, 1 wide-gap group; 1 removable, 1 blocked
python3 dewey.py apply       # dry run: what would be set aside, what is blocked and why
python3 dewey.py place       # dry run: where the 6 new files would go
python3 dewey.py reorg       # the folder-layout plan (9 files to move)
python3 dewey.py reorg-apply # dry run of the first batch
python3 dewey.py tagaudit    # missing tags, art and lyrics
```

Open `demo/out/report.html` in a browser for the duplicate report.

## 3. Look at it in the web console

With Docker Compose (the console starts **read-only**; it can show everything and run dry runs):

```sh
cp .env.example .env
# in .env set:  DEWEY_MEDIA=<this folder>/demo/media   DEWEY_NAVIDROME_DATA=<this folder>/demo/navidrome-data
#               MD_WEB_PASS=<any password>   DEWEY_UID / DEWEY_GID = your user (id -u, id -g)
mkdir -p data/out data/config
docker compose up -d
```

Open <http://127.0.0.1:8099>, run **Scan**, **File new music** and **Folder layout** from the Run page, then browse Duplicates, New music and Folder layout.

![Overview](../docs/screenshots/overview.png)
![Duplicates](../docs/screenshots/duplicates.png)

## 4. Try a real run (on the demo only)

Real runs change files, so do this only on the demo, and read each dry run first. From the command line:

```sh
python3 dewey.py apply --execute --expect-remove=1    # sets the stray Stone Bridge aside in demo/media/_dedupe-quarantine
python3 dewey.py undo --manifest=demo/media/_dedupe-quarantine/<run folder>/manifest.json --execute   # ...and puts it back
```

(The exact `--expect-...` options are printed at the end of each dry run. From the console, real runs go through the runner: arming, a code from your authenticator app, and typing the count shown on the card; see the main README.)

When you are done: `python3 demo/make_demo.py --clean` resets everything, `rm -rf demo/media demo/navidrome-data demo/out` removes it.
