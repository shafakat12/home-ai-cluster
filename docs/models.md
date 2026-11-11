# Models

## Adding a model from the web

Control panel → **Models** → **Search Hugging Face** (only GGUF models are listed) → **Choose size** → **Add**.

* The size tag shows whether it fits: *fits: server GPU* (solo), *fits: all GPUs* (pool), *all GPUs + RAM (slow)*, or *too big*.
* Q4_K_M is a good default size: much smaller than the original and only slightly less accurate.
* A vision projector (`mmproj`) is downloaded automatically when the repository has one, so the model can read pictures.
* The download runs in the background (progress in the panel). The model appears in Open WebUI when it is ready.
* New models are **private to admins** in Open WebUI at first: **Admin Panel → Settings → Models → ✏️ → Access → Public**.

From the command line on the server:

```bash
/opt/ai/bin/add-model.sh <name> <hf-owner/repo> <quant-or-exact-file> <solo|pool> [context]
/opt/ai/bin/add-model.sh --list
/opt/ai/bin/add-model.sh --remove <name>
```

## Suggested models

Every Monday the server checks what is trending on Hugging Face and lists models that fit your GPUs under **Models → Suggested new models** (with an Add button). Nothing downloads by itself. "Check now" runs the check immediately.

The size limits it uses are in `/opt/ai/ai.conf` (`SOLO_GB`, `POOL_GB`). The installer sets them from the server GPU; raise `POOL_GB` after adding GPU PCs.

## team and auto

* **team**: the `team` models answer the same question in parallel, then the `judge` model writes one combined answer. Slower, often better.
* **auto**: a small model classifies each message; pictures go to the vision model, code to the coding model, long or detailed questions to the "deep" model, everything else to the quick model.

The choices live in `/opt/ai/router.json`. Renaming a model in the control panel updates it automatically.

## Editing the model menu by hand

`/opt/ai/llama-swap.yaml` is reloaded automatically when it changes. Useful per-model settings:
* `ttl:` seconds of no use before the model is stopped
* `-c` context length (memory grows with it), `-ctk q8_0 -ctv q8_0` halves context memory
* `aliases:` extra names for the same model

Re-running the installer **keeps** this file. To rebuild it from a preset: `sudo -u $USER python3 /opt/ai/bin/make-config.py standard "$(grep -oP '^MODELS_DIR=\K.*' /opt/ai/ai.conf)" --force`.
