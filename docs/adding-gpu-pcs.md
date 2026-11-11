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
* adds a scheduled task "AI GPU Worker" that starts with Windows and restarts if it crashes.

NVIDIA: install the latest driver first. A driver from 2025 or newer gets the CUDA 13 build; older ones get CUDA 12 or Vulkan.

## Linux (Ubuntu, Debian, ...)

```bash
curl -fsSL http://SERVER-IP:8090/add-worker-linux.sh | bash
```

If it installs an NVIDIA driver, reboot and run the same command again. It installs a systemd service `ai-gpu-worker` and lets only the server connect to port 50052 (ufw).

## After adding

* The server finds PCs on the same network automatically. Check the **Add a PC** or **GPUs** tab, or press **Look for new PCs now**.
* In **GPUs**, give it a name and choose **Use**, **Standby** or **Off**.
* Running models pick it up the next time they start. "Stop all running models" in the GPUs tab forces that.

PC on a different subnet? Add its address in **Add a PC → PC on a different network?** (stored in `/opt/ai/workers.txt`).

## Removing a PC

* Temporarily: set it to **Off** in the GPUs tab, or just turn the PC off. Models then use the remaining GPUs.
* For good: Windows: delete the task **AI GPU Worker** in Task Scheduler, then the folder `C:\ai-worker`. Linux: `sudo systemctl disable --now ai-gpu-worker`.

## Tips

* A wired connection matters: while a model loads, its data travels over the network to the GPU PCs. 2.5 Gbit loads a 12 GB model in under a minute; Wi-Fi can take ten times longer.
* Gaming on a GPU PC while a pooled model is loaded shares that GPU's memory. Set it to **Off** or **Standby** while playing.
* After updating the server (new llama.cpp version), run the PC installer again so the versions match.
