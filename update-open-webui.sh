#!/usr/bin/env bash
# Updates Open WebUI. Downloads while the current version keeps running, then restarts (~30 s downtime).
#   sudo bash update-open-webui.sh
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
echo "Downloading the newest Open WebUI (chat keeps working meanwhile)..."
docker pull ghcr.io/open-webui/open-webui:main || { echo "download failed - nothing changed"; exit 1; }
systemctl restart open-webui
for i in $(seq 1 60); do curl -sf -o /dev/null http://127.0.0.1:3000/health && break; sleep 3; done
echo "Open WebUI: $(curl -s -m 5 http://127.0.0.1:3000/health || echo NOT-UP)"
docker image prune -f >/dev/null 2>&1
