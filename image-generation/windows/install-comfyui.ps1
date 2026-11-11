# Installs ComfyUI (image generation, Z-Image Turbo model) on this Windows PC's NVIDIA GPU (8 GB+ VRAM recommended).
# Open WebUI on the AI server then sends image requests to it. Run in PowerShell as Administrator. Safe to re-run.
#   .\install-comfyui.ps1 -Server <AI-server-IP> [-Dir C:\AI]
param([Parameter(Mandatory = $true)][string]$Server, [string]$Dir = "C:\AI")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Please run as Administrator." }

New-Item -ItemType Directory -Force $Dir | Out-Null
$root = Join-Path $Dir "ComfyUI_windows_portable"
$archive = Join-Path $Dir "ComfyUI_windows_portable_nvidia.7z"

# ---------- 1. ComfyUI portable ----------
if (-not (Test-Path "$root\python_embeded\python.exe")) {
  Write-Output "== Downloading ComfyUI portable (about 2 GB)..."
  & curl.exe -fsSL --retry 5 -C - -o $archive "https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z"
  if ($LASTEXITCODE -ne 0) { throw "ComfyUI download failed" }
  Write-Output "== Extracting (a few minutes)..."
  cmd /c "tar.exe -xf `"$archive`" -C `"$Dir`" 2>nul"
  if (-not (Test-Path "$root\python_embeded\python.exe")) {
    $7z = "C:\Program Files\7-Zip\7z.exe"; $rar = "C:\Program Files\WinRAR\WinRAR.exe"
    if (Test-Path $7z) { Start-Process $7z -ArgumentList "x `"$archive`" -o`"$Dir`" -y" -Wait -WindowStyle Hidden }
    elseif (Test-Path $rar) { Start-Process $rar -ArgumentList "x -ibck -y `"$archive`" `"$Dir\`"" -Wait }
  }
  if (-not (Test-Path "$root\python_embeded\python.exe")) { throw "Could not extract ComfyUI" }
  Remove-Item $archive -Force
}
Write-Output "== ComfyUI installed at $root"

# ---------- 2. Background start at login (hidden), frees GPU memory after each job ----------
$runner = Join-Path $root "run-background.cmd"
@"
@echo off
cd /d "%~dp0"
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --listen 0.0.0.0 --port 8188 --disable-smart-memory --disable-auto-launch --reserve-vram 0.6 > comfyui.log 2>&1
"@ | Set-Content -Path $runner -Encoding ASCII
$user = "$env:USERDOMAIN\$env:USERNAME"
$action    = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless cmd.exe /c `"$runner`"" -WorkingDirectory $root
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ComfyUI (AI images)" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

# ---------- 3. Firewall: only this PC and the AI server (for Open WebUI) ----------
$py = "$root\python_embeded\python.exe"
Get-NetFirewallRule -DisplayName "ComfyUI (AI images)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "ComfyUI (AI images)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8188 -RemoteAddress $Server -Program $py -Profile Any | Out-Null
Write-Output "== Firewall: port 8188 open only for $Server (and this PC)"

# ---------- 4. Desktop shortcuts ----------
$desk = [Environment]::GetFolderPath('Desktop')
Remove-Item (Join-Path $desk "AI Images & Video (ComfyUI).url") -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $desk "AI Images (ComfyUI).url") -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8188/" -Encoding ASCII
@"
@echo off
rem Unloads the text AI models so this GPU has all its memory for images.
curl.exe -s -m 10 http://${Server}:8080/unload >nul 2>&1
curl.exe -s -m 10 http://${Server}:8080/models/unload >nul 2>&1
echo Text AI unloaded - the GPU is free for images. (It reloads by itself when you chat again.)
timeout /t 5
"@ | Set-Content -Path (Join-Path $desk "Free GPU for images.bat") -Encoding ASCII
Write-Output "== Desktop shortcuts created"

Start-ScheduledTask -TaskName "ComfyUI (AI images)"
Write-Output "== ComfyUI started in the background (http://127.0.0.1:8188)"

# ---------- 5. Models (resumable downloads) ----------
$m = "$root\ComfyUI\models"
$files = @(
  @("diffusion_models", "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors"),
  @("text_encoders",    "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors"),
  @("vae",              "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors")
)
foreach ($f in $files) {
  $dest = Join-Path (Join-Path $m $f[0]) (Split-Path $f[1] -Leaf)
  $part = "$dest.part"
  if ((Test-Path $dest) -and -not (Test-Path $part)) { Write-Output "[have] $(Split-Path $dest -Leaf)"; continue }
  New-Item -ItemType File -Force $part | Out-Null
  Write-Output "[download] $(Split-Path $dest -Leaf) ..."
  & curl.exe -fsSL --retry 10 --retry-delay 5 -C - -o $dest $f[1]
  if ($LASTEXITCODE -eq 0) { Remove-Item $part -Force; Write-Output ("[done] {0} ({1:N1} GB)" -f (Split-Path $dest -Leaf), ((Get-Item $dest).Length/1GB)) }
  else { Write-Output "[FAILED] $(Split-Path $dest -Leaf) - run this step again to resume" }
}
$ok = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8188/ -TimeoutSec 10 -ErrorAction SilentlyContinue).StatusCode
Write-Output "== ComfyUI web page status: $ok"
Write-Output ""
Write-Output "Next: in Open WebUI (as admin) > Admin Panel > Settings > Images:"
Write-Output "  Image Generation Engine: ComfyUI, Base URL: http://$((Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -in 'Dhcp','Manual' -and $_.IPAddress -notlike '169.*' } | Select-Object -First 1).IPAddress):8188"
Write-Output "  Workflow: paste image-generation/openwebui-zimage-workflow.json from the repository (see docs/image-generation.md)"
Write-Output "COMFYUI-DONE"
