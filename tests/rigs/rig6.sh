#!/bin/bash
# usage: setup_rig6.sh <name>   fresh rig: REAL runs made with the tool itself (apply, place, reorg-apply) so undo/purge work on genuine manifests
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
SP="${DEWEY_TEST_TMP:-/tmp/dewey-tests}"; mkdir -p "$SP"
R=$SP/$1
mkdir -p $R/out $R/queue $R/state $R/nd $R/media/Music/Playlists $R/media/_dedupe-quarantine $R/media/_incoming $R/media/_reorg
cd $R
printf 'url=http://127.0.0.1:4533\nuser=tester\n' > navidrome.cred
touch rules.txt
python3 - <<PY
import sqlite3
c = sqlite3.connect("$R/nd/navidrome.db")
c.executescript("create table media_file(id text primary key, path text); insert into media_file values('a','x/y.flac'),('b','x/z.flac'); create table playlist(id text, name text);")
c.commit(); c.close()
PY
mk() { mkdir -p "$(dirname "$1")"
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=$2:duration=${8:-9}" -metadata artist="$3" -metadata title="$4" -metadata album="$5" -metadata track="$6" -metadata date="$7" -metadata album_artist="$3" "$1"; }
M=$R/media/Music; I=$R/media/_incoming
mk "$M/Alpha Band/First Album/a1.flac" 440 "Alpha Band" "Opening" "First Album" 1 2001
mk "$M/Alpha Band/First Album/a2.flac" 450 "Alpha Band" "Middle" "First Album" 2 2001
mk "$M/Alpha Band/First Album/a3.flac" 460 "Alpha Band" "Closing" "First Album" 3 2001
printf '[00:01.00]hello\n' > "$M/Alpha Band/First Album/a1.lrc"
mk "$M/Beta Group/Second Album/b1.flac" 500 "Beta Group" "Uno" "Second Album" 1 2010
mk "$M/Beta Group/Second Album/b2.flac" 510 "Beta Group" "Dos" "Second Album" 2 2010
mk "$M/Gamma/Done Album (2020)/01 - Fine.flac" 600 "Gamma" "Fine" "Done Album" 1 2020
mk "$M/Gamma/Done Album (2020)/01 - Fine.mp3" 600 "Gamma" "Fine" "Done Album" 1 2020
mk "$M/Delta/Single (2015)/01 - Song.flac" 700 "Delta" "Song" "Single" 1 2015
mk "$M/Delta/Single (2015)/01 - Song.mp3" 700 "Delta" "Song" "Single" 1 2015
printf '#EXTM3U\n../Alpha Band/First Album/a2.flac\n../Beta Group/Second Album/b1.flac\n../Gamma/Done Album (2020)/01 - Fine.flac\n' > "$M/Playlists/Mix.m3u"
printf '#EXTM3U\n../Gamma/Done Album (2020)/01 - Fine.mp3\n../Delta/Single (2015)/01 - Song.mp3\n' > "$M/Playlists/Old.m3u"
mk "$I/Fresh Mix/Fine.flac" 600 "Gamma" "Fine" "Done Album" 1 2020
mk "$I/Fresh Mix/New One.flac" 800 "Newbie" "New One" "New Album" 1 2022
cat > env.sh <<EOF2
export MD_QUEUE=$R/queue MD_OUT=$R/out MD_STATE=$R/state MD_APP=$REPO/dewey.py MD_ND_CRED=$R/navidrome.cred MD_CRED=$R/navidrome.cred MD_NDDB=$R/nd/navidrome.db MD_MUSIC=$R/media/Music MD_QUAR=$R/media/_dedupe-quarantine MD_INCOMING=$R/media/_incoming MD_REORG=$R/media/_reorg MD_RULES=$R/rules.txt MD_WEBROOT=$REPO/web MD_MEDIA=$R/media
export MD_WEB_USER=tester MD_WEB_PASS=test-password-1234 MD_WEB_PORT=18099 MD_WEB_HOST=127.0.0.1
EOF2
. ./env.sh
D=$REPO/dewey.py
python3 $D scan --workers 1 > out/scan.txt 2>&1 || { tail -20 out/scan.txt; exit 1; }
python3 $D apply --execute --no-navidrome-check > out/apply.txt 2>&1 || { tail -20 out/apply.txt; exit 1; }
python3 $D place --execute > out/place.txt 2>&1 || { tail -20 out/place.txt; exit 1; }
python3 $D reorg --workers 1 > out/reorg.txt 2>&1 || { tail -20 out/reorg.txt; exit 1; }
python3 $D reorg-apply --execute > out/reorgapply.txt 2>&1 || { tail -20 out/reorgapply.txt; exit 1; }
nohup env -u MD_REORG python3 $REPO/runner.py > runner.log 2>&1 &
echo $! > runner.pid
nohup python3 $REPO/webui.py > web.log 2>&1 &
echo $! > web.pid
sleep 2.5
tail -2 runner.log
ls media/_dedupe-quarantine media/_reorg
