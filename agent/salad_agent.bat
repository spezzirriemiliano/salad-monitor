@echo off
:: ============================================================
::  Salad Monitor Agent — no requiere Python ni instalacion
::  Usa PowerShell (incluido en Windows) + nvidia-smi (drivers NVIDIA)
::
::  Uso:
::    salad_agent.bat            -> inicia el agente
::    salad_agent.bat -Install   -> crea tarea programada y arranca
::    salad_agent.bat -Uninstall -> elimina la tarea programada
:: ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$env:BAT_PATH = '%~f0';" ^
    "$content = Get-Content '%~f0' -Raw -Encoding UTF8;" ^
    "$ps = ($content -split '(?m)^#PS_START\r?\n', 2)[1];" ^
    "Invoke-Expression $ps" -- %*
exit /b

#PS_START
# ============================================================
#  PowerShell agent — todo lo de arriba es solo el lanzador
# ============================================================

param([switch]$Install, [switch]$Uninstall)

$ScriptPath  = $env:BAT_PATH
$ScriptDir   = Split-Path $ScriptPath
$ConfigPath  = Join-Path $ScriptDir "salad_agent_config.json"
$TaskName    = "SaladMonitorAgent"

# ── Instalar / desinstalar tarea programada ─────────────────

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[OK] Tarea '$TaskName' eliminada."
    exit 0
}

if ($Install) {
    if (-not (Test-Path $ConfigPath)) {
        Write-Host "[ERROR] No se encontro salad_agent_config.json"
        Write-Host "        Copia salad_agent_config.example.json y editalo."
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
    Write-Host "[OK] Tarea '$TaskName' creada. Se ejecutara automaticamente al iniciar Windows."
    Write-Host "[  ] Iniciando agente ahora..."
    Start-ScheduledTask -TaskName $TaskName
    exit 0
}

# ── Cargar configuracion ────────────────────────────────────

if (-not (Test-Path $ConfigPath)) {
    Write-Host "[ERROR] No se encontro salad_agent_config.json en $ScriptDir"
    Write-Host "        Copia salad_agent_config.example.json y editalo."
    exit 1
}

$cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$MachineId       = if ($cfg.machine_id) { $cfg.machine_id } else { $env:COMPUTERNAME }
$ServerUrl       = $cfg.server_url.TrimEnd("/")
$ApiKey          = $cfg.api_key
$IntervalSeconds = if ($cfg.interval_seconds) { [int]$cfg.interval_seconds } else { 60 }
$CachedSaladMachineId = $cfg.salad_machine_id

$Version = "v0.1"
Write-Host "[INFO] Salad Monitor Agent $Version"
Write-Host "[INFO] Machine ID : $MachineId"
Write-Host "[INFO] Server     : $ServerUrl"
Write-Host "[INFO] Intervalo  : ${IntervalSeconds}s"

# ── Nombres de proceso de Salad ─────────────────────────────

$SaladProcessNames = @("salad", "saladcloud", "salad-client")

# ── Constantes throttle (bitmask NVML) ──────────────────────

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
    $n = 0
    if ([int]::TryParse($v, [ref]$n)) { return $n }
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

function Get-GpuMetrics {
    $gpus = [System.Collections.ArrayList]@()

    # Una sola llamada para todas las metricas GPU
    $fields = "index,name,utilization.gpu,temperature.gpu,temperature.memory," +
              "memory.used,memory.total,power.draw,power.limit,fan.speed," +
              "pstate,clocks_throttle_reasons.active," +
              "ecc.errors.uncorrected.volatile.total," +
              "clocks.current.sm,clocks.max.sm,clocks.current.memory,clocks.max.memory"

    $rows = & nvidia-smi --query-gpu=$fields --format=csv,noheader,nounits 2>$null
    if (-not $rows) { return $gpus }

    # Una sola llamada para todos los procesos compute en todas las GPUs
    $computeProcs = & nvidia-smi --query-compute-apps=gpu_index,process_name --format=csv,noheader 2>$null

    # Armar mapa: gpu_index -> [process_names]
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

        # ¿Salad esta realmente usando esta GPU? (del mapa ya construido)
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

function Get-SystemMetrics {
    $cpu   = [Math]::Round((Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor `
                 -Filter "Name='_Total'").PercentProcessorTime, 1)
    $os    = Get-CimInstance Win32_OperatingSystem
    $ramPct = [Math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) /
                             $os.TotalVisibleMemorySize * 100, 1)
    $ramUsedGb  = [Math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB, 1)
    $ramTotalGb = [Math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
    $disk = Get-PSDrive C -ErrorAction SilentlyContinue
    $diskPct = if ($disk -and $disk.Used + $disk.Free -gt 0) {
        [Math]::Round($disk.Used / ($disk.Used + $disk.Free) * 100, 1)
    } else { $null }

    return @{
        cpu_pct       = [Math]::Round($cpu, 1)
        ram_used_pct  = $ramPct
        ram_used_gb   = $ramUsedGb
        ram_total_gb  = $ramTotalGb
        disk_used_pct = $diskPct
        uptime_hours  = [Math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
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
        Write-Host "[ERROR] No se pudo enviar: $_"
        return $false
    }
}

# ── Info estática (se obtiene una sola vez) ──────────────────

$CpuName = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name

$SaladExe = "C:\Program Files\Salad\Salad.exe"
$SaladVersion = if (Test-Path $SaladExe) {
    try { (Get-Item $SaladExe).VersionInfo.FileVersion } catch { $null }
} else { $null }

if ($CachedSaladMachineId) {
    # Ya estaba guardado en el config, usarlo directamente
    $SaladMachineId = $CachedSaladMachineId
    Write-Host "[INFO] Salad Machine ID : $SaladMachineId (desde config)"
} else {
    # Buscarlo en los logs de Salad
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
        # Guardarlo en el config para la proxima vez
        try {
            $cfg | Add-Member -NotePropertyName "salad_machine_id" -NotePropertyValue $SaladMachineId -Force
            $cfg | ConvertTo-Json -Depth 5 | Set-Content $ConfigPath -Encoding UTF8
            Write-Host "[INFO] Salad Machine ID : $SaladMachineId (guardado en config)"
        } catch {
            Write-Host "[INFO] Salad Machine ID : $SaladMachineId (no se pudo guardar en config)"
        }
    } else {
        Write-Host "[ERROR] Salad Machine ID not found in logs or config. Please set 'salad_machine_id' manually in salad_agent_config.json." -ForegroundColor Red
    }
}

# ── Loop principal ───────────────────────────────────────────

while ($true) {
    try {
        $sys  = Get-SystemMetrics
        $gpus = Get-GpuMetrics
        $saladRunning = Is-SaladRunning

        $payload = [PSCustomObject]@{
            machine_id    = $MachineId
            hostname      = $env:COMPUTERNAME
            timestamp     = (Get-Date -Format "o")
            salad_running = $saladRunning
            cpu_pct       = $sys.cpu_pct
            ram_used_pct  = $sys.ram_used_pct
            ram_used_gb   = $sys.ram_used_gb
            ram_total_gb  = $sys.ram_total_gb
            disk_used_pct = $sys.disk_used_pct
            uptime_hours  = $sys.uptime_hours
            cpu_name          = $CpuName
            salad_version     = $SaladVersion
            salad_machine_id  = $SaladMachineId
            gpus              = @($gpus)
        }

        $ok = Send-Metrics $payload
        $status = if ($ok) { "OK" } else { "FAIL" }
        $salad  = if ($saladRunning) { "ON" } else { "OFF" }
        $gpuInfo = ""
        $gpuArr = @($gpus)
        if ($gpuArr.Count -gt 0) {
            $g = $gpuArr[0]
            $gpuInfo = " | GPU:$($g.utilization_pct)% Temp:$($g.temperature_c)C Fan:$($g.fan_speed_pct)%"
        }
        $ts = Get-Date -Format "HH:mm:ss"
        Write-Host "[$ts] [$status] Salad:$salad CPU:$($sys.cpu_pct)%$gpuInfo"

    } catch {
        Write-Host "[ERROR] $_"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
