# ComfyUI: on a PC or on the server

ComfyUI makes the images (and video) for the chat. It needs a GPU. You can run it in two places.

| | On a GPU PC (default) | On the server (`INSTALL_COMFYUI=yes`) |
|---|---|---|
| Where it runs | a Windows or Linux PC with a good GPU | the AI server itself |
| GPU used | that PC's GPU (separate from the chat) | the **same** GPU as the chat models |
| Reached at | `http://PC-IP:8188` (from the server only) | `127.0.0.1:8188` on the server (SSH tunnel) |
| Best when | your PC has the strong GPU | the server has the strong GPU and the PCs don't |
| Set up with | [image generation on a PC](image-generation.md) | the section below |

Most people run it **on a PC**, because the chat models already use the server's GPU and a desktop usually has the bigger card. Run it **on the server** only if the server is where the strong GPU is.

## On a GPU PC

See [Image generation](image-generation.md). The PC installer sets ComfyUI to free its GPU memory between jobs, and adds a **Free GPU for images** shortcut that unloads the chat models first. The **Add a PC** tab shows whether that PC's ComfyUI is answering.

## On the server

> ⚠️ **Experimental.** This path has **not been tested on real hardware** yet. The PC version (above) is the tested, supported one. Try the server version only if you're comfortable reading the install step and fixing a Python/PyTorch issue yourself.

Off by default. To turn it on, set this in `config.env` and run the installer:

```
INSTALL_COMFYUI=yes
```
```
sudo bash install.sh 85-comfyui
```

What it does:

- creates an unprivileged **`comfyui`** user and a Python environment in `/opt/ai/comfyui` (never root);
- installs the PyTorch that matches the server's GPU - for NVIDIA it reads the CUDA version from `nvidia-smi` and picks the matching wheel (AMD/ROCm and CPU are also handled; CPU is very slow, so a GPU is strongly recommended);
- keeps the models and your generated images on the **data disk** (next to the AI models), not the system disk;
- binds ComfyUI to **127.0.0.1 only** - it opens no firewall port, so nothing on the network can reach it directly;
- downloads the Z-Image Turbo model (about 20 GB, resumable - re-run the step if a download stops).

**Reach it** from your PC with an SSH tunnel, then open the address in your browser:

```
ssh -N -L 8188:127.0.0.1:8188 YOU@SERVER-IP
# then: http://127.0.0.1:8188
```

**Hook it into the chat (optional):** in Open WebUI → Admin Panel → Settings → Images, set the engine to ComfyUI and the Base URL to `http://127.0.0.1:8188` (Open WebUI runs on the same machine), and paste `image-generation/openwebui-zimage-workflow.json` as the workflow.

### Sharing the GPU with the chat

On the server, ComfyUI and the chat models (llama-swap) use the **same** graphics card and the same memory. ComfyUI is started with `--disable-smart-memory`, so it hands its memory back after each image. But if a large chat model is loaded **and** you start an image job, the GPU can run out of memory and the job fails with *out of memory*.

If that happens:

- let the chat model unload first (it unloads by itself after a few minutes idle, or stop it in the **GPUs** tab), then start the image;
- or use a smaller chat model so both fit;
- or run ComfyUI on a PC instead, so the two never compete.

There is no automatic "chat or images" switch on the server version - the manual steps above are the whole story. (On a PC, the **Free GPU for images** shortcut does the unload for you.)

### Custom nodes

ComfyUI-Manager and custom nodes are **not** installed - they run code from the internet with the `comfyui` user's access. Add them yourself only if you trust them: as the `comfyui` user, into `/opt/ai/comfyui/ComfyUI/custom_nodes`.

### Remove it

```
sudo bash uninstall.sh            # removes the service, keeps the models and images
sudo bash uninstall.sh --purge    # also removes the comfyui user and its data
```

---

Home AI Cluster · Made by [Shafakat Sayeedi](https://shafakat12.github.io/shafakat/)
