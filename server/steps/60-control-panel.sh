#!/usr/bin/env bash
# Control panel (port 8091, login required) + weekly "better models" finder.
source "$REPO/server/lib.sh"; set -uo pipefail
[ "${INSTALL_CONTROL_PANEL:-yes}" = yes ] || { info "skipped (INSTALL_CONTROL_PANEL=no)"; exit 0; }
F=$REPO/server/files

install -o "$ADMIN" -g "$ADMIN" -m 755 "$F/control-panel.py" "$F/model-scout.py" /opt/ai/bin/
install -d -m 755 -o root -g root /opt/ai/sbin
install -m 755 -o root -g root "$F/api-allow-sync" "$F/ai-storage" /opt/ai/sbin/
[ -f /opt/ai/api-allow.txt ] || echo "# extra devices allowed to use the AI API directly (one IP per line)" > /opt/ai/api-allow.txt
chown "$ADMIN:$ADMIN" /opt/ai/api-allow.txt; chmod 644 /opt/ai/api-allow.txt

# one-time setup code for creating the admin password (deleted once the password exists)
if [ ! -f /opt/ai/secrets/panel-pass ] && [ ! -f /opt/ai/secrets/setup-code ]; then
  tr -dc 'A-HJ-NP-Z2-9' </dev/urandom | head -c 8 > /opt/ai/secrets/setup-code
  chown "$ADMIN:$ADMIN" /opt/ai/secrets/setup-code; chmod 600 /opt/ai/secrets/setup-code
fi

cat > /etc/systemd/system/ai-control-panel.service <<EOF
[Unit]
Description=Home AI control panel (port 8091)
After=network-online.target llama-swap.service
[Service]
User=$ADMIN
ExecStart=/usr/bin/python3 -I /opt/ai/bin/control-panel.py --port 8091
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/ai-model-scout.service <<EOF
[Unit]
Description=Home AI - look for better models that fit the GPUs
[Service]
Type=oneshot
User=$ADMIN
ExecStart=/usr/bin/python3 -I /opt/ai/bin/model-scout.py
EOF
cat > /etc/systemd/system/ai-model-scout.timer <<'EOF'
[Unit]
Description=Home AI - weekly better-model check
[Timer]
OnCalendar=Mon 04:00
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF

cat > /etc/sudoers.d/home-ai <<EOF
# Home AI control panel: the only admin actions it may run (read disk health, update the AI-API device list,
# storage tab: ai-storage - never touches the system disk or mounted drives; erasing needs ERASE + the panel password)
$ADMIN ALL=(root) NOPASSWD: /opt/ai/sbin/disk-health, /opt/ai/sbin/api-allow-sync, /opt/ai/sbin/ai-storage
EOF
chmod 440 /etc/sudoers.d/home-ai; visudo -cf /etc/sudoers.d/home-ai >/dev/null || { rm -f /etc/sudoers.d/home-ai; die "sudoers check failed"; }

ufw allow from "$LAN_CIDR" to any port 8091 proto tcp comment 'AI control panel (login required)' >/dev/null
systemctl daemon-reload
systemctl enable --now ai-control-panel ai-model-scout.timer >/dev/null 2>&1
systemctl restart ai-control-panel
/opt/ai/sbin/api-allow-sync >/dev/null
sleep 2
info "control panel: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8091/)"
systemctl start --no-block ai-model-scout.service
