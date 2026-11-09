<#
.SYNOPSIS
    RETIRED (without -Legacy this script does nothing) in Waypoint 1.2: the always-on engine at 127.0.0.1:8751 is no longer started or registered at logon.
    Legacy:     Registers (or removes) the Task Scheduler task that keeps the engine running ("DshCC-Engine" for port 8751,
    "DshCC-Engine-<port>" for any other port).
.DESCRIPTION
    RETIRED in Waypoint 1.2: the always-on engine at 127.0.0.1:8751 is no longer started or registered at logon.
    Start the runner instead (it runs until idle, then exits): python -m waypoint_runner  (from runner\, pinned interpreter).
    Pass -Legacy to get the old behaviour; the CLI bridge on 8753 is retired with the engine.
    The task runs scripts\Start-Engine.ps1 -Foreground [-Port N] in a hidden PowerShell window:
      - at logon of the current user,
      - no idle condition (runs whether the PC is idle or not), on battery too, no time limit,
      - restarts up to 3 times, 1 minute apart, if the engine exits with an error,
      - no elevation (least privilege, current user's interactive session).
    A deliberate Stop-Engine.ps1 counts as success, so it does not trigger a restart.
    -DryRun prints the task XML and the command, and changes nothing.
    -Unregister removes the task (with -DryRun, only says it would).
.PARAMETER Port
    Engine port (default: env WAYPOINT_PORT, else 8751). The data dir is read by Start-Engine.ps1 at each start.
.EXAMPLE
    powershell -NoProfile -File scripts\Register-Engine.ps1 -DryRun
.EXAMPLE
    powershell -NoProfile -File scripts\Register-Engine.ps1 -Unregister
#>
param(
    [int]$Port = $(if ($env:WAYPOINT_PORT) { [int]$env:WAYPOINT_PORT } else { 8751 }),
    [string]$TaskName = "",
    [switch]$DryRun,
    [switch]$Unregister,
    [switch]$Legacy
)

$ErrorActionPreference = "Stop"
if (-not $Legacy -and -not $Unregister) {
    Write-Output "engine: RETIRED in Waypoint 1.2. The always-on engine (port 8751) is no longer registered at logon."
    Write-Output "Start the runner instead (runs until idle, then exits): cd runner; <pinned python> -m waypoint_runner   (or --once for one tick)."
    Write-Output "Pass -Legacy only to bring the old engine back."
    exit 0
}
if (-not $TaskName) { $TaskName = if ($Port -eq 8751) { "DshCC-Engine" } else { "DshCC-Engine-$Port" } }

function ConvertTo-XmlText([string]$s) {
    return [System.Security.SecurityElement]::Escape($s)
}

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($DryRun) {
        Write-Output ("DryRun: would run: Unregister-ScheduledTask -TaskName '{0}' -Confirm:`$false ({1})" -f $TaskName, $(if ($existing) { "task exists" } else { "task not present" }))
        return
    }
    if (-not $existing) { Write-Output "Task '$TaskName' is not registered; nothing to remove."; return }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed scheduled task '$TaskName'. The engine itself keeps running until Stop-Engine.ps1."
    return
}

$startScript = Join-Path $PSScriptRoot "Start-Engine.ps1"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$startScript`" -Foreground -Legacy"
if ($Port -ne 8751) { $arguments += " -Port $Port" }
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Waypoint engine (keeps the local engine at 127.0.0.1:$Port running)</Description>
    <URI>\$(ConvertTo-XmlText $TaskName)</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(ConvertTo-XmlText $user)</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(ConvertTo-XmlText $user)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(ConvertTo-XmlText $powershell)</Command>
      <Arguments>$(ConvertTo-XmlText $arguments)</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText (Split-Path -Parent $PSScriptRoot))</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

if ($DryRun) {
    Write-Output $xml
    Write-Output ""
    Write-Output "DryRun: would run: Register-ScheduledTask -TaskName '$TaskName' -Xml <above> -Force"
    return
}

if (-not (Test-Path -LiteralPath $startScript)) { throw "Start-Engine.ps1 not found: $startScript" }
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Output "Registered scheduled task '$TaskName' (at logon, restart on failure 3x). Start now: Start-ScheduledTask -TaskName '$TaskName'"
