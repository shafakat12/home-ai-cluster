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

Logs: `/opt/ai/logs/install.log`, `/opt/ai/logs/model-download.log`, `journalctl -u llama-swap`, `journalctl -u open-webui`, `journalctl -u ai-control-panel`.
