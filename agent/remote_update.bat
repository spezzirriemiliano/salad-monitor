@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$env:BAT_PATH = '%~f0';" ^
    "$content = Get-Content '%~f0' -Raw -Encoding UTF8;" ^
    "$ps = ($content -split '(?m)^#PS_START\r?\n', 2)[1];" ^
    "Invoke-Expression $ps"
exit /b

#PS_START
# ============================================================
#  Salad Monitor — Remote Agent Updater (no prompts)
# ============================================================

$ScriptDir    = Split-Path $env:BAT_PATH
$selfUpdateBat = Join-Path $ScriptDir "agent_self_update.bat"
$agentBat      = Join-Path $ScriptDir "salad_agent.bat"

Write-Host ""
Write-Host "  Salad Monitor — Remote Agent Update" -ForegroundColor Cyan
Write-Host ""

# Wait for the agent process to die
Write-Host "  Waiting for agent to stop..." -NoNewline
Start-Sleep -Seconds 4
Write-Host " done." -ForegroundColor Green

# Run self-update in auto mode (no prompts, no y/n)
if (Test-Path $selfUpdateBat) {
    $env:AGENT_AUTO_UPDATE = "1"
    & cmd.exe /c "`"$selfUpdateBat`""
    $env:AGENT_AUTO_UPDATE = ""
} else {
    Write-Host "  [ERROR] agent_self_update.bat not found." -ForegroundColor Red
    exit 1
}

# Relaunch the agent
Write-Host ""
if (Test-Path $agentBat) {
    Write-Host "  Relaunching agent..." -ForegroundColor Cyan
    Start-Process "cmd.exe" -ArgumentList "/c `"$agentBat`"" -WindowStyle Normal
    Write-Host "  Agent relaunched." -ForegroundColor Green
} else {
    Write-Host "  [ERROR] salad_agent.bat not found — cannot relaunch." -ForegroundColor Red
}

Write-Host ""
