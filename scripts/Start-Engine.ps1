<#
.SYNOPSIS
    RETIRED (without -Legacy this script does nothing). Legacy:     Starts the Waypoint engine (python -m cc_engine serve) on 127.0.0.1:<Port>, once.
.DESCRIPTION
    RETIRED in Waypoint 1.2: the always-on engine at 127.0.0.1:8751 is no longer started or registered at logon.
    Start the runner instead (it runs until idle, then exits): python -m waypoint_runner  (from runner\, pinned interpreter).
    Pass -Legacy to get the old behaviour; the CLI bridge on 8753 is retired with the engine.
    - Port: -Port, else env WAYPOINT_PORT, else 8751. Data dir: env CC_DATA_DIR / WAYPOINT_DATA_DIR, else
      data_dir in ~/.waypoint/config.json (or the file named by WAYPOINT_CONFIG), else <repo>\data.
    - Uses the configured interpreter directly (no venv activation) and runs from engine\.
    - Hidden window; stdout and stderr are appended to <data dir>\engine.log (rolled to engine.log.1 past 5 MB).
    - Single instance per port: a named mutex guards the start, and a healthy engine already on the port is reused.
    - Waits until GET http://127.0.0.1:<Port>/health returns 200 (default 60 s), then prints one line and writes
      <data dir>\engine.pid ({"pid", "port", "data_dir"}) so Stop-Engine.ps1 stops this engine and no other.
    - If the port is held by something that is not a healthy engine, nothing is started (exit 1).
    -Foreground keeps this script attached to the engine until it exits and returns the engine's exit code;
    the scheduled task (Register-Engine.ps1) uses it so "restart on failure" applies.
    A deliberate Stop-Engine.ps1 leaves <data dir>\engine.stop so that exit counts as success.
.PARAMETER Python
    Interpreter (default: env WAYPOINT_PYTHON, then the python key of the config file).
.PARAMETER Port
    Loopback port (default: env WAYPOINT_PORT, else 8751).
.PARAMETER TimeoutSec
    Seconds to wait for /health (default 60).
.PARAMETER Foreground
    Stay attached until the engine exits (for Task Scheduler).
.EXAMPLE
    powershell -NoProfile -File scripts\Start-Engine.ps1
.EXAMPLE
    powershell -NoProfile -File scripts\Start-Engine.ps1 -Port 8760
#>
param(
    [string]$Python = $(. (Join-Path $PSScriptRoot "waypoint_config.ps1"); Get-WaypointPython),
    [int]$Port = $(if ($env:WAYPOINT_PORT) { [int]$env:WAYPOINT_PORT } else { 8751 }),
    [int]$TimeoutSec = 60,
    [switch]$Foreground,
    [switch]$Legacy
)

$ErrorActionPreference = "Stop"
if (-not $Legacy) {
    Write-Output "engine: RETIRED in Waypoint 1.2. The always-on engine (port 8751) is no longer started."
    Write-Output "Start the runner instead (runs until idle, then exits): cd runner; <pinned python> -m waypoint_runner   (or --once for one tick)."
    Write-Output "Pass -Legacy only to bring the old engine back."
    exit 0
}
if ($Port -lt 1 -or $Port -gt 65535) { Write-Output "engine: FAILED, port $Port is not a valid port"; exit 1 }
$repo = Split-Path -Parent $PSScriptRoot
$engineDir = Join-Path $repo "engine"
. (Join-Path $PSScriptRoot "waypoint_config.ps1")
$dataDir = Get-WaypointSetting "data_dir" @("CC_DATA_DIR", "WAYPOINT_DATA_DIR") (Join-Path $repo "data")
$dataDir = [System.IO.Path]::GetFullPath($dataDir).TrimEnd([char]92)
$log = Join-Path $dataDir "engine.log"
$stopFlag = Join-Path $dataDir "engine.stop"
$pidFile = Join-Path $dataDir "engine.pid"
$healthUrl = "http://127.0.0.1:$Port/health"

function Test-Health {
    # HttpWebRequest with no proxy: Invoke-WebRequest can spend seconds on proxy discovery.
    try {
        $req = [System.Net.HttpWebRequest]::Create($healthUrl)
        $req.Proxy = $null
        $req.Timeout = 2000
        $resp = $req.GetResponse()
        try { return ([int]$resp.StatusCode -eq 200) } finally { $resp.Close() }
    } catch { return $false }
}

function Test-Listening {
    $c = New-Object System.Net.Sockets.TcpClient
    try { return ($c.ConnectAsync("127.0.0.1", $Port).Wait(500) -and $c.Connected) } catch { return $false } finally { $c.Dispose() }
}

function Get-PortOwner {
    # The pid serving the port, from netstat (fast; Get-NetTCPConnection takes many seconds). Restricted sessions
    # show pid 0 for the listener, so a probe connection is held open while netstat runs: the server side of that
    # connection (local :Port, foreign = the probe) carries the real pid.
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
    # How the process on the port is known to be this data dir's engine: "pid file", "log" (an ancestor's command
    # line appends to this data dir's engine.log, true of every engine Start-Engine.ps1 has started) or $null.
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

function Write-PidFile([int]$Id) {
    $json = '{"pid": ' + $Id + ', "port": ' + $Port + ', "data_dir": ' + (ConvertTo-Json $dataDir) + ', "started": "' + (Get-Date -Format s) + '"}'
    [System.IO.File]::WriteAllText($pidFile, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-LogTail {
    if (Test-Path -LiteralPath $log) {
        return ((Get-Content -LiteralPath $log -Tail 5 -ErrorAction SilentlyContinue) -join " | ")
    }
    return "(no log)"
}

if (-not $Python) { Write-Output "engine: FAILED, no Python 3.10+ found; set WAYPOINT_PYTHON or pass -Python"; exit 1 }
if (-not (Test-Path -LiteralPath $Python)) { Write-Output "engine: FAILED, python not found: $Python (set WAYPOINT_PYTHON or pass -Python)"; exit 1 }
if (-not (Test-Path -LiteralPath (Join-Path $engineDir "cc_engine\__main__.py"))) {
    Write-Output "engine: FAILED, engine\cc_engine\__main__.py not found"; exit 1
}
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$mutex = New-Object System.Threading.Mutex($false, "Local\DshCC-Engine-Start-$Port")
$held = $false
try {
    try { $held = $mutex.WaitOne(($TimeoutSec + 5) * 1000) } catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { Write-Output "engine: FAILED, another start is still in progress"; exit 1 }

    if (Test-Health) {
        $owner = Get-PortOwner
        $how = if ($owner) { Get-EngineMatch $owner } else { $null }
        if ($how) {
            Write-PidFile $owner
            Write-Output "engine: already running at http://127.0.0.1:$Port (pid $owner)"
        } else {
            Write-Output "engine: already running at http://127.0.0.1:$Port (pid $owner, not started for $dataDir)"
        }
        exit 0
    }
    $owner = Get-PortOwner
    if ($owner) {
        $cmd = (Get-Proc $owner).CommandLine
        Write-Output "engine: FAILED, port $Port is held by pid $owner but /health is not 200 ($cmd)"
        exit 1
    }

    if ((Test-Path -LiteralPath $log) -and ((Get-Item -LiteralPath $log).Length -gt 5MB)) {
        Move-Item -LiteralPath $log -Destination "$log.1" -Force
    }
    Remove-Item -LiteralPath $stopFlag -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Add-Content -LiteralPath $log -Value "`r`n=== start $(Get-Date -Format s) port $Port ===" -Encoding UTF8

    $env:PYTHONUNBUFFERED = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:CC_DATA_DIR = $dataDir  # tokens, usage cache and the rest of the engine's files go to this data dir
    $db = if ($env:CC_DB) { $env:CC_DB } else { Join-Path $dataDir "cc.sqlite" }
    # cmd.exe merges and appends both streams into one log; it exits with python's exit code.
    $inner = "`"$Python`" -m cc_engine serve --port $Port --db `"$db`" >> `"$log`" 2>&1"
    $proc = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList "/d /s /c `"$inner`"" `
        -WorkingDirectory $engineDir -WindowStyle Hidden -PassThru
    $null = $proc.Handle  # cache the handle so ExitCode is readable later

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $healthy = $false
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if (Test-Health) { $healthy = $true; break }
        if ($proc.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $healthy) {
        $why = if ($proc.HasExited) { "exited with code $($proc.ExitCode)" } else { "no healthy reply in $TimeoutSec s" }
        Write-Output "engine: FAILED, $why; log: $(Get-LogTail)"
        exit 1
    }
    $owner = Get-PortOwner
    if ($owner) { Write-PidFile $owner }
    Write-Output ("engine: running at http://127.0.0.1:{0} (pid {1}, healthy in {2:N1} s, log {3})" -f $Port, $owner, $sw.Elapsed.TotalSeconds, $log)
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

if ($Foreground) {
    $proc.WaitForExit()
    $code = $proc.ExitCode
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $stopFlag) {
        Remove-Item -LiteralPath $stopFlag -Force -ErrorAction SilentlyContinue
        Write-Output "engine: stopped on request"
        exit 0
    }
    Write-Output "engine: exited with code $code"
    exit $code
}
exit 0
