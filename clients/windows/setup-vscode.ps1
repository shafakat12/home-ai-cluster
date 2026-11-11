# Sets up this Windows PC to use the Home AI server for coding: a "claude-local" launcher for Claude Code,
# the Cline extension for VS Code, and a desktop shortcut to the chat website.
# Usage (PowerShell):  .\setup-vscode.ps1 -Server 192.168.1.50 [-Model gpt-oss-20b]
# The server must allow this PC's IP (ADMIN_PC_IP in config.env, or control panel > Settings > devices).
param([Parameter(Mandatory = $true)][string]$Server, [string]$Model = "gpt-oss-20b")
$ErrorActionPreference = "Continue"
$desk = [Environment]::GetFolderPath('Desktop')

Write-Output "== Checks"
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
$code   = (Get-Command code -ErrorAction SilentlyContinue).Source
Write-Output "Claude Code CLI : $(if ($claude) { $claude } else { 'NOT installed' })"
Write-Output "VS Code         : $(if ($code) { $code } else { 'NOT found' })"
try { $m = Invoke-RestMethod "http://${Server}:8080/v1/models" -TimeoutSec 10; Write-Output "AI API reachable: $($m.data.id -join ', ')" }
catch { Write-Output "AI API NOT reachable from this PC: $_" }

Write-Output "== Claude Code launcher (uses the home AI server; your normal Claude Code is untouched)"
$launcher = @"
@echo off
rem Starts Claude Code using the HOME AI SERVER instead of Anthropic's cloud.
rem Usage: open a terminal in your project folder and run:  claude-local
set ANTHROPIC_BASE_URL=http://${Server}:8080
set ANTHROPIC_AUTH_TOKEN=local
set ANTHROPIC_API_KEY=
set ANTHROPIC_MODEL=$Model
set ANTHROPIC_DEFAULT_OPUS_MODEL=$Model
set ANTHROPIC_DEFAULT_SONNET_MODEL=$Model
set ANTHROPIC_DEFAULT_HAIKU_MODEL=$Model
set CLAUDE_CODE_SUBAGENT_MODEL=$Model
set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
claude %*
"@
$binDir = Join-Path $env:USERPROFILE "home-ai-bin"
New-Item -ItemType Directory -Force $binDir | Out-Null
Set-Content -Path (Join-Path $binDir "claude-local.bat") -Value $launcher -Encoding ASCII
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
  Write-Output "Added $binDir to your PATH (open a NEW terminal to use 'claude-local')"
}
Write-Output "Created: $binDir\claude-local.bat"

Write-Output "== Desktop shortcut"
Set-Content -Path (Join-Path $desk "Home AI.url") -Value "[InternetShortcut]`r`nURL=http://${Server}:3000/" -Encoding ASCII
Write-Output "Created: $desk\Home AI.url"

if ($code) {
  Write-Output "== VS Code extensions"
  $have = & code --list-extensions 2>$null
  Write-Output ("Installed now: " + (($have | Where-Object { $_ -match 'claude|cline|continue|roo' }) -join ', '))
  if ($have -notcontains "saoudrizwan.claude-dev") {
    & code --install-extension saoudrizwan.claude-dev 2>&1 | Select-Object -Last 2
  } else { Write-Output "Cline already installed" }
}
Write-Output "CLIENTS-OK"
