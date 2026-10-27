# Security notes

Dewey is designed so that **a stolen console login cannot change a file**, and so that every change is reviewed first and undoable.

## What protects the library

- **The console is read-only.** Its container mounts the library read-only and the tool it runs refuses any command line containing `--execute`.
- **Writing happens in one place: the runner.** It has no port, no web server and no Docker socket. It is a separate, opt-in service (Compose profile `runner`).
- **Two factors for every real run:** the console login, plus a 6-digit code from an authenticator app that only the runner can check. Arming lasts 15 minutes (`MD_ARM_MINUTES`, 1 to 120), is one-use per code, and wrong codes lock out.
- **The runner trusts only its own list.** Each command and every option must be on the runner's fixed list; the request must name a finished dry run of the same command (under 6 hours old, unused, the newest of its kind); the plan file hash or fingerprint must be unchanged; the typed phrase with the count must match; no other job may be running. The real run is that dry run's own recorded options plus `--execute`.
- **Nothing is deleted** except by `purge`, which removes quarantine runs older than 7 days (never fewer) after the same checks.
- Under Compose, every service runs with `no-new-privileges` and **all Linux capabilities dropped**, as a non-root user you choose (`DEWEY_UID`/`DEWEY_GID`). Only the two short-lived helper containers that create folders run as root, with just the capabilities to do that.

## Exposing the console

- Bind it to `127.0.0.1` (`DEWEY_WEB_BIND`) and reach it through an HTTPS front end on a private network (Tailscale Serve, a VPN, or a reverse proxy with its own login). **Do not publish it on the public internet**, and do not use a public tunnel.
- Set `MD_WEB_HOSTS` so the console answers only to your host name (this blocks DNS-rebinding tricks).
- The login is HTTP Basic with a lockout after repeated failures; every POST needs a custom header and a same-origin check. A Navidrome password typed into the console is accepted only over HTTPS and is never stored.
- The Security page of the console shows how exposed it is right now, and the exact setting that fixes each weak spot.

## Honest limits

- The runner keeps the library writable for as long as it runs, so its safety is the arming and the checks, not the mount. A taken-over console could ask the runner for any allowed command with allowed options inside an armed window; that is why each run is tied to a fingerprinted dry run and a typed count, and why quarantined files and backups exist.
- Someone with root on the host, or who can read the runner's state volume (it holds the authenticator secret), can do anything.
- `navidrome.cred` may hold a plaintext password if you choose to store one. Prefer typing it per run.

## Reporting a problem

Please report a vulnerability privately (use the repository's security advisory feature) rather than in a public issue.
