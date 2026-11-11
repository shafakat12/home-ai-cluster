#!/usr/bin/env bash
# Home AI Cluster - installer. Run ON THE UBUNTU SERVER, from the repository folder:
#     sudo bash install.sh              install / update everything (safe to run again)
#     sudo bash install.sh <step>       run one step only (e.g. sudo bash install.sh 40-models)
# Where models are stored (folder / whole drive / RAID) can be changed any time later:  sudo ai-storage
# Settings: config.env (copy from config.example.env). Log: /opt/ai/logs/install.log
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1
REPO=$(pwd)

if [ "$(id -u)" -ne 0 ]; then echo "Please run with sudo:  sudo bash install.sh"; exit 1; fi
ADMIN=${SUDO_USER:-}
if [ -z "$ADMIN" ] || [ "$ADMIN" = root ]; then
  echo "Run this as your normal user with sudo (not as root directly), e.g.:  sudo bash install.sh"; exit 1
fi
export REPO ADMIN

mkdir -p /opt/ai/logs
exec > >(tee -a /opt/ai/logs/install.log) 2>&1
source "$REPO/server/lib.sh"
if ! [[ "${SERVER_IP:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || ! [[ "${LAN_CIDR:-}" =~ ^[0-9.]+/[0-9]+$ ]]; then
  echo "Could not detect this server's IP address / home network. Set SERVER_IP and LAN_CIDR in config.env"
  echo "(cp config.example.env config.env), then run  sudo bash install.sh  again."; exit 1
fi

STEPS=(10-base 15-storage 20-llama 30-services 40-models 50-router 60-control-panel 70-web-search 80-health 90-finish)
if [ $# -gt 0 ]; then STEPS=("$@"); fi

banner "Home AI Cluster installer  ($(date))"
info "server ${SERVER_IP}  network ${LAN_CIDR}  admin user ${ADMIN}  models ${MODEL_PRESET}"
for s in "${STEPS[@]}"; do
  f="$REPO/server/steps/$s.sh"
  [ -f "$f" ] || { warn "unknown step $s"; exit 1; }
  banner "Step $s"
  bash "$f"; rc=$?
  if [ $rc -ne 0 ]; then
    if [ $rc -eq 100 ]; then
      warn "A reboot is needed (NVIDIA driver). Reboot, then run  sudo bash install.sh  again - it continues where it stopped."
      exit 0
    fi
    warn "Step $s failed (exit $rc). Fix the problem shown above and run  sudo bash install.sh  again."
    exit $rc
  fi
done
