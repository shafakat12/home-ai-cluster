# Turns this Windows PC into a GPU worker for the home AI server (llama.cpp rpc-server).
# Run as Administrator. Safe to run again (it updates/reinstalls).
param(
  [string]$Server = "__SERVER_IP__",
  [string]$Tag = "",
  [int]$Port = 50052,
  [string]$InstallDir = "C:\ai-worker",
  [switch]$Cache   # keep a local copy of model data for faster loading (off by default)
)
$ErrorActionPreference = "Stop"
if ($Server -like "__*") { throw "No server address. Run add-this-pc.bat from http://YOUR-AI-SERVER:8090/ or pass -Server <ip>." }
$ProgressPreference = "SilentlyContinue"
$TaskName = "AI GPU Worker"
$RuleName = "AI GPU Worker (llama.cpp RPC)"

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Please run as Administrator." }

if (-not $Tag) { $Tag = (Invoke-RestMethod "http://${Server}:8090/llama-version.txt").Trim() }
Write-Output "== llama.cpp version (must match server): $Tag"

# --- pick the best build for this PC's GPU ---
$flavor = "vulkan"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  $drv = (& nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | Select-Object -First 1)
  Write-Output "== GPU: $drv"
  $major = [int](($drv.Split(',')[1]).Trim().Split('.')[0])
  if ($major -ge 580) { $flavor = "cuda13" } elseif ($major -ge 551) { $flavor = "cuda12" }
}
Write-Output "== Build: $flavor"

$rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/$Tag" -Headers @{ "User-Agent" = "home-ai-cluster" }
function Get-Asset($pattern) { $rel.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1 }
switch ($flavor) {
  "cuda13" { $assets = @((Get-Asset '^llama-.*-bin-win-cuda-13[\d.]*-x64\.zip$'), (Get-Asset '^cudart-llama-bin-win-cuda-13[\d.]*-x64\.zip$')) }
  "cuda12" { $assets = @((Get-Asset '^llama-.*-bin-win-cuda-12[\d.]*-x64\.zip$'), (Get-Asset '^cudart-llama-bin-win-cuda-12[\d.]*-x64\.zip$')) }
  default  { $assets = @((Get-Asset '^llama-.*-bin-win-vulkan-x64\.zip$')) }
}
if ($assets -contains $null) { throw "Could not find the right download for $flavor in release $Tag" }

# --- stop old worker ---
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
Get-Process rpc-server, ggml-rpc-server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 2

# --- download + extract ---
$dir = Join-Path $InstallDir "llama.cpp\$Tag"
New-Item -ItemType Directory -Force $dir | Out-Null
foreach ($a in $assets) {
  $zip = Join-Path $InstallDir $a.name
  if (-not (Test-Path $zip)) {
    Write-Output "== Downloading $($a.name) ($([math]::Round($a.size/1MB)) MB)"
    & curl.exe -fsSL --retry 3 -o $zip $a.browser_download_url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $($a.name)" }
  }
  Expand-Archive -Path $zip -DestinationPath $dir -Force
}
$exe = Get-ChildItem $dir -Recurse -Include ggml-rpc-server.exe, rpc-server.exe | Select-Object -First 1
if (-not $exe) { throw "rpc-server program not found after extracting" }
$help = (cmd /c "`"$($exe.FullName)`" --help 2>&1" | Out-String)
$cacheFlag = ""; if ($Cache -and $help -match '--cache') { $cacheFlag = "-c" }
Write-Output "== Worker program: $($exe.Name)  (cache flag: '$cacheFlag')"
$bin = $exe.DirectoryName
Write-Output "== Installed to $bin"
Get-ChildItem (Join-Path $InstallDir "*.zip") | Remove-Item -Force

# --- firewall: only the AI server may connect ---
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
  -RemoteAddress $Server -Program $exe.FullName -Profile Any | Out-Null
Write-Output "== Firewall: port $Port open ONLY for $Server"

# --- background task: starts with Windows, restarts if it crashes ---
$log = Join-Path $InstallDir "rpc-server.log"
$cache = Join-Path $InstallDir "cache"
New-Item -ItemType Directory -Force $cache | Out-Null
$runner = Join-Path $InstallDir "run-worker.cmd"
@"
@echo off
set LLAMA_CACHE=$cache
"$($exe.FullName)" --host 0.0.0.0 --port $Port $cacheFlag > "$log" 2>&1
"@ | Set-Content -Path $runner -Encoding ASCII

$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`"" -WorkingDirectory $bin
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep 6

# --- GPU use report for the server's auto-off (port 50053, only the AI server may ask) ---
$StatusTask = "AI GPU Status"; $StatusRule = "AI GPU Status (use report for the AI server)"; $StatusPort = 50053
$status = Join-Path $InstallDir "gpu-status.ps1"
if (Get-ScheduledTask -TaskName $StatusTask -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $StatusTask -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '[\\"]gpu-status\.ps1' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Invoke-WebRequest "http://${Server}:8090/gpu-status.ps1" -OutFile $status -UseBasicParsing
Get-NetFirewallRule -DisplayName $StatusRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $StatusRule -Direction Inbound -Action Allow -Protocol TCP -LocalPort $StatusPort -RemoteAddress $Server -Profile Any | Out-Null
$sAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$status`" -Server $Server -Port $StatusPort"
Register-ScheduledTask -TaskName $StatusTask -Action $sAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $StatusTask
Write-Output "== GPU use report on port $StatusPort (only $Server may ask) - lets the server switch this GPU off while you use it"

$ok = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
Write-Output "== Worker listening on port ${Port}: $ok"
if (Test-Path $log) { Write-Output "== Worker log:"; Get-Content $log -Tail 20 }
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -in 'Dhcp','Manual' -and $_.IPAddress -notlike '169.*' } | Select-Object -First 1).IPAddress
Write-Output "== This PC's IP: $ip"
try { $r = Invoke-RestMethod "http://127.0.0.1:$StatusPort/" -TimeoutSec 10; Write-Output "== GPU use report works: this PC's own programs use $($r.pc_pct)% of the GPU" }
catch { Write-Output "== GPU use report did not answer yet (auto-off will not work for this PC until it does)" }
if ($ok) { Write-Output "WORKER-OK" } else { Write-Output "WORKER-FAILED" }
