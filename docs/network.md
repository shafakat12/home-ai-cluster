# Network watch (optional)

The network watch keeps a list of every device on your home network and tells you when something new shows up. It can also block a device or a website, but only after you approve it.

It's off by default. To turn it on, set this in `config.env` and run the installer again:

```bash
INSTALL_NETWORK_WATCH=yes
ROUTER_TYPE=auto        # or one of the types in the table below
```

```bash
sudo bash install.sh 45-network
```

Once it's on, the control panel gets two more tabs: **Network** and **Approvals**.

## What it watches

- **Devices.** Every 10 minutes the server scans your network (`arp-scan`, plus the router's own client list if your router is supported). Each device shows its IP address, MAC address, maker and when it was last seen.
- **New devices.** The first scan marks every device it finds as yours. After that, a device it hasn't seen before gets a *new* tag in the Network tab, and you get a Telegram message if Telegram is set up. Tick **Mine** and type a name (for example "Living room TV") to stop the alerts for it.
- **Risky open ports.** Once a week (Sunday morning) it checks every device for services that shouldn't be open on a home network: Telnet, FTP, Windows file sharing, Remote Desktop, VNC, Android debug, a Docker API without a password, and a few more. They show as red tags in the Network tab.

It only looks. It never changes anything on its own.

## Approvals: nothing changes without you

Blocking a device or a website is always a two-step thing:

1. Someone asks for the change. That's you pressing **Block...** in the Network tab, or a script on the server running `netctl request ...`.
2. You approve or reject it, in the **Approvals** tab or with the buttons in the Telegram message.

Only then does a small background service (`ai-net-exec`) carry it out. Requests you don't answer stay waiting. Every request records who asked, when, who decided and how the change was made.

What can be asked for:

| Change | How it's done |
|---|---|
| Block / unblock a device | on the router (if your router can do it, see the table), otherwise its DNS is stopped in AdGuard Home |
| Block / unblock a website for the whole network | AdGuard Home |
| Stop / allow DNS for one device | AdGuard Home |

**AdGuard Home** is not installed by this project. If you already run it somewhere at home, connect it in the control panel under **Settings > AdGuard Home**. Without it, and without a router that can block, the watch is read-only.

A DNS block stops a device from looking up names. A device that uses its own DNS server (some phones and smart TVs do) gets past it. A router block is stronger.

## Supported routers

`generic` works with every router: devices come from the server's own scan and blocking goes through AdGuard Home. The other types also read the router's own list (which sees devices that are asleep or on Wi-Fi only), and some can block on the router itself.

| `ROUTER_TYPE` | Routers | Reads devices | Blocks on the router | Login | Tested on a real router |
|---|---|---|---|---|---|
| `generic` | any router | server scan | no (AdGuard instead) | none | yes |
| `openwrt` | OpenWrt, iStoreOS, ImmortalWrt, GL.iNet, Turris | yes | yes (firewall rule by MAC) | SSH key | **yes** (iStoreOS) |
| `mikrotik` | MikroTik, RouterOS 7.1 or newer | yes | yes (firewall rule by MAC) | REST API user | no |
| `omada` | TP-Link Omada controller | yes | yes (client block) | Open API app | no |
| `opnsense` | OPNsense | yes | no | API key + secret | no |
| `pfsense` | pfSense | yes | no | SSH key | no |
| `unifi` | UniFi Network application | yes | no | API key | no |
| `asuswrt` | ASUS (stock firmware and Asuswrt-Merlin) | yes | no | SSH key | no |
| `fritzbox` | AVM FRITZ!Box | yes | no | FRITZ!Box user | no |
| `ddwrt` | DD-WRT | yes | no | SSH key | no |

Only OpenWrt (on iStoreOS) has been tested on real hardware. The others were written from each maker's official documentation, but I don't own those routers. If you try one, please open an issue and say whether it worked.

Why some are read-only: OPNsense and pfSense firewall rules match IP addresses rather than MAC addresses, so a block would break when the device gets a new address. UniFi's official API documents no block action, and on ASUS and DD-WRT a block can't be undone safely from outside.

`ROUTER_TYPE=auto` (the default) looks at your router and suggests a type in the install log, but uses `generic` until you choose one. You can change it any time in the control panel under **Settings > Router**.

## Setting up your router

Logins are entered in the control panel (**Settings > Router**), never in `config.env`. Press **Test** there afterwards: it only reads (status and device list) and never changes anything.

Give the server the fewest rights that work. Where a router supports it, make a separate user just for the server.

For the SSH types, the server uses its own key. Its public half is shown under **Settings > Router**: copy it into the router.

- **OpenWrt / iStoreOS:** System > Administration > SSH-Keys, paste the server's key. The user is `root` (OpenWrt has no smaller user that can change firewall rules). The server adds block rules named `ai_block_...` and only ever removes rules with that name.
- **MikroTik:** turn on the `www-ssl` service (IP > Services). Make a group with only the `read`, `write` and `rest-api` policies, and a user in that group. Block rules get the comment `ai_block <mac>`, and only those are ever removed.
- **TP-Link Omada:** Settings > Platform Integration > Open API, add an app in *Client* mode with access to your site's clients only. Enter the controller ID, client ID and client secret.
- **OPNsense:** System > Access > Users, make a user with only the *Diagnostics: ARP Table*, *Status: DHCP leases* and *System: Status* privileges, then create an API key for it.
- **pfSense:** make a user under System > User Manager, give it shell access, and paste the server's key there.
- **UniFi:** Settings > Control Plane > Integrations, create an API key.
- **ASUS:** Administration > System, turn on SSH with key login only (LAN only) and paste the server's key.
- **FRITZ!Box:** Home Network > Network > Network Settings, turn on *Allow access for applications*. Make a FRITZ!Box user with only the rights it needs.
- **DD-WRT:** Services > Secure Shell, turn on SSH with key login and paste the server's key.

### How the router login is kept safe

- The server only connects to addresses on your home network (10.x, 172.16–31.x, 192.168.x or a `.lan` / `.local` name), and only on the fixed ports for that brand. Settings can't be used to make it connect anywhere else.
- Logins are stored only on the server in `/var/lib/home-ai/net/router.env` (owner root, mode 600). The control panel never shows a saved password or key again. It only says that one is saved.
- HTTPS routers usually have a self-signed certificate. The server accepts it the first time and remembers its fingerprint. If the certificate changes later, it refuses to connect until you press **Trust the new certificate** in Settings > Router. Only do that if you replaced the router or its certificate yourself.
- MAC and IP addresses are checked with strict patterns before they are used, and no shell command is ever built from text typed in the panel.

## Where things live

| What | Where |
|---|---|
| The helpers (`netctl`, `net-scan`, `net-exec`, `router-admin`, `routers.py`) | `/usr/local/lib/home-ai` (root-owned) |
| Device list, names, requests, alerts, router login | `/var/lib/home-ai/net` (root-owned) |
| Scan every 10 minutes / weekly port check | `ai-net-scan.timer`, `ai-net-ports.timer` |
| Carries out approved changes | `ai-net-exec` service |
| Log | control panel > Logs > Network watch, or `journalctl -u ai-net-scan -u ai-net-exec` |

The control panel runs as your normal user and may only do these things as root (`/etc/sudoers.d/home-ai-net`): ask for a change with `netctl request`, start a scan, and use `router-admin` to show, save, test or detect the router settings. Asking is not doing: every change still waits for your approval.

## Turning it off

Set `INSTALL_NETWORK_WATCH=no` and stop the services:

```bash
sudo systemctl disable --now ai-net-scan.timer ai-net-ports.timer ai-net-exec
```

`sudo bash uninstall.sh` removes the helpers, services and sudo rules. Rules the server added on your router stay there until you unblock the device first (or delete the `ai_block` rules on the router yourself).

---

Home AI Cluster · Made by [Shafakat Sayeedi](https://shafakat12.github.io/shafakat/)
