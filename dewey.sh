#!/bin/bash
# Dewey launcher - runs the tool in a throwaway Docker container on the NAS.
#
#   bash dewey.sh scan                                  read-only; writes out/report.html
#   bash dewey.sh apply [--artist "Name"]               DRY RUN (library mounted read-only)
#   bash dewey.sh apply --execute [--artist ...]        quarantine the losers
#   bash dewey.sh playlistfix [--playlist "Name"]       DRY RUN: what would change in the Navidrome playlists
#   bash dewey.sh playlistfix --execute                 point playlists at the kept copies (backs them up first)
#   bash dewey.sh playlistfix --restore /out/playlist-backups/<run> [--execute]
#   bash dewey.sh reorg [--rules ...]                   read-only: plan the Artist/Album (YYYY)/NN - Title layout
#   bash dewey.sh reorg-apply --limit 50                DRY RUN: the first moves from that plan
#   bash dewey.sh reorg-apply --limit 50 --execute      move them (navidrome.db is backed up first)
#   bash dewey.sh beets-remap                           DRY RUN: repoint the Beets library.db at the reorganised files
#   bash dewey.sh beets-remap --execute                 do it (library.db is backed up first; no music file is touched)
#   bash dewey.sh place                                 DRY RUN: file new music from the _incoming landing folder into the library
#   bash dewey.sh place --execute [--m3u-update "Name"]   do it (duplicates are set aside in the quarantine, nothing is deleted)
#   bash dewey.sh compare "<A rel path>" "<B rel path>"   read-only: same audio (same version) or a real clean/explicit pair?
#   bash dewey.sh nd-missing                            read-only: explain Navidrome's 'Missing Files' list
#   bash dewey.sh tagaudit                              read-only: files/albums missing tags, cover art or lyrics (out/tagaudit*.csv)
#   bash dewey.sh undo  --manifest <manifest.json> [--execute]   (quarantine, place and reorg runs)
#   bash dewey.sh purge [--older-than 30] [--execute]
#   bash dewey.sh web [up|stop|logs|status]             the read-only web console (container dewey-web, port 8099)
#   bash dewey.sh runner [up|stop|logs|status|secret|newsecret]   the runner: the only container with the library writable; real runs from the console, armed by an authenticator-app code
#
# The library is mounted READ-ONLY unless --execute is on the command line.
# Env overrides: MD_MEDIA (default /volume1/Media), MD_NDDATA (Navidrome data dir),
#                MD_BEETS (folder with the Beets library.db; found automatically under /volume1/docker/beets), MD_TZ, MD_LABEL,
#                MD_REBUILD=1 (rebuild the image)
# playlistfix logs in to Navidrome with navidrome.cred (next to this script; never sent anywhere else).
# web: optional web.env next to this script (KEY=value lines): MD_WEB_USER, MD_WEB_PASS (login), MD_WEB_PORT (default 8099).
set -eu

if [ "$(id -u)" != "0" ]; then
  exec sudo bash "$0" "$@"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA="${MD_MEDIA:-/volume1/Media}"
IMAGE="dewey:2"

# A non-interactive SSH command gets DSM's minimal PATH (no /usr/local/bin), so find docker explicitly.
export PATH="$PATH:/usr/local/bin:/usr/syno/bin:/usr/syno/sbin:/var/packages/ContainerManager/target/usr/bin:/var/packages/Docker/target/usr/bin"
DOCKER="${DOCKER:-}"
if [ -z "$DOCKER" ]; then
  for c in docker /usr/local/bin/docker /var/packages/ContainerManager/target/usr/bin/docker \
           /var/packages/Docker/target/usr/bin/docker /volume1/@appstore/ContainerManager/usr/bin/docker; do
    if command -v "$c" >/dev/null 2>&1; then DOCKER="$(command -v "$c")"; break; fi
  done
fi
if [ -z "$DOCKER" ]; then
  echo "ERROR: can't find the docker binary. On the NAS run:  sudo find / -maxdepth 6 -name docker -type f 2>/dev/null"
  echo "then re-run with that path:  sudo DOCKER=/path/to/docker bash $0 $*"
  exit 1
fi
echo "docker: $DOCKER"

cmd="${1:-help}"
if [ $# -gt 0 ]; then shift; fi
case "$cmd" in
  scan|apply|playlistfix|reorg|reorg-apply|beets-remap|nd-missing|tagaudit|compare|place|undo|purge|web|runner) ;;
  *) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

WNAME="dewey-web"
if [ "$cmd" = "web" ]; then
  case "${1:-up}" in
    stop)   "$DOCKER" rm -f "$WNAME" >/dev/null 2>&1 || true; echo "web console stopped"; exit 0 ;;
    logs)   "$DOCKER" logs --tail 80 "$WNAME"; exit $? ;;
    status) "$DOCKER" ps -a --filter "name=$WNAME" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'; exit 0 ;;
    up|start|restart) ;;
    *) echo "usage: dewey.sh web [up|stop|logs|status]"; exit 2 ;;
  esac
fi

RNAME="dewey-runner"
RSUB="up"
if [ "$cmd" = "runner" ]; then
  RSUB="${1:-up}"
  case "$RSUB" in
    stop)   "$DOCKER" rm -f "$RNAME" >/dev/null 2>&1 || true; echo "runner stopped (write access from the console is off)"; exit 0 ;;
    logs)   "$DOCKER" logs --tail 80 "$RNAME"; exit $? ;;
    status) "$DOCKER" ps -a --filter "name=$RNAME" --format 'table {{.Names}}\t{{.Status}}'; exit 0 ;;
    up|start|restart|secret|newsecret) ;;
    *) echo "usage: dewey.sh runner [up|stop|logs|status|secret|newsecret]"; exit 2 ;;
  esac
fi

if [ ! -d "$MEDIA/Music" ]; then
  echo "ERROR: $MEDIA/Music not found (set MD_MEDIA=/path/to/the/folder/that/contains/Music)"; exit 1
fi
mkdir -p "$HERE/out"
if [ ! -f "$HERE/rules.txt" ]; then : > "$HERE/rules.txt"; fi

# --- find the Navidrome data dir (read-only copy of its db is used for playlists/plays) ---
ND="${MD_NDDATA:-}"
if [ -z "$ND" ]; then
  for c in /volume1/docker/navidrome/data /volume1/docker/navidrome /volume1/docker/navidrome/config; do
    if [ -f "$c/navidrome.db" ]; then ND="$c"; break; fi
  done
fi
if [ -z "$ND" ]; then
  found="$(find /volume1/docker -maxdepth 5 -name navidrome.db -not -path '*/@eaDir/*' 2>/dev/null | head -n 1 || true)"
  if [ -n "$found" ]; then ND="$(dirname "$found")"; fi
fi
if [ -n "$ND" ]; then
  echo "navidrome data: $ND (mounted read-only)"
else
  echo "WARNING: navidrome.db not found - set MD_NDDATA. 'apply' will refuse to run without it, and 'reorg-apply --execute' refuses because it backs the database up first."
fi

# --- Beets library (beets-remap only): the folder that holds library.db ---
BEETS="${MD_BEETS:-}"
BMODE=ro
if [ "$cmd" = "beets-remap" ]; then
  if [ -z "$BEETS" ]; then
    for c in /volume1/docker/beets /volume1/docker/beets/config; do
      if [ -f "$c/library.db" ]; then BEETS="$c"; break; fi
    done
  fi
  if [ -z "$BEETS" ] || [ ! -f "$BEETS/library.db" ]; then
    echo "ERROR: Beets library.db not found - set MD_BEETS=/path/to/the/folder/that/holds/library.db"; exit 1
  fi
  case " $* " in *" --execute "*) BMODE=rw ;; esac
  echo "beets data: $BEETS (mounted $BMODE)"
fi

# --- library is read-only unless this run will actually change something ---
MODE=ro
case "$cmd" in
  apply|playlistfix|reorg-apply|place|undo|purge)
    case " $* " in *" --execute "*) MODE=rw ;; esac ;;
esac
if [ "$MODE" = "rw" ]; then echo ">>> library mounted READ-WRITE for this run"; fi

if [ "${MD_REBUILD:-}" = "1" ] || ! "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE (first run only)..."
  BARGS=()
  if [ -n "${MD_TZ:-}" ]; then BARGS+=(--build-arg "TZ=$MD_TZ"); fi
  "$DOCKER" build ${BARGS[@]+"${BARGS[@]}"} -t "$IMAGE" "$HERE"
fi

# --- the read-only web console: a long-running container, library mounted :ro, no way to change it from the browser ---
if [ "$cmd" = "web" ]; then
  WPORT=8099
  WBIND=""
  WARGS=()
  WNET=""
  if [ -f "$HERE/web.env" ]; then
    p="$(grep -E '^MD_WEB_PORT=' "$HERE/web.env" | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)"
    if [ -n "$p" ]; then WPORT="$p"; fi
    # MD_WEB_BIND=127.0.0.1 publishes the port on the NAS itself only (reach the console through Tailscale Serve, which adds HTTPS)
    b="$(grep -E '^MD_WEB_BIND=' "$HERE/web.env" | tail -n 1 | cut -d= -f2- | tr -d '\r "' || true)"
    if [ "$b" = "localhost" ]; then b="127.0.0.1"; fi
    if [ -n "$b" ]; then
      if printf '%s' "$b" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then WBIND="$b"; else echo "ignoring MD_WEB_BIND=$b (use an IPv4 address such as 127.0.0.1)"; fi
    fi
    # MD_WEB_NETWORK=<docker network>: also join that network (for example the one Homepage runs on) so a dashboard container can
    # reach http://dewey-web:8099/api/widget without the port being open on the LAN. Needs MD_WIDGET_TOKEN in web.env.
    n="$(grep -E '^MD_WEB_NETWORK=' "$HERE/web.env" | tail -n 1 | cut -d= -f2- | tr -d '\r "' || true)"
    if [ -n "$n" ]; then WNET="$n"; fi
    chmod 600 "$HERE/web.env" 2>/dev/null || true
    WARGS+=(--env-file "$HERE/web.env")
  else
    echo "WARNING: no web.env next to this script, so the web console has NO login. Anyone on the LAN can open it (read-only)."
    echo "         To require one, create web.env with the lines  MD_WEB_USER=...  and  MD_WEB_PASS=...  and run this again."
  fi
  BEETS="${MD_BEETS:-}"
  if [ -z "$BEETS" ]; then
    for c in /volume1/docker/beets /volume1/docker/beets/config; do
      if [ -f "$c/library.db" ]; then BEETS="$c"; break; fi
    done
  fi
  for wf in webui.py web/index.html web/app.js web/style.css; do
    if [ ! -f "$HERE/$wf" ]; then
      echo "MISSING on the NAS: $HERE/$wf"
      echo "The web console files were not copied here. On the PC, webui.py and the web\\ folder (index.html, app.js, style.css)"
      echo "must sit next to dewey.ps1; then run:  .\\dewey.ps1 deploy   and   .\\dewey.ps1 web"
      exit 1
    fi
  done
  "$DOCKER" rm -f "$WNAME" >/dev/null 2>&1 || true
  PUBLISH="$WPORT:8099"
  if [ -n "$WBIND" ]; then PUBLISH="$WBIND:$WPORT:8099"; fi
  NETARGS=()
  if [ -n "$WNET" ]; then
    if "$DOCKER" network inspect "$WNET" >/dev/null 2>&1; then
      NETARGS=(--network "$WNET")
    else
      echo "ignoring MD_WEB_NETWORK=$WNET: no docker network with that name (list them with:  sudo docker network ls )"
      WNET=""
    fi
  fi
  RUNW=("$DOCKER" run -d --name "$WNAME" --restart unless-stopped -p "$PUBLISH" --security-opt no-new-privileges:true -e PYTHONDONTWRITEBYTECODE=1
        -v "$MEDIA:/media:ro" -v "$HERE/out:/out" -v "$HERE/rules.txt:/rules.txt:ro" -v dewey-queue:/queue
        -v "$HERE/dewey.py:/app/dewey.py:ro" -v "$HERE/webui.py:/app/webui.py:ro" -v "$HERE/web:/app/web:ro")
  RUNW+=(${NETARGS[@]+"${NETARGS[@]}"})
  if [ -n "$ND" ]; then RUNW+=(-v "$ND:/navidrome-data:ro"); fi
  if [ -n "$BEETS" ] && [ -f "$BEETS/library.db" ]; then RUNW+=(-v "$BEETS:/beets:ro"); fi
  if [ -f "$HERE/navidrome.cred" ]; then chmod 600 "$HERE/navidrome.cred" 2>/dev/null || true; RUNW+=(-v "$HERE/navidrome.cred:/navidrome.cred:ro"); fi
  RUNW+=(${WARGS[@]+"${WARGS[@]}"} --entrypoint python "$IMAGE" -u /app/webui.py)
  if ! "${RUNW[@]}"; then
    "$DOCKER" rm -f "$WNAME" >/dev/null 2>&1 || true
    echo "the web console container could not be started (see the docker error above)"
    exit 1
  fi
  sleep 2
  "$DOCKER" ps --filter "name=$WNAME" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  WIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ -z "$WIP" ]; then WIP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n 1)"; fi
  if [ -z "$WIP" ]; then WIP="<the NAS address>"; fi
  if [ -n "$WBIND" ]; then
    echo "web console: listening on $WBIND:$WPORT only (not on the network). Reach it through Tailscale Serve, e.g.:"
    echo "    sudo tailscale serve --bg --https=443 http://127.0.0.1:$WPORT"
  else
    echo "web console: http://$WIP:$WPORT  (library mounted read-only; see: bash dewey.sh web logs)"
    echo "WARNING: this port is open to every device on the network over plain http. Once the https address works, add MD_WEB_BIND=127.0.0.1 to web.env and run this again."
  fi
  if [ -n "$WNET" ]; then echo "also on docker network $WNET: a container there can reach http://$WNAME:8099/api/widget (token from web.env)"; fi
  chmod -R a+rX "$HERE/out" 2>/dev/null || true
  exit 0
fi

# --- the runner: a long-running container with the Music folder WRITABLE (the _dedupe-quarantine and _incoming folders too (for apply and place), _reorg (run folders of the layout moves), the Beets folder (for beets-remap), the rest of the Media share
#     read-only; widened only when a later command needs it), no port, no Docker socket, no web server.
#     It only acts on requests the console leaves in the dewey-queue volume, and only while armed with an authenticator-app code. ---
if [ "$cmd" = "runner" ]; then
  if [ ! -f "$HERE/runner.py" ]; then
    echo "MISSING on the NAS: $HERE/runner.py  (copy runner.py next to dewey.ps1 on the PC, then  .\\dewey.ps1 deploy  and  .\\dewey.ps1 runner)"
    exit 1
  fi
  case "$RSUB" in
    secret|newsecret)
      "$DOCKER" run --rm -e PYTHONDONTWRITEBYTECODE=1 -e "MD_LABEL=${MD_LABEL:-}" -v dewey-runner-state:/state -v "$HERE/runner.py:/app/runner.py:ro" --entrypoint python "$IMAGE" /app/runner.py "$RSUB" || exit $?
      if [ "$RSUB" = "newsecret" ] && "$DOCKER" ps -q --filter "name=$RNAME" | grep -q .; then "$DOCKER" restart "$RNAME" >/dev/null && echo "runner restarted with the new secret"; fi
      exit 0 ;;
  esac
  ARMMIN=""
  if [ -f "$HERE/web.env" ]; then
    ARMMIN="$(grep -E '^MD_ARM_MINUTES=' "$HERE/web.env" | tail -n 1 | cut -d= -f2- | tr -d '\r "' || true)"
    if ! printf '%s' "$ARMMIN" | grep -Eq '^[0-9]{1,3}$'; then ARMMIN=""; fi
  fi
  "$DOCKER" rm -f "$RNAME" >/dev/null 2>&1 || true
  # the quarantine is where apply and place set duplicates aside, _incoming is where place takes new files from, _reorg is where reorg-apply keeps each run's journal, manifest and
  # backups (Navidrome's database, the .m3u files it rewrites); all must exist first, or Docker would create them owned by root
  mkdir -p "$MEDIA/_dedupe-quarantine" "$MEDIA/_incoming" "$MEDIA/_reorg" 2>/dev/null || true
  RUNR=("$DOCKER" run -d --name "$RNAME" --restart unless-stopped --security-opt no-new-privileges:true -e PYTHONDONTWRITEBYTECODE=1
        -v "$MEDIA:/media:ro" -v "$MEDIA/Music:/media/Music:rw" -v "$MEDIA/_dedupe-quarantine:/media/_dedupe-quarantine:rw" -v "$MEDIA/_incoming:/media/_incoming:rw" -v "$MEDIA/_reorg:/media/_reorg:rw" -v "$HERE/out:/out" -v "$HERE/rules.txt:/rules.txt:ro"
        -v "$HERE/dewey.py:/app/dewey.py:ro" -v "$HERE/runner.py:/app/runner.py:ro" -v dewey-queue:/queue -v dewey-runner-state:/state)
  if [ -n "$ARMMIN" ]; then RUNR+=(-e "MD_ARM_MINUTES=$ARMMIN"); fi
  if [ -n "$ND" ]; then RUNR+=(-v "$ND:/navidrome-data:ro"); fi
  # the Beets folder (library.db and its config) WRITABLE: only for "Repoint Beets after a reorg", which edits library.db after backing it up next to it
  # (SQLite needs the folder itself writable for that). Without it the runner does everything else and that one card stays dry-run only.
  RBEETS="${MD_BEETS:-}"
  if [ -z "$RBEETS" ]; then
    for c in /volume1/docker/beets /volume1/docker/beets/config; do
      if [ -f "$c/library.db" ]; then RBEETS="$c"; break; fi
    done
  fi
  if [ -n "$RBEETS" ] && [ -f "$RBEETS/library.db" ]; then
    RUNR+=(-v "$RBEETS:/beets:rw" -e MD_BEETSDB=/beets/library.db)
    echo "beets data: $RBEETS (writable in the runner, for 'Repoint Beets')"
  else
    echo "beets library.db not found (set MD_BEETS): 'Repoint Beets after a reorg' cannot run for real until it is"
  fi
  if [ -f "$HERE/navidrome.cred" ]; then chmod 600 "$HERE/navidrome.cred" 2>/dev/null || true; RUNR+=(-v "$HERE/navidrome.cred:/navidrome.cred:ro"); fi
  RUNR+=(--entrypoint python "$IMAGE" -u /app/runner.py)
  if ! "${RUNR[@]}"; then
    "$DOCKER" rm -f "$RNAME" >/dev/null 2>&1 || true
    echo "the runner could not be started (see the docker error above)"; exit 1
  fi
  sleep 2
  "$DOCKER" ps --filter "name=$RNAME" --format 'table {{.Names}}\t{{.Status}}'
  echo "runner is up. The Music folder is writable ONLY inside this container, and it does nothing until you arm it from the console's Run page."
  echo "First time: show the authenticator secret with   .\\dewey.ps1 runner secret   and add it to your authenticator app (Add account > Enter a setup key)."
  echo "Then restart the console once so it shares the request queue:   .\\dewey.ps1 web"
  exit 0
fi

RUN=("$DOCKER" run --rm)
if [ -t 1 ]; then RUN+=(-t); fi
RUN+=(-v "$MEDIA:/media:$MODE"
      -v "$HERE/out:/out"
      -v "$HERE/rules.txt:/rules.txt:ro"
      -v "$HERE/dewey.py:/app/dewey.py:ro")
if [ -n "$ND" ]; then RUN+=(-v "$ND:/navidrome-data:ro"); fi
if [ "$cmd" = "beets-remap" ]; then RUN+=(-v "$BEETS:/beets:$BMODE" -e MD_BEETSDB=/beets/library.db); fi
if [ "$cmd" = "playlistfix" ]; then
  # talks to Navidrome's API: host networking so the URL in navidrome.cred (http://<nas-address>:4533) is reachable,
  # and -i so a password prompt works when there is no navidrome.cred
  RUN+=(--network host)
  if [ -t 0 ]; then RUN+=(-i); fi
  if [ -f "$HERE/navidrome.cred" ]; then
    chmod 600 "$HERE/navidrome.cred" 2>/dev/null || true
    RUN+=(-v "$HERE/navidrome.cred:/navidrome.cred:ro")
  fi
fi
RUN+=("$IMAGE" "$cmd")
for a in "$@"; do
  if [ -n "$a" ]; then RUN+=("$a"); fi   # ignore empty arguments
done

rc=0
"${RUN[@]}" || rc=$?
chmod -R a+rX "$HERE/out" 2>/dev/null || true
exit $rc
