#!/usr/bin/env bash
# Model switcher (llama-swap), GPU-worker discovery, worker kit (port 8090), Open WebUI (port 3000), firewall.
source "$REPO/server/lib.sh"; set -uo pipefail
F=$REPO/server/files

info "helper scripts..."
install -o "$ADMIN" -g "$ADMIN" -m 755 "$F/find-workers.sh" "$F/llama-run.sh" "$F/download-models.sh" "$F/router.py" \
        "$F/add-model.sh" "$F/make-config.py" /opt/ai/bin/
[ -f /opt/ai/workers.txt ] || printf '# GPU worker PCs on OTHER networks only (ip[:port] per line). PCs on this network are found automatically.\n' > /opt/ai/workers.txt
[ -f /opt/ai/gpu-policy.json ] || echo '{"server":"use","workers":{}}' > /opt/ai/gpu-policy.json
chown "$ADMIN:$ADMIN" /opt/ai/workers.txt /opt/ai/gpu-policy.json; chmod 644 /opt/ai/gpu-policy.json

# ---- starter model menu (kept if it already exists)
P=$MODEL_PRESET
if [ "$P" = auto ]; then
  V=$(server_vram); V=${V:-0}; RAM=$(awk '/MemTotal/{print int($2/1048576)}' /proc/meminfo)
  if [ "$V" -ge 4000 ] && [ "$RAM" -ge 15 ]; then P=standard; else P=small; fi
  info "MODEL_PRESET=auto -> $P (server GPU ${V} MiB, RAM ${RAM} GB)"
fi
conf_set MODEL_PRESET "$P"
V=$(server_vram); V=${V:-0}
SOLO=$(awk -v v="$V" 'BEGIN{printf "%.1f", (v>0? v*0.75/1024 : 2)}')
conf_set SOLO_GB "$SOLO"; grep -q '^POOL_GB=' /opt/ai/ai.conf || conf_set POOL_GB "$(awk -v s="$SOLO" 'BEGIN{printf "%.0f", s+9}')"
as_admin python3 /opt/ai/bin/make-config.py "$P" "$MODELS_DIR"

info "llama-swap (model switcher, port 8080)..."
if [ ! -x /opt/ai/bin/llama-swap ]; then
  URL=$(curl -fsSL https://api.github.com/repos/mostlygeek/llama-swap/releases/latest \
        | jq -r '.assets[] | select(.name|test("linux_amd64\\.tar\\.gz$")) | .browser_download_url' | head -1)
  [ -n "$URL" ] || die "could not find llama-swap download"
  curl -fsSL --retry 3 -o /tmp/llama-swap.tgz "$URL" && rm -rf /tmp/lsx && mkdir -p /tmp/lsx && tar -xzf /tmp/llama-swap.tgz -C /tmp/lsx
  install -o "$ADMIN" -g "$ADMIN" -m 755 "$(find /tmp/lsx -type f -name llama-swap | head -1)" /opt/ai/bin/llama-swap
  rm -rf /tmp/llama-swap.tgz /tmp/lsx
fi
WATCH=""; /opt/ai/bin/llama-swap --help 2>&1 | grep -q -- -watch-config && WATCH="--watch-config"
cat > /etc/systemd/system/llama-swap.service <<EOF
[Unit]
Description=llama-swap (local AI models API, port 8080)
After=network-online.target
Wants=network-online.target
[Service]
User=$ADMIN
ExecStart=/opt/ai/bin/llama-swap --config /opt/ai/llama-swap.yaml --listen 0.0.0.0:8080 $WATCH
Restart=always
RestartSec=5
LimitMEMLOCK=infinity
[Install]
WantedBy=multi-user.target
EOF

info "worker kit (port 8090: installers for GPU PCs, with this server's address filled in)..."
for f in "$REPO/worker/windows/add-this-pc.bat" "$REPO/worker/windows/add-worker-windows.ps1" "$REPO/worker/linux/add-worker-linux.sh" \
         "$REPO/worker/windows/gpu-status.ps1" "$REPO/worker/linux/gpu-status.py"; do
  sed "s/__SERVER_IP__/$SERVER_IP/g" "$f" > "/opt/ai/worker-kit/$(basename "$f")"
done
sed -i 's/\r$//; s/$/\r/' /opt/ai/worker-kit/add-this-pc.bat
chmod 644 /opt/ai/worker-kit/*
cat > /etc/systemd/system/ai-worker-kit.service <<'EOF'
[Unit]
Description=AI worker kit (installers for adding GPU PCs, port 8090)
After=network-online.target
[Service]
DynamicUser=yes
ExecStart=/usr/bin/python3 -m http.server 8090 --bind 0.0.0.0 --directory /opt/ai/worker-kit
Restart=always
[Install]
WantedBy=multi-user.target
EOF

if [ "${INSTALL_OPEN_WEBUI:-yes}" = yes ]; then
  info "Open WebUI (chat website, port 3000) - first download is ~2-4 GB..."
  docker pull -q ghcr.io/open-webui/open-webui:main >/dev/null || warn "Open WebUI download failed - will retry on next run"
  cat > /etc/systemd/system/open-webui.service <<'EOF'
[Unit]
Description=Open WebUI (chat website, port 3000)
After=docker.service llama-swap.service network-online.target
Requires=docker.service
[Service]
TimeoutStartSec=0
ExecStartPre=-/usr/bin/docker rm -f open-webui
# Updates are NOT downloaded on restart (that can take very long). To update: sudo bash update-open-webui.sh
ExecStart=/usr/bin/docker run --rm --name open-webui --network host \
  -v open-webui:/app/backend/data \
  -e PORT=3000 \
  -e ENABLE_OLLAMA_API=false \
  -e OPENAI_API_BASE_URL=http://127.0.0.1:8080/v1 \
  -e OPENAI_API_KEY=local \
  -e ENABLE_FORWARD_USER_INFO_HEADERS=true \
  @WEBSEARCH@
  -e ANONYMIZED_TELEMETRY=false \
  ghcr.io/open-webui/open-webui:main
ExecStop=/usr/bin/docker stop open-webui
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  # web search preset (used by Open WebUI on its FIRST start only; later change it in Admin Panel > Settings > Web Search)
  if [ "${INSTALL_WEB_SEARCH:-yes}" = yes ]; then
    sed -i 's|^  @WEBSEARCH@$|  -e ENABLE_WEB_SEARCH=true -e WEB_SEARCH_ENGINE=searxng -e "SEARXNG_QUERY_URL=http://127.0.0.1:8888/search?q=<query>" \\|' /etc/systemd/system/open-webui.service
  else
    sed -i '/^  @WEBSEARCH@$/d' /etc/systemd/system/open-webui.service
  fi
fi

info "firewall..."
ufw allow from "$LAN_CIDR" to any port 3000 proto tcp comment 'Open WebUI (home network)' >/dev/null
ufw allow from "$LAN_CIDR" to any port 8090 proto tcp comment 'AI worker kit (home network)' >/dev/null
[ -n "${ADMIN_PC_IP:-}" ] && ufw allow from "$ADMIN_PC_IP" to any port 8080 proto tcp comment 'AI API (admin PC)' >/dev/null

systemctl daemon-reload
systemctl enable --now ai-worker-kit llama-swap >/dev/null 2>&1
systemctl restart llama-swap
[ "${INSTALL_OPEN_WEBUI:-yes}" = yes ] && { systemctl enable open-webui >/dev/null 2>&1; systemctl restart open-webui; }
sleep 3
info "models in the menu: $(curl -s http://127.0.0.1:8080/v1/models | jq -r '.data[].id' 2>/dev/null | paste -sd ' ' -)"
info "GPU worker PCs online now: [$(/opt/ai/bin/find-workers.sh)]"
