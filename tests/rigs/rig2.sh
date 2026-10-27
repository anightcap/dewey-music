#!/bin/bash
# usage: setup_rig2.sh <name>   fresh rig with the REAL dewey.py, a tiny hand-made library and a hand-made plan
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
SP="${DEWEY_TEST_TMP:-/tmp/dewey-tests}"; mkdir -p "$SP"
R=$SP/$1
mkdir -p $R/out $R/queue $R/state $R/nd $R/media/Music $R/media/_dedupe-quarantine
cd $R
printf 'url=http://127.0.0.1:4533\nuser=tester\n' > navidrome.cred
touch nd/navidrome.db rules.txt
python3 - <<PY
import os, json, time
R="$R"; M=R+"/media/Music"
def mk(rel, data=b"x"*1000, mt=None):
    p=os.path.join(M,rel); os.makedirs(os.path.dirname(p),exist_ok=True); open(p,"wb").write(data)
    if mt: os.utime(p,(mt,mt))
    st=os.stat(p); return {"path":rel,"size":st.st_size,"mtime":st.st_mtime}
mt=1700000000
files={}
for rel,n in (("A/One (2000)/01 - Song.flac",5000),("A/One (2000)/01 - Song.mp3",900),("B/Two (2001)/02 - Tune.flac",6000),("B/Two (2001)/02 - Tune.mp3",800),
              ("B/Two (2001)/02 - Tune (Radio Edit).mp3",700),("C/Three (2002)/03 - Hold.mp3",650),("C/Three (2002)/03 - Hold.flac",7000),
              ("D/Four (2003)/04 - Same.flac",5100),("D/Four (2003)/04 - Same.mp3",910)):
    files[rel]=mk(rel,b"y"*n,mt)
open(M+"/A/One (2000)/01 - Song.lrc","w").write("[00:01]hi")
os.makedirs(M+"/Playlists",exist_ok=True); open(M+"/Playlists/mix.m3u","w").write("#EXTM3U\n../A/One (2000)/01 - Song.mp3\n../B/Two (2001)/02 - Tune.mp3\n")
def mem(rel,role,winner=None,fmt="mp3",**kw):
    f=files[rel]; d={"path":rel,"role":role,"winner":winner,"size":f["size"],"mtime":f["mtime"],"fmt":fmt,"duration":200.0,"reason":"lossless wins" if role=="remove" else ""}
    d.update(kw); return d
def grp(i,kind,artist,title,members): return {"id":i,"kind":kind,"artist":artist,"title":title,"members":members,"note":""}
plan={"generated":"2026-10-24 10:00:00","music_root":M,"navidrome_checked":True,"groups":[
 grp("g001","exact","A","Song",[mem("A/One (2000)/01 - Song.flac","keep",fmt="flac"),mem("A/One (2000)/01 - Song.mp3","remove","A/One (2000)/01 - Song.flac")]),
 grp("g002","variant","B","Tune",[mem("B/Two (2001)/02 - Tune.flac","keep",fmt="flac"),mem("B/Two (2001)/02 - Tune.mp3","remove","B/Two (2001)/02 - Tune.flac"),mem("B/Two (2001)/02 - Tune (Radio Edit).mp3","remove","B/Two (2001)/02 - Tune.flac")]),
 grp("g003","exact","C","Hold",[mem("C/Three (2002)/03 - Hold.flac","keep",fmt="flac"),mem("C/Three (2002)/03 - Hold.mp3","blocked",blocked_by=["Road trip"])]),
 grp("g004","wide","D","Same",[mem("D/Four (2003)/04 - Same.flac","keep",fmt="flac"),mem("D/Four (2003)/04 - Same.mp3","remove","D/Four (2003)/04 - Same.flac")]),
]}
json.dump(plan,open(R+"/out/plan.json","w"))
PY
cat > env.sh <<EOF2
export MD_QUEUE=$R/queue MD_OUT=$R/out MD_STATE=$R/state MD_APP=$REPO/dewey.py MD_ND_CRED=$R/navidrome.cred MD_CRED=$R/navidrome.cred MD_NDDB=$R/nd/navidrome.db MD_MUSIC=$R/media/Music MD_QUAR=$R/media/_dedupe-quarantine MD_RULES=$R/rules.txt MD_WEBROOT=$REPO/web MD_MEDIA=$R/media
export MD_WEB_USER=tester MD_WEB_PASS=test-password-1234 MD_WEB_PORT=18099 MD_WEB_HOST=127.0.0.1
EOF2
. ./env.sh
nohup python3 $REPO/runner.py > runner.log 2>&1 &
echo $! > runner.pid
nohup python3 $REPO/webui.py > web.log 2>&1 &
echo $! > web.pid
sleep 2.5
cat runner.log web.log
