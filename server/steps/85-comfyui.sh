#!/usr/bin/env bash
# OPTIONAL (INSTALL_COMFYUI=yes): ComfyUI image generation ON THIS SERVER. Off by default.
#  - its own unprivileged 'comfyui' user and a Python venv in /opt/ai/comfyui (never root);
#  - bound to 127.0.0.1 ONLY (no firewall rule) - reach it with an SSH tunnel or through Open WebUI on this server;
#  - models and outputs go on the data disk (next to the AI models), not the system disk;
#  - PyTorch is chosen for the GPU found (NVIDIA / AMD / none). No GPU = CPU = very slow.
#  - custom nodes / ComfyUI-Manager are NOT installed (they run arbitrary code). Add them yourself if you trust them.
# It SHARES the server's GPU with the chat models (llama-swap): a large chat model loaded at the same time as an
# image job can run the GPU out of memory. ComfyUI frees its own memory between jobs; if a job fails with "out of
# memory", let the chat model unload first (GPUs tab) or use a smaller chat model. See docs/comfyui-server.md.
# Remove with: sudo bash uninstall.sh  (keeps models) or --purge (also removes the comfyui user and its data).
source "$REPO/server/lib.sh"; set -uo pipefail
[ "${INSTALL_COMFYUI:-no}" = yes ] || { info "skipped (INSTALL_COMFYUI=no in config.env)"; exit 0; }

CU=comfyui
APP=/opt/ai/comfyui                                   # code + venv (small, on the system disk)
MD=$(aiconf MODELS_DIR); MD=${MD:-/opt/ai/models}
CDATA="$(dirname "$MD")/comfyui"                       # models + outputs on the same (big) disk as the AI models
[ "$CDATA" = "$APP" ] && CDATA="$APP/data"            # models in /opt/ai/models: keep data apart from the code

info "packages (python venv, git)..."
apt_install python3-venv python3-dev git

id "$CU" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/comfyui --shell /usr/sbin/nologin "$CU"
install -d -o "$CU" -g "$CU" -m 755 "$APP" "$CDATA" "$CDATA/models" "$CDATA/output" "$CDATA/input"

info "ComfyUI code..."
if [ -d "$APP/ComfyUI/.git" ]; then sudo -u "$CU" git -C "$APP/ComfyUI" pull --ff-only >/dev/null 2>&1 || warn "ComfyUI update skipped (kept the installed version)"
else sudo -u "$CU" git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$APP/ComfyUI" >/dev/null 2>&1 || die "could not download ComfyUI (no internet?)"; fi

# GPU -> the matching PyTorch wheel index. For NVIDIA, read the driver's CUDA version from nvidia-smi and pick the
# newest PyTorch CUDA wheel that is not newer than it (a wheel built for a newer CUDA won't run on an older driver).
cuda_wheel(){ # $1 = "12.4" -> "cu124" (newest wheel <= that CUDA), empty if unknown
  local n; n=$(awk -v v="$1" 'BEGIN{if(v ~ /^[0-9]+\.[0-9]+$/){split(v,a,"."); print a[1]*100+a[2]}else print 0}')
  if   [ "$n" -ge 1208 ]; then echo cu128
  elif [ "$n" -ge 1206 ]; then echo cu126
  elif [ "$n" -ge 1204 ]; then echo cu124
  elif [ "$n" -ge 1201 ]; then echo cu121
  elif [ "$n" -ge 1108 ]; then echo cu118; fi; }
GPU=cpu; IDX="https://download.pytorch.org/whl/cpu"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  GPU=cuda; CV=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)
  WH=$(cuda_wheel "${CV:-}")
  if [ -n "$WH" ]; then IDX="https://download.pytorch.org/whl/$WH"; info "NVIDIA GPU, CUDA ${CV} -> PyTorch ${WH}"
  else WH=cu124; IDX="https://download.pytorch.org/whl/cu124"; warn "could not read the CUDA version from nvidia-smi (got '${CV:-none}') - defaulting to ${WH}. If PyTorch fails to load, pick the right wheel at https://pytorch.org/get-started/locally/ and set --index-url in /etc/systemd/system/ai-comfyui.service's install, or edit this step."; fi
elif [ -d /sys/class/kfd ] || command -v rocminfo >/dev/null 2>&1; then GPU=rocm; IDX="https://download.pytorch.org/whl/rocm6.2"; fi
[ "$GPU" = cpu ] && warn "no GPU found - image generation will be VERY slow (CPU). A GPU is strongly recommended."
info "GPU: $GPU (PyTorch index: $IDX)"

info "Python environment (first run downloads PyTorch, ~2-3 GB - a few minutes)..."
[ -x "$APP/venv/bin/python" ] || sudo -u "$CU" python3 -m venv "$APP/venv"
sudo -u "$CU" "$APP/venv/bin/pip" install -q --upgrade pip >/dev/null
sudo -u "$CU" "$APP/venv/bin/pip" install -q torch torchvision --index-url "$IDX" || die "PyTorch install failed"
sudo -u "$CU" "$APP/venv/bin/pip" install -q -r "$APP/ComfyUI/requirements.txt" || die "ComfyUI requirements failed"

cat > /etc/systemd/system/ai-comfyui.service <<EOF
[Unit]
Description=ComfyUI image generation (127.0.0.1:8188, this server)
After=network-online.target
[Service]
User=$CU
WorkingDirectory=$APP/ComfyUI
# 127.0.0.1 only. --disable-smart-memory frees VRAM between jobs so chat models can use the GPU again.
ExecStart=$APP/venv/bin/python main.py --listen 127.0.0.1 --port 8188 --base-directory $CDATA --disable-auto-launch --disable-smart-memory
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP $CDATA /var/lib/comfyui
[Install]
WantedBy=multi-user.target
EOF

# Z-Image Turbo model (same files as the PC installer), onto the data disk. Resumable; re-run to finish a partial download.
info "model: Z-Image Turbo (~20 GB, resumable)..."
dl(){ # subdir url
  local d="$CDATA/models/$1" f; f="$(basename "$2")"; install -d -o "$CU" -g "$CU" "$d"
  if [ -s "$d/$f" ]; then info "[have] $f"; return 0; fi
  if sudo -u "$CU" curl -fsSL --retry 10 --retry-delay 5 -C - -o "$d/$f.part" "$2" && sudo -u "$CU" mv "$d/$f.part" "$d/$f"
  then info "[done] $f"; else warn "[failed] $f - run this step again to resume"; fi
}
dl diffusion_models https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors
dl text_encoders    https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors
dl vae              https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors

systemctl daemon-reload
systemctl enable --now ai-comfyui >/dev/null 2>&1
systemctl restart ai-comfyui
sleep 3
info "ComfyUI: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8188/ 2>/dev/null) (127.0.0.1:8188)"
info "Open WebUI image hookup (optional): Admin Panel > Settings > Images > ComfyUI, Base URL http://127.0.0.1:8188,"
info "  workflow = image-generation/openwebui-zimage-workflow.json. From your PC: ssh -N -L 8188:127.0.0.1:8188 $ADMIN@${SERVER_IP:-this-server}  then open http://127.0.0.1:8188"
