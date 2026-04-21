@echo off
:: ============================================================
::  Salad Monitor Agent — no Python required
::  Uses PowerShell (included in Windows) + nvidia-smi (NVIDIA drivers)
::
::  Usage:
::    salad_agent.bat            -> start the agent
::    salad_agent.bat -Install   -> register scheduled task and start
::    salad_agent.bat -Uninstall -> remove the scheduled task
:: ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$env:BAT_PATH = '%~f0';" ^
    "$content = Get-Content '%~f0' -Raw -Encoding UTF8;" ^
    "$ps = ($content -split '(?m)^#PS_START\r?\n', 2)[1];" ^
    "Invoke-Expression $ps" -- %*
exit /b

#PS_START
# ============================================================
#  PowerShell agent — everything above is just the launcher
# ============================================================

param([switch]$Install, [switch]$Uninstall)

$ScriptPath  = $env:BAT_PATH
$ScriptDir   = Split-Path $ScriptPath
$DevConfigPath = Join-Path $ScriptDir ".dev.salad_agent_config.json"
$ConfigPath    = if (Test-Path $DevConfigPath) { $DevConfigPath } else { Join-Path $ScriptDir "salad_agent_config.json" }
$TaskName    = "SaladMonitorAgent"

$LhmDir     = Join-Path $ScriptDir "lhm"
$LhmDllPath = Join-Path $LhmDir "LibreHardwareMonitorLib.dll"

# ── Install / uninstall scheduled task ──────────────────────

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[OK] Task '$TaskName' removed."
    exit 0
}

if ($Install) {
    if (-not (Test-Path $ConfigPath)) {
        Write-Host "[ERROR] salad_agent_config.json not found."
        Write-Host "        Copy salad_agent_config.example.json and edit it."
        pause; exit 1
    }
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" `
                   -Argument "/c `"$ScriptPath`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
                    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "[OK] Task '$TaskName' created. It will run automatically on Windows startup."
    Write-Host "[  ] Starting agent now..."
    Start-ScheduledTask -TaskName $TaskName
    exit 0
}

# ── Load configuration ───────────────────────────────────────

if (-not (Test-Path $ConfigPath)) {
    Write-Host "[ERROR] salad_agent_config.json not found in $ScriptDir"
    Write-Host "        Copy salad_agent_config.example.json and edit it."
    exit 1
}

$cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$MachineId       = if ($cfg.machine_id) { $cfg.machine_id } else { $env:COMPUTERNAME }
$ServerUrl       = $cfg.server_url.TrimEnd("/")
$ApiKey          = $cfg.api_key
$IntervalSeconds = if ($cfg.interval_seconds) { [int][Math]::Min([long]$cfg.interval_seconds, [int]::MaxValue) } else { 60 }
$CachedSaladMachineId = $cfg.salad_machine_id
$CommandPort     = if ($cfg.command_port) { [int]$cfg.command_port } else { 8765 }
$CommandToken    = $ApiKey

$Version = "v0.7"
Write-Host "[INFO] Salad Monitor Agent $Version"
Write-Host "[INFO] Machine ID : $MachineId"
Write-Host "[INFO] Server     : $ServerUrl"
Write-Host "[INFO] Interval   : ${IntervalSeconds}s"

# ── Version check ────────────────────────────────────────────
try {
    $rawUrl = "https://raw.githubusercontent.com/spezzirriemiliano/salad-monitor/main/agent/salad_agent.bat"
    $remote = (Invoke-WebRequest $rawUrl -UseBasicParsing -TimeoutSec 8).Content
    $remoteVersion = if ($remote -match '\$Version\s*=\s*"(v[^"]+)"') { $Matches[1] } else { $null }
    if ($remoteVersion -and $remoteVersion -ne $Version) {
        Write-Host "[UPDATE] New version available: $remoteVersion — run agent_self_update.bat to update." -ForegroundColor Green
    }
} catch { }

# ── Salad process names ──────────────────────────────────────

$SaladProcessNames = @("salad", "saladcloud", "salad-client")

# ── Throttle constants (NVML bitmask) ───────────────────────

$ThrottleMap = [ordered]@{
    power_cap       = 0x04
    hw_thermal      = 0x40
    sw_thermal      = 0x20
    hw_slowdown     = 0x08
    hw_power_brake  = 0x80
    app_clocks      = 0x02
    sync_boost      = 0x10
}

# ── Helpers ─────────────────────────────────────────────────

function NvVal($raw) {
    $v = "$raw".Trim()
    if ($v -match '^\[?N/A\]?$' -or $v -eq "") { return $null }
    return $v
}

function NvInt($raw) {
    $v = NvVal $raw
    if ($null -eq $v) { return $null }
    $n = [long]0
    if ([long]::TryParse($v, [ref]$n)) { return $n }
    return $null
}

function NvFloat($raw) {
    $v = NvVal $raw
    if ($null -eq $v) { return $null }
    $n = 0.0
    if ([double]::TryParse($v, [Globalization.NumberStyles]::Any,
            [Globalization.CultureInfo]::InvariantCulture, [ref]$n)) {
        return [Math]::Round($n, 1)
    }
    return $null
}

function Get-ThrottleReasons($hexStr) {
    $v = NvVal $hexStr
    if ($null -eq $v) { return @() }
    try {
        $mask = [long]([convert]::ToInt64($v.Replace("0x","").Replace("0X",""), 16))
    } catch { return @() }
    $reasons = [System.Collections.ArrayList]@()
    foreach ($kv in $ThrottleMap.GetEnumerator()) {
        if ($mask -band $kv.Value) { [void]$reasons.Add($kv.Key) }
    }
    return ,$reasons
}

function Is-SaladRunning {
    foreach ($name in $SaladProcessNames) {
        if (Get-Process -Name $name -ErrorAction SilentlyContinue) { return $true }
    }
    return $false
}

function Get-ActiveAdapterName {
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
             Sort-Object RouteMetric | Select-Object -First 1
    if (-not $route) { return $null }
    $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
               Where-Object { $_.Status -eq "Up" } | Select-Object -First 1
    return $adapter.Name
}

function Get-BandwidthMetrics {
    param($PrevSample)
    $result = @{ upload_mbps = $null; download_mbps = $null
                 delta_uploaded_mb = $null; delta_downloaded_mb = $null
                 current_sample = $null }

    $adapterName = Get-ActiveAdapterName
    if (-not $adapterName) { return $result }

    $stats = Get-NetAdapterStatistics -Name $adapterName -ErrorAction SilentlyContinue
    if (-not $stats) { return $result }

    $now     = Get-Date
    $current = @{ AdapterName = $adapterName
                  SentBytes     = $stats.SentBytes
                  ReceivedBytes = $stats.ReceivedBytes
                  Time          = $now }
    $result.current_sample = $current

    if ($PrevSample -and $PrevSample.AdapterName -eq $adapterName) {
        $elapsed = ($now - $PrevSample.Time).TotalSeconds
        if ($elapsed -gt 1) {
            $sentDelta = [Math]::Max([long]0, $stats.SentBytes     - $PrevSample.SentBytes)
            $recvDelta = [Math]::Max([long]0, $stats.ReceivedBytes - $PrevSample.ReceivedBytes)
            $result.upload_mbps         = [Math]::Round($sentDelta * 8 / $elapsed / 1MB, 2)
            $result.download_mbps       = [Math]::Round($recvDelta * 8 / $elapsed / 1MB, 2)
            $result.delta_uploaded_mb   = [Math]::Round($sentDelta / 1MB, 3)
            $result.delta_downloaded_mb = [Math]::Round($recvDelta / 1MB, 3)
        }
    }
    return $result
}

function Get-GpuMetrics {
    $gpus = [System.Collections.ArrayList]@()

    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { return $gpus }

    # Single call for all GPU metrics
    $fields = "index,name,utilization.gpu,temperature.gpu,temperature.memory," +
              "memory.used,memory.total,power.draw,power.limit,fan.speed," +
              "pstate,clocks_throttle_reasons.active," +
              "ecc.errors.uncorrected.volatile.total," +
              "clocks.current.sm,clocks.max.sm,clocks.current.memory,clocks.max.memory"

    try { $rows = & nvidia-smi --query-gpu=$fields --format=csv,noheader,nounits 2>$null } catch { return $gpus }
    if (-not $rows) { return $gpus }

    # Single call for all compute processes across all GPUs
    try { $computeProcs = & nvidia-smi --query-compute-apps=gpu_index,process_name --format=csv,noheader 2>$null } catch { $computeProcs = @() }

    # Build map: gpu_index -> [process_names]
    $procsByGpu = @{}
    foreach ($line in $computeProcs) {
        $parts = "$line".Trim() -split ",\s*", 2
        if ($parts.Count -lt 2) { continue }
        $gIdx = $parts[0].Trim()
        $pname = $parts[1].Trim().ToLower()
        if (-not $procsByGpu.ContainsKey($gIdx)) { $procsByGpu[$gIdx] = @() }
        $procsByGpu[$gIdx] += $pname
    }

    foreach ($row in $rows) {
        $c = $row -split ",\s*"
        if ($c.Count -lt 10) { continue }

        $idx        = NvInt   $c[0]
        $gpuName    = NvVal   $c[1]
        $util       = NvInt   $c[2]
        $temp       = NvInt   $c[3]
        $memTemp    = NvInt   $c[4]
        $memUsedMb  = NvInt   $c[5]
        $memTotalMb = NvInt   $c[6]
        $powerW     = NvFloat $c[7]
        $powerLimW  = NvFloat $c[8]
        $fanPct     = NvInt   $c[9]
        $pstateRaw  = NvVal   $c[10]
        $pstate     = if ($pstateRaw -match 'P(\d+)') { [int]$Matches[1] } else { $null }
        $throttle   = Get-ThrottleReasons $c[11]
        $eccErrors  = NvInt   $c[12]
        $clockSm    = NvInt   $c[13]
        $clockSmMax = NvInt   $c[14]
        $clockMem   = NvInt   $c[15]
        $clockMemMax= NvInt   $c[16]

        # Is Salad actually using this GPU? (from the map already built)
        $saladOnGpu = $false
        $procsOnThisGpu = $procsByGpu["$idx"]
        if ($procsOnThisGpu) {
            foreach ($pname in $procsOnThisGpu) {
                foreach ($s in $SaladProcessNames) {
                    if ($pname -like "*$s*") { $saladOnGpu = $true; break }
                }
                if ($saladOnGpu) { break }
            }
        }

        $memPct = if ($memUsedMb -and $memTotalMb -and $memTotalMb -gt 0) {
            [Math]::Round($memUsedMb / $memTotalMb * 100)
        } else { $null }

        [void]$gpus.Add([PSCustomObject]@{
            index                  = $idx
            name                   = $gpuName
            utilization_pct        = $util
            temperature_c          = $temp
            memory_temperature_c   = $memTemp
            memory_used_mb         = $memUsedMb
            memory_total_mb        = $memTotalMb
            memory_utilization_pct = $memPct
            power_w                = $powerW
            power_limit_w          = $powerLimW
            fan_speed_pct          = $fanPct
            perf_state             = $pstate
            throttle_reasons       = $throttle
            ecc_errors             = $eccErrors
            clock_sm_mhz           = $clockSm
            clock_sm_max_mhz       = $clockSmMax
            clock_mem_mhz          = $clockMem
            clock_mem_max_mhz      = $clockMemMax
            salad_on_gpu           = $saladOnGpu
        })
    }
    return $gpus
}

# ── LibreHardwareMonitor ─────────────────────────────────────

function Get-LhmTypes($asm) {
    # GetExportedTypes may throw ReflectionTypeLoadException if dependencies are missing;
    # in that case return only the types that loaded successfully.
    try { return $asm.GetExportedTypes() }
    catch [System.Reflection.ReflectionTypeLoadException] {
        return $_.Exception.Types | Where-Object { $null -ne $_ }
    }
}

function Initialize-Lhm {
    if (-not (Test-Path $LhmDllPath)) { return $false }
    try {
        $asm = [System.Reflection.Assembly]::LoadFrom($LhmDllPath)
        if (-not $asm) { throw "LoadFrom returned null for $LhmDllPath" }

        $allTypes = Get-LhmTypes $asm

        # Look up the Computer type (namespace may vary between versions)
        $computerType = $asm.GetType("LibreHardwareMonitor.Hardware.Computer")
        if (-not $computerType) {
            $computerType = $allTypes | Where-Object { $_.Name -eq "Computer" } | Select-Object -First 1
        }
        if (-not $computerType) { throw "Type 'Computer' not found in assembly. Available types: $(($allTypes | Select-Object -ExpandProperty FullName) -join ', ')" }

        # Find parameterless constructor (public or non-public)
        $bf = [System.Reflection.BindingFlags]'Public,NonPublic,Instance'
        $ctor = $computerType.GetConstructor($bf, $null, [Type[]]@(), $null)
        if (-not $ctor) { throw "No parameterless constructor found in '$($computerType.FullName)'" }

        $script:LhmComputer = $ctor.Invoke($null)
        if (-not $script:LhmComputer) { throw "Constructor returned null" }

        # Enable GPU monitoring
        $gpuProp = $computerType.GetProperty("IsGpuEnabled")
        if ($gpuProp) { $gpuProp.SetValue($script:LhmComputer, $true) }

        $computerType.GetMethod("Open").Invoke($script:LhmComputer, $null)

        # Cache SensorType.Temperature enum value for use in Read-LhmSensors
        $sensorTypeEnum = $asm.GetType("LibreHardwareMonitor.Hardware.SensorType")
        if (-not $sensorTypeEnum) {
            $sensorTypeEnum = $allTypes | Where-Object { $_.Name -eq "SensorType" } | Select-Object -First 1
        }
        if ($sensorTypeEnum) {
            $script:LhmTempSensorType = [Enum]::Parse($sensorTypeEnum, "Temperature")
        }

        Write-Host "[INFO] LibreHardwareMonitor loaded successfully."
        return $true
    } catch {
        Write-Host "[WARN] Failed to load LibreHardwareMonitor: $_"
        $script:LhmComputer       = $null
        $script:LhmTempSensorType = $null
        return $false
    }
}

function Read-LhmSensors($hw, $out) {
    $hw.Update()
    $idx = ($hw.Identifier.ToString().TrimStart('/').Split('/'))[-1]
    foreach ($sensor in $hw.Sensors) {
        if ($sensor.SensorType -eq $script:LhmTempSensorType -and $null -ne $sensor.Value) {
            $n = $sensor.Name.ToLower()
            $val = [Math]::Round([float]$sensor.Value, 1)
            if ($val -ge 255) { continue }  # 255 = driver sentinel for unsupported sensor
            if ($n -like '*memory junction*') {
                if (-not $out.ContainsKey($idx)) { $out[$idx] = @{} }
                $out[$idx]['memory_junction_c'] = $val
            } elseif ($n -like '*hot spot*' -or $n -like '*hotspot*') {
                if (-not $out.ContainsKey($idx)) { $out[$idx] = @{} }
                $out[$idx]['hotspot_c'] = $val
            }
        }
    }
    foreach ($sub in $hw.SubHardware) { Read-LhmSensors $sub $out }
}

function Get-GpuExtraTemps {
    $result = @{}
    if ($null -eq $script:LhmComputer) { return $result }
    try {
        foreach ($hw in $script:LhmComputer.Hardware) { Read-LhmSensors $hw $result }
    } catch { }
    return $result
}

$script:LhmComputer       = $null
$script:LhmTempSensorType = $null

function Get-SystemMetrics {
    try {
        $cpu = (Get-CimInstance Win32_Processor |
                Measure-Object -Property LoadPercentage -Average).Average
    } catch { $cpu = 0 }

    try {
        $os         = Get-CimInstance Win32_OperatingSystem
        $ramUsed    = $os.TotalVisibleMemorySize - $os.FreePhysicalMemory
        $ramPct     = $ramUsed / $os.TotalVisibleMemorySize * 100
        $ramUsedGb  = $ramUsed / 1MB
        $ramTotalGb = $os.TotalVisibleMemorySize / 1MB
        $uptime     = ((Get-Date) - $os.LastBootUpTime).TotalHours
    } catch {
        $ramPct = 0; $ramUsedGb = 0; $ramTotalGb = 0; $uptime = 0
    }

    $diskPct = $null
    try {
        $disk = Get-PSDrive C
        if ($disk.Used + $disk.Free -gt 0) {
            $diskPct = [Math]::Round($disk.Used / ($disk.Used + $disk.Free) * 100, 1)
        }
    } catch { }

    return @{
        cpu_pct       = [Math]::Round($cpu, 1)
        ram_used_pct  = [Math]::Round($ramPct, 1)
        ram_used_gb   = [Math]::Round($ramUsedGb, 1)
        ram_total_gb  = [Math]::Round($ramTotalGb, 1)
        disk_used_pct = $diskPct
        uptime_hours  = [Math]::Round($uptime, 1)
    }
}

function Send-Metrics($payload) {
    $url  = "$ServerUrl/report"
    $body = $payload | ConvertTo-Json -Depth 5 -Compress
    try {
        $resp = Invoke-RestMethod -Uri $url -Method Post -Body $body `
                    -ContentType "application/json" `
                    -Headers @{ "X-API-Key" = $ApiKey } `
                    -TimeoutSec 10
        return $true
    } catch {
        Write-Host "[ERROR] Failed to send: $_"
        return $false
    }
}

# ── Static info (fetched once) ───────────────────────────────

$CpuName = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name

$SaladExe = "C:\Program Files\Salad\Salad.exe"

if ($CachedSaladMachineId) {
    # Already saved in config, use it directly
    $SaladMachineId = $CachedSaladMachineId
    Write-Host "[INFO] Salad Machine ID : $SaladMachineId (from config)"
} else {
    # Search for it in the Salad logs
    $SaladMachineId = $null
    try {
        $saladLogs = @(
            Get-Item "C:\Users\*\AppData\Roaming\Salad\logs\main.log"                -ErrorAction SilentlyContinue
            Get-Item "C:\Users\*\AppData\Roaming\Salad\logs\main.old.log"             -ErrorAction SilentlyContinue
            Get-Item "C:\Users\*\AppData\Roaming\Salad\Local Storage\leveldb\*.log"   -ErrorAction SilentlyContinue
            Get-Item "C:\Users\*\AppData\Roaming\Salad\Local Storage\leveldb\*.ldb"   -ErrorAction SilentlyContinue
        ) | Where-Object { $_ -ne $null }

        $uuidPattern = "machineId[`"'\s:]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"

        foreach ($log in $saladLogs) {
            $match = Select-String -Path $log.FullName -Pattern $uuidPattern |
                     Select-Object -Last 1
            if ($match) {
                $SaladMachineId = $match.Matches[0].Groups[1].Value
                break
            }
        }
    } catch { }

    if ($SaladMachineId) {
        # Save it to config for next time
        try {
            $cfg | Add-Member -NotePropertyName "salad_machine_id" -NotePropertyValue $SaladMachineId -Force
            $cfg | ConvertTo-Json -Depth 5 | Set-Content $ConfigPath -Encoding UTF8
            Write-Host "[INFO] Salad Machine ID : $SaladMachineId (saved to config)"
        } catch {
            Write-Host "[INFO] Salad Machine ID : $SaladMachineId (could not save to config)"
        }
    } else {
        Write-Host "[ERROR] Salad Machine ID not found in logs or config. Please set 'salad_machine_id' manually in salad_agent_config.json." -ForegroundColor Red
    }
}

# Load LHM if the DLL is present (once at startup, fails silently)
Initialize-Lhm | Out-Null

# ── Local IP ─────────────────────────────────────────────────

$LocalIP = try {
    (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
     Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
     Sort-Object InterfaceMetric | Select-Object -First 1).IPAddress
} catch { $null }

# ── Command listener (reboot etc.) ───────────────────────────

$script:LogQueue = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()

$EnableCommandListener = $false

if ($CommandToken) {
    $cfgListenerKey = $cfg.PSObject.Properties['command_listener']

    if ($null -eq $cfgListenerKey) {
        # Not yet decided — ask only if running interactively
        if ([Environment]::UserInteractive -and $Host.Name -eq 'ConsoleHost') {
            Write-Host ""
            Write-Host "  ┌─────────────────────────────────────────────────────────┐" -ForegroundColor Cyan
            Write-Host "  │  Remote Reboot Feature                                  │" -ForegroundColor Cyan
            Write-Host "  │                                                         │" -ForegroundColor Cyan
            Write-Host "  │  The dashboard can send a reboot command to this PC.    │" -ForegroundColor Cyan
            Write-Host "  │  This requires opening port $CommandPort in the firewall.      │" -ForegroundColor Cyan
            Write-Host "  │                                                         │" -ForegroundColor Cyan
            Write-Host "  │  A firewall rule 'SaladMonitorAgent-CMD-$CommandPort' will be │" -ForegroundColor Cyan
            Write-Host "  │  created to allow inbound TCP on port $CommandPort.           │" -ForegroundColor Cyan
            Write-Host "  └─────────────────────────────────────────────────────────┘" -ForegroundColor Cyan
            Write-Host ""
            $answer = Read-Host "  Enable remote reboot on this machine? (y/n)"
            $accepted = $answer.Trim().ToLower() -eq 'y'
        } else {
            $accepted = $false
        }

        # Save decision to config
        $cfg | Add-Member -NotePropertyName 'command_listener' -NotePropertyValue $accepted -Force
        try {
            $cfg | ConvertTo-Json -Depth 5 | Set-Content $ConfigPath -Encoding UTF8
            Write-Host "[INFO] Remote reboot preference saved to config (enabled=$accepted)"
        } catch {
            Write-Host "[WARN] Could not save preference to config: $_"
        }
    } else {
        $accepted = [bool]$cfg.command_listener
    }

    if ($accepted) {
        $EnableCommandListener = $true
        $ruleName = "SaladMonitorAgent-CMD-$CommandPort"
        $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
        if (-not $existing) {
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
                -LocalPort $CommandPort -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
        }
    } else {
        Write-Host "[INFO] Remote reboot disabled — no listener started"
    }
}

if ($EnableCommandListener) {
    $listenerRunspace = [runspacefactory]::CreateRunspace()
    $listenerRunspace.Open()
    $listenerRunspace.SessionStateProxy.SetVariable('CommandPort',  $CommandPort)
    $listenerRunspace.SessionStateProxy.SetVariable('CommandToken', $CommandToken)
    $listenerRunspace.SessionStateProxy.SetVariable('LogQueue',     $script:LogQueue)
    $listenerRunspace.SessionStateProxy.SetVariable('ScriptDir',    $ScriptDir)
    $listenerPs = [powershell]::Create()
    $listenerPs.Runspace = $listenerRunspace
    [void]$listenerPs.AddScript({
        try {
            $tcp = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $CommandPort)
            $tcp.Start()
            $LogQueue.Enqueue("[INFO] Command listener ready on 0.0.0.0:$CommandPort")
        } catch {
            $LogQueue.Enqueue("[WARN] Command listener could not start on port ${CommandPort}: $_")
            return
        }
        while ($true) {
            try {
                $client = $tcp.AcceptTcpClient()
                $stream = $client.GetStream()
                $reader = [System.IO.StreamReader]::new($stream)
                $writer = [System.IO.StreamWriter]::new($stream)
                $writer.AutoFlush = $true

                $requestLine = $reader.ReadLine()
                $headers = @{}
                $line = $reader.ReadLine()
                while ($line -and $line -ne "") {
                    $parts = $line -split ": ", 2
                    if ($parts.Count -eq 2) { $headers[$parts[0]] = $parts[1] }
                    $line = $reader.ReadLine()
                }

                $rParts = ($requestLine -split " ")
                $method = $rParts[0]; $path = $rParts[1]
                $token  = $headers["X-Command-Token"]
                $ts     = (Get-Date -Format "HH:mm:ss")

                if ($method -eq "POST" -and $token -eq $CommandToken -and $path -eq "/reboot") {
                    $body = '{"ok":true}'
                    $writer.WriteLine("HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n$body")
                    $client.Close()
                    $LogQueue.Enqueue("[$ts] [CMD] Reboot command received — restarting in 10s")
                    Start-Sleep -Seconds 2
                    & shutdown.exe /r /t 10
                } elseif ($method -eq "POST" -and $token -eq $CommandToken -and $path -eq "/restart-salad") {
                    $body = '{"ok":true}'
                    $writer.WriteLine("HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n$body")
                    $client.Close()
                    $LogQueue.Enqueue("[$ts] [CMD] Restart Salad command received")
                    $saladProcs = Get-Process -Name salad,saladcloud,salad-client -ErrorAction SilentlyContinue
                    if ($saladProcs) {
                        $saladExePath = ($saladProcs | Select-Object -First 1).Path
                        $saladProcs | Stop-Process -Force
                        $LogQueue.Enqueue("[$ts] [CMD] Salad stopped — relaunching in 3s")
                        Start-Sleep -Seconds 3
                        if ($saladExePath -and (Test-Path $saladExePath)) {
                            Start-Process $saladExePath
                        } else {
                            Start-Process "C:\Program Files\Salad\Salad.exe" -ErrorAction SilentlyContinue
                        }
                        $LogQueue.Enqueue("[$ts] [CMD] Salad relaunched")
                    } else {
                        $LogQueue.Enqueue("[$ts] [CMD] Salad not running — launching")
                        Start-Process "C:\Program Files\Salad\Salad.exe" -ErrorAction SilentlyContinue
                    }
                } elseif ($method -eq "POST" -and $token -eq $CommandToken -and $path -eq "/update-agent") {
                    $body = '{"ok":true}'
                    $writer.WriteLine("HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n$body")
                    $client.Close()
                    $LogQueue.Enqueue("[$ts] [CMD] Remote update command received — launching remote_update.bat")
                    $updateBat = Join-Path $ScriptDir "remote_update.bat"
                    if (Test-Path $updateBat) {
                        Start-Process "cmd.exe" -ArgumentList "/c `"$updateBat`"" -WindowStyle Normal
                        Start-Sleep -Seconds 1
                        Start-Process "cmd.exe" -ArgumentList "/c taskkill /F /PID $PID" -WindowStyle Hidden
                    } else {
                        $LogQueue.Enqueue("[$ts] [CMD] remote_update.bat not found — aborting")
                    }
                } else {
                    $writer.WriteLine("HTTP/1.1 403 Forbidden`r`nContent-Length: 0`r`nConnection: close`r`n`r`n")
                    $client.Close()
                    $LogQueue.Enqueue("[$ts] [WARN] Rejected: $method $path token_ok=$(($token -eq $CommandToken))")
                }
            } catch { }
        }
    })
    [void]$listenerPs.BeginInvoke()
}

# ── Main loop ────────────────────────────────────────────────

$PrevNetSample       = $null
$SessionUploadedMB   = 0.0
$SessionDownloadedMB = 0.0

while ($true) {
    # Drain messages from background runspaces
    $msg = $null
    while ($script:LogQueue.TryDequeue([ref]$msg)) { Write-Host $msg }

    try {
        $sys          = Get-SystemMetrics
        $gpus         = Get-GpuMetrics

        # Enrich GPU temperatures via LHM if it loaded successfully
        if ($null -ne $script:LhmComputer) {
            $extraTemps = Get-GpuExtraTemps
            foreach ($g in @($gpus)) {
                $idxStr = "$($g.index)"
                if ($extraTemps.ContainsKey($idxStr)) {
                    $extra = $extraTemps[$idxStr]
                    if ($null -eq $g.memory_temperature_c -and $null -ne $extra.memory_junction_c) {
                        $g.memory_temperature_c = $extra.memory_junction_c
                    }
                    if ($null -ne $extra.hotspot_c) {
                        $g | Add-Member -NotePropertyName "hotspot_c"          -NotePropertyValue $extra.hotspot_c          -Force
                    }
                    if ($null -ne $extra.memory_junction_c) {
                        $g | Add-Member -NotePropertyName "memory_junction_c"  -NotePropertyValue $extra.memory_junction_c  -Force
                    }
                }
            }
        }

        $saladRunning = Is-SaladRunning
        $oxyRunning   = $null -ne (Get-Process -Name "oxy" -ErrorAction SilentlyContinue)
        $mode         = if ($oxyRunning) { "bandwidth" } elseif ($saladRunning) { "gpu" } else { "idle" }

        $SaladVersion = if (Test-Path $SaladExe) {
            try { (Get-Item $SaladExe).VersionInfo.FileVersion } catch { $null }
        } else { $null }

        $bw = Get-BandwidthMetrics -PrevSample $PrevNetSample
        $PrevNetSample = $bw.current_sample
        if ($null -ne $bw.delta_uploaded_mb) {
            $SessionUploadedMB   += $bw.delta_uploaded_mb
            $SessionDownloadedMB += $bw.delta_downloaded_mb
        }

        $payload = [PSCustomObject]@{
            machine_id    = $MachineId
            hostname      = $env:COMPUTERNAME
            local_ip      = $LocalIP
            command_port  = if ($EnableCommandListener) { $CommandPort } else { $null }
            timestamp     = (Get-Date -Format "o")
            salad_running = $saladRunning
            mode          = $mode
            cpu_pct       = $sys.cpu_pct
            ram_used_pct  = $sys.ram_used_pct
            ram_used_gb   = $sys.ram_used_gb
            ram_total_gb  = $sys.ram_total_gb
            disk_used_pct = $sys.disk_used_pct
            uptime_hours  = $sys.uptime_hours
            cpu_name          = $CpuName
            agent_version     = $Version
            salad_version     = $SaladVersion
            salad_machine_id  = $SaladMachineId
            gpus              = @($gpus)
            bandwidth     = [PSCustomObject]@{
                upload_mbps            = $bw.upload_mbps
                download_mbps          = $bw.download_mbps
                session_uploaded_mb    = [Math]::Round($SessionUploadedMB,   1)
                session_downloaded_mb  = [Math]::Round($SessionDownloadedMB, 1)
                interval_uploaded_mb   = $bw.delta_uploaded_mb
                interval_downloaded_mb = $bw.delta_downloaded_mb
            }
        }

        $ok = Send-Metrics $payload
        $status = if ($ok) { "OK" } else { "FAIL" }
        $salad  = if ($saladRunning) { "ON" } else { "OFF" }
        $modeInfo = " | Mode:$mode"
        $ts = Get-Date -Format "HH:mm:ss"
        if ($mode -eq "bandwidth") {
            $upStr = if ($null -ne $bw.upload_mbps)   { "$($bw.upload_mbps) Mbps" }   else { "—" }
            $dnStr = if ($null -ne $bw.download_mbps) { "$($bw.download_mbps) Mbps" } else { "—" }
            Write-Host "[$ts] [$status] Salad:$salad CPU:$($sys.cpu_pct)%$modeInfo | ↑$upStr ↓$dnStr"
        } else {
            $gpuInfo = ""
            $gpuArr = @($gpus)
            if ($gpuArr.Count -gt 0) {
                $g = $gpuArr[0]
                $gpuInfo = " | GPU:$($g.utilization_pct)% Temp:$($g.temperature_c)C"
                if ($null -ne $g.memory_junction_c) { $gpuInfo += " MemJ:$($g.memory_junction_c)C" }
                if ($null -ne $g.hotspot_c)         { $gpuInfo += " Hot:$($g.hotspot_c)C" }
                $gpuInfo += " Fan:$($g.fan_speed_pct)%"
            }
            Write-Host "[$ts] [$status] Salad:$salad CPU:$($sys.cpu_pct)%$modeInfo$gpuInfo"
        }

    } catch {
        Write-Host "[ERROR] $_"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
