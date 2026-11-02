#!/bin/bash
# macOS launcher: double-click in Finder (the first time: right-click > Open).
cd "$(dirname "$0")" || exit 1

fail() {
  echo
  echo "Setup failed: $1"
  read -r -p "Press Enter to close."
  exit 1
}

# uv (https://docs.astral.sh/uv/) downloads the Python this project uses into .venv, so nothing has to be
# installed by hand. The version is fixed here and only changes after it has been tried.
PYTHON_VERSION=3.12
export PATH="$HOME/.local/bin:$PATH"
export UV_LINK_MODE=copy  # uv's cache and .venv may sit on different disks; copying is fine for a few packages

# A .venv from another Python version (e.g. made by an older version of this file) is replaced.
REBUILD=
if [ -x .venv/bin/python ] && ! .venv/bin/python -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '$PYTHON_VERSION'.split('.'))))" 2>/dev/null; then
  REBUILD=1
fi
SETUP=
if [ ! -x .venv/bin/python ] || [ -n "$REBUILD" ] || ! .venv/bin/python -c "import playwright, keyring" >/dev/null 2>&1; then
  SETUP=1
fi

if [ -n "$SETUP" ]; then
  echo "============================================================"
  echo " First launch: setting up the environment."
  echo " This downloads Python and Playwright (about 75 MB) and"
  echo " usually takes 1 to 3 minutes. Please keep this window open."
  echo "============================================================"
  echo
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv, the tool that sets up Python ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null \
    || fail "uv could not be installed. Check the internet connection and open this file again, or install uv yourself from https://docs.astral.sh/uv/"
fi
command -v uv >/dev/null 2>&1 || fail "uv was not found after installing it. Open this file again."

if [ -n "$REBUILD" ]; then
  echo "The Python version has changed: removing the old environment ..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python $PYTHON_VERSION ..."
  uv venv --quiet --python "$PYTHON_VERSION" .venv || fail "Python could not be set up. See the messages above, then open this file again."
fi

if ! .venv/bin/python -c "import playwright, keyring" >/dev/null 2>&1; then
  echo "Installing Playwright, the largest download ..."
  uv pip install --python .venv/bin/python -r requirements.txt \
    || fail "Playwright could not be installed. See the messages above, then open this file again."
fi

# Edge or Chrome is used when installed; otherwise Playwright's own Chromium.
if [ ! -d "/Applications/Microsoft Edge.app" ] && [ ! -d "/Applications/Google Chrome.app" ] \
   && [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
  echo "No Edge or Chrome found: downloading Chromium for Playwright (about 150 MB) ..."
  .venv/bin/python -m playwright install chromium || fail "Chromium could not be downloaded. See the messages above, then open this file again."
fi

if [ -n "$SETUP" ]; then
  echo
  echo "Setup complete. Next time the console starts right away."
  echo
fi
echo "Starting the console ..."
.venv/bin/python server.py
read -r -p "Press Enter to close."
