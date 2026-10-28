# Changelog

Versions are the tool's (`dewey.py`) and the web console's (`webui.py`).

## 1.12.8 (tool) / 1.10.4 (console), first public release

Dewey had been in daily use on the author's own NAS before this release; this is the first version published as a repository.

**Added since the private version**
- Docker Compose install (`docker-compose.yml`, `.env.example`, optional `compose.beets.yml`) with the same separation as the PC launcher: a read-only console, an
  opt-in runner as the only writer, non-root, all capabilities dropped, no Docker socket. Tested on a real Docker host.
- `dewey.config.psd1` for the PC launcher (NAS address, user, folders, time zone, authenticator label), so no personal value is in the scripts.
- `tools/check_public.py`, a leak check for the repository (private addresses, secrets, personal paths, your own denylist), usable as a pre-commit hook and run in CI.
- `GET /api/widget` also returns `artists`, `albums` (from the library scan), `flac_files`, `mp3_files` and `last_scan` (ISO time with offset), so a dashboard such as Home Assistant can show the library at a glance.
- The test suite (`tests/`, one command, no real library needed) and a generated demo library with screenshots (`demo/`).

**Fixed**
- Console 1.10.2: the "Run for real" box now appears by itself when a dry run finishes, instead of after a page reload.
- Console 1.10.3: a wide-gap duplicate group (held until you approve it by name) shows as "needs review", not "removable", and no longer counts toward the space to free.
- Wording that assumed one particular install (NAS, `web.env`) now follows the install type.

**What the tool does** (see the README): finds the same song stored more than once and checks the audio before removing a copy; plans one folder layout
(`Artist/Album (YYYY)/NN - Title.ext`); files new music from a landing folder; keeps Navidrome playlists and Beets' database pointing at the right files;
quarantines instead of deleting; records every move so any run can be undone; and does real runs from a read-only web console only through an armed runner
(authenticator code, a finished dry run of the same command, a typed count).
