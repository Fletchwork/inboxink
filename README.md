# InboxInk

Read your newsletters and Substacks on your e-reader, cleaned of ads and trackers and away from your inbox.

[![CI](https://github.com/Fletchwork/inboxink/actions/workflows/ci.yml/badge.svg)](https://github.com/Fletchwork/inboxink/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

<!-- photo: docs/images/kobo.jpg — a Kobo showing a cleaned newsletter (add before launch) -->

## Why

Newsletters pile up in email, and an e-reader is a much better place to read them. A Kobo doesn't do email, but it does sync with Instapaper, though only for articles saved by web address. Emailing a newsletter to Instapaper doesn't work. So InboxInk puts each issue on a web page of its own and saves that address.

## How it works

1. You label a newsletter in Gmail, or point InboxInk at a Gmail account that only receives newsletters.
2. InboxInk cleans each new issue: ads, tracking pixels, unsubscribe and account links, oversized images and layout tables are gone or flattened. Substack issues get a comment link and a QR code at the bottom.
3. It puts the cleaned page on your own free Cloudflare Worker, at a link nobody can guess.
4. It saves that link to Instapaper.
5. Instapaper syncs it to your Kobo or other e-reader, or to any Instapaper app.

Then it labels the email as delivered and archives it. This runs every 30 minutes.

## What you need

- A personal Gmail account with 2-Step Verification turned on. Work and school Google accounts usually can't make app passwords; see [Google Workspace accounts](CONTRIBUTING.md#advanced-google-workspace-accounts) for the harder route.
- A free [Cloudflare](https://dash.cloudflare.com/sign-up) account.
- A free [Instapaper](https://www.instapaper.com) account, connected to your e-reader.
- A Mac or Linux computer that is on and awake during the day. macOS is what it's tested on; Linux should work but gets less testing.
- Optional: the [Claude Code](https://claude.com/claude-code) command line tool, for ad removal.

## Install

Paste this into Terminal:

```
curl -fsSL https://raw.githubusercontent.com/Fletchwork/inboxink/main/install.sh | sh
```

It installs [uv](https://docs.astral.sh/uv/) if you don't have it, then InboxInk. Then run:

```
inboxink setup
```

Setup takes about 15 minutes and asks for three things, each with a link and a "paste it here":

- A Gmail app password, a 16-letter password just for InboxInk that you make at Google's app passwords page.
- A Cloudflare token. The link opens Cloudflare's token page with the permissions already filled in. Setup then creates the storage and the Worker for you, so you don't need Node or any other tools.
- Your Instapaper email and password, which setup checks before saving.

Setup finishes by installing the schedule and doing a dry run. You can run it again whenever you like; it offers what you entered last time as the default.

Passwords and tokens go in your system keychain (macOS Keychain, or the keyring on Linux). If there's no keychain, they go in a file only your user can read. They are never put in the settings file and are sent only to the service they belong to.

> If you'd rather not connect your main inbox, make a second Gmail just for newsletters and subscribe it to them. An app password can read the whole mailbox it belongs to, and a mailbox that only holds newsletters gives it nothing else to read. Tell setup "yes" when it asks whether the Gmail is only for newsletters.

## Everyday use

There's nothing to do. New newsletters arrive on your reader after the next sync.

- `inboxink doctor` checks the login, the Cloudflare token, the Worker and the schedule, and says how to fix what's wrong.
- `inboxink run --dry-run` lists what's waiting without sending anything.
- If an issue fails three times, InboxInk gives up and labels it `Newsletter/Failed`. Remove that label to try again.
- The log is at `~/.local/state/inboxink/inboxink.log` (it is trimmed to its last 200 KB whenever it passes 1 MB), and your settings at `~/.config/inboxink/config.toml`. [config.example.toml](config.example.toml) lists every setting.

## Updates

InboxInk checks once a day and installs new stable releases by itself, then updates the Worker on Cloudflare if it changed. Setup asks about this. To turn it off, answer no in `inboxink setup` or set `auto = false` under `[updates]` in the settings file; it then only logs that an update exists. To update by hand, run `inboxink update`. Dependencies are pinned to exact versions in each release, so an update only changes them when a new InboxInk release does.

## Privacy and safety

InboxInk runs on your computer. There's no InboxInk server and no account with us.

- Hosted pages are public to anyone who has the link. Each link carries a random 128-bit id, search engines are told to skip it, and the page deletes itself after 30 days.
- Links in a newsletter are rebuilt before publishing. Tracking and personal parameters are stripped, unsubscribe and account links are removed, and your own email addresses are scrubbed from the text.
- It decodes tracking links without visiting them where it can. For the rest it asks the tracker where it points, one step at a time and never loading the destination, and drops the link if that fails. A tracker may count that request as a click. Unsubscribe links are never touched.
- Ad removal is off unless you turn it on. It needs the Claude Code tool, and setup asks before enabling it. When it's on, the newsletter's text goes to Claude through your own Claude Code login. If that fails, nothing is removed and the issue is still delivered. If you install Claude Code after setup, run `inboxink setup` again to turn it on.
- Each cover shows the publication's icon, fetched from its website. If the site blocks automated requests, InboxInk asks Google's favicon service instead, which tells Google the publication's domain and nothing else. With no icon at all, the cover shows the publication's initials.

Details and how to report a problem are in [SECURITY.md](SECURITY.md).

## Limits

- Gmail only. Other mail providers aren't supported.
- Cloudflare's free plan allows 1,000 storage writes a day. An issue uses one for the page, one for each image, one for the cover and one for a Substack QR code, so someone who gets a lot of image-heavy newsletters can hit the cap. Issues that fail three times get the `Newsletter/Failed` label; remove it the next day to try again.
- Instapaper shows every image at the full width of the page, so small images, icons and wide banners are dropped.
- Tables don't survive Instapaper either, so each table row becomes one line.

## Uninstall

```
inboxink uninstall
```

It stops the schedule, then asks separately about deleting the Worker and storage on Cloudflare and about deleting your saved settings and passwords. Each question defaults to no. To remove the program, run `uv tool uninstall inboxink`.

What it leaves for you to remove by hand: the Gmail labels and any filter you made, the app password (revoke it at Google's app passwords page), and the Cloudflare token (delete it on Cloudflare's API tokens page). Articles already in Instapaper stay there.

It also leaves two folders on your computer. `~/.local/state/inboxink` holds the log (which lists newsletter titles) and `state.json` (which holds the links to the pages InboxInk published). `~/.cache/inboxink` holds small site icons. To delete them, run `rm -r ~/.local/state/inboxink ~/.cache/inboxink`.

## Contributing

Issues are welcome. For code changes, read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## License

[MIT](LICENSE).

Built with Claude Code as a pair programmer.

Not affiliated with Rakuten Kobo, Instapaper, Substack or Google.
