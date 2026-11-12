#!/usr/bin/env bash
# Start the web app and the Cloudflare Tunnel if they are not running. Meant to be run by cron every few
# minutes (and @reboot), so the service comes back after the machine restarts or a process is killed.
#
#   deploy/run.sh           start whatever is not running
#   deploy/run.sh --cron    print the crontab lines for this folder
#   deploy/run.sh --stop    stop both
#
# Workstations often share home directories (NFS) across machines. The pid files remember the host, so
# the service only ever runs on the machine it was started on, and two machines never write data/ at once.
set -uo pipefail
cd "$(dirname "$0")/.."
DIR=$(pwd)
export PATH="$HOME/.local/bin:$PATH"
PORT=8765
TUNNEL_TOKEN=""
[ -f deploy/env ] && . deploy/env
export PORT PUBLIC_URL="${PUBLIC_URL:-}" PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-}"
[ -z "$PUBLIC_URL" ] && unset PUBLIC_URL
[ -z "$PLAYWRIGHT_BROWSERS_PATH" ] && unset PLAYWRIGHT_BROWSERS_PATH
LOGS=data/logs
mkdir -p "$LOGS"
HOST=$(hostname -s)

if [ "${1:-}" = "--cron" ]; then
  echo "*/5 * * * * $DIR/deploy/run.sh >/dev/null 2>&1"
  echo "@reboot $DIR/deploy/run.sh >/dev/null 2>&1"
  exit 0
fi

# pidfile holds "host pid"; prints the pid when it is alive on this host and is ours
alive() {
  [ -f "$1" ] || return 1
  read -r h pid < "$1" || return 1
  if [ "$h" != "$HOST" ]; then
    echo "$1: running on $h, not here ($HOST); delete the file to move the service" >&2
    return 0  # counts as running: don't start a second copy
  fi
  kill -0 "$pid" 2>/dev/null && grep -q "$2" "/proc/$pid/cmdline" 2>/dev/null
}

if [ "${1:-}" = "--stop" ]; then
  for f in data/server.pid data/tunnel.pid; do
    [ -f "$f" ] && read -r h pid < "$f" && [ "$h" = "$HOST" ] && kill "$pid" 2>/dev/null && rm -f "$f"
  done
  echo "stopped"
  exit 0
fi

# Keep the logs from growing without end.
for f in "$LOGS"/*.log; do
  [ -f "$f" ] && [ "$(stat -c %s "$f")" -gt 5000000 ] && mv "$f" "$f.1"
done

if ! alive data/server.pid server.py; then
  echo "$(date '+%F %T') starting server on 127.0.0.1:$PORT" >> "$LOGS/server.log"
  nohup .venv/bin/python -u server.py >> "$LOGS/server.log" 2>&1 &
  echo "$HOST $!" > data/server.pid
fi

if ! alive data/tunnel.pid cloudflared; then
  if [ -n "$TUNNEL_TOKEN" ]; then
    # A named tunnel: fixed address (PUBLIC_URL), routed to 127.0.0.1:$PORT in the Cloudflare dashboard.
    nohup cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" >> "$LOGS/tunnel.log" 2>&1 &
    echo "$HOST $!" > data/tunnel.pid
  else
    # A quick tunnel: a new random https://*.trycloudflare.com address every start, sent to everybody on Discord.
    : > "$LOGS/tunnel.log"
    nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >> "$LOGS/tunnel.log" 2>&1 &
    echo "$HOST $!" > data/tunnel.pid
    url=""
    for _ in $(seq 40); do
      url=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOGS/tunnel.log" | head -1)
      [ -n "$url" ] && break
      sleep 1
    done
    if [ -n "$url" ]; then
      .venv/bin/python manage.py set-public-url "$url" --notify >> "$LOGS/server.log" 2>&1
      echo "public address: $url"
    else
      echo "cloudflared gave no address yet; see $LOGS/tunnel.log" >&2
    fi
  fi
fi
