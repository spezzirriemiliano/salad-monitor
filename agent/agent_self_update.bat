@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$env:BAT_PATH = '%~f0';" ^
    "$content = Get-Content '%~f0' -Raw -Encoding UTF8;" ^
    "$ps = ($content -split '(?m)^#PS_START\r?\n', 2)[1];" ^
    "Invoke-Expression $ps"
exit /b

#PS_START
# ============================================================
#  Salad Monitor — Agent Self-Updater
# ============================================================

$ApiBase   = "https://api.github.com/repos/spezzirriemiliano/salad-monitor"
$RepoPath  = "agent"
$ScriptDir = Split-Path $env:BAT_PATH

# Files and folders to never overwrite / scan
$SkipFiles = @("salad_agent_config.json", ".dev.salad_agent_config.json")
$SkipDirs  = @()

Write-Host ""
Write-Host "  Salad Monitor — Agent Updater" -ForegroundColor Cyan
Write-Host ""

# ── Get local version ────────────────────────────────────────
$agentBat = Join-Path $ScriptDir "salad_agent.bat"
if (-not (Test-Path $agentBat)) {
    Write-Host "  Local version not found (salad_agent.bat missing)." -ForegroundColor Yellow
    $localVersion = $null
} else {
    $localContent = Get-Content $agentBat -Raw -Encoding UTF8
    $localVersion = if ($localContent -match '\$Version\s*=\s*"(v[^"]+)"') { $matches[1] } else { $null }
    if (-not $localVersion) {
        Write-Host "  Local version not found (could not read version from salad_agent.bat)." -ForegroundColor Yellow
    }
}

# ── Scan repo recursively via GitHub API ─────────────────────
function Get-RepoFiles($repoPath) {
    $url   = "$script:ApiBase/contents/$repoPath"
    $items = Invoke-RestMethod -Uri $url -UseBasicParsing -ErrorAction Stop
    $result = @()
    foreach ($item in $items) {
        if ($item.type -eq "dir") {
            if ($script:SkipDirs -notcontains $item.name) {
                $result += Get-RepoFiles $item.path
            }
        } elseif ($item.type -eq "file") {
            if ($script:SkipFiles -notcontains $item.name) {
                $result += $item
            }
        }
    }
    return $result
}

Write-Host "  Checking for updates..." -NoNewline
try {
    $allFiles = Get-RepoFiles $RepoPath
} catch {
    Write-Host ""
    Write-Host "[ERROR] Could not reach GitHub: $_" -ForegroundColor Red
    Read-Host "`n  Press Enter to close"
    exit 1
}

# ── Get remote version ───────────────────────────────────────
$remoteAgentBat = $allFiles | Where-Object { $_.name -eq "salad_agent.bat" } | Select-Object -First 1
if (-not $remoteAgentBat) {
    Write-Host ""
    Write-Host "[ERROR] salad_agent.bat not found in remote repository." -ForegroundColor Red
    Read-Host "`n  Press Enter to close"
    exit 1
}
$remoteContent = (Invoke-WebRequest -Uri $remoteAgentBat.download_url -UseBasicParsing).Content
$remoteVersion = if ($remoteContent -match '\$Version\s*=\s*"(v[^"]+)"') { $matches[1] } else { "unknown" }
Write-Host " done." -ForegroundColor Green

if ($localVersion) {
    Write-Host "  Local  version : $localVersion"
}
Write-Host "  Remote version : $remoteVersion"
Write-Host ""

# ── Already up to date? ──────────────────────────────────────
if ($localVersion -and $localVersion -eq $remoteVersion) {
    Write-Host "  Already up to date ($localVersion). No update needed." -ForegroundColor Green
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 0
}

# ── New version available (or local unknown) ─────────────────
if ($localVersion) {
    Write-Host "  New version available: $remoteVersion  (current: $localVersion)" -ForegroundColor Yellow
} else {
    Write-Host "  Remote version: $remoteVersion" -ForegroundColor Yellow
}
Write-Host "  Files to update: $($allFiles.Count)"
$resp = Read-Host "  Download and install update? (y/n)"
if ($resp.Trim().ToLower() -ne "y") {
    Write-Host "  Update cancelled."
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 0
}

# ── Download all scanned files ───────────────────────────────
Write-Host ""
$downloaded = @()
$errors     = 0
foreach ($f in $allFiles) {
    $relativePath = $f.path -replace "^$RepoPath/", ""
    $localPath    = Join-Path $ScriptDir ($relativePath -replace "/", "\")
    $localDir     = Split-Path $localPath

    if (-not (Test-Path $localDir)) {
        New-Item -ItemType Directory -Path $localDir -Force | Out-Null
    }

    try {
        Invoke-WebRequest -Uri $f.download_url -OutFile $localPath -UseBasicParsing -ErrorAction Stop
        $downloaded += $relativePath
    } catch {
        Write-Host "  [FAIL] $relativePath — $_" -ForegroundColor Red
        $errors++
    }
}

# ── Download config if missing ───────────────────────────────
$configLocal = Join-Path $ScriptDir "salad_agent_config.json"
if (-not (Test-Path $configLocal)) {
    Write-Host "  salad_agent_config.json not found locally — downloading template..." -NoNewline
    try {
        $configRemote = $allFiles | Where-Object { $_.name -eq "salad_agent_config.json" } | Select-Object -First 1
        if (-not $configRemote) {
            $configRemote = Invoke-RestMethod -Uri "$ApiBase/contents/agent/salad_agent_config.json" -UseBasicParsing -ErrorAction Stop
        }
        Invoke-WebRequest -Uri $configRemote.download_url -OutFile $configLocal -UseBasicParsing -ErrorAction Stop
        Write-Host " done." -ForegroundColor Green
        $downloaded += "salad_agent_config.json"
    } catch {
        Write-Host " failed: $_" -ForegroundColor Red
        $errors++
    }
}

# ── Summary ──────────────────────────────────────────────────
Write-Host ""
if ($downloaded.Count -gt 0) {
    Write-Host "  Downloaded files:" -ForegroundColor Cyan
    foreach ($file in $downloaded) {
        Write-Host "    - $file"
    }
    Write-Host ""
}

if ($errors -eq 0) {
    Write-Host "  Update complete! Agent is now at $remoteVersion." -ForegroundColor Green
    Write-Host "  Restart the agent for the changes to take effect." -ForegroundColor Cyan
} else {
    Write-Host "  Update finished with $errors error(s). Check the output above." -ForegroundColor Red
}

Write-Host ""
Read-Host "  Press Enter to close"
