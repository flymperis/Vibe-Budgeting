param(
    [string]$BaseDir = "C:\Users\fotis\Documents\Tailscale-Personal",
    [string]$RepoZipUrl = "https://github.com/flymperis/Vibe-Budgeting/archive/refs/heads/main.zip",
    [string]$CodeloadZipUrl = "https://codeload.github.com/flymperis/Vibe-Budgeting/zip/refs/heads/main",
    [string]$ServiceName = "vibe-budgeting",
    [int]$MaxAttempts = 5,
    [int]$TimeoutSec = 600
)

$ErrorActionPreference = "Stop"

# GitHub requires TLS 1.2+ on Windows PowerShell 5.x (.NET Framework)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ZipHeaders = @{
    "User-Agent"      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    "Accept"        = "application/zip, application/octet-stream, */*"
}

function Test-ZipFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 4) { return $false }
    # ZIP local file header magic "PK`003`004"
    return ($bytes[0] -eq 0x50 -and $bytes[1] -eq 0x4B -and $bytes[2] -eq 0x03 -and $bytes[3] -eq 0x04)
}

function Save-ZipFromGitHub {
    param(
        [string]$Uri,
        [string]$OutPath,
        [hashtable]$Headers,
        [int]$Timeout
    )
    $progressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -Uri $Uri -OutFile $OutPath -UseBasicParsing -TimeoutSec $Timeout -MaximumRedirection 10 -Headers $Headers
    } finally {
        $progressPreference = "Continue"
    }
}

function Save-ZipWithCurl {
    param([string]$Uri, [string]$OutPath, [int]$Timeout)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { return $false }
    & curl.exe --silent --show-error --location `
        --connect-timeout 60 --max-time $Timeout `
        -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120" `
        -o $OutPath $Uri
    if ($LASTEXITCODE -ne 0) { return $false }
    return $true
}

Write-Host "==> Updating $ServiceName in $BaseDir"

if (-not (Test-Path $BaseDir)) {
    throw "Base directory not found: $BaseDir"
}

Set-Location $BaseDir

$zipPath = Join-Path $BaseDir "Vibe-Budgeting.zip"
$extractPath = Join-Path $BaseDir "Vibe-Budgeting-main"
$targetPath = Join-Path $BaseDir "Vibe-Budgeting"

$urls = @($RepoZipUrl, $CodeloadZipUrl)

Write-Host "==> Downloading latest source zip (timeout ${TimeoutSec}s, up to $MaxAttempts attempts per URL)..."

$downloaded = $false
foreach ($url in $urls) {
    for ($a = 1; $a -le $MaxAttempts; $a++) {
        Write-Host "    Try $a/$MaxAttempts : $url"
        try {
            if (Test-Path $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
        } catch {}

        $ok = $false
        try {
            Save-ZipFromGitHub -Uri $url -OutPath $zipPath -Headers $ZipHeaders -Timeout $TimeoutSec
            if (Test-ZipFile -Path $zipPath) { $ok = $true }
        } catch {
            Write-Warning "    Invoke-WebRequest failed: $($_.Exception.Message)"
        }

        if (-not $ok -and (Save-ZipWithCurl -Uri $url -OutPath $zipPath -Timeout $TimeoutSec)) {
            if (Test-ZipFile -Path $zipPath) { $ok = $true }
        }

        if (-not $ok -and (Test-Path $zipPath)) {
            try {
                $fs = [System.IO.File]::OpenRead($zipPath)
                $buf = New-Object byte[] 128
                $n = $fs.Read($buf, 0, 128)
                $fs.Close()
                $head = [System.Text.Encoding]::ASCII.GetString($buf, 0, $n)
                if ($head -match "<!DOCTYPE|<html") {
                    Write-Warning "    Response looks like HTML (not a zip). GitHub may be slow or blocking; retrying..."
                }
            } catch {}
            Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
        }

        if ($ok) {
            $downloaded = $true
            break
        }

        $delaySec = [Math]::Min(60, 5 * $a)
        Write-Host "    Waiting ${delaySec}s before retry..."
        Start-Sleep -Seconds $delaySec
    }
    if ($downloaded) { break }
}

if (-not $downloaded) {
    throw @"
Zip download failed after retries.

Hints:
  - Check https://www.githubstatus.com and your internet / firewall / VPN (Tailscale).
  - Clone once with git instead of zip: git clone https://github.com/flymperis/Vibe-Budgeting.git then pull updates.
  - Run this script again later if GitHub had a transient outage.
"@
}

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
