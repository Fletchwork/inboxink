# CLAUDE.md

Guide for contributors and coding agents working on InboxInk. User-facing docs are in
[README.md](README.md); how to contribute is in [CONTRIBUTING.md](CONTRIBUTING.md); the reasons
behind big decisions are in [docs/adr/](docs/adr/README.md). Read the ADR index before planning a
change, and add a record for any significant decision in the same PR.

## What this is

`inboxink run` is what the schedule calls every 30 minutes. For each newsletter in a Gmail mailbox it:

1. cleans the email into a small e-reader-friendly HTML page (`clean.py`);
2. uploads the page and its images to the user's own Cloudflare Worker KV (`publish.py`);
3. checks that every URL answers 200, then saves the page URL to Instapaper (`instapaper.py`);
4. labels the email as delivered and archives it.

Instapaper only syncs URL saves to Kobo e-readers, which is why the page is hosted rather than emailed.
Users install with `install.sh` (which installs `uv`, then the PyPI package) and configure with
`inboxink setup`. There is no server run by the project.

## Module map

- `cli.py`: the `inboxink` command and its subcommands (`setup`, `run`, `update`, `doctor`, `uninstall`).
- `config.py`: loads and validates `~/.config/inboxink/config.toml` (or `$INBOXINK_CONFIG`); `INBOXINK_<SECTION>_<KEY>` env vars override single keys.
- `config.example.toml`: every setting, commented, with its default.
- `run.py`: one delivery pass, with state file, per-issue retry count and a lock against overlapping runs.
- `sources/`: where mail comes from. `__init__.py` defines the `Source` interface; `imap.py` is Gmail over IMAP with an app password (default); `gog.py` uses the `gog` tool for Workspace accounts.
- `clean.py`: email to cleaned HTML, image re-encoding, link scrubbing, table flattening.
- `safety.py`: `safe_fetch` (the only outbound fetch), query-parameter and tracker rules.
- `adstrip.py`: optional ad removal (off unless `adstrip.enabled = true`) with a no-tools Claude call through the `claude` CLI.
- `cover.py`: square cover thumbnail with publication badge.
- `publish.py`: writes to KV through the Cloudflare API and waits until each URL is live.
- `cloudflare.py`: small standard-library Cloudflare REST client (token check, KV namespace, Worker upload).
- `worker/index.js`: the read-only Worker that serves pages at `/a/<id>` and images at `/i/<id>-<n>.jpg`, `/i/<id>-qr.jpg` (Substack QR code) and `/i/<id>-cover.jpg` (list thumbnail). Its source is hashed into a version so `inboxink update` knows when to redeploy it.
- `credentials.py`: secrets in the system keyring, with a 0600 file fallback.
- `setup_wizard.py`: `setup` and `uninstall` flows. `schedule.py`: launchd / systemd timer / cron line. `updater.py`: daily update check and `inboxink update`. `doctor.py`: health checks.

## Tests

```
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Tests are offline. `tests/conftest.py` pins an invented config for every test, so nothing reads
a real settings file or touches the network; keep it that way. New behavior needs a test, and
every attack case against the cleaner stays pinned in `tests/test_clean.py` and `tests/test_safety.py`.

## Hard rules

These exist because hosted pages are public and the job runs inside a home network. Don't loosen them
without a discussion and an ADR.

- **Cleaning is allow-list based.** Only `a[href]` and `img[src,alt]` keep attributes. Scripts, styles, forms, embeds, comments and every other tag or attribute go. The Worker adds a Content-Security-Policy as a backstop; it is not a reason to relax the cleaner.
- **Links survive only as public http(s) URLs.** Tracking and token-shaped query parameters, account, login, preference and unsubscribe links, and the reader's own addresses are removed. When in doubt, drop the link and keep its text.
- **Never follow click trackers.** Decode wrapped targets offline where the format allows. For opaque trackers `resolve()` reads one redirect hop and never fetches the destination (a tracker may still count that request), is capped per issue, and drops the link if it fails. Unsubscribe-style links are never resolved.
- **All outbound fetches go through `safety.safe_fetch`.** Public addresses only, with DNS pinning: resolve the host once per hop, check every address, connect to the checked address, and keep the original hostname for the Host header and TLS. Byte cap, deadline, manual redirect handling. Do not add a second fetch path.
- **Ad stripping fails open and has no tools.** Newsletter text is untrusted input to the model. The model can only name block numbers, which are validated; any error or implausible answer removes nothing.
- **Every hosted URL must return 200 before the Instapaper save.** KV caches a just-missed key for a short time and Instapaper fetches a URL once. The check sends a browser-like User-Agent because Cloudflare answers Python's default one with 403.
- **Secrets never appear in argv, logs, exceptions or the config file.** They go through `credentials.py`. Error messages must be safe to paste into a public issue.
- **Images can't be sized and tables don't survive Instapaper.** Thumbnails, icons and wide banners are dropped and table rows become lines. Loosening those thresholds brings back full-screen icons on the reader.
- **New config keys** go in `config.py` (with a default and validation), `config.example.toml` and a test, in the same change.
- **The Worker stays read-only** and serves only the id patterns it matches today.

## Residual risk

The cleaner can only strip what it recognizes. An unknown tracker host or an unknown personal token
parameter name that isn't in `safety.py` can still end up in a published link. New patterns are
welcome as PRs with a test.
