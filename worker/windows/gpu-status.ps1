# Home AI Cluster - GPU use report for the AI server (runs on each Windows GPU PC).
# Answers on port 50053, only to the AI server, with how busy this PC's GPU is from its OWN programs
# (games, video, ...). The AI worker (rpc-server) and ComfyUI are left out, so the server can switch this GPU
# to "Off" while you use the PC and back on when you stop. Nothing else about the PC is shared.
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [int]$Port = 50053
)
$ErrorActionPreference = "Continue"

function Get-GpuUse {
  # processes that work FOR the AI server
  $ai = @{}
  Get-Process ggml-rpc-server, rpc-server -ErrorAction SilentlyContinue | ForEach-Object { $ai[[int]$_.Id] = $true }
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'ComfyUI' } | ForEach-Object { $ai[[int]$_.ProcessId] = $true }

  # per-process GPU engine use (Task Manager's numbers); class names are the same on every Windows language
  $own = @{}; $aiUse = @{}; $byPid = @{}
  foreach ($e in Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine -ErrorAction SilentlyContinue) {
    if ($e.Name -notmatch '^pid_(\d+)_.*_engtype_(.+)$') { continue }
    $procId = [int]$Matches[1]; $type = $Matches[2]; $u = [double]$e.UtilizationPercentage
    if ($u -le 0) { continue }
    if ($ai.ContainsKey($procId)) { $aiUse[$type] = [double]$aiUse[$type] + $u }
    else { $own[$type] = [double]$own[$type] + $u; $byPid[$procId] = [double]$byPid[$procId] + $u }
  }
  # like Task Manager: the busiest engine type counts
  $pc = 0; foreach ($v in $own.Values) { if ($v -gt $pc) { $pc = $v } }
  $aiPct = 0; foreach ($v in $aiUse.Values) { if ($v -gt $aiPct) { $aiPct = $v } }
  $top = ""
  if ($byPid.Count -gt 0) {
    $topPid = ($byPid.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1).Key
    $p = Get-Process -Id $topPid -ErrorAction SilentlyContinue
    if ($p -and $byPid[$topPid] -ge 5) { $top = "$($p.ProcessName).exe" }
  }
  [ordered]@{ pc_pct = [math]::Min(100, [math]::Round($pc)); ai_pct = [math]::Min(100, [math]::Round($aiPct)); top = $top;
              time = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") }
}

$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://+:$Port/")
$listener.Start()
while ($listener.IsListening) {
  $ctx = $listener.GetContext()
  try {
    $from = $ctx.Request.RemoteEndPoint.Address.ToString()
    if ($from -ne $Server -and $from -ne "127.0.0.1" -and $from -ne "::1") {
      $ctx.Response.StatusCode = 403; $body = '{"error":"only the AI server may ask"}'
    } else {
      $body = Get-GpuUse | ConvertTo-Json -Compress
    }
  } catch {
    $ctx.Response.StatusCode = 500; $body = '{"error":"could not read GPU use"}'
  }
  $bytes = [Text.Encoding]::UTF8.GetBytes($body)
  $ctx.Response.ContentType = "application/json"
  $ctx.Response.ContentLength64 = $bytes.Length
  try { $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length) } catch {}
  $ctx.Response.Close()
}
