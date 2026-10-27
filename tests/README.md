# Tests

`python3 tests/run_tests.py` builds a throw-away rig for each suite (a few generated audio files, a stand-in Navidrome
database, a runner and a console on `127.0.0.1:18099`), runs the suite against it, and shuts everything down.
Nothing outside a temporary folder is touched; no real library, Navidrome or Docker is needed.

```sh
python3 tests/run_tests.py             # everything (about 5 minutes)
python3 tests/run_tests.py --no-ui     # API and engine suites only (about 3 minutes)
python3 tests/run_tests.py undo        # only groups whose name contains "undo"
DEWEY_TEST_KEEP=1 python3 tests/run_tests.py   # keep the temporary rigs (path printed per group) to look around
```

**Needs:** Linux, macOS or WSL; `python3`, `ffmpeg`, `bash`, `mutagen`, `numpy` (`pip install mutagen numpy`). The Beets groups also need the real `beet` tool (`pip install beets`) and permission to create `/music` (root or passwordless `sudo`); without them those groups are skipped, not failed. The browser groups also need
`node`, `openssl` and Playwright with Chromium (`npm install -g playwright && playwright install chromium`, then `export NODE_PATH=$(npm root -g)`).

| Group | What it proves |
|---|---|
| console basics, arming lockout, arming expiry | The login, the six-digit code and its lockout, arming that ends by itself, and every refusal while not armed |
| remove duplicates, file new music, folder layout, layout batches, Beets repoint, undo and purge | Each real run through the runner: it only starts from a finished dry run of the same command that has not been used, with the exact typed count; forged options, forged fingerprints and stale plans are refused; the files end up where the dry run said |
| no false "interrupted" | Reading a job's log while it finishes never reports it as interrupted |
| demo library | `demo/make_demo.py` still builds the library the demo README describes, and every dry run on it gives the documented result |
| browser groups | The same flows driven through the real page in Chromium, including that the "Run for real" box appears by itself when a dry run finishes |

The rigs (`tests/rigs/`) are plain shell scripts, one per kind of starting library; `tests/run_tests.py` lists which suite uses which.
The browser tests reach the console through a tiny HTTPS proxy with a throw-away certificate, because arming only works over https.
