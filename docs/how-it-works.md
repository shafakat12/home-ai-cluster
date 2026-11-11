# How it works

This page is about what runs where. For what each tab and button of the control panel does, see [The control panel, tab by tab](control-panel.md).

## The parts

| Part | Runs on | Port | What it does |
|---|---|---|---|
| **llama.cpp** (`llama-server`) | server | 10001+ (internal) | runs one AI model, split across the allowed GPUs |
| **rpc-server** | each GPU PC | 50052 | lends that PC's GPU to the server (only the server may connect) |
| **GPU-use report** | each GPU PC | 50053 | tells the server how much the PC's own programs use its GPU, for auto-off (only the server may ask) |
| **llama-swap** | server | 8080 | one API address for all models; starts the model a request asks for, stops idle ones |
| **router.py** | server (started by llama-swap) | internal | the virtual models `team` and `auto` |
| **Open WebUI** | server (Docker) | 3000 | the chat website, user accounts, image generation, web search, documents |
| **SearXNG** | server (Docker) | 8888 (server only) | private web search |
| **Control panel** | server | 8091 | admin page: models, GPUs, health, add a PC, settings |
| **Worker kit** | server | 8090 | serves the GPU-PC installers with the server's address filled in |
| **ComfyUI** (optional) | a Windows NVIDIA PC | 8188 | image generation |

## What happens when you send a message

1. Open WebUI sends it to llama-swap (`127.0.0.1:8080`) with the chosen model name.
2. If that model isn't running, llama-swap starts it through `/opt/ai/bin/llama-run.sh`:
   * `find-workers.sh` scans the home network for GPU PCs (port 50052) and adds `/opt/ai/workers.txt`.
   * The GPU choices from the control panel (`/opt/ai/gpu-policy.json`) decide which GPUs may be used.
   * `llama-server` loads the model file and spreads its layers over the allowed GPUs (`--rpc`). The model data is streamed to the GPU PCs over the network, so nothing is stored on them.
3. The answer streams back. After the model's idle time (`ttl`) it is stopped and the GPUs are free again.

## Model modes

| Mode | GPUs used | Use for |
|---|---|---|
| `solo` | server GPU only | small models that fit the server GPU, so they don't need the network |
| `pool` | server GPU + all GPU PCs | big models |
| `remote` | GPU PCs only | a second model running next to a `solo` one |

Groups in `llama-swap.yaml` decide which models may run at the same time. Small models (`duo` group) stay loaded together; a big pooled model (`big` group) pauses them while it runs.

## GPU choices (control panel → GPUs)

* **Use**: every model may use this GPU.
* **Standby**: only big pooled models use it, or models with nowhere else to go.
* **Off**: never used. Good for a gaming PC while someone is playing.
* **Auto-off**: the control panel checks every GPU PC every 5 seconds. Above 40% use by the PC's own programs it sets that GPU to Off and stops the models using it; 5 minutes after the PC is quiet it restores the old mode. State: `/opt/ai/gpu-guard.json`, settings in `/opt/ai/gpu-policy.json` (`auto_off_pct`, `auto_back_min`).
* **In use by a model**: a GPU PC serves one model at a time and doesn't answer other connections meanwhile. The panel sees which GPU PCs the running models use and shows them as in use instead of offline.

## Files on the server

```
/opt/ai/
  ai.conf                 settings written by the installer (server IP, model sizes that fit, ...)
  llama-swap.yaml         the model menu (models added in the control panel are appended)
  router.json             which models team / auto use
  gpu-policy.json         Use / Standby / Off per GPU, GPU names
  workers.txt             GPU PCs on other networks (others are found automatically)
  models/                 model files (MODELS_DIR - or on a drive / RAID at /mnt/ai-storage/models, see storage.md)
  llama.cpp/              llama.cpp versions (current -> the one in use)
  bin/                    llama-run.sh, find-workers.sh, router.py, add-model.sh, control-panel.py, ...
  sbin/                   root-owned helpers (disk health, firewall list, ai-storage)
  secrets/                panel password hash, sessions, Telegram token (only the admin user can read)
  worker-kit/             installers served on port 8090
  backups/                nightly backups (BACKUP_DIR - can also be on the storage drive)
  logs/                   install log, model downloads
```

All llama.cpp builds (server and GPU PCs) must be **the same version**. The installer publishes the version in `/opt/ai/worker-kit/llama-version.txt`, and the worker installers download exactly that version.
