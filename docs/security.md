# Security

The setup is meant for a **home network behind a router**. Please don't expose these ports to the internet.

## What is reachable

| Port | Who can reach it | Protection |
|---|---|---|
| 3000 chat | home network | Open WebUI accounts (first account = admin) |
| 8091 control panel | home network | admin password (PBKDF2 hash), login cookie, lock-out after 5 wrong tries, blocks other websites |
| 8090 worker kit | home network | read-only installer files |
| 8080 AI API | `ADMIN_PC_IP` + devices you list in the panel | firewall only (the API has no password) |
| 8888 web search | the server itself | bound to 127.0.0.1 |
| 22 SSH | home network | your SSH login |
| 50052 on GPU PCs | the AI server's IP only | firewall rule on each PC |

Everything else is blocked by `ufw` (`deny incoming`).

## Good to know

* **The first Open WebUI account becomes the admin.** Create it right after installing. Later, either turn off sign-up (Admin Panel → Settings → General) or set new sign-ups to "pending".
* The control-panel password can only be created with the **setup code** printed by the installer (or from `ADMIN_PC_IP`). The code is deleted once the password exists. Forgot the password? On the server: `rm /opt/ai/secrets/panel-pass` and re-run `sudo bash install.sh 60-control-panel` for a new code.
* The panel runs as your normal user. It may run exactly three admin helpers via `/etc/sudoers.d/home-ai`: read disk health, update the API device list, and `ai-storage` for the **Storage** tab.
* `ai-storage` never touches the system disk, mounted drives, or drives already in a RAID/LVM. Erasing drives from the Storage tab needs you to type `ERASE` **and** enter the admin password again (wrong passwords count toward the 5-tries limit). Anyone with your server login can still erase spare, unmounted drives with it, so protect that login like any admin account.
* **rpc-server has no authentication.** That's why the worker installers allow only the server's IP. Never open port 50052 to other devices.
* Telegram tokens and the panel password hash are in `/opt/ai/secrets` (readable by the admin user only) and in the nightly backups (`BACKUP_DIR`, mode 700).
* Remote access from outside your home: use a VPN such as WireGuard or Tailscale, **not** port forwarding.
