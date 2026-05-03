param(
    [string]$BaseDir = "C:\Users\fotis\Documents\Tailscale-Personal",
    [string]$RepoZipUrl = "https://github.com/flymperis/Vibe-Budgeting/archive/refs/heads/main.zip",
    [string]$ServiceName = "vibe-budgeting"
)

$ErrorActionPreference = "Stop"

Write-Host "==> Updating $ServiceName in $BaseDir"

if (-not (Test-Path $BaseDir)) {
    throw "Base directory not found: $BaseDir"
}

Set-Location $BaseDir

$zipPath = Join-Path $BaseDir "Vibe-Budgeting.zip"
$extractPath = Join-Path $BaseDir "Vibe-Budgeting-main"
$targetPath = Join-Path $BaseDir "Vibe-Budgeting"

Write-Host "==> Downloading latest source zip..."
Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zipPath

if (Test-Path $extractPath) {
    Remove-Item -Recurse -Force $extractPath
}

Write-Host "==> Extracting zip..."
Expand-Archive -Path $zipPath -DestinationPath $BaseDir -Force

if (Test-Path $targetPath) {
    Write-Host "==> Removing old Vibe-Budgeting folder..."
    Remove-Item -Recurse -Force $targetPath
}

Write-Host "==> Replacing with latest Vibe-Budgeting..."
Rename-Item -Path $extractPath -NewName "Vibe-Budgeting"

Write-Host "==> Rebuilding only $ServiceName..."
docker compose build --no-cache $ServiceName

Write-Host "==> Restarting only $ServiceName..."
docker compose up -d $ServiceName

Write-Host "==> Last logs for ${ServiceName}:"
docker compose logs --tail=50 $ServiceName

Write-Host "==> Done."
