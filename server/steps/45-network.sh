#!/usr/bin/env bash
# Network watch (optional, INSTALL_NETWORK_WATCH=yes): a list of every device on the home network, alerts on new
# devices and risky open ports, AdGuard Home and router support (docs/network.md), and changes - block a device or a
# website - that only run after you approve them in the control panel or in Telegram.
source "$REPO/server/lib.sh"; set -uo pipefail
L=/usr/local/lib/home-ai; D=/var/lib/home-ai/net; F=$REPO/server/files
if [ "${INSTALL_NETWORK_WATCH:-no}" != yes ]; then
  info "skipped (INSTALL_NETWORK_WATCH=no in config.env)"
  conf_set NETWORK_WATCH no; exit 0
fi

info "packages (nmap, arp-scan)..."
apt_install nmap arp-scan openssh-client

info "helpers -> $L, data -> $D (both root-owned up to /)"
install -d -m 755 -o root -g root "$L" /var/lib/home-ai "$D" "$D/requests"   # root writes requests: root-owned folder
install -m 755 -o root -g root "$F/netctl" "$F/net-exec" "$F/net-scan" "$F/router-admin" "$L/"
install -m 644 -o root -g root "$F/routers.py" "$L/"
[ -f "$D/labels.json" ] || echo '{}' > "$D/labels.json"
chown "$ADMIN:$ADMIN" "$D/labels.json"; chmod 644 "$D/labels.json"           # the panel's "Mark as mine" writes it in place

# SSH key for routers that are read over SSH (OpenWrt, pfSense, ASUS, DD-WRT); its public half is shown in the panel
[ -f /opt/ai/secrets/ssh_key ] || as_admin ssh-keygen -q -t ed25519 -N '' -C home-ai-server -f /opt/ai/secrets/ssh_key

# router type: only on the first run (later: control panel > Settings > Router). auto = suggest one, use generic for now
if [ ! -f "$D/router.env" ]; then
  RT=${ROUTER_TYPE:-auto}; GW=$(ip -4 route show default | awk '{print $3; exit}')
  if [ "$RT" = auto ]; then
    S=$(echo "{\"host\": \"$GW\"}" | "$L/router-admin" detect 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("label") or "")' 2>/dev/null)
    info "router: ${S:-not recognised} - using 'generic' for now (works with any router). Set the type in the control panel > Settings > Router."
    RT=generic
  fi
  if echo "{\"brand\": \"$RT\", \"host\": \"$GW\"}" | "$L/router-admin" save >/dev/null; then info "router type: $RT"
  else warn "ROUTER_TYPE=$RT is not a known type - using generic (see docs/network.md)"; echo '{"brand": "generic"}' | "$L/router-admin" save >/dev/null; fi
fi

cat > /etc/systemd/system/ai-net-scan.service <<EOF
[Unit]
Description=Home AI - scan the home network (new devices)
[Service]
Type=oneshot
ExecStart=$L/net-scan
EOF
cat > /etc/systemd/system/ai-net-scan.timer <<'EOF'
[Unit]
Description=Home AI - network scan every 10 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/ai-net-ports.service <<EOF
[Unit]
Description=Home AI - weekly check for risky open ports
[Service]
Type=oneshot
Nice=10
ExecStart=$L/net-scan --ports
EOF
cat > /etc/systemd/system/ai-net-ports.timer <<'EOF'
[Unit]
Description=Home AI - weekly port check
[Timer]
OnCalendar=Sun 05:00
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/ai-net-exec.service <<EOF
[Unit]
Description=Home AI - run network changes after you approve them (control panel or Telegram)
After=network-online.target
[Service]
ExecStart=$L/net-exec
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

# the panel may only: ask for a change (it still needs your approval), use the router settings helper, start a scan
cat > /etc/sudoers.d/home-ai-net <<EOF
# Home AI network watch: the only admin actions the control panel may run (written by server/steps/45-network.sh)
$ADMIN ALL=(root) NOPASSWD: $L/netctl request *, /usr/bin/systemctl start --no-block ai-net-scan.service
$ADMIN ALL=(root) NOPASSWD: $L/router-admin show, $L/router-admin save, $L/router-admin test, $L/router-admin detect, $L/router-admin forget-pin, $L/router-admin reserve
EOF
chmod 440 /etc/sudoers.d/home-ai-net
visudo -cf /etc/sudoers.d/home-ai-net >/dev/null || { rm -f /etc/sudoers.d/home-ai-net; die "sudoers check failed"; }

conf_set NETWORK_WATCH yes
systemctl daemon-reload
systemctl enable --now ai-net-scan.timer ai-net-ports.timer ai-net-exec >/dev/null 2>&1
systemctl restart ai-net-exec
systemctl start --no-block ai-net-scan.service
info "network watch: on. The first scan takes 1-2 minutes; every device found then is marked as yours."
