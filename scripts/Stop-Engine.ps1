<#
.SYNOPSIS
    Stops the Waypoint engine that serves 127.0.0.1:<Port> for this data dir, and no other.
.DESCRIPTION
    NOTE: the always-on engine is RETIRED in Waypoint 1.2; this script only stops a leftover legacy engine
    (start the runner with: python -m waypoint_runner).
    Port: -Port, else env WAYPOINT_PORT, else 8751. Data dir: env CC_DATA_DIR / WAYPOINT_DATA_DIR, else data_dir in
    the config file, else <repo>\data (the same rules as Start-Engine.ps1).
    The process that serves the port is stopped only when
      - its command line contains "cc_engine", and
      - it is this data dir's engine: its pid is the one in <data dir>\engine.pid (written by Start-Engine.ps1),
        or one of its parent processes appends to <data dir>\engine.log (engines started before pid files).
    Anything else on the port, including an engine for another data dir, is left alone (exit 1).
    Leaves <data dir>\engine.stop so a Start-Engine.ps1 -Foreground supervisor (the scheduled task) treats the exit
    as deliberate and does not restart it. -DryRun reports what would be stopped and changes nothing.
.EXAMPLE
    powershell -NoProfile -File scripts\Stop-Engine.ps1
.EXAMPLE
    powershell -NoProfile -File scripts\Stop-Engine.ps1 -Port 8760 -DryRun
#>
param(
    [int]$Port = $(if ($env:WAYPOINT_PORT) { [int]$env:WAYPOINT_PORT } else { 8751 }),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "waypoint_config.ps1")
$dataDir = Get-WaypointSetting "data_dir" @("CC_DATA_DIR", "WAYPOINT_DATA_DIR") (Join-Path $repo "data")
$dataDir = [System.IO.Path]::GetFullPath($dataDir).TrimEnd([char]92)
$log = Join-Path $dataDir "engine.log"
$stopFlag = Join-Path $dataDir "engine.stop"
$pidFile = Join-Path $dataDir "engine.pid"

function Test-Listening {
    # A connect probe; listener tables (netstat, .NET) can omit the socket in restricted sessions.
    $c = New-Object System.Net.Sockets.TcpClient
    try { return ($c.ConnectAsync("127.0.0.1", $Port).Wait(500) -and $c.Connected) } catch { return $false } finally { $c.Dispose() }
}

function Get-PortOwner {
    # The pid serving the port, from netstat. Restricted sessions show pid 0 for the listener, so a probe connection
    # is held open while netstat runs: the server side of that connection carries the real pid.
    $probe = New-Object System.Net.Sockets.TcpClient
    $rows = @()
    $probeEnd = ""
    try {
        try {
            if ($probe.ConnectAsync("127.0.0.1", $Port).Wait(1000) -and $probe.Connected) {
                $probeEnd = [string]$probe.Client.LocalEndPoint
            }
        } catch { }
        $rows = @(netstat -ano -p TCP)
    } finally { $probe.Dispose() }
    $listen = $null
    $served = $null
    foreach ($line in $rows) {
        $f = @($line -split '\s+' | Where-Object { $_ })
        if ($f.Count -lt 5 -or $f[1] -notmatch "^(127\.0\.0\.1|0\.0\.0\.0):$Port$") { continue }
        $id = 0
        if (-not [int]::TryParse($f[4], [ref]$id) -or $id -le 0) { continue }
        if ($f[3] -eq "LISTENING" -and -not $listen) { $listen = $id }
        if ($probeEnd -and $f[2] -eq $probeEnd -and -not $served) { $served = $id }
    }
    if ($listen) { return $listen }
    return $served
}

function Get-Proc([int]$Id) {
    if ($Id -le 0) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$Id" -ErrorAction SilentlyContinue
}

function Read-PidFile {
    try { return (Get-Content -LiteralPath $pidFile -Encoding UTF8 -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop) } catch { return $null }
}

function Get-EngineMatch([int]$Id) {
    $pf = Read-PidFile
    if ($pf -and [int]$pf.pid -eq $Id -and [int]$pf.port -eq $Port) { return "pid file" }
    # One process query (each single CIM query costs about a second on a busy machine).
    $byId = @{}
    Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, CommandLine, CreationDate -ErrorAction SilentlyContinue |
        ForEach-Object { $byId[[int]$_.ProcessId] = $_ }
    $p = $byId[$Id]
    for ($i = 0; $p -and $i -lt 4; $i++) {
        $cmd = [string]$p.CommandLine
        if ($cmd.IndexOf($log, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return "log" }
        $parent = $byId[[int]$p.ParentProcessId]
        if (-not $parent -or ($parent.CreationDate -and $p.CreationDate -and $parent.CreationDate -gt $p.CreationDate)) { break }
        $p = $parent
    }
    return $null
}

function Remove-StalePidFile {
    $pf = Read-PidFile
    if ($pf -and [int]$pf.port -eq $Port -and -not $DryRun) { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue }
}

if (-not (Test-Listening)) {
    Remove-StalePidFile
    Write-Output "engine: not running (nothing listens on $Port)"
    exit 0
}
$owner = Get-PortOwner
if (-not $owner) { Write-Output "engine: port $Port is in use but its owner could not be read; left alone"; exit 1 }
$info = Get-Proc $owner
$cmd = if ($info) { [string]$info.CommandLine } else { "" }
if ($cmd -notmatch "cc_engine") {
    Write-Output "engine: port $Port is owned by pid $owner ($($info.Name)), not cc_engine; left alone"
    exit 1
}
$how = Get-EngineMatch $owner
if (-not $how) {
    Write-Output "engine: port $Port is served by another engine (pid $owner), not the one for $dataDir; left alone"
    exit 1
}
if ($DryRun) { Write-Output "DryRun: would stop pid $owner on port $Port (matched by $how)"; exit 0 }

Set-Content -LiteralPath $stopFlag -Value (Get-Date -Format s) -Encoding UTF8 -ErrorAction SilentlyContinue
Stop-Process -Id $owner -Force
for ($i = 0; $i -lt 40; $i++) {
    if (-not (Test-Listening)) { break }
    Start-Sleep -Milliseconds 250
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
if (Test-Listening) { Write-Output "engine: FAILED, port $Port still answers after stopping pid $owner"; exit 1 }
Write-Output "engine: stopped (pid $owner, port $Port)"
exit 0
