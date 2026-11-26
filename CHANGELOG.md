# Changelog

## v0.2.0-beta — 2026-11-26

A large update to the control panel, networking and image generation. Everything from v0.1 should keep working (the upgrade was tested on
copies, not on a real v0.1 install - please report problems); re-run the installer to get the new pieces (see **Upgrading from v0.1** below).

### Redesigned control panel
- A cleaner, faster admin page (port 8091) that works on phones as well as desktops, with a light and a dark theme.
- The page refreshes by itself; the tab you're on updates live without flicker and without interrupting typing.
- Tabs now are: **Overview** (everything at a glance, plus one list of what needs attention), **Models**, **GPUs**,
  **Network** and **Approvals** (with the network watch), **Health**, **Storage**, **Logs**, **Add a PC**, **Settings**.
- A full tab-by-tab guide: [docs/control-panel.md](docs/control-panel.md), and a short version in the README.

### Models
- Downloads from Hugging Face now show **speed, gigabytes done and progress**, with **pause / continue / cancel**.
  A download stopped by a reboot can be continued where it left off; cancel deletes the half-finished file.
- Weekly suggestions of better models that fit your GPUs, and search across Hugging Face from the Models tab.
- Test, load and unload any installed model from the panel.

### Network watch (optional)
- A list of every device on your home network, with alerts for new devices and risky open ports.
- Router drivers for many brands so the panel can read the device list and (on some) block a device:
  **tested: iStoreOS (OpenWrt-based) only.** Other OpenWrt-based firmware (ImmortalWrt, GL.iNet, Turris...) should work
  but is untested; MikroTik, OPNsense, pfSense, UniFi, ASUSWRT, FRITZ!Box, DD-WRT and Omada are included but untested.
- Changes that touch the network (block a device or a website) only run **after you approve them** — in the panel
  or by tapping Approve in a Telegram message. See [docs/network.md](docs/network.md).

### Restart / shut down from the panel (new)
- New **Restart server** and **Shut down server** buttons (Settings → Power, and a power icon on every tab).
  v0.1 had no power buttons at all.
- Each waits **10 seconds** so you can change your mind: a red banner counts down on every tab with a **Cancel**
  button. A shutdown asks you to type the server's name first. Every restart/shutdown/cancel is logged with the time
  and the device it came from.

### Add a PC
- Besides lending a GPU, the **Add a PC** tab now manages **per-PC access**: tick **AI API** to let one PC use the
  models API (port 8080) directly, or **Image share** to let it mount the image folder (NFS). The firewall rule is
  made for you, for that one address only — nothing is opened to the whole network.
- **Reserve IP** asks a supported router (OpenWrt) to always give a PC the same address (it backs up the router's
  config first and asks you to confirm).
- Live **ComfyUI status** for each PC that runs image generation.

### HTTPS (optional)
- The control panel can now serve **HTTPS**. Default stays plain HTTP (fine on a home LAN). Turn it on with
  `PANEL_HTTPS=yes` (a self-signed certificate), or put the panel behind **Caddy** or **Nginx** with a real
  certificate. Over HTTPS the login cookie becomes `Secure` and HSTS is sent; in reverse-proxy mode the panel trusts
  `X-Forwarded-Proto` only from localhost. Guide: [docs/https.md](docs/https.md).

### ComfyUI on the server (optional, experimental)
- You can now run ComfyUI (image generation) **on the server itself** instead of on a GPU PC
  (`INSTALL_COMFYUI=yes`). It gets its own unprivileged `comfyui` user, keeps models and outputs on the data disk,
  and is bound to **127.0.0.1 only** (reach it with an SSH tunnel). PyTorch is chosen to match the server's GPU
  (for NVIDIA, from the CUDA version reported by `nvidia-smi`).
- **Experimental: not tested on real hardware yet.** The PC version remains the tested, supported one. See
  [docs/comfyui-server.md](docs/comfyui-server.md).

### Image generation on a Linux PC
- Step-by-step guide for running ComfyUI on a **Linux** GPU PC (manual steps, untested), next to the Windows installer.
  See the README, Part 3.

### Security
- A security review hardened several things:
  - **Approval checks** on network changes were tightened: requests are re-validated before they run and are accepted
    only when stamped by the panel login or your own Telegram account.
  - The panel returns **generic errors** to the browser (details go to the log), and validates names on remove/add.
  - Root helpers were moved **out of user-writable folders** (to `/usr/local/lib/home-ai`, root-owned), so the panel
    user can't swap one out.
- Full notes, including what is protected and what isn't: [docs/security.md](docs/security.md).

### Upgrading from v0.1
- Re-run the installer: `sudo bash install.sh`. It is safe to re-run and keeps your models, settings and backups.
- What it migrates for you: the list of devices allowed to use the AI API (from `api-allow.txt` and the current
  firewall) becomes the new **per-PC registry**, so no PC loses access. The 10-second power buttons and the
  per-PC firewall rules are set up on this run.
- Nothing is switched to HTTPS or to server-side ComfyUI unless you opt in (`PANEL_HTTPS` / `INSTALL_COMFYUI`).

### Known limits
- Tested on the maintainer's hardware (Ubuntu server, NVIDIA GPU, a Windows GPU PC, OpenWrt router). Other router
  brands, AMD/CPU ComfyUI and the **experimental server-side ComfyUI** are not yet tested on real hardware.
- The AI API (port 8080) still has no password of its own; access is controlled by the firewall (per-PC list).
- This is a **beta**. Please don't expose the panel or the chat directly to the internet — use a VPN.

---

Home AI Cluster · Made by [Shafakat Sayeedi](https://shafakat12.github.io/shafakat/)
