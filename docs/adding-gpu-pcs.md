# Adding GPU PCs

Any PC on the home network with a graphics card can lend its GPU to the server. Its GPU memory is added to the pool, so bigger models fit and run faster. Only the GPU is used: its files stay untouched, and only the AI server may connect to it.

The control panel's **Add a PC** tab shows these same instructions with your server's address filled in.

## Windows

1. On that PC, open `http://SERVER-IP:8090/add-this-pc.bat` in the browser and save the file.
2. Double-click it and click **Yes** when Windows asks for permission.
   If Windows SmartScreen says "Windows protected your PC", click **More info → Run anyway** (the file came from your own server).
3. Wait for **WORKER-OK**. The first run downloads ~300 MB.

What it does:
* downloads the llama.cpp build matching the server's version (CUDA for NVIDIA, otherwise Vulkan) to `C:\ai-worker`;
* opens port 50052 in Windows Firewall **only for the server's IP**;
* installs a small GPU-use report (task **AI GPU Status**, port 50053, also only for the server's IP) for auto-off;
* adds a scheduled task "AI GPU Worker" that starts with Windows and restarts if it crashes.

NVIDIA: install the latest driver first. A driver from 2025 or newer gets the CUDA 13 build; older ones get CUDA 12 or Vulkan.

## Linux (Ubuntu, Debian, ...)

```bash
curl -fsSL http://SERVER-IP:8090/add-worker-linux.sh | bash
```

If it installs an NVIDIA driver, reboot and run the same command again. It installs the systemd services `ai-gpu-worker` and `ai-gpu-status` (GPU-use report for auto-off, NVIDIA only) and lets only the server connect to ports 50052 and 50053 (ufw).

## After adding

* The server finds PCs on the same network automatically. Check the **Add a PC** or **GPUs** tab, or press **Look for new PCs now**.
* In **GPUs**, give it a name and choose **Use**, **Standby** or **Off**.
* Its status shows **online**, **in use by a model** (a model is running on it right now; a GPU PC serves one model at a time, so its free memory isn't shown meanwhile) or **offline** (PC off, worker not running, or blocked by its firewall).
* Running models pick it up the next time they start. "Stop all running models" in the GPUs tab forces that.

PC on a different subnet? Add its address in **Add a PC → PC on a different network?** (stored in `/opt/ai/workers.txt`).

**Reserve the PC's IP address in your router.** The server remembers each GPU PC (name, mode, auto-off) by its address. With a new address it shows up as a new GPU, and the old entry stays as offline until you press **Forget**. See [IP addresses](ip-addresses.md).

## Auto-off while you use the PC

Each GPU PC tells the server how much **its own programs** (a game, a video, 3D work...) use its GPU. The AI worker itself
and ComfyUI don't count. When that is above **40%** for two checks in a row (about 10 seconds):

1. the server switches that GPU to **Off**,
2. stops the models that were using it (they start again without it on the next message, so chat keeps working on the other GPUs),
3. and sends a Telegram message, if Telegram is set up.

When the PC's own use stays at or below 40% for **5 minutes**, the GPU goes back to the mode it had (Use or Standby).
The GPUs tab shows each PC's own use and "off automatically" while it is switched off. Both numbers can be changed there
(0% turns auto-off off). If you click **Use** yourself while the PC is busy, that choice is kept until the PC is quiet again.

The report only contains GPU-use percentages and the name of the busiest program, and only the AI server may ask for it.
GPU PCs added before this feature need their installer run once more (same steps as above) to get the report.

## Removing a PC

* Temporarily: set it to **Off** in the GPUs tab, or just turn the PC off. Models then use the remaining GPUs.
* For good: Windows: delete the tasks **AI GPU Worker** and **AI GPU Status** in Task Scheduler, then the folder `C:\ai-worker`. Linux: `sudo systemctl disable --now ai-gpu-worker ai-gpu-status`.

## Tips

* A wired connection matters: while a model loads, its data travels over the network to the GPU PCs. 2.5 Gbit loads a 12 GB model in under a minute; Wi-Fi can take ten times longer.
* Gaming on a GPU PC while a pooled model is loaded shares that GPU's memory. Set it to **Off** or **Standby** while playing.
* After updating the server (new llama.cpp version), run the PC installer again so the versions match.
