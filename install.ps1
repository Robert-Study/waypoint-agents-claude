<#
.SYNOPSIS
    Installs Waypoint Core from this folder: checks Python, adds the waypoint command, starts the engine and adds
    the Claude Code mod. Safe to run again (each step checks first).
.DESCRIPTION
    1. Python 3.10+: $env:WAYPOINT_PYTHON, then `py -3`, then `python` on PATH. Its path is written to the python key
       of ~/.waypoint/config.json (or the file named by WAYPOINT_CONFIG), and <this folder>\data to its data_dir key,
       each only when the file or that key is missing (a value already there is kept). The Claude Code mod needs
       data_dir to find owner.token, because it runs from Claude Code's plugin cache, not from this folder.
    2. The command: this folder's bin folder (it holds the shipped waypoint.cmd, which runs scripts\waypoint.cmd) is
       added to the user Path once (-NoPath skips it). No file is generated. A forwarding file that an older install
       wrote to %LOCALAPPDATA%\Waypoint\bin is removed, with its Path entry.
    3. The engine: scripts\Start-Engine.ps1 (-Port, else env WAYPOINT_PORT, else 8751; -NoStart skips it).
    4. The mod, when `claude` is on PATH: claude plugin marketplace add <this folder>\claude_mod, then
       claude plugin install waypoint@waypoint-local --scope user (-NoMod skips it).
    5. -AutoStart registers the engine to start at sign-in (scripts\Register-Engine.ps1).
    -DryRun prints what each step would do and changes nothing. Exit 0 when every step worked, else 1.
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Port 8760 -NoMod -DryRun
#>
param(
    [int]$Port = 0,
    [switch]$NoPath,
    [switch]$NoStart,
    [switch]$NoMod,
    [switch]$AutoStart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$scripts = Join-Path $root "scripts"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"  # Windows PowerShell, also from pwsh
$failed = New-Object System.Collections.ArrayList
$done = New-Object System.Collections.ArrayList
$pre = if ($DryRun) { "install (dry run):" } else { "install:" }
$oldMarker = "Waypoint Core command (made by install.ps1"  # the forwarding file older installs wrote

function Say([string]$Text) { Write-Host "$pre $Text" }
function Fail([string]$Step, [string]$Text) { Say "$Step FAILED, $Text"; [void]$failed.Add($Step) }

function Invoke-Native([string]$Exe, [string[]]$ArgList) {
    # Native call with stdout and stderr merged; never throws (Windows PowerShell 5.1 turns stderr into errors).
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = @(& $Exe @ArgList 2>&1 | ForEach-Object { "$_" })
        $code = $LASTEXITCODE
    } catch {
        $out = @("$_")
        $code = 1
    } finally {
        $ErrorActionPreference = $old
    }
    if ($null -eq $code) { $code = 0 }
    return New-Object PSObject -Property @{ Code = [int]$code; Out = $out }
}

function Find-Python {
    # The first interpreter that is 3.10 or newer: @{ Exe = full path; Version = "3.x"; From = where it was found }.
    # The probe prints one JSON line with non-ASCII escaped (\u00e9), so a path with accented letters survives
    # whatever code page the console uses. -X utf8 keeps Python's own output UTF-8 too.
    $probe = "import sys, json; print(json.dumps(dict(exe=sys.executable, ver='%d.%d' % sys.version_info[:2])))"
    $candidates = @()
    if ($env:WAYPOINT_PYTHON) { $candidates += , @($env:WAYPOINT_PYTHON, @(), "WAYPOINT_PYTHON") }
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) { $candidates += , @($py.Source, @("-3"), "py -3") }
    $python = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) { $candidates += , @($python.Source, @(), "python on PATH") }
    foreach ($c in $candidates) {
        $r = Invoke-Native $c[0] (@($c[1]) + @("-X", "utf8", "-c", $probe))
        $lines = @($r.Out | Where-Object { $_ -and $_.Trim().StartsWith("{") })
        if ($r.Code -ne 0 -or $lines.Count -lt 1) { continue }
        try { $info = $lines[-1].Trim() | ConvertFrom-Json -ErrorAction Stop } catch { continue }
        if (-not $info.exe -or -not $info.ver) { continue }
        $ver = $null
        if (-not [version]::TryParse([string]$info.ver, [ref]$ver)) { continue }
        if ($ver -lt [version]"3.10") { Say "python: $($c[2]) is $ver, older than 3.10; skipped"; continue }
        return @{ Exe = [string]$info.exe; Version = "$ver"; From = $c[2] }
    }
    return $null
}

function Get-ConfigPath {
    if ($env:WAYPOINT_CONFIG) { return $env:WAYPOINT_CONFIG }
    $home_ = if ($env:USERPROFILE) { $env:USERPROFILE } else { [Environment]::GetFolderPath("UserProfile") }
    return Join-Path $home_ ".waypoint\config.json"
}

function Write-Utf8([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}


function Get-UserPath {
    if ($env:WAYPOINT_TEST_PATH_STORE) {
        if (Test-Path -LiteralPath $env:WAYPOINT_TEST_PATH_STORE) {
            return ([string](Get-Content -LiteralPath $env:WAYPOINT_TEST_PATH_STORE -Encoding UTF8 -Raw)).Trim()
        }
        return ""
    }
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment")
    if (-not $key) { return "" }
    try { return [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }
    finally { $key.Close() }
}

function Set-UserPath([string]$Value) {
    if ($env:WAYPOINT_TEST_PATH_STORE) { Write-Utf8 $env:WAYPOINT_TEST_PATH_STORE $Value; return }
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Environment")
    try { $key.SetValue("Path", $Value, [Microsoft.Win32.RegistryValueKind]::ExpandString) } finally { $key.Close() }
    try {  # tell open programs the environment changed, so new terminals see the Path
        if (-not ("WaypointInstall.Native" -as [type])) {
            Add-Type -Namespace WaypointInstall -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Auto)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.UIntPtr lpdwResult);
'@
        }
        $result = [UIntPtr]::Zero
        [void][WaypointInstall.Native]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment", 2, 2000, [ref]$result)
    } catch { }
}

function Test-SameDir([string]$Entry, [string]$Dir) {
    $e = [Environment]::ExpandEnvironmentVariables($Entry.Trim().Trim('"')).TrimEnd([char]92)
    return ($e -and ($e -ieq $Dir.TrimEnd([char]92)))
}

# ---------------------------------------------------------------- 1. Python
$found = Find-Python
if ($found) {
    Say "python: $($found.Exe) ($($found.Version), from $($found.From))"
    [void]$done.Add("python $($found.Version)")
} else {
    Fail "python" "no Python 3.10+ found; set WAYPOINT_PYTHON (or install Python 3.10+) and re-run"
}

# ---------------------------------------------------------------- 1b. The config file
# Writes python and data_dir only when the file or that key is missing; a value already there is never changed.
# data_dir matters to the Claude Code mod: it runs from Claude Code's plugin cache, so without data_dir it cannot
# find this clone's data folder (and owner.token). Read and written as UTF-8 (no BOM), so non-ASCII paths survive.
$cfgPath = Get-ConfigPath
$dataDefault = [System.IO.Path]::GetFullPath((Join-Path $root "data"))
$cfg = $null
$readable = $true
if (Test-Path -LiteralPath $cfgPath) {
    try {
        $raw = [string](Get-Content -LiteralPath $cfgPath -Encoding UTF8 -Raw -ErrorAction Stop)
        $raw = $raw.TrimStart([char]0xFEFF)
        if ($raw.Trim()) { $cfg = $raw | ConvertFrom-Json -ErrorAction Stop }
    } catch { $readable = $false }
}
$want = [ordered]@{}
if ($found) { $want["python"] = $found.Exe }
$want["data_dir"] = $dataDefault
$missing = @($want.Keys | Where-Object { -not ($cfg -and $cfg.PSObject.Properties[$_] -and $cfg.$_) })
$kept = @($want.Keys | Where-Object { $missing -notcontains $_ })
if (-not $readable) {
    Say "config: $cfgPath is not valid JSON; left alone (add `"python`" and `"data_dir`" yourself)"
} elseif (-not $missing.Count) {
    Say "config: $($kept -join ' and ') already set in $cfgPath; kept"
} elseif ($DryRun) {
    Say "config: would write $($missing -join ' and ') to $cfgPath"
} else {
    if (-not $cfg) { $cfg = New-Object PSObject }
    foreach ($k in $missing) {
        if ($cfg.PSObject.Properties[$k]) { $cfg.$k = $want[$k] }
        else { $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $want[$k] }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cfgPath) | Out-Null
    Write-Utf8 $cfgPath ($cfg | ConvertTo-Json -Depth 10)
    $note = if ($kept.Count) { " (kept $($kept -join ' and '))" } else { "" }
    Say "config: wrote $($missing -join ' and ') to $cfgPath$note"
}

# ---------------------------------------------------------------- 2. The waypoint command
# The command is the shipped, ASCII-only bin\waypoint.cmd ("%~dp0..\scripts\waypoint.cmd" %*). Nothing with a
# path in it is generated, so a clone folder with accents works whatever code page cmd uses.
$binDir = Join-Path $root "bin"
$command = Join-Path $binDir "waypoint.cmd"
$oldBin = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Waypoint\bin" } else { "" }
if (-not (Test-Path -LiteralPath $command)) {
    Fail "command" "bin\waypoint.cmd is missing from $root"
} elseif (-not (Test-Path -LiteralPath (Join-Path $scripts "waypoint.cmd"))) {
    Fail "command" "scripts\waypoint.cmd is missing from $root"
} else {
    Say "command: $command"
    [void]$done.Add("command")
}
$oldShim = if ($oldBin) { Join-Path $oldBin "waypoint.cmd" } else { "" }
if ($oldShim -and (Test-Path -LiteralPath $oldShim) -and ([System.IO.File]::ReadAllText($oldShim)).Contains($oldMarker)) {
    if ($DryRun) {
        Say "command: would delete the old $oldShim"
    } else {
        Remove-Item -LiteralPath $oldShim -Force
        foreach ($d in @($oldBin, (Split-Path -Parent $oldBin))) {
            if ((Test-Path -LiteralPath $d) -and -not (Get-ChildItem -LiteralPath $d -Force | Select-Object -First 1)) {
                Remove-Item -LiteralPath $d -Force
            }
        }
        Say "command: deleted the old $oldShim (bin\waypoint.cmd replaces it)"
    }
}

if ($NoPath) {
    Say "PATH: skipped (-NoPath); run $command directly"
} else {
    $entries = @((Get-UserPath) -split ";" | Where-Object { $_.Trim() })
    $hasOld = $oldShim -and -not (Test-Path -LiteralPath $oldShim) -and
              (@($entries | Where-Object { Test-SameDir $_ $oldBin }).Count -gt 0)
    $keep = @($entries | Where-Object { -not ($hasOld -and (Test-SameDir $_ $oldBin)) })
    $hasNew = @($keep | Where-Object { Test-SameDir $_ $binDir }).Count -gt 0
    if ($hasNew -and -not $hasOld) {
        Say "PATH: already has $binDir"
    } elseif ($DryRun) {
        if ($hasOld) { Say "PATH: would remove the old $oldBin" }
        if (-not $hasNew) { Say "PATH: would add $binDir to the user Path" }
    } else {
        if (-not $hasNew) { $keep = @($keep) + $binDir }
        Set-UserPath ($keep -join ";")
        if ($hasOld) { Say "PATH: removed the old $oldBin" }
        if ($hasNew) { Say "PATH: already has $binDir" }
        else { Say "PATH: added $binDir to the user Path (open a new terminal to use 'waypoint')" }
    }
    [void]$done.Add("PATH")
}

# ---------------------------------------------------------------- 3. The engine
$portArgs = @()
if ($Port -gt 0) { $portArgs = @("-Port", "$Port") }
$shownPort = if ($Port -gt 0) { $Port } elseif ($env:WAYPOINT_PORT) { $env:WAYPOINT_PORT } else { 8751 }
if ($NoStart) {
    Say "engine: skipped (-NoStart)"
} elseif ($DryRun) {
    Say "engine: would run scripts\Start-Engine.ps1 on port $shownPort"
} elseif (-not $found) {
    Fail "engine" "not started because no Python was found"
} else {
    $r = Invoke-Native $powershell (@("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $scripts "Start-Engine.ps1")) + $portArgs)
    foreach ($line in $r.Out) { if ($line.Trim()) { Say $line.Trim() } }
    if ($r.Code -eq 0) { [void]$done.Add("engine on $shownPort") } else { Fail "engine" "Start-Engine.ps1 exited with $($r.Code)" }
}

# ---------------------------------------------------------------- 4. The Claude Code mod
$claude = Get-Command claude -CommandType Application, ExternalScript -ErrorAction SilentlyContinue | Select-Object -First 1
$market = Join-Path $root "claude_mod"
if ($NoMod) {
    Say "mod: skipped (-NoMod)"
} elseif (-not (Test-Path -LiteralPath $market)) {
    Say "mod: skipped (this tree has no claude_mod folder)"
} elseif (-not $claude) {
    Say "mod: skipped (claude is not on PATH); later run: claude plugin marketplace add `"$market`""
} elseif ($DryRun) {
    Say "mod: would run: claude plugin marketplace add `"$market`""
    Say "mod: would run: claude plugin install waypoint@waypoint-local --scope user"
} else {
    $steps = @(
        @("marketplace", @("plugin", "marketplace", "add", $market)),
        @("plugin", @("plugin", "install", "waypoint@waypoint-local", "--scope", "user"))
    )
    $modOk = $true
    foreach ($s in $steps) {
        $r = Invoke-Native $claude.Source $s[1]
        $text = ($r.Out -join " ").Trim()
        if ($r.Code -eq 0 -or $text -match "already") { Say "mod: $($s[0]) ok" }
        else { $modOk = $false; Fail "mod" "claude $($s[1] -join ' ') exited with $($r.Code): $text" }
    }
    if ($modOk) { [void]$done.Add("mod") }
}

# ---------------------------------------------------------------- 5. Start at sign-in
if ($AutoStart) {
    $regArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $scripts "Register-Engine.ps1")) + $portArgs
    if ($DryRun) { $regArgs += "-DryRun" }
    $r = Invoke-Native $powershell $regArgs
    if ($DryRun) { Say "autostart: would register the engine task (scripts\Register-Engine.ps1 -DryRun printed it)" }
    elseif ($r.Code -eq 0) { Say "autostart: $(($r.Out | Where-Object { $_.Trim() } | Select-Object -Last 1))"; [void]$done.Add("autostart") }
    else { Fail "autostart" (($r.Out -join " ").Trim()) }
}

# ---------------------------------------------------------------- summary
if ($failed.Count) {
    Say ("finished with problems: failed {0}; ok {1}" -f ($failed -join ", "), ($done -join ", "))
    exit 1
}
Say ("done: {0}" -f ($done -join ", "))
exit 0
