#!/usr/bin/env bash
# Shared helpers + settings for the install steps. Sourced by install.sh and every step.
banner(){ echo; echo "=== $* ==="; }
info(){ echo "  $*"; }
warn(){ echo "!! $*"; }
die(){ echo "!! $*"; exit 1; }
as_admin(){ sudo -u "$ADMIN" -H "$@"; }
apt_install(){ DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null; }

# ---- settings: config.env > config.example.env defaults > auto-detect
[ -f "$REPO/config.example.env" ] && set -a && source "$REPO/config.example.env" && set +a
[ -f "$REPO/config.env" ] && set -a && source "$REPO/config.env" && set +a
IFACE=$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')
[ -n "${SERVER_IP:-}" ] || SERVER_IP=$(ip -4 -o addr show dev "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
[ -n "${LAN_CIDR:-}" ] || LAN_CIDR=$(ip -4 route show dev "$IFACE" scope link 2>/dev/null | awk '/\//{print $1; exit}')
[ -n "${LAN_CIDR:-}" ] || LAN_CIDR="${SERVER_IP%.*}.0/24"
# models / backups folder: after the first install /opt/ai/ai.conf is the truth (changed with  sudo ai-storage);
# a MODELS_DIR / BACKUP_DIR in config.env (older style) is applied - checked and moved - by step 15-storage.
CFG_MODELS_DIR=${MODELS_DIR:-}; CFG_BACKUP_DIR=${BACKUP_DIR:-}
aiconf(){ grep -oP "^$1=\K.*" /opt/ai/ai.conf 2>/dev/null | tail -1 || true; }
MODELS_DIR=$(aiconf MODELS_DIR); MODELS_DIR=${MODELS_DIR:-/opt/ai/models}
BACKUP_DIR=$(aiconf BACKUP_DIR); BACKUP_DIR=${BACKUP_DIR:-/opt/ai/backups}
MODEL_PRESET=${MODEL_PRESET:-auto}
HAS_NVIDIA=$(lspci 2>/dev/null | grep -qi 'nvidia' && echo yes || echo no)
export ADMIN REPO IFACE SERVER_IP LAN_CIDR ADMIN_PC_IP MODELS_DIR BACKUP_DIR MODEL_PRESET HAS_NVIDIA \
       CFG_MODELS_DIR CFG_BACKUP_DIR STORAGE STORAGE_BACKUPS STORAGE_ERASE_OK \
       INSTALL_OPEN_WEBUI INSTALL_WEB_SEARCH INSTALL_CONTROL_PANEL INSTALL_HEALTH INSTALL_NVIDIA_DRIVER

# server GPU memory in MiB (0 = no working NVIDIA GPU)
server_vram(){ nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc 0-9 || true; }

# write a key into /opt/ai/ai.conf (read by the control panel and helper scripts)
conf_set(){
  touch /opt/ai/ai.conf
  grep -v "^$1=" /opt/ai/ai.conf > /opt/ai/ai.conf.tmp || true
  echo "$1=$2" >> /opt/ai/ai.conf.tmp && mv /opt/ai/ai.conf.tmp /opt/ai/ai.conf && chmod 644 /opt/ai/ai.conf
}
