#!/bin/bash
# macOS launcher: double-click in Finder (the first time: right-click > Open).
cd "$(dirname "$0")" || exit 1

fail() {
  echo "$1"
  read -r -p "Press Enter to close."
  exit 1
}

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
  fail "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and run this file again."
fi

# A private environment in .venv: Homebrew's Python refuses installs into itself.
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up ..."
  python3 -m venv .venv || fail "Creating .venv failed. See the messages above."
fi

if ! .venv/bin/python -c "import playwright, keyring" >/dev/null 2>&1; then
  echo "Installing Playwright ..."
  .venv/bin/python -m pip install -r requirements.txt || fail "Installing Playwright failed. See the messages above."
fi

# Edge or Chrome is used when installed; otherwise Playwright's own Chromium.
if [ ! -d "/Applications/Microsoft Edge.app" ] && [ ! -d "/Applications/Google Chrome.app" ] \
   && [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
  echo "No Edge or Chrome found: downloading Chromium for Playwright ..."
  .venv/bin/python -m playwright install chromium || fail "Downloading Chromium failed. See the messages above."
fi

.venv/bin/python server.py
read -r -p "Press Enter to close."
