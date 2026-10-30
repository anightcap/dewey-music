# Installing Dewey

The quick version is in the [README](../README.md). This is the full setup, plus the usual problems.

Requirements: a machine running Docker with the library on one volume (a NAS, a home server); Navidrome (recommended: Dewey reads its database to protect what you play, star and put in playlists); Beets (optional); an authenticator app for real runs from the console. The image installs `ffmpeg`, `mutagen` and `numpy` itself. Back up the library first.

### Option A: Docker Compose (any Docker host)

```sh
git clone <this repository> dewey && cd dewey
cp .env.example .env            # then edit it: DEWEY_MEDIA, DEWEY_NAVIDROME_DATA, DEWEY_UID/GID, a console password
mkdir -p data/out data/config   # required on Synology (its Docker will not create missing folders); harmless elsewhere
docker compose up -d            # builds the image, starts the READ-ONLY console on http://127.0.0.1:8099
docker compose --profile tools run --rm dewey scan      # a command-line scan (read-only)
```

- **Folders:** Dewey keeps its reports and settings in `./data` (`data/out`, `data/config/rules.txt`, `data/config/navidrome.cred`). Edit `rules.txt` from `rules.example.txt`; for playlist jobs create `data/config/navidrome.cred` with `url=` and `user=`.
- **Who can write:** only the **runner**, and it is opt-in: `docker compose --profile runner up -d`. First time, show its authenticator secret with `docker compose --profile runner run --rm runner secret` and add it to your authenticator app. The console and the `dewey` service mount the library read-only. For command-line real runs use `docker compose --profile tools-write run --rm dewey-write apply --execute`.
- **Beets:** set `DEWEY_BEETS` and `COMPOSE_FILE=docker-compose.yml:compose.beets.yml` in `.env` (see `compose.beets.yml`).
- **Reaching the console from other devices:** keep `DEWEY_WEB_BIND=127.0.0.1` and put HTTPS in front of it (a reverse proxy, Tailscale Serve). Never publish it on the internet.
- **Updating:** `git pull`, then `docker compose build` and `docker compose up -d` (add `--profile runner` if you use the runner).
- The Compose files pass `docker compose config` for every profile, and I've run the setup on my own Synology NAS (as a separate project with a scratch library: read-only library, https through Tailscale Serve, arming, folder layout, undo and file new music, real and dry). The Beets overlay and a wrong user id haven't been exercised, and I haven't tried other hosts. Please report what you find.

### Option B: from a Windows PC with ssh (what the author runs on a Synology NAS)

```powershell
copy dewey.config.example.psd1 dewey.config.psd1     # then edit it: NAS address, user, folders
.\dewey.ps1 deploy            # copy the files to the NAS
.\dewey.ps1 scan              # first run builds the image; read-only
.\dewey.ps1 web               # start the console (read-only); optional web.env on the NAS (see web.env.example)
.\dewey.ps1 runner            # start the runner (only if you want real runs from the console)
.\dewey.ps1 runner secret     # the authenticator-app secret: add it in your phone's authenticator
```

Updating: `.\dewey.ps1 deploy`, then `.\dewey.ps1 runner` if `runner.py` or the mounts changed, then `.\dewey.ps1 web`, then a hard refresh in the browser. `web [up|stop|logs|status]` and `runner [up|stop|logs|status|secret|newsecret]` manage the containers. Everything also runs with `bash dewey.sh <command>` on the NAS.

Keep these together in one folder on the PC: `dewey.ps1`, `dewey.py`, `dewey.sh`, `Dockerfile`, `rules.txt`, `webui.py`, `web/`, `runner.py`, `sync-music.ps1` (optional, copies new music from a PC) and your `dewey.config.psd1`.

## Troubleshooting

- `navidrome.db not found`: pass `-NdData <folder holding navidrome.db>` to `dewey.ps1`, or set `MD_NDDATA` when running `dewey.sh` directly.
- Changed the `Dockerfile` (PC launcher): `MD_REBUILD=1`. With Compose: `docker compose build`. `dewey.sh` must have LF line endings (`dewey.ps1` writes it that way).
- The console says write access is off, or a card's "Run for real" is greyed out: the runner is not running, it is not armed, or there is no usable dry run of that card (finished OK, under 6 hours old, not already used, and it must be the newest one).
- A real run refused with "REFUSED": the library, a plan or a database changed after the dry run. Nothing was changed; do a fresh dry run.
- The console looks out of date after an update: it needs `.\dewey.ps1 web` and a hard refresh.
- Run logs: the History on the Run page, the Logs page, or `out/logs/` on the NAS. `runner logs` lists every arming, wrong code and real run.
