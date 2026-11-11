#!/usr/bin/env bash
# Starts downloading the starter models in the background (resumable). Chat works once they finish.
source "$REPO/server/lib.sh"; set -uo pipefail
cat > /etc/systemd/system/ai-model-download.service <<EOF
[Unit]
Description=Download the starter AI models
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=$ADMIN
TimeoutStartSec=0
ExecStart=/bin/bash -c '/opt/ai/bin/download-models.sh > /opt/ai/logs/model-download.log 2>&1'
EOF
systemctl daemon-reload
N=$(grep -c . /opt/ai/download-list.txt 2>/dev/null || echo 0)
if [ "$N" -gt 0 ]; then
  systemctl start --no-block ai-model-download
  info "downloading $N model files in the background. Progress:  tail -f /opt/ai/logs/model-download.log"
else
  info "no starter models selected - add models in the control panel (Models tab)."
fi
