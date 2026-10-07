# ADR-0001: Release the tool as open-source InboxInk

- **Status:** accepted
- **Date:** 2026-10-06
- **Deciders:** maintainer
- **Decided in:** the plan for the first public release

## Context and problem

The tool began as one person's script. It read a single Gmail account through a command line tool, and its Cloudflare address, storage id, labels, paths, time zone and scheduler name were all written into the code. It was deployed with Node and wrangler. That works for its author and for nobody else.

The aim of the public release is that someone who can paste a command into Terminal, and isn't a software developer, can set it up in about fifteen minutes and keep it running without maintenance. That needs three things the script didn't have: no personal values in the code, a way in that needs no developer tools, and updates that don't depend on the user.

## Decision

We will publish the tool as InboxInk under the MIT license, with these choices:

- **Rename and configure.** The package is `inboxink`. Every personal value moves into a settings file at `~/.config/inboxink/config.toml` (or `$INBOXINK_CONFIG`), read with the standard library. Any single-value setting can be overridden by an `INBOXINK_<SECTION>_<KEY>` environment variable. Passwords and tokens are not in that file.
- **Gmail over IMAP is the default mail source.** A Gmail app password signs in to `imap.gmail.com`, and Gmail labels are read and written through the `X-GM-LABELS` extension. The earlier `gog` source stays as `source.kind = "gog"`, documented as an advanced route for Google Workspace accounts, which usually can't make app passwords.
- **Each user hosts on their own Cloudflare account.** `inboxink setup` takes a Cloudflare API token that the user creates from a pre-filled link, then creates the KV namespace, uploads the Worker and finds the free workers.dev address through Cloudflare's REST API. No Node or wrangler is needed, and the project runs no server.
- **Distribution is PyPI plus a setup wizard.** `install.sh` installs `uv` if needed and then the `inboxink` package. `inboxink setup` walks through Gmail, Cloudflare and Instapaper, stores secrets in the system keychain (with a private-file fallback), installs a launchd job on macOS or a systemd user timer on Linux, and ends with a dry run. `inboxink doctor` and `inboxink uninstall` cover checking and removal.
- **Updates are on by default.** Once a day a run installs the newest stable release from PyPI and redeploys the Worker if its source changed. Pre-releases are ignored. Setup asks, and the user can turn it off, in which case a run only logs that an update exists. Releases are published from a version tag through PyPI trusted publishing.
- **Fresh history.** The public repository starts from a single commit rather than carrying over the private one, so nothing from the personal phase comes with it.

## Options considered

- **The chosen set** wins because each piece removes a step that needs a developer: a settings file instead of edited code, IMAP instead of a separate tool, the REST API instead of wrangler, one command instead of a README of steps.
- **Keep `gog` as the only source.** Rejected: it requires every user to create their own Google Cloud OAuth client, which is the hardest part of the setup for a non-developer.
- **A shared hosted service run by the project.** Rejected: it would hold every user's newsletters and make the project responsible for them, and it costs money to run. Each user's own free Cloudflare account keeps the data theirs.
- **Wrangler for deployment.** Rejected: it needs Node and an interactive login.
- **Manual updates only.** Rejected as the default. Users who never think about the tool would run old cleaning rules indefinitely, and the rules are the security-relevant part. It stays available as an opt-out.
- **Support more mail providers at launch.** Rejected for now: Gmail's label model is what the delivery flow relies on, and each other provider would need its own testing.

## Consequences

- A user needs three outside accounts: Gmail, Cloudflare and Instapaper. The wizard reduces the effort but can't remove them.
- Personal Gmail with 2-Step Verification is the supported path. Workspace users need the advanced route.
- The Worker is deployed to the user's account by the tool, so changing it means shipping a new release and having `update` redeploy it. The Worker's source hash is how the tool knows.
- Auto-update means a bad release reaches users quickly. Releases need to be careful, and yanked PyPI versions are skipped.
- A fresh history means the repository doesn't show how the code got here.
- Linux support is best-effort. The scheduler works with systemd user timers, and prints a cron line when those aren't available.

## Revisit when

A second mail provider is wanted, the Cloudflare free plan's 1,000 daily KV writes stops being enough for typical users, or a Homebrew formula would serve users better than the install script.
