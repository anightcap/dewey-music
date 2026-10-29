#!/bin/bash
# usage: rig7.sh <name>   fresh rig with a small REAL library (generated tones) that has two groups waiting for a decision, scanned by the real tool
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
command -v ffmpeg >/dev/null || exit 77
SP="${DEWEY_TEST_TMP:-/tmp/dewey-tests}"; mkdir -p "$SP"
R=$SP/$1
mkdir -p $R/out $R/queue $R/state $R/nd $R/media/Music $R/media/_dedupe-quarantine
cd $R
printf 'url=http://127.0.0.1:4533\nuser=tester\n' > navidrome.cred
touch nd/navidrome.db rules.txt
python3 - <<PY
import sys
sys.path.insert(0, "$REPO/demo")
import make_demo
M = "$R/media/Music"
make_demo.tone(M + "/Test Artist/Compilation Hits (Explicit Version) (1998)/02 - Shared Song.flac", 330, 12, "Test Artist", "Shared Song", "Compilation Hits (Explicit Version)", 2, "1998")
make_demo.tone(M + "/Test Artist/Real Album (1996)/1-06 - Shared Song (ft. Guest Person).flac", 330, 8, "Test Artist", "Shared Song", "Real Album", 6, "1996")
make_demo.tone(M + "/Other Artist/First (2001)/01 - Odd Length.flac", 440, 8, "Other Artist", "Odd Length", "First", 1, "2001")
make_demo.tone(M + "/Other Artist/Second (2003)/01 - Odd Length.flac", 440, 40, "Other Artist", "Odd Length", "Second", 1, "2003")
PY
cat > env.sh <<EOF2
export MD_QUEUE=$R/queue MD_OUT=$R/out MD_STATE=$R/state MD_APP=$REPO/dewey.py MD_ND_CRED=$R/navidrome.cred MD_CRED=$R/navidrome.cred MD_NDDB=$R/nd/navidrome.db MD_MUSIC=$R/media/Music MD_QUAR=$R/media/_dedupe-quarantine MD_RULES=$R/rules.txt MD_WEBROOT=$REPO/web MD_MEDIA=$R/media
export MD_WEB_USER=tester MD_WEB_PASS=test-password-1234 MD_WEB_PORT=18099 MD_WEB_HOST=127.0.0.1
EOF2
. ./env.sh
python3 $REPO/dewey.py scan --tolerance 5 --wide-tolerance 10 > scan.log 2>&1
nohup python3 $REPO/runner.py > runner.log 2>&1 &
echo $! > runner.pid
nohup python3 $REPO/webui.py > web.log 2>&1 &
echo $! > web.pid
sleep 2.5
cat runner.log web.log
