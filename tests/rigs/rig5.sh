#!/bin/bash
# usage: setup_rig4.sh <name>   fresh rig: real dewey.py, a small MESSY library of generated audio (to be reorganised), a Navidrome database file, a playlist
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
SP="${DEWEY_TEST_TMP:-/tmp/dewey-tests}"; mkdir -p "$SP"
R=$SP/$1
mkdir -p $R/beets $R/out $R/queue $R/state $R/nd $R/media/Music/Playlists $R/media/_dedupe-quarantine $R/media/_incoming $R/media/_reorg
cd $R
printf 'url=http://127.0.0.1:4533\nuser=tester\n' > navidrome.cred
touch rules.txt
python3 - <<PY
import sqlite3
c = sqlite3.connect("$R/nd/navidrome.db")
c.executescript("create table media_file(id text primary key, path text); insert into media_file values('a','x/y.flac'),('b','x/z.flac'); create table playlist(id text, name text);")
c.commit(); c.close()
PY
mk() { # path freq artist title album track year [secs]
  mkdir -p "$(dirname "$1")"
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=$2:duration=${8:-3}" -metadata artist="$3" -metadata title="$4" -metadata album="$5" -metadata track="$6" -metadata date="$7" -metadata album_artist="$3" "$1"
}
M=$R/media/Music
# artist one: 3 songs with names that do not follow the layout; a lyrics file, cover art and a playlist that points at them
mk "$M/Alpha Band/First Album/a1.flac" 440 "Alpha Band" "Opening" "First Album" 1 2001
mk "$M/Alpha Band/First Album/a2.flac" 450 "Alpha Band" "Middle" "First Album" 2 2001
mk "$M/Alpha Band/First Album/a3.flac" 460 "Alpha Band" "Closing" "First Album" 3 2001
printf '[00:01.00]hello\n' > "$M/Alpha Band/First Album/a1.lrc"
printf 'JPEGDATA' > "$M/Alpha Band/First Album/cover.jpg"
# artist two: 4 songs in one folder, also mis-named
mk "$M/Beta Group/Second Album/b1.flac" 500 "Beta Group" "Uno" "Second Album" 1 2010
mk "$M/Beta Group/Second Album/b2.flac" 510 "Beta Group" "Dos" "Second Album" 2 2010
mk "$M/Beta Group/Second Album/b3.flac" 520 "Beta Group" "Tres" "Second Album" 3 2010
mk "$M/Beta Group/Second Album/b4.flac" 530 "Beta Group" "Cuatro" "Second Album" 4 2010
# artist three: already in the right place (the plan leaves it alone)
mk "$M/Gamma/Done Album (2020)/01 - Fine.flac" 600 "Gamma" "Fine" "Done Album" 1 2020
printf '#EXTM3U\n../Alpha Band/First Album/a2.flac\n../Beta Group/Second Album/b1.flac\n../Gamma/Done Album (2020)/01 - Fine.flac\n' > "$M/Playlists/Mix.m3u"
ln -sfn $M /music
cat > beets/config.yaml <<CFG
directory: /music
library: $R/beets/library.db
import:
  copy: no
  move: no
  write: no
  incremental: no
plugins: []
CFG
beet -c beets/config.yaml import -A -C -W -q /music >/dev/null 2>&1
python3 - <<PY
import sqlite3
c = sqlite3.connect("$R/beets/library.db")
r = c.execute("select album_id from items where cast(path as text) like '%a1.flac'").fetchone()
c.execute("update albums set artpath=? where id=?", (b"/music/Alpha Band/First Album/cover.jpg" if str(c.execute("select path from items limit 1").fetchone()[0]).startswith("b'/music") or True else b"", r[0]))
c.commit()
print("beets items:", c.execute("select count(*) from items").fetchone()[0], "sample path:", c.execute("select path from items limit 1").fetchone()[0], "artpath:", c.execute("select artpath from albums where artpath is not null").fetchall())
PY
# the whole library goes through the layout (CLI, as on the PC): leaves Beets pointing at old paths
export MD_MUSIC=$M MD_OUT=$R/out MD_RULES=$R/rules.txt MD_NDDB=$R/nd/navidrome.db
python3 $REPO/dewey.py reorg --music $M --out $R/out >/dev/null
python3 $REPO/dewey.py reorg-apply --music $M --out $R/out --reorg-root $R/media/_reorg --nd-db $R/nd/navidrome.db --execute 2>&1 | grep -E "^moved|FAILED|Traceback"
cat > env.sh <<EOF2
export MD_QUEUE=$R/queue MD_OUT=$R/out MD_STATE=$R/state MD_APP=$REPO/dewey.py MD_ND_CRED=$R/navidrome.cred MD_CRED=$R/navidrome.cred MD_NDDB=$R/nd/navidrome.db MD_MUSIC=$R/media/Music MD_QUAR=$R/media/_dedupe-quarantine MD_INCOMING=$R/media/_incoming MD_REORG=$R/media/_reorg MD_RULES=$R/rules.txt MD_WEBROOT=$REPO/web MD_MEDIA=$R/media MD_BEETSDB=$R/beets/library.db
export MD_WEB_USER=tester MD_WEB_PASS=test-password-1234 MD_WEB_PORT=18099 MD_WEB_HOST=127.0.0.1
EOF2
. ./env.sh
nohup env -u MD_REORG python3 $REPO/runner.py > runner.log 2>&1 &
echo $! > runner.pid
nohup python3 $REPO/webui.py > web.log 2>&1 &
echo $! > web.pid
sleep 2.5
tail -2 runner.log
