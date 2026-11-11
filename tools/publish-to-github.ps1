# Publishes this folder as a new GitHub repository (or pushes new changes to it).
# Run from the repository folder in PowerShell (VS Code terminal or Claude Code):
#   powershell -ExecutionPolicy Bypass -File tools\publish-to-github.ps1 [-Name home-ai-cluster] [-Private]
# Needs: Git and GitHub CLI (installed with winget if missing) and a GitHub login (gh auth login - you do this yourself).
param([string]$Name = "home-ai-cluster", [switch]$Private,
      [string]$Description = "Private home AI server: pool the GPUs of several PCs, chat for the whole family, live health page.")
$ErrorActionPreference = "Continue"   # git/gh print normal messages on stderr; we check exit codes instead
Set-Location (Split-Path -Parent $PSScriptRoot)

function Need($cmd, $id) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    Write-Output "== Installing $id ..."
    winget install --id $id -e --accept-source-agreements --accept-package-agreements | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd is not available - install $id and open a new terminal." }
  }
}
Need git "Git.Git"
Need gh "GitHub.cli"

cmd /c "gh auth status >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
  Write-Output "You are not logged in to GitHub. In this terminal run:  gh auth login"
  Write-Output "(choose GitHub.com, HTTPS, and log in with the browser), then run this script again."
  exit 1
}
$login = "$(gh api user --jq .login)".Trim()
$uid   = "$(gh api user --jq .id)".Trim()
if (-not $login) { Write-Output "Could not read your GitHub account (gh api user failed)."; exit 1 }
Write-Output "== GitHub account: $login"

# --- repository + author (GitHub's private no-reply address, so your real e-mail is not published)
if (-not (Test-Path .git)) { git init -b main | Out-Null }
git config core.autocrlf false
if (-not (git config user.name))  { git config user.name $login }
git config user.email "$uid+$login@users.noreply.github.com"   # always (this repo only) - hides your real e-mail

# --- fill in the clone address in the README
$readme = [IO.File]::ReadAllText((Resolve-Path README.md), [Text.Encoding]::UTF8)   # not Get-Content: PowerShell 5.1 reads it as ANSI and breaks emoji
if ($readme -match "YOUR-USERNAME") {
  [IO.File]::WriteAllText((Resolve-Path README.md), $readme.Replace("YOUR-USERNAME/home-ai-cluster", "$login/$Name"), (New-Object Text.UTF8Encoding $false))
  Write-Output "== README: clone address set to github.com/$login/$Name"
}

# --- commit (Linux scripts marked executable, line endings normalised by .gitattributes)
git add -A
git add --renormalize . 2>$null
$exec = @("install.sh", "uninstall.sh", "update-open-webui.sh") +
        (git ls-files "server/steps/*.sh" "server/files/*" "worker/linux/*.sh" "tools/*.sh")
foreach ($f in $exec) { if ($f) { git update-index --chmod=+x -- $f 2>$null } }
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
  git rev-parse --verify HEAD 2>$null | Out-Null
  $msg = if ($LASTEXITCODE -eq 0) { "Update" } else { "Initial release" }
  git commit -q -m $msg
  if ($LASTEXITCODE -ne 0) { Write-Output "Commit failed (see above)."; exit 1 }
  Write-Output "== Committed: $msg"
} else { Write-Output "== Nothing new to commit" }

# --- create the GitHub repository the first time, then push
$remote = git remote 2>$null
if (-not ($remote -contains "origin")) {
  $vis = if ($Private) { "--private" } else { "--public" }
  gh repo create $Name $vis --source . --remote origin --description $Description --push
} else {
  git push -u origin main
}
if ($LASTEXITCODE -ne 0) { Write-Output "Upload failed (see the message above)."; exit 1 }
Write-Output ""
Write-Output "Done: https://github.com/$login/$Name"
