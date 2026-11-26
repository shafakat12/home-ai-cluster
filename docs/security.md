# Security

The setup is meant for a **home network behind a router**. Please don't expose these ports to the internet.

## What is reachable

| Port | Who can reach it | Protection |
|---|---|---|
| 3000 chat | home network | Open WebUI accounts (first account = admin) |
| 8091 control panel | home network | admin password (PBKDF2 hash), login cookie, lock-out after 5 wrong tries, blocks other websites (HTTPS optional, see [HTTPS](https.md)) |
| 8090 worker kit | home network | read-only installer files |
| 8080 AI API | `ADMIN_PC_IP` + devices you list in the panel | firewall only (the API has no password) |
| 8888 web search | the server itself | bound to 127.0.0.1 |
| 22 SSH | home network | your SSH login |
| 50052 on GPU PCs | the AI server's IP only | firewall rule on each PC |
| 50053 on GPU PCs | the AI server's IP only | firewall rule + the report itself refuses other addresses; it only shares GPU-use % and the busiest program's name |

Everything else is blocked by `ufw` (`deny incoming`).

The network watch opens no ports. It connects out to your router (SSH on port 22, or HTTPS on the brand's fixed port) and, if you connect it, to AdGuard Home.

## Good to know

* **The first Open WebUI account becomes the admin.** Create it right after installing. Later, either turn off sign-up (Admin Panel → Settings → General) or set new sign-ups to "pending".
* The control-panel password can only be created with the **setup code** printed by the installer (or from `ADMIN_PC_IP`). The code is deleted once the password exists. Forgot the password? On the server: `rm /opt/ai/secrets/panel-pass` and re-run `sudo bash install.sh 60-control-panel` for a new code.
* The panel runs as your normal user. Everything it may run as root is listed, as exact command lines, in `/etc/sudoers.d/home-ai`:
  * three helpers: read disk health, update the API device list, and `ai-storage` for the **Storage** tab;
  * restart llama-swap, Open WebUI or the network watch's `ai-net-exec` (Health tab), and start a backup now (Storage tab).
* `/etc/sudoers.d/home-ai-power` allows exactly three more command lines for **Settings > Power**: `systemd-run --unit=home-ai-restart --on-active=10 systemctl reboot` (restart in 10 seconds), the same with `home-ai-poweroff` and `systemctl poweroff` (shut down in 10 seconds), and `systemctl stop home-ai-restart.timer home-ai-poweroff.timer` (cancel). The panel only sends them after login, from its own page (it checks the `Origin` / `Referer`), and a shutdown needs the server's name typed. Don't want these buttons? `sudo rm /etc/sudoers.d/home-ai-power`.
* With the network watch, `/etc/sudoers.d/home-ai-net` adds: ask for a network change (`netctl request`, which still waits for your approval), start a network scan, and show / save / test / detect the router settings or forget a pinned router certificate (`router-admin`). See [Network watch](network.md).
* **How a network change is approved.** A change is only *requested* by the assistant; it is written to a queue and runs only after **you** approve it — in the panel (your login) or by tapping Approve in the Telegram alert, which is accepted **only from your own Telegram account** (the chat ID you saved). The assistant's own account can't approve: the queue files are owned by the admin user in a root-owned folder, so the assistant (a separate, lower-privileged user) can neither write them nor drop new ones, and there is no "approve" command it can call. Before running, the root worker re-validates the action and its parameters and ignores any approval that wasn't stamped by the panel or Telegram. **Residual risk:** the panel runs as the admin user, so code that fully takes over the panel could approve a request on your behalf — but only the fixed, validated actions (block/unblock a site or device, update a listed Linux host), never an arbitrary command. Keep the admin login protected like any admin account.
* **ComfyUI on the server** (optional, `INSTALL_COMFYUI=yes`) runs as its own unprivileged `comfyui` user and is bound to **127.0.0.1 only** - it opens no port. Reach it with an SSH tunnel. Custom nodes (which run arbitrary code) are not installed. See [ComfyUI on the server](comfyui-server.md).
* The helpers that run as root live in `/usr/local/lib/home-ai`, and the network watch's data in `/var/lib/home-ai/net`. Both are owned by root all the way up, so your normal user (and so the panel) can't swap a helper for something else. Older versions used `/opt/ai/sbin`, which sits inside a folder your user owns; the installer removes it.
* **Router logins** (network watch) are stored only in `/var/lib/home-ai/net/router.env` (root, mode 600) and in the nightly backup. The panel never shows them again. The server only connects to addresses on your home network and only on each brand's fixed ports. HTTPS routers' certificates are pinned the first time. Prefer SSH keys or API keys with the fewest rights over an admin password. Details in [Network watch](network.md).
* The panel user is in the `systemd-journal` group so the **Logs** tab can read the service logs. That lets it read all system logs, but not change them.
* `ai-storage` never touches the system disk, mounted drives, or drives already in a RAID/LVM. Erasing drives from the Storage tab needs you to type `ERASE` **and** enter the admin password again (wrong passwords count toward the 5-tries limit). Anyone with your server login can still erase spare, unmounted drives with it, so protect that login like any admin account.
* **rpc-server has no authentication.** That's why the worker installers allow only the server's IP. Never open port 50052 to other devices.
* Telegram tokens and the panel password hash are in `/opt/ai/secrets` (readable by the admin user only) and in the nightly backups (`BACKUP_DIR`, mode 700).
* Remote access from outside your home: use a VPN such as WireGuard or Tailscale, **not** port forwarding.

---

Home AI Cluster · Made by [Shafakat Sayeedi](https://shafakat12.github.io/shafakat/)
