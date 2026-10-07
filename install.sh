#!/bin/sh
# Installs InboxInk. Run it with:
#   curl -fsSL https://raw.githubusercontent.com/Fletchwork/inboxink/main/install.sh | sh
# It installs the `uv` tool manager if you do not have it, then InboxInk itself. It does not run
# setup, because a piped script has no keyboard: run `inboxink setup` afterwards.
#
# Everything lives inside main(), which runs on the last line: a download cut short never reaches
# that line, so a partial script does nothing.
set -eu

say() { printf '%s\n' "$*"; }

main() {
  if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (a small tool that installs Python programs)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv puts itself in one of these; make it usable for the rest of this script.
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
  fi

  command -v uv >/dev/null 2>&1 || { say "Could not find uv after installing it. Open a new terminal window and run this again."; exit 1; }

  say "Installing InboxInk..."
  if uv tool list 2>/dev/null | grep -q '^inboxink '; then
    uv tool upgrade inboxink
  else
    uv tool install inboxink
  fi

  say ""
  say "InboxInk is installed."
  if ! command -v inboxink >/dev/null 2>&1; then
    say "If the next command is not found, open a new terminal window first (or run: uv tool update-shell)."
  fi
  say "Now run: inboxink setup"
}

main "$@"
