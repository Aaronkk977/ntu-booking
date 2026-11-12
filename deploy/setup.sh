#!/usr/bin/env bash
# One-time setup on a Linux machine without root (e.g. a CSIE workstation):
# Python + packages in .venv, Playwright's headless Chromium, cloudflared, and the password key.
# Safe to run again; it only fills in what is missing.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_VERSION=3.12
BIN="$HOME/.local/bin"
export PATH="$BIN:$PATH"
mkdir -p "$BIN"
# deploy/env may set PLAYWRIGHT_BROWSERS_PATH; export it so Playwright sees it
if [ -f deploy/env ]; then set -a; . deploy/env; set +a; fi
[ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ] || unset PLAYWRIGHT_BROWSERS_PATH

if ! command -v uv >/dev/null; then
  echo "== Installing uv into $BIN"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" INSTALLER_NO_MODIFY_PATH=1 sh
fi

if [ ! -x .venv/bin/python ]; then
  echo "== Creating .venv with Python $PYTHON_VERSION"
  uv venv --python "$PYTHON_VERSION" .venv
fi
echo "== Installing packages"
uv pip install --python .venv/bin/python -r requirements.txt

# The headless shell is enough (nothing is ever shown on a screen) and much smaller than full Chromium.
# Home directories have a quota: put PLAYWRIGHT_BROWSERS_PATH=/tmp2/<you>/ms-playwright in deploy/env to
# keep it elsewhere (then rerun this script if /tmp2 gets cleaned).
echo "== Installing Playwright's headless Chromium (${PLAYWRIGHT_BROWSERS_PATH:-~/.cache/ms-playwright})"
.venv/bin/python -m playwright install chromium --only-shell

echo "== Checking that the browser starts"
if ! .venv/bin/python - <<'EOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    print("   ok:", b.version)
    b.close()
EOF
then
  echo "!! Chromium did not start. Missing system libraries are the usual cause; the error above names them."
  echo "   Without root, ask the workstation admins, or try another workstation."
  exit 1
fi

if ! command -v cloudflared >/dev/null; then
  echo "== Downloading cloudflared into $BIN"
  arch=$(uname -m); case "$arch" in x86_64) arch=amd64;; aarch64) arch=arm64;; esac
  curl -fsSL -o "$BIN/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
  chmod +x "$BIN/cloudflared"
fi

echo "== Preparing data/"
.venv/bin/python -c "import config, credentials; config.init_dirs(); credentials._key()"
[ -f deploy/env ] || cp deploy/env.example deploy/env
chmod 600 deploy/env

cat <<EOF

Done. Next:
  .venv/bin/python manage.py set-admin <your 計中 account>
  .venv/bin/python manage.py allow <friend> <friend> ...
  .venv/bin/python manage.py set-discord-token
  edit deploy/env (port; TUNNEL_TOKEN + PUBLIC_URL for a fixed address)
  deploy/run.sh                        start now
  crontab -e   and add the two lines printed by:  deploy/run.sh --cron
EOF
