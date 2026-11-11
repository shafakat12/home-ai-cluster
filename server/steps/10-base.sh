#!/usr/bin/env bash
# Base system: packages, NVIDIA driver, Docker, firewall, folders.
source "$REPO/server/lib.sh"; set -uo pipefail

info "packages..."
apt-get update -qq
apt_install curl jq unzip ca-certificates pciutils ufw libgomp1 libvulkan1 vulkan-tools python3 openssl rsync
apt_install libcurl4t64 2>/dev/null || apt_install libcurl4 2>/dev/null || true
HAS_NVIDIA=$(lspci 2>/dev/null | grep -qi nvidia && echo yes || echo no)     # re-check now that pciutils is installed

if [ "$HAS_NVIDIA" = yes ]; then
  if nvidia-smi >/dev/null 2>&1; then
    info "NVIDIA driver working: $(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | head -1)"
  elif [ "${INSTALL_NVIDIA_DRIVER:-yes}" = yes ]; then
    info "NVIDIA GPU found without a working driver - installing the recommended driver..."
    apt_install ubuntu-drivers-common
    ubuntu-drivers install || die "driver install failed - install an NVIDIA driver manually (sudo ubuntu-drivers install), reboot, run again"
    exit 100          # install.sh asks for a reboot
  else
    warn "NVIDIA GPU found but no driver, and INSTALL_NVIDIA_DRIVER=no - the server will use Vulkan/CPU."
  fi
else
  info "no NVIDIA GPU on the server - it will use Vulkan (AMD/Intel) or CPU, plus any GPU PCs you add."
fi

info "docker..."
command -v docker >/dev/null || apt_install docker.io
systemctl enable --now docker >/dev/null 2>&1

# models/backups on a storage drive or RAID (sudo ai-storage): never create them on the system disk by mistake
for d in "$MODELS_DIR" "$BACKUP_DIR"; do
  case "$d" in /mnt/ai-storage*)
    mp=$(echo "$d" | cut -d/ -f1-3)
    mountpoint -q "$mp" || die "the storage drive $mp is not mounted (cable? power? RAID: cat /proc/mdstat). Fix it - see docs/storage.md - then run again."
  esac
done

info "folders (owned by $ADMIN)..."
mkdir -p /opt/ai/{bin,logs,worker-kit,llama.cpp} "$MODELS_DIR" "$BACKUP_DIR"
install -d -m 700 -o "$ADMIN" -g "$ADMIN" /opt/ai/secrets
chown "$ADMIN:$ADMIN" /opt/ai /opt/ai/bin /opt/ai/logs /opt/ai/worker-kit /opt/ai/llama.cpp "$MODELS_DIR"
chmod 700 "$BACKUP_DIR"
conf_set SERVER_IP "$SERVER_IP"; conf_set LAN_CIDR "$LAN_CIDR"; conf_set ADMIN "$ADMIN"
conf_set MODELS_DIR "$MODELS_DIR"; conf_set BACKUP_DIR "$BACKUP_DIR"
[ -n "${ADMIN_PC_IP:-}" ] && echo "$ADMIN_PC_IP" > /opt/ai/panel-allow.txt || touch /opt/ai/panel-allow.txt
chmod 644 /opt/ai/panel-allow.txt

info "firewall: allow SSH from the home network only, block other incoming..."
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
ufw allow from "$LAN_CIDR" to any port 22 proto tcp comment 'SSH (home network)' >/dev/null
ufw --force enable >/dev/null
info "ok"
