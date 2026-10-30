# Dewey

Dewey tidies a self-hosted music library. It finds songs that are stored more than once, plans one folder layout for the whole collection, files new music into place, and keeps Navidrome's playlists and Beets' database pointing at the right files while it does.

I wrote it for my own library, which lives on a Synology NAS with Navidrome, and I've used it there every day since. Because it moves files around, it's careful: nothing changes until you've read a dry run, duplicates go to a quarantine folder instead of being deleted, and every run can be undone.

*Named after the Dewey Decimal system. Every item gets one right place on the shelf.*

Version: tool 1.13.0, web console 1.11.0.

![The overview page of the web console](docs/screenshots/overview.png)

## What it does

- **Finds duplicates.** It groups copies of the same song, then decodes both files to check they really are the same audio before it touches either one. Live, remix and clean/explicit versions are left alone.
- **Plans one folder layout.** Everything ends up at `Artist/Album (YYYY)/NN - Title.ext`.
- **Files new music.** Drop files into a landing folder and Dewey puts them where they belong, builds playlists from folder names, and sets aside anything you already own.
- **Keeps Navidrome and Beets in step.** Playlists follow the copy that stays, and Beets' database is repointed at moved files without a re-import.
- **Has a web console.** Look at the plan, listen to both copies of a duplicate, and run jobs from your phone if you want to.

## How it stays safe

- Every job is a dry run first. A real run only happens against a dry run you've read, and you confirm it by typing the count.
- Duplicates are moved to a quarantine folder with a record of where they came from. `undo` puts them back.
- The only thing that deletes anything is `purge`, which clears out old quarantine runs and asks the most of you.
- The web console can't change a file. Real runs go through a separate runner that needs a code from your authenticator app, and the console is meant to stay on a tailnet or VPN, never the open internet.

The full picture, including what it doesn't protect against, is in [`docs/SECURITY.md`](docs/SECURITY.md).

## Try it on a demo library first

```sh
python3 demo/make_demo.py
```

That builds a small invented library (generated tones, made-up artists) with duplicates, a playlist that blocks one of them, badly named files and a folder of new music. You can run every dry run on it and click around the console before you point Dewey at anything real. The steps are in [`demo/README.md`](demo/README.md).

| | |
|---|---|
| ![Duplicate groups](docs/screenshots/duplicates.png) | ![The folder layout plan](docs/screenshots/layout.png) |
| ![New music waiting to be filed](docs/screenshots/new-music.png) | ![The Run page](docs/screenshots/run.png) |

## Install

You need Docker, Navidrome (Dewey reads its database so it never removes something you play, star or have in a playlist), and an authenticator app if you want real runs from the browser. Beets is optional. Back up your library first.

```sh
git clone https://github.com/anightcap/dewey-music.git dewey && cd dewey
cp .env.example .env            # edit it: your media folder, Navidrome data folder, a console password
mkdir -p data/out data/config   # Synology's Docker won't create these for you
docker compose up -d            # starts the read-only console on http://127.0.0.1:8099
docker compose --profile tools run --rm dewey scan    # a read-only scan from the command line
```

There's also a Windows launcher that drives a NAS over ssh, which is how I run it. Both routes, the optional runner, and fixes for the usual snags are in [`docs/INSTALL.md`](docs/INSTALL.md). Compose files pass `docker compose config` and the test suite, but I haven't run them on many setups, so tell me what you find.

## Using it

[`docs/USAGE.md`](docs/USAGE.md) covers what each job does, the usual sequences (clean up duplicates, add new music, change the layout, undo), how Dewey decides which copy to keep, and the `rules.txt` options.

A few things worth knowing before you rely on it:

- **Tags are the source of truth.** Files are placed from their tags, never their names, so a file with no artist or title is left where it is.
- **Navidrome identifies a track partly by its tags.** Changing a title or album tag gives the track a new id and loses its plays, stars and playlist spots. Dewey never writes tags.
- **Beets stores absolute paths,** so after a layout change run `beets-remap` before its import task runs again.
- **Back up first.** Dewey is careful, but it does move a lot of files.

## Tests

`python3 tests/run_tests.py` runs everything against throw-away libraries, with no real music, Navidrome or Docker needed. GitHub runs it on every push. More in [`tests/README.md`](tests/README.md).

## About

**Built with AI.** I built Dewey with a lot of help from Claude. I run, test and review all of it.

**Support.** This is a hobby project. There's at most one release every two weeks, and fixes arrive in releases rather than one-off patches. Issues are welcome, but I may be slow to answer.

**Contributing and security.** A bug report with the dry-run log is the most useful thing you can send. See [`CONTRIBUTING.md`](CONTRIBUTING.md). Please report security problems privately, as described in [`SECURITY.md`](SECURITY.md). Changes are listed in [`CHANGELOG.md`](CHANGELOG.md).

**No warranty.** Dewey moves your files. It's built so nothing happens without a dry run you've read, but that doesn't replace a backup.

**License.** [MIT](LICENSE).
