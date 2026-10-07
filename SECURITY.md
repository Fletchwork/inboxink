# Security policy

## Reporting a problem

Please report security problems privately through GitHub: [report a vulnerability](https://github.com/Fletchwork/inboxink/security/advisories/new). Don't open a public issue for them. Include what you saw, how to reproduce it, and which version (`inboxink --version`). Use invented addresses and tokens in anything you send.

You'll get an answer as soon as the maintainer can give one; this is a one-person project, so there's no response-time promise.

## Supported versions

The latest release. Fixes land there and in the next release; older versions aren't patched. Updates are on by default, so most installs follow along.

## What counts

- A way to make the cleaner publish something it should have removed: a personal token, the reader's email address, a subscriber id, or active content such as a script.
- A way to make InboxInk fetch an internal address, either through content in a newsletter or through DNS tricks (SSRF, DNS rebinding).
- Any path that puts a password or token in a log, an error message, a command line or the settings file.
- A flaw in the Cloudflare Worker, such as serving anything but pages and images, or listing what's stored.
- A way for a newsletter to change what the ad-removal step does beyond naming blocks to remove.

## What doesn't

- Anyone who has a hosted page's link can read that page. That's the design: the links are random 128-bit ids, the pages ask search engines to skip them and they expire after 30 days.
- Problems in Gmail, Cloudflare, Instapaper or Claude themselves. Report those to the vendors.
- A computer that's already compromised.

## Known residual risks

- The cleaner strips tracking and personal parameters it recognizes. A tracker host or a parameter name it has never seen can end up in a published link. Pattern additions are welcome.
- To clean opaque click-tracking links, InboxInk asks the tracker where it points (one redirect, never loading the destination). The tracker may count that as a click.
- A Gmail app password can read the whole mailbox it belongs to. Using a separate Gmail for newsletters limits that.
- If no system keychain is available, secrets are kept in a file in `~/.config/inboxink/` that only your user can read. Anyone with access to your account can read it.
- Ad removal is off by default. If you turn it on, it sends the newsletter's text to Claude through your Claude Code login.
