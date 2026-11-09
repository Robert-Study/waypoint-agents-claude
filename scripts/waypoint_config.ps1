# Shared settings reader for the Waypoint scripts (dot-source it: . (Join-Path $PSScriptRoot 'waypoint_config.ps1')).
# Same order as engine/cc_engine/config.py: environment variable, then ~/.waypoint/config.json
# (or the file named by WAYPOINT_CONFIG), then the built-in default. Paths default under the user's home
# (USERPROFILE), which gives the original install's values on its own machine.
# Get-WaypointPython is the exception: see its comment. A line ending in "# export: drop line" is left out of
# the exported package (scripts/export_standalone.py).
# String join: Join-Path throws when the drive in the default does not exist on this machine.
function Join-Wp([string]$Base, [string]$Rest) { $Base.TrimEnd([char]92, [char]47) + [string][char]92 + $Rest }

# The user's home folder, read like Python's Path.home() on Windows: USERPROFILE first.
function Get-WaypointUserProfile {
    if ($env:USERPROFILE) { return $env:USERPROFILE }
    return [Environment]::GetFolderPath('UserProfile')
}

function Get-WaypointSetting {
    param([string]$Key, [string[]]$EnvNames = @(), [string]$Default = '')
    foreach ($n in $EnvNames) {
        $v = [Environment]::GetEnvironmentVariable($n)
        if ($v) { return $v }
    }
    $file = $env:WAYPOINT_CONFIG
    if (-not $file) { $file = Join-Wp (Get-WaypointUserProfile) '.waypoint\config.json' }
    try {
        # -Encoding UTF8: Windows PowerShell 5.1 would read the file in the ANSI code page; this reads it with or without a BOM.
        $obj = Get-Content -LiteralPath $file -Encoding UTF8 -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $prop = $obj.PSObject.Properties[$Key]
        if ($prop -and $prop.Value) { return [string]$prop.Value }
    } catch { }
    return $Default
}

# The engine's data folder, found the way engine/cc_engine/config.py finds it: CC_DATA_DIR or
# WAYPOINT_DATA_DIR, then data_dir in the settings file, then <repo>\data.
function Get-WaypointDataDir {
    param([string]$Repo)
    Get-WaypointSetting 'data_dir' @('CC_DATA_DIR', 'WAYPOINT_DATA_DIR') (Join-Wp $Repo 'data')
}

# The full path of the Python 3.10+ that $Exe (plus $Pre arguments) starts, or ''. The Microsoft Store
# "python" alias prints an error and no path, so it is skipped.
function Resolve-WaypointPythonExe {
    param([string]$Exe, [string[]]$Pre = @())
    if (-not (Get-Command $Exe -CommandType Application -ErrorAction SilentlyContinue)) { return '' }
    try {
        $out = & $Exe @Pre -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
        $p = ([string](@($out) | Select-Object -Last 1)).Trim()
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    } catch { }
    return ''
}

# Python for the Waypoint scripts: WAYPOINT_PYTHON, then the "python" key in the settings file, then the
# original install's venv (only when that file exists; not in the exported package), then "py -3", then
# python / python3 on PATH. Returns '' when none is found.
function Get-WaypointPython {
    $set = Get-WaypointSetting 'python' @('WAYPOINT_PYTHON') ''
    if ($set) { return $set }
    $home_ = Get-WaypointSetting 'user_home' @('WAYPOINT_USER_HOME') (Get-WaypointUserProfile)
    $ws = Get-WaypointSetting 'workspace_root' @('AI_WORKSPACE_ROOT', 'AI_DISK_ROOT') (Join-Wp $home_ 'workspace')
    $old = ''
    if ($old -and (Test-Path -LiteralPath $old -PathType Leaf)) { return $old }
    foreach ($try in @(@('py', '-3'), @('python'), @('python3'))) {
        $p = Resolve-WaypointPythonExe $try[0] @($try | Select-Object -Skip 1)
        if ($p) { return $p }
    }
    return ''
}
function Get-WaypointLauncher {
    $home_ = Get-WaypointSetting 'user_home' @('WAYPOINT_USER_HOME') (Get-WaypointUserProfile)
    $ws = Get-WaypointSetting 'workspace_root' @('AI_WORKSPACE_ROOT', 'AI_DISK_ROOT') (Join-Wp $home_ 'workspace')
    $tools = Get-WaypointSetting 'tools_dir' @('WAYPOINT_TOOLS_DIR') (Join-Wp $ws 'waypoint-workspace\tools')
    Get-WaypointSetting 'launcher' @('WAYPOINT_LAUNCHER') (Join-Wp $tools 'start_dsh_app.py')
}
