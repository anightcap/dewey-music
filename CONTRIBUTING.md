# Contributing

Thanks for looking. Dewey is a hobby project with one maintainer, so please keep expectations modest: replies and fixes may be slow, and
releases come at most once every two weeks (fixes arrive in releases, not as individual patches).

## Reporting a problem

The most useful report contains:

1. What you ran (the command, or the card on the Run page) and what you expected.
2. The **dry-run output** or the log from `out/logs/`. Remove anything private first (host names, your music folder names if you mind).
3. Your versions: they are in the footer of the console, or `python3 dewey.py --version`, and how you installed it (Docker Compose or the PC launcher).
4. Whether the library and Navidrome were backed up. If something was moved wrongly, **do not run anything else on it**: `undo` is built for this, and the manifest in the quarantine or `_reorg` folder is the evidence.

Please do not paste authenticator secrets, `navidrome.cred`, `.env` or `web.env`. Security problems go through [`SECURITY.md`](SECURITY.md), not a public issue.

## Changing the code

- Open an issue first for anything bigger than a typo, so we can agree it fits. The safety rules are deliberately strict (dry run first, typed counts,
  nothing deleted except `purge`); changes that loosen them are unlikely to be accepted.
- Run the tests before you send a change: `python3 tests/run_tests.py` (see [`tests/README.md`](tests/README.md)). A change to a real run needs a test that
  shows it refuses what it should refuse, not only that it works.
- Run `python3 tools/check_public.py` too (install it as a pre-commit hook with `--install-hook`). It refuses a commit that contains private addresses,
  secrets or personal paths, and the project's CI runs it on every push.
- Keep the web console free of build steps: it is plain HTML, CSS and JavaScript in `web/`, and the tool is one Python file with no third-party
  dependencies beyond `mutagen` and `numpy`.
- Do not commit your own `rules.txt`, `dewey.config.psd1`, `.env`, `web.env` or `navidrome.cred`; they are git-ignored for a reason.
- New demo or test music must be generated (see `demo/make_demo.py`): never add real recordings or anything copyrighted.

## Releases

Version numbers are the tool's (`VERSION` in `dewey.py`) and the console's (`WEB_VERSION` in `webui.py`). Each release adds an entry to
[`CHANGELOG.md`](CHANGELOG.md) and a matching Git tag.
