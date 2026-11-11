#!/usr/bin/env bash
# Turns this Ubuntu PC into a GPU worker for the home AI server.
# Usage (on the Ubuntu PC):  curl -fsSL http://YOUR-AI-SERVER:8090/add-worker-linux.sh | bash
# Works with NVIDIA (driver installed), AMD or Intel GPUs via Vulkan. Safe to run again.
set -euo pipefail
SERVER="${SERVER:-__SERVER_IP__}"
case "$SERVER" in __*) echo "No server address. Use: curl -fsSL http://YOUR-AI-SERVER:8090/add-worker-linux.sh | bash   (or SERVER=<ip> bash add-worker-linux.sh)"; exit 1;; esac
PORT="${PORT:-50052}"
DIR=/opt/ai-worker

echo "== Getting llama.cpp version from the AI server ($SERVER)"
TAG=$(curl -fsSL "http://$SERVER:8090/llama-version.txt")
echo "   version: $TAG"

echo "== Installing packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl libvulkan1 vulkan-tools libgomp1 >/dev/null
if lspci | grep -qi nvidia && ! command -v nvidia-smi >/dev/null; then
  echo "!! NVIDIA GPU found but no driver. Installing the recommended driver - REBOOT afterwards and run this again."
  sudo ubuntu-drivers install
  exit 0
fi

echo "== Downloading llama.cpp $TAG (Vulkan build)"
sudo mkdir -p "$DIR/$TAG"
if [ ! -x "$DIR/$TAG/ggml-rpc-server" ] && [ ! -x "$DIR/$TAG/rpc-server" ]; then
  URL=$(curl -fsSL "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/$TAG" \
    | grep -oE '"browser_download_url": *"[^"]*bin-ubuntu-vulkan-x64\.tar\.gz"' | head -1 | sed 's/.*"\(http[^"]*\)"/\1/')
  [ -n "$URL" ] || { echo "Could not find the Linux Vulkan download for $TAG"; exit 1; }
  curl -fL --retry 3 -o /tmp/llama.tgz "$URL"
  sudo tar -xzf /tmp/llama.tgz -C "$DIR/$TAG" --strip-components=1
  rm -f /tmp/llama.tgz
fi
sudo ln -sfn "$DIR/$TAG" "$DIR/current"
RPC="$DIR/current/ggml-rpc-server"; [ -x "$RPC" ] || RPC="$DIR/current/rpc-server"
CACHE=""; "$RPC" --help 2>&1 | grep -q -- '--cache' && CACHE="-c"
echo "   GPUs seen:"; "$DIR/current/llama-server" --list-devices 2>&1 | tail -4 || true

echo "== Installing background service (starts at boot)"
sudo tee /etc/systemd/system/ai-gpu-worker.service >/dev/null <<EOF
[Unit]
Description=AI GPU Worker (llama.cpp rpc-server)
After=network-online.target
Wants=network-online.target

[Service]
DynamicUser=yes
SupplementaryGroups=video render
CacheDirectory=ai-gpu-worker
Environment=LLAMA_CACHE=/var/cache/ai-gpu-worker
ExecStart=$RPC --host 0.0.0.0 --port $PORT $CACHE
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ai-gpu-worker
sudo systemctl restart ai-gpu-worker

echo "== GPU use report (port 50053, only the AI server may ask) - lets the server switch this GPU off while you use it"
sudo curl -fsSL -o "$DIR/gpu-status.py" "http://$SERVER:8090/gpu-status.py"
sudo chmod 755 "$DIR/gpu-status.py"
sudo tee /etc/systemd/system/ai-gpu-status.service >/dev/null <<UNIT
[Unit]
Description=AI GPU use report for the AI server's auto-off
After=network-online.target
Wants=network-online.target

[Service]
DynamicUser=yes
SupplementaryGroups=video render
ExecStart=/usr/bin/python3 $DIR/gpu-status.py $SERVER 50053
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now ai-gpu-status >/dev/null 2>&1
sudo systemctl restart ai-gpu-status

echo "== Firewall: only the AI server may connect to ports $PORT and 50053"
if sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow from "$SERVER" to any port "$PORT" proto tcp comment 'AI GPU worker'
  sudo ufw allow from "$SERVER" to any port 50053 proto tcp comment 'AI GPU use report'
else
  # ufw was off: turn it on (blocks all incoming except SSH and the AI server). Outgoing traffic is unaffected.
  sudo ufw default deny incoming >/dev/null
  sudo ufw default allow outgoing >/dev/null
  sudo ufw allow OpenSSH >/dev/null 2>&1 || true
  sudo ufw allow from "$SERVER" to any port "$PORT" proto tcp comment 'AI GPU worker'
  sudo ufw allow from "$SERVER" to any port 50053 proto tcp comment 'AI GPU use report'
  sudo ufw --force enable
  echo "   Firewall (ufw) was off and is now ON: incoming blocked except SSH and the AI server."
fi

sleep 3
if systemctl is-active --quiet ai-gpu-worker; then
  echo "WORKER-OK  This PC ($(hostname -I | awk '{print $1}')) is now a GPU worker."
  echo "The AI server will find it automatically the next time a 'pooled' model starts."
else
  echo "WORKER-FAILED"; sudo journalctl -u ai-gpu-worker -n 30 --no-pager
fi
