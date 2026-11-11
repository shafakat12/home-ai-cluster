# Image generation (optional)

Image generation runs [ComfyUI](https://github.com/comfyanonymous/ComfyUI) with the **Z-Image Turbo** model on a Windows PC with an NVIDIA GPU (8 GB+ recommended, ~20 GB disk). Open WebUI sends picture requests to it, so everyone can make images from the chat.

## 1. Install ComfyUI on the GPU PC

Copy `image-generation/windows/install-comfyui.ps1` to that PC, open PowerShell **as Administrator** in that folder:

```powershell
Unblock-File .\install-comfyui.ps1                     # files downloaded from GitHub are blocked by Windows
Set-ExecutionPolicy -Scope Process Bypass
.\install-comfyui.ps1 -Server <AI-server-IP>          # optional: -Dir D:\AI
```

It installs ComfyUI portable, downloads Z-Image Turbo (~20 GB), starts ComfyUI hidden at login on port 8188, and opens that port **only for the AI server**. It also puts two shortcuts on the desktop: ComfyUI itself, and "Free GPU for images", which unloads the text models.

## 2. Connect Open WebUI

As the Open WebUI admin: **Admin Panel → Settings → Images**

| Setting | Value |
|---|---|
| Image Generation | on |
| Image Generation Engine | ComfyUI |
| ComfyUI Base URL | `http://<GPU-PC-IP>:8188` |
| ComfyUI Workflow | paste the contents of `image-generation/openwebui-zimage-workflow.json` |
| Workflow nodes | Prompt → node `27` (text) · Model → node `28` (unet_name) · Width/Height → node `13` · Steps → node `3` · Seed → node `3` |
| Default model | `z_image_turbo_bf16.safetensors` |
| Image size | `1024x1024` · Steps `8` |

Then in a chat, click **Image** (or ➕ → Image) and describe the picture.

Family members also need **Admin Panel → Users → Groups → Default permissions → Features → Image Generation**.

## Notes

* The same GPU can also be a text-model worker. ComfyUI frees its memory after each picture (`--disable-smart-memory`). If a big text model is using that GPU, images are slower or fail; press "Free GPU for images" first.
* Speed depends on GPU **and** system RAM: on a 2080 Ti with 16 GB RAM one image takes about 2 minutes, mostly loading time. 32 GB RAM is much faster.
