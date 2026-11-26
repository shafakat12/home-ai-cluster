# Image generation (optional)

Image generation runs [ComfyUI](https://github.com/comfyanonymous/ComfyUI) with the **Z-Image Turbo** model on a PC with an NVIDIA GPU (8 GB+ recommended, about 25 GB of free disk). Open WebUI sends picture requests to it, so everyone can make images from the chat. The PC can run **Windows** (installer script, tested) or **Linux** (manual steps, untested).

> Prefer to run it **on the server** instead of a PC (when the server has the strong GPU)? See [ComfyUI: on a PC or on the server](comfyui-server.md).

## 1. Install ComfyUI on the GPU PC

### Windows

Copy `image-generation/windows/install-comfyui.ps1` to that PC, then open PowerShell **as Administrator** in that folder and run:

```powershell
Unblock-File .\install-comfyui.ps1                     # files downloaded from GitHub are blocked by Windows
Set-ExecutionPolicy -Scope Process Bypass
.\install-comfyui.ps1 -Server <AI-server-IP>          # optional: -Dir D:\AI
```

It installs ComfyUI portable, downloads Z-Image Turbo (~20 GB), starts ComfyUI hidden at login on port 8188, and opens that port **only for the AI server**. It also puts two shortcuts on the desktop: ComfyUI itself, and "Free GPU for images", which unloads the text models.

### Linux (Ubuntu, Debian, ...)

There's no installer script for Linux yet. These are the manual steps for an NVIDIA GPU with a working driver (`nvidia-smi` must show your card):

```bash
# 1. ComfyUI in your home folder, with its own Python environment
sudo apt update && sudo apt -y install git python3-venv curl
git clone https://github.com/comfyanonymous/ComfyUI ~/ComfyUI
cd ~/ComfyUI && python3 -m venv venv && ./venv/bin/pip install --upgrade pip

# 2. PyTorch for your GPU: check your CUDA version with nvidia-smi and pick the matching command at
#    https://pytorch.org/get-started/locally/  (for CUDA 12.8 or newer:)
./venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
./venv/bin/pip install -r requirements.txt

# 3. The Z-Image Turbo model (~20 GB; run a line again to resume a stopped download)
M=~/ComfyUI/models; B=https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files
curl -fL -C - -o $M/diffusion_models/z_image_turbo_bf16.safetensors $B/diffusion_models/z_image_turbo_bf16.safetensors
curl -fL -C - -o $M/text_encoders/qwen_3_4b.safetensors            $B/text_encoders/qwen_3_4b.safetensors
curl -fL -C - -o $M/vae/ae.safetensors                             $B/vae/ae.safetensors
```

Start it with the PC as a systemd service (replace `YOUR-USER` with your user name in all three places):

```bash
sudo tee /etc/systemd/system/comfyui.service >/dev/null <<'EOF'
[Unit]
Description=ComfyUI (AI images)
After=network-online.target
[Service]
User=YOUR-USER
WorkingDirectory=/home/YOUR-USER/ComfyUI
ExecStart=/home/YOUR-USER/ComfyUI/venv/bin/python main.py --listen 0.0.0.0 --port 8188 --disable-smart-memory --disable-auto-launch
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now comfyui
```

ComfyUI has no login, so let **only the AI server** reach it:

```bash
sudo ufw allow OpenSSH                                   # keep SSH working if you log in remotely
sudo ufw allow from SERVER-IP to any port 8188 proto tcp comment 'ComfyUI for the AI server'
sudo ufw enable                                          # if ufw wasn't on yet
```

Check it with `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8188/` (prints `200`). To free the GPU for images before a big job, unload the chat models with `curl -s http://SERVER-IP:8080/unload` (they reload by themselves on the next message). This works only if the PC may use the AI API: tick **AI API** for it in the panel's **Add a PC** tab.

## 2. Connect Open WebUI

As the Open WebUI admin: **Admin Panel → Settings → Images**

| Setting | Value |
|---|---|
| Image Generation | on |
| Image Generation Engine | ComfyUI |
| ComfyUI Base URL | `http://<GPU-PC-IP>:8188` (or `http://127.0.0.1:8188` if ComfyUI runs on the server) |
| ComfyUI Workflow | paste the contents of `image-generation/openwebui-zimage-workflow.json` |
| Workflow nodes | Prompt → node `27` (text) · Model → node `28` (unet_name) · Width/Height → node `13` · Steps → node `3` · Seed → node `3` |
| Default model | `z_image_turbo_bf16.safetensors` |
| Image size | `1024x1024` · Steps `8` |

Then in a chat, click **Image** (or ➕ → Image) and describe the picture.

Family members also need **Admin Panel → Users → Groups → Default permissions → Features → Image Generation**.

## Notes

* The same GPU can also be a text-model worker. ComfyUI frees its memory after each picture (`--disable-smart-memory`). If a big text model is using that GPU, images are slower or fail; press "Free GPU for images" first.
* Speed depends on GPU **and** system RAM: on a 2080 Ti with 16 GB RAM one image takes about 2 minutes, mostly loading time. With 32 GB RAM, it's much faster.

---

Home AI Cluster · Made by [Shafakat Sayeedi](https://shafakat12.github.io/shafakat/)
