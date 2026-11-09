<#
.SYNOPSIS
    Reverses install.ps1: the start-at-sign-in task, the engine it started, the Claude Code mod, the waypoint
    command and its Path entry. Never deletes data\ or the config file. Safe to run again.
.DESCRIPTION
    1. Removes the engine's sign-in task (scripts\Register-Engine.ps1 -Unregister; -Port picks the task for that port).
    2. Stops this folder's engine on -Port (else env WAYPOINT_PORT, else 8751) with scripts\Stop-Engine.ps1, which
       stops only the engine named by this data dir's pid file; any other engine on that port is left alone
       (-KeepEngine skips this step).
    3. When `claude` is on PATH: claude plugin uninstall waypoint@waypoint-local --scope user, then
       claude plugin marketplace remove waypoint-local (-NoMod skips it).
    4. Removes this folder's bin folder from the user Path (-NoPath skips it). bin\waypoint.cmd itself is part of
       the clone and stays.
    5. Removes what an older install left: %LOCALAPPDATA%\Waypoint\bin\waypoint.cmd (only the one install.ps1
       wrote), its empty folders and its Path entry.
    -DryRun prints what each step would do and changes nothing. Exit 0 when every step worked, else 1.
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1
#>
param(
    [int]$Port = 0,
    [switch]$NoPath,
    [switch]$NoMod,
    [switch]$KeepEngine,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$scripts = Join-Path $root "scripts"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"  # Windows PowerShell, also from pwsh
$failed = New-Object System.Collections.ArrayList
$done = New-Object System.Collections.ArrayList
$pre = if ($DryRun) { "uninstall (dry run):" } else { "uninstall:" }
$marker = "Waypoint Core command (made by install.ps1"

function Say([string]$Text) { Write-Host "$pre $Text" }
function Fail([string]$Step, [string]$Text) { Say "$Step FAILED, $Text"; [void]$failed.Add($Step) }

function Invoke-Native([string]$Exe, [string[]]$ArgList) {
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
    try {
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

$portArgs = @()
if ($Port -gt 0) { $portArgs = @("-Port", "$Port") }

# ---------------------------------------------------------------- 1. Start at sign-in
$regArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $scripts "Register-Engine.ps1"), "-Unregister") + $portArgs
if ($DryRun) { $regArgs += "-DryRun" }
$r = Invoke-Native $powershell $regArgs
$last = ($r.Out | Where-Object { $_.Trim() } | Select-Object -Last 1)
if ($r.Code -eq 0) { Say "autostart: $last"; [void]$done.Add("autostart") } else { Fail "autostart" (($r.Out -join " ").Trim()) }

# ---------------------------------------------------------------- 2. The engine (before the command goes)
if ($KeepEngine) {
    Say "engine: left running (-KeepEngine)"
} else {
    $stopArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $scripts "Stop-Engine.ps1")) + $portArgs
    if ($DryRun) { $stopArgs += "-DryRun" }
    $r = Invoke-Native $powershell $stopArgs
    foreach ($line in $r.Out) { if ($line.Trim()) { Say $line.Trim() } }
    if ($r.Code -eq 0) { [void]$done.Add("engine") }
    elseif (($r.Out -join " ") -match "left alone") { Say "engine: not this folder's engine; left running" }
    else { Fail "engine" "Stop-Engine.ps1 exited with $($r.Code)" }
}

# ---------------------------------------------------------------- 3. The Claude Code mod
$claude = Get-Command claude -CommandType Application, ExternalScript -ErrorAction SilentlyContinue | Select-Object -First 1
if ($NoMod) {
    Say "mod: skipped (-NoMod)"
} elseif (-not (Test-Path -LiteralPath (Join-Path $root "claude_mod"))) {
    Say "mod: skipped (this tree has no claude_mod folder)"
} elseif (-not $claude) {
    Say "mod: skipped (claude is not on PATH)"
} elseif ($DryRun) {
    Say "mod: would run: claude plugin uninstall waypoint@waypoint-local --scope user"
    Say "mod: would run: claude plugin marketplace remove waypoint-local"
} else {
    $steps = @(
        @("plugin", @("plugin", "uninstall", "waypoint@waypoint-local", "--scope", "user")),
        @("marketplace", @("plugin", "marketplace", "remove", "waypoint-local"))
    )
    $modOk = $true
    foreach ($s in $steps) {
        $r = Invoke-Native $claude.Source $s[1]
        $text = ($r.Out -join " ").Trim()
        if ($r.Code -eq 0 -or $text -match "not (found|installed)|no such|does not exist") { Say "mod: $($s[0]) removed" }
        else { $modOk = $false; Fail "mod" "claude $($s[1] -join ' ') exited with $($r.Code): $text" }
    }
    if ($modOk) { [void]$done.Add("mod") }
}

# ---------------------------------------------------------------- 4. The waypoint command and its Path entry
$binDir = Join-Path $root "bin"
$oldBin = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Waypoint\bin" } else { "" }
if ($oldBin) {  # the forwarding file an older install wrote
    $shim = Join-Path $oldBin "waypoint.cmd"
    if (-not (Test-Path -LiteralPath $shim)) {
        Say "command: no old $shim"
    } elseif (-not ([System.IO.File]::ReadAllText($shim)).Contains($marker)) {
        Say "command: $shim was not written by install.ps1; left alone"
    } elseif ($DryRun) {
        Say "command: would delete the old $shim"
    } else {
        Remove-Item -LiteralPath $shim -Force
        foreach ($d in @($oldBin, (Split-Path -Parent $oldBin))) {
            if ((Test-Path -LiteralPath $d) -and -not (Get-ChildItem -LiteralPath $d -Force | Select-Object -First 1)) {
                Remove-Item -LiteralPath $d -Force
            }
        }
        Say "command: deleted the old $shim"
    }
}
[void]$done.Add("command")

# ---------------------------------------------------------------- 5. PATH
if ($NoPath) {
    Say "PATH: skipped (-NoPath)"
} else {
    $entries = @((Get-UserPath) -split ";" | Where-Object { $_.Trim() })
    $oldGone = $oldBin -and -not (Test-Path -LiteralPath (Join-Path $oldBin "waypoint.cmd"))
    $keep = @($entries | Where-Object { -not ((Test-SameDir $_ $binDir) -or ($oldGone -and (Test-SameDir $_ $oldBin))) })
    if ($keep.Count -eq $entries.Count) {
        Say "PATH: $binDir is not on the user Path"
    } elseif ($DryRun) {
        Say "PATH: would remove $binDir from the user Path"
    } else {
        Set-UserPath ($keep -join ";")
        Say "PATH: removed $binDir from the user Path"
    }
    [void]$done.Add("PATH")
}

Say "kept: data\ and the config file"

if ($failed.Count) {
    Say ("finished with problems: failed {0}; ok {1}" -f ($failed -join ", "), ($done -join ", "))
    exit 1
}
Say ("done: {0}" -f ($done -join ", "))
exit 0
