#!/usr/bin/env bash
# Drive SMART health + service checks every 10 minutes (Telegram alerts if set up) + nightly backups.
source "$REPO/server/lib.sh"; set -uo pipefail
[ "${INSTALL_HEALTH:-yes}" = yes ] || { info "skipped (INSTALL_HEALTH=no)"; exit 0; }
F=$REPO/server/files
apt_install smartmontools sqlite3
install -d -m 755 -o root -g root /opt/ai/sbin
install -m 755 -o root -g root "$F/disk-health" "$F/ai-backup" /opt/ai/sbin/

cat > /etc/systemd/system/ai-health-check.service <<'EOF'
[Unit]
Description=Home AI - check disks + services, alert on new problems
[Service]
Type=oneshot
ExecStart=/opt/ai/sbin/disk-health check
EOF
cat > /etc/systemd/system/ai-health-check.timer <<'EOF'
[Unit]
Description=Home AI - health check every 10 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/ai-backup.service <<'EOF'
[Unit]
Description=Home AI nightly backup
[Service]
Type=oneshot
Nice=15
IOSchedulingClass=idle
ExecStart=/opt/ai/sbin/ai-backup
EOF
cat > /etc/systemd/system/ai-backup.timer <<'EOF'
[Unit]
Description=Home AI nightly backup
[Timer]
OnCalendar=*-*-* 03:10
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now ai-health-check.timer ai-backup.timer >/dev/null 2>&1
/opt/ai/sbin/disk-health json | jq -r '.disks[] | "  \(.name) \(.model) \(.size) \(.kind): health=\(.health) temp=\(.temp)C \(.problems|join("; "))"' 2>/dev/null
/opt/ai/sbin/disk-health check >/dev/null
/opt/ai/sbin/ai-backup | sed 's/^/  /'
