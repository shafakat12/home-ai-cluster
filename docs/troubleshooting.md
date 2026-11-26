# Troubleshooting

Start with the control panel's **Health** tab: it shows every service, whether each one answers, temperatures, memory and disks.

| Problem | Fix |
|---|---|
| Page doesn't load at all ("took too long to respond") | Is the server on? Did its IP change (see [IP addresses](ip-addresses.md))? `ping SERVER-IP`. Reserve the IP in your router. |
| A model never answers / "loading" forever | First load can take minutes (model size ÷ disk speed, plus network to the GPU PCs). Check `journalctl -u llama-swap -f`. |
| Model fails to load: out of memory | Too big for the allowed GPUs. Set GPU PCs to **Use**, add a GPU PC, use a smaller quant, or lower `-c` (context). |
| GPU PC not found | Same network? PC on? `Test-NetConnection SERVER-IP` from it. On the PC, is the task/service running and the firewall rule present? Re-run the PC installer. |
| Pooled model errors after an update | llama.cpp versions differ: run the PC installer again on every GPU PC. |
| Very slow answers with a GPU PC | Wi-Fi or 100 Mbit link. Use a cable; check the network card speed in the Health tab. |
| Web search returns nothing | `curl "http://127.0.0.1:8888/search?q=test&format=json"` on the server. Google often blocks home servers; the other engines still answer. `docker restart searxng`. |
| Open WebUI restart takes forever | It was downloading an update. Updates now only happen with `sudo bash update-open-webui.sh`. |
| Family can't see a model | New models are admin-only: Admin Panel → Settings → Models → Access → Public. |
| Images fail | Is ComfyUI running on the GPU PC (`http://GPU-PC:8188`)? Is the workflow pasted and are the node numbers right? Is a big text model using that GPU? Press "Free GPU for images". |
| First answer after a server reboot is slow (CUDA) | Normal once: the GPU code is compiled and cached. |
| Health tab: "RAID … degraded" | A RAID drive failed or came loose. It still works; check cables, then see "When a RAID drive fails" in [storage.md](storage.md). |
| Models gone after a restart / "model file not found" | The storage drive did not mount. `findmnt /mnt/ai-storage`, `cat /proc/mdstat`, `sudo mount -a`. Check its cable/power. |
| `ai-storage` won't offer a drive | It is the system disk, mounted, or part of a RAID/LVM. `sudo ai-storage list` says which. Unmount it, or stop the old array with `sudo mdadm --stop /dev/mdX`. |
| All GPU PCs offline after the server got a new IP | Their firewalls only allow the old address. Give the server its old address back, or follow [IP addresses](ip-addresses.md). |
| The same GPU shows twice, one entry offline | That PC got a new IP address. Press **Forget** on the offline entry, set name and mode on the new one, and reserve the address in your router. |
| A GPU switched to Off by itself | Auto-off: that PC's own programs used its GPU (see "PC's own use" in the GPUs tab). It comes back 5 minutes after the PC is quiet. Change or disable it in the GPUs tab. |
| GPUs tab: "no GPU-use report from this PC" | Run the PC installer again (it adds the report). Windows: check the task **AI GPU Status**; Linux: `systemctl status ai-gpu-status`. |
| Forgot the control-panel password | `rm /opt/ai/secrets/panel-pass` then `sudo bash install.sh 60-control-panel` (new setup code). |
| Logs tab is empty / "last used" never shows | The panel user can't read the system logs. Run `sudo bash install.sh 60-control-panel` again (it adds the user to `systemd-journal`). |
| A download is stuck or was stopped by a restart | Models tab > Downloads: **Continue** picks up where it stopped. **Cancel** deletes the partial file. |
| Health tab: no **Restart** buttons | The sudo rules are from an older install. Run `sudo bash install.sh 60-control-panel` again. |
| Settings > Power says "Not set up yet" or "Needs update" | The sudo rule for the power buttons is missing or from an older install (1-minute delay). Run `sudo bash install.sh 60-control-panel` again. |
| A restart was started by mistake | You have 10 seconds: press **Cancel** in the red banner at the top of any tab, or on the server: `sudo systemctl stop home-ai-restart.timer home-ai-poweroff.timer`. |
| Network and Approvals tabs are missing | The network watch is off. Set `INSTALL_NETWORK_WATCH=yes` in `config.env` and run `sudo bash install.sh 45-network`. |
| Router **Test connection** fails | Check the type and address under Settings > Router. SSH types: is the server's public key on the router? HTTPS types: is the API or REST service turned on? *Certificate changed*: only if you replaced the router or its certificate, press **Trust the new certificate**. More in [Network watch](network.md). |
| An approved block shows *failed* | The Approvals tab shows the reason under *failed*. The Logs tab (Network watch) has the details. Routers marked read-only in [Network watch](network.md) can't block; use AdGuard instead. |

Logs: the control panel's **Logs** tab, or `/opt/ai/logs/install.log`, `/opt/ai/logs/model-download.log`, `journalctl -u llama-swap`, `journalctl -u open-webui`, `journalctl -u ai-control-panel`, `journalctl -u ai-net-scan -u ai-net-exec`.

---

Home AI Cluster · Made by [Shafakat Sayeedi](https://shafakat12.github.io/shafakat/)
