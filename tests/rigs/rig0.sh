#!/bin/bash
# usage: setup_rig.sh <name>   builds a fresh rig directory and starts the runner + console from it
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
SP="${DEWEY_TEST_TMP:-/tmp/dewey-tests}"; mkdir -p "$SP"
R=$SP/$1
mkdir -p $R/out $R/queue $R/state $R/nd $R/media/Music
cd $R
echo '{"groups":[],"generated":"2026-10-24 10:00:00"}' > out/plan.json
printf 'url=http://127.0.0.1:4533\nuser=tester\n' > navidrome.cred
touch nd/navidrome.db rules.txt
cat > stubapp.py <<'EOF'
import os, sys, time
print("STUB dewey args:", sys.argv[1:])
print("password present:", bool(os.environ.get("MD_ND_PASS")), "len", len(os.environ.get("MD_ND_PASS","")))
open(os.environ.get("MD_OUT","/tmp")+"/stub-ran.txt","a").write(" ".join(sys.argv[1:])+"\n")
if "--limit=999" in sys.argv:
    for i in range(12):
        print("working", i, flush=True); time.sleep(1)
sys.exit(0)
EOF
cat > env.sh <<EOF
export MD_QUEUE=$R/queue MD_OUT=$R/out MD_STATE=$R/state MD_APP=$R/stubapp.py MD_ND_CRED=$R/navidrome.cred MD_CRED=$R/navidrome.cred MD_NDDB=$R/nd/navidrome.db MD_MUSIC=$R/media/Music MD_RULES=$R/rules.txt MD_WEBROOT=$REPO/web
export MD_WEB_USER=tester MD_WEB_PASS=test-password-1234 MD_WEB_PORT=18099 MD_WEB_HOST=127.0.0.1
EOF
. ./env.sh
nohup python3 $REPO/runner.py > runner.log 2>&1 &
echo $! > runner.pid
nohup python3 $REPO/webui.py > web.log 2>&1 &
echo $! > web.pid
sleep 2.5
cat runner.log web.log
