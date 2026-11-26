#!/usr/bin/env bash
# Control panel (port 8091, login required) + weekly "better models" finder.
source "$REPO/server/lib.sh"; set -uo pipefail
[ "${INSTALL_CONTROL_PANEL:-yes}" = yes ] || { info "skipped (INSTALL_CONTROL_PANEL=no)"; exit 0; }
F=$REPO/server/files

install -o "$ADMIN" -g "$ADMIN" -m 755 "$F/control-panel.py" "$F/model-scout.py" /opt/ai/bin/
install -d -m 755 -o root -g root /usr/local/lib/home-ai
install -m 755 -o root -g root "$F/api-allow-sync" "$F/pc-access-sync" "$F/ai-storage" /usr/local/lib/home-ai/
[ -f /opt/ai/api-allow.txt ] || echo "# extra devices allowed to use the AI API directly (one IP per line)" > /opt/ai/api-allow.txt
chown "$ADMIN:$ADMIN" /opt/ai/api-allow.txt; chmod 644 /opt/ai/api-allow.txt

# one-time setup code for creating the admin password (deleted once the password exists)
if [ ! -f /opt/ai/secrets/panel-pass ] && [ ! -f /opt/ai/secrets/setup-code ]; then
  tr -dc 'A-HJ-NP-Z2-9' </dev/urandom | head -c 8 > /opt/ai/secrets/setup-code
  chown "$ADMIN:$ADMIN" /opt/ai/secrets/setup-code; chmod 600 /opt/ai/secrets/setup-code
fi

# Optional HTTPS (PANEL_HTTPS=yes): make a self-signed certificate and serve the panel over TLS. Default is HTTP.
TLS_ENV=""
if [ "${PANEL_HTTPS:-no}" = yes ]; then
  install -d -m 700 -o "$ADMIN" -g "$ADMIN" /opt/ai/secrets
  if [ ! -f /opt/ai/secrets/panel-cert.pem ]; then
    CN=${SERVER_IP:-$(hostname -I | awk '{print $1}')}
    if as_admin openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
         -keyout /opt/ai/secrets/panel-key.pem -out /opt/ai/secrets/panel-cert.pem \
         -subj "/CN=$CN" -addext "subjectAltName=IP:$CN" >/dev/null 2>&1; then
      chmod 600 /opt/ai/secrets/panel-key.pem
      info "panel HTTPS: made a self-signed certificate (CN=$CN). Your browser warns once - trust it, or install your own at /opt/ai/secrets/panel-cert.pem."
    else warn "panel HTTPS: could not make a certificate (is openssl installed?) - staying on HTTP"; fi
  fi
  [ -f /opt/ai/secrets/panel-cert.pem ] && TLS_ENV="Environment=PANEL_TLS_CERT=/opt/ai/secrets/panel-cert.pem PANEL_TLS_KEY=/opt/ai/secrets/panel-key.pem"
fi
cat > /etc/systemd/system/ai-control-panel.service <<EOF
[Unit]
Description=Home AI control panel (port 8091)
After=network-online.target llama-swap.service
[Service]
User=$ADMIN
$TLS_ENV
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
$ADMIN ALL=(root) NOPASSWD: /usr/local/lib/home-ai/disk-health, /usr/local/lib/home-ai/api-allow-sync, /usr/local/lib/home-ai/pc-access-sync, /usr/local/lib/home-ai/ai-storage
# Health tab: restart these services; Overview: start a backup now (exact command lines only)
$ADMIN ALL=(root) NOPASSWD: /usr/bin/systemctl restart --no-block llama-swap.service, /usr/bin/systemctl restart --no-block open-webui.service, /usr/bin/systemctl restart --no-block ai-net-exec.service, /usr/bin/systemctl start --no-block ai-backup.service
EOF
usermod -aG systemd-journal "$ADMIN"      # Logs tab and "last used": read the service logs without sudo
chmod 440 /etc/sudoers.d/home-ai; visudo -cf /etc/sudoers.d/home-ai >/dev/null || { rm -f /etc/sudoers.d/home-ai; die "sudoers check failed"; }

# Settings > Power: restart / shut down in 10 seconds (systemd timers), or cancel. Exactly these three, no wildcards.
# Replaces the whole file, so older 1-minute "shutdown" rules are gone. sudoers needs '=' in arguments as '\='.
P=$(mktemp)
cat > "$P" <<EOF
# Home AI control panel: restart / shut down the server in 10 seconds, or cancel it (written by step 60)
$ADMIN ALL=(root) NOPASSWD: /usr/bin/systemd-run --unit\=home-ai-restart --on-active\=10 /usr/bin/systemctl reboot, /usr/bin/systemd-run --unit\=home-ai-poweroff --on-active\=10 /usr/bin/systemctl poweroff, /usr/bin/systemctl stop home-ai-restart.timer home-ai-poweroff.timer
EOF
if visudo -cf "$P" >/dev/null; then install -m 440 -o root -g root "$P" /etc/sudoers.d/home-ai-power
else warn "power buttons: sudoers check failed, Settings > Power stays off"; fi
rm -f "$P"

ufw allow from "$LAN_CIDR" to any port 8091 proto tcp comment 'AI control panel (login required)' >/dev/null

# Per-PC firewall registry (/opt/ai/pcs.json): the panel's "Add a PC" tab manages the AI API (8080) and the image
# share (NFS) per PC through pc-access-sync. Build it once from config + anything already allowed (v0.1 upgrade adopts
# the existing rules), then apply. No hard-coded addresses: it starts from ADMIN_PC_IP and the current firewall.
if [ ! -f /opt/ai/pcs.json ]; then
  ADMIN_PC_IP="${ADMIN_PC_IP:-}" python3 - <<'PY' > /opt/ai/pcs.json
import ipaddress, json, os, re, subprocess
reg = {}
def priv(ip):
    try: a = ipaddress.ip_address(ip); return a.version == 4 and a.is_private
    except ValueError: return False
def mark(ip, key):
    if priv(ip): reg.setdefault(ip, {"api": False, "share": False, "name": "", "mac": ""})[key] = True
admin = os.environ.get("ADMIN_PC_IP", "").strip()
if admin: mark(admin, "api")
try:                                   # adopt existing API clients (v0.1 used api-allow.txt)
    for l in open("/opt/ai/api-allow.txt"):
        l = l.strip()
        if l and not l.startswith("#"): mark(l, "api")
except OSError: pass
try:                                   # adopt whatever the firewall already allows (8080 = api, NFS ports = share)
    for line in subprocess.run(["ufw", "status"], capture_output=True, text=True).stdout.splitlines():
        m = re.match(r"\s*(\d+)(?:/\w+)?\s+ALLOW\s+(\d+\.\d+\.\d+\.\d+)", line)
        if not m: continue
        port, ip = m.group(1), m.group(2)
        if port == "8080": mark(ip, "api")
        elif port in ("111", "2049", "20048"): mark(ip, "share")
except Exception: pass
print(json.dumps(reg, indent=1))
PY
  chown "$ADMIN:$ADMIN" /opt/ai/pcs.json; chmod 644 /opt/ai/pcs.json
fi

systemctl daemon-reload
systemctl enable --now ai-control-panel ai-model-scout.timer >/dev/null 2>&1
systemctl restart ai-control-panel
/usr/local/lib/home-ai/pc-access-sync >/dev/null || /usr/local/lib/home-ai/api-allow-sync >/dev/null
sleep 2
[ -n "$TLS_ENV" ] && PURL=https://127.0.0.1:8091/ || PURL=http://127.0.0.1:8091/
info "control panel: $(curl -sk -o /dev/null -w '%{http_code}' "$PURL")${TLS_ENV:+ (HTTPS)}"
systemctl start --no-block ai-model-scout.service
