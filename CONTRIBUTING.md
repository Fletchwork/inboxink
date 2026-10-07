# Contributing

Thanks for looking. InboxInk is a small tool maintained by one person, so the process is light.

## Issues

Bug reports and ideas are welcome as issues. For a bug, include the output of `inboxink doctor` and the relevant lines of the log (`~/.local/state/inboxink/inboxink.log`). The log lists the titles of the newsletters you receive, so remove or trim those lines too, along with any email addresses, Worker links and tokens, before you paste it.

Security problems go through private reporting instead; see [SECURITY.md](SECURITY.md).

## Code changes

Open an issue first and say what you want to change. That saves you from writing something that doesn't fit. The maintainer may decline a pull request that widens the scope, for example support for another mail provider or a new hosting service, even if it's well made.

Set up a working copy:

```
git clone https://github.com/Fletchwork/inboxink.git
cd inboxink
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Style: match the code around yours. Keep the cleaner allow-list based (read the hard rules in [CLAUDE.md](CLAUDE.md) before touching `clean.py`, `safety.py` or the Worker), and don't add a dependency for something the standard library does. A change in behavior comes with a test, and a new setting goes in `config.py`, `config.example.toml` and the tests together.

A pull request needs green CI before it can merge. Big decisions get a short record in `docs/adr/`; the template is there.

## Advanced: Google Workspace accounts

Work and school Google accounts usually can't make app passwords, so the default Gmail setup doesn't work for them. There's another way in: set `source.kind = "gog"` in `~/.config/inboxink/config.toml` and let [gog](https://gogcli.sh), a command line tool for Google services, read the mailbox instead.

```toml
[source]
kind = "gog"

[gog]
account = "you@your-company.example"
```

You need your own Google Cloud OAuth client for gog to sign in with, which means creating a project in Google Cloud and following gog's instructions. `inboxink setup` only knows the app-password route and will offer to switch you to it, so on this path you write the settings file by hand, starting from [config.example.toml](config.example.toml), including the Worker details. If `gog` isn't on your PATH when the schedule runs, set `bin` under `[gog]` to its full path. This route has had far less testing than the default one.
