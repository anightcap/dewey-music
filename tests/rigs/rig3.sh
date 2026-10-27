#!/bin/bash
# usage: setup_rig3.sh <name>   fresh rig: real dewey.py, a tiny library of real (generated) audio, an _incoming folder to place
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
SP="${DEWEY_TEST_TMP:-/tmp/dewey-tests}"; mkdir -p "$SP"
R=$SP/$1
mkdir -p $R/out $R/queue $R/state $R/media/Music/Playlists $R/media/_dedupe-quarantine $R/media/_incoming
cd $R
printf 'url=http://127.0.0.1:4533\nuser=tester\n' > navidrome.cred
touch rules.txt
mk() { # path freq artist title album track year [ext codec]
  mkdir -p "$(dirname "$1")"
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=$2:duration=${8:-4}" -metadata artist="$3" -metadata title="$4" -metadata album="$5" -metadata track="$6" -metadata date="$7" -metadata album_artist="$3" "$1"
}
M=$R/media/Music; I=$R/media/_incoming
mk "$M/Lib Artist/Old Album (1999)/01 - Existing Song.flac" 440 "Lib Artist" "Existing Song" "Old Album" 1 1999
mk "$M/Lib Artist/Old Album (1999)/02 - Other Song.flac" 520 "Lib Artist" "Other Song" "Old Album" 2 1999
printf '#EXTM3U\n../Lib Artist/Old Album (1999)/02 - Other Song.flac\n' > "$M/Playlists/Old Mix.m3u"
# incoming: a folder per playlist, plus loose files
mk "$I/Fresh Mix/Existing Song.flac" 440 "Lib Artist" "Existing Song" "Old Album" 1 1999
mk "$I/Fresh Mix/New Song.flac" 600 "New Artist" "New Song" "New Album" 1 2020
mk "$I/Fresh Mix/Another One.mp3" 700 "New Artist" "Another One" "New Album" 2 2020
mk "$I/Old Mix/Brand New.flac" 800 "Third Artist" "Brand New" "Third Album" 1 2021
mk "$I/Loose Song.flac" 900 "Loose Artist" "Loose Song" "Loose Album" 1 2022
cat > env.sh <<EOF2
export MD_QUEUE=$R/queue MD_OUT=$R/out MD_STATE=$R/state MD_APP=$REPO/dewey.py MD_ND_CRED=$R/navidrome.cred MD_CRED=$R/navidrome.cred MD_NDDB=$R/nd/none.db MD_MUSIC=$R/media/Music MD_QUAR=$R/media/_dedupe-quarantine MD_INCOMING=$R/media/_incoming MD_RULES=$R/rules.txt MD_WEBROOT=$REPO/web MD_MEDIA=$R/media
export MD_WEB_USER=tester MD_WEB_PASS=test-password-1234 MD_WEB_PORT=18099 MD_WEB_HOST=127.0.0.1
EOF2
. ./env.sh
nohup python3 $REPO/runner.py > runner.log 2>&1 &
echo $! > runner.pid
nohup python3 $REPO/webui.py > web.log 2>&1 &
echo $! > web.pid
sleep 2.5
cat runner.log | tail -2
