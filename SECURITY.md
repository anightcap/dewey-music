# Security policy

## Reporting a vulnerability

Please report a vulnerability **privately**: on GitHub, open the repository's **Security** tab and choose **Report a vulnerability**
(a private security advisory). Do not open a public issue for it.

Include what you found, how to reproduce it, and the version (footer of the console). This is a hobby project: expect a reply when the maintainer
can get to it, not within a fixed time, and fixes arrive in the next release.

## Supported versions

Only the latest release gets fixes.

## What is in scope

Anything that lets someone change, delete or read files or secrets they should not be able to, in particular:

- getting the web console (or someone with only the console login) to change a file without the authenticator code, or to run a command or option
  that is not on the runner's fixed list;
- bypassing the dry-run, fingerprint or typed-count checks of a real run;
- leaking `navidrome.cred`, the console password or the authenticator secret.

How the safety design works, what it does not protect against, and how to expose the console safely are in [`docs/SECURITY.md`](docs/SECURITY.md).
