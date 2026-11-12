# IP addresses: reserve them, and what happens when one changes

Your router gives every device an address (DHCP). Most of the time a device keeps it, but after a router restart,
a long time switched off or a new network card, it can get a different one. Home AI Cluster finds and trusts devices
**by their address**, so give these devices a fixed address:

* the **AI server** (most important),
* every **GPU PC**,
* your **admin PC** (`ADMIN_PC_IP`) and any device you allowed to use the AI API directly.

## How to reserve an address

In your router's admin page, look for **DHCP reservation**, **static lease**, **address reservation** or
**IP & MAC binding** (usually under LAN or DHCP). Pick the device from the list of connected devices and save its
current address. That's all: the device keeps asking the router for an address as before, and always gets the same one.

You can also type a fixed address into the device itself. Then choose one outside the router's DHCP range, or two
devices may end up with the same address.

## If the AI server's address changes

**What stops working**

| What | Why |
|---|---|
| Chat website, control panel, bookmarks, phone home-screen shortcuts | they point to the old address |
| All GPU PCs (they show **offline**, models run on the server GPU only, big models may not fit) | each GPU PC's firewall only lets the **old** server address in (ports 50052 and 50053) |
| Auto-off | the GPU-use reports also answer only the old server address |
| The installers on port 8090 | they contain the old address |
| VS Code, Claude Code, scripts | they are set up with the old address |

**What keeps working:** the server itself, models on its own GPU, and all chats and accounts (they are stored on the server).

**Fix (easiest first)**

1. In the router, reserve the server's **old** address for it again and restart the server. Everything works again;
   there's nothing else to do.
2. Or keep the new address:
   * on the server: `cd home-ai-cluster && sudo bash install.sh`. It detects the new address and rebuilds the installers.
     (If you set `SERVER_IP` in `config.env`, change it there first.)
   * on **every GPU PC**: run the installer again from the new address (`http://NEW-IP:8090/add-this-pc.bat` or the
     Linux command). It replaces the firewall rules.
   * update bookmarks, and run `clients/windows/setup-vscode.ps1 -Server NEW-IP` again on PCs that use VS Code.

## If a GPU PC's address changes

**What happens**

* The server finds the PC again by itself (it scans the home network for GPU PCs), so models keep using its GPU.
* But the control panel remembers each GPU PC's **name**, its **Use / Standby / Off** choice and its auto-off state
  **by address**. So the PC shows up as a **new GPU** with its default name and mode **Use**, and the old entry stays in
  the list as **offline**.
* If you had set that PC to **Off** (for example a gaming PC), it is now **Use** again. Auto-off still steps in while
  its own programs use the GPU, but set the mode again if you want it off.
* A PC on another subnet that you typed in under **Add a PC → PC on a different network?** is not found until you
  change its address there.

**Fix:** reserve its address in the router, give the new entry its name and mode again, and remove the old offline
entry with its **Forget** button in the GPUs tab.

## If another device's address changes

* **Your admin PC (`ADMIN_PC_IP`)** or a device in the control panel's **Settings → devices allowed to use the AI API**:
  it can no longer use the AI API directly (VS Code, scripts). Add its new address in Settings. Logging in to the
  control panel with your password works from any address.
* **Phones, tablets and family PCs** only open the chat website. Their own addresses don't matter.
