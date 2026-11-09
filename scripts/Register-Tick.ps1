<#
.SYNOPSIS
    Registers the Task Scheduler task "DshCC-Tick" that runs one Control Centre tick.
.DESCRIPTION
    The task runs `python -m cc_engine tick` from the engine folder:
      - every 10 minutes (indefinitely),
      - at logon of the current user,
      - on resume from sleep (System log, Microsoft-Windows-Power-Troubleshooter, event 1).
    Ticks are idempotent (lease row in SQLite), so overlapping triggers are harmless.
    Multiple instances are ignored (IgnoreNew); a run is stopped after 30 minutes.
    -DryRun prints the task XML and the command that would register it, and changes nothing.
    -Unregister removes the task (with -DryRun, only says it would).
.PARAMETER Python
    Interpreter used for the tick.
.PARAMETER EngineDir
    Working directory containing the cc_engine package.
.PARAMETER TaskName
    Task Scheduler name (default DshCC-Tick).
.PARAMETER DryRun
    Print the XML instead of registering.
.EXAMPLE
    powershell -NoProfile -File scripts\Register-Tick.ps1 -DryRun
#>
param(
    [string]$Python = $(. (Join-Path $PSScriptRoot "waypoint_config.ps1"); Get-WaypointPython),
    [string]$EngineDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "engine"),
    [string]$TaskName = "DshCC-Tick",
    [switch]$DryRun,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($DryRun) {
        Write-Output ("DryRun: would remove scheduled task '{0}' ({1})" -f $TaskName, $(if ($existing) { "task exists" } else { "task not present" }))
        return
    }
    if (-not $existing) { Write-Output "Task '$TaskName' is not registered; nothing to remove."; return }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed scheduled task '$TaskName'."
    return
}

function ConvertTo-XmlText([string]$s) {
    return [System.Security.SecurityElement]::Escape($s)
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$start = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
$resumeQuery = "<QueryList><Query Id=`"0`" Path=`"System`"><Select Path=`"System`">*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>"

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>dsh Control Centre: run one scheduler tick (idempotent, lease-protected).</Description>
    <URI>\$(ConvertTo-XmlText $TaskName)</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT10M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(ConvertTo-XmlText $user)</UserId>
    </LogonTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>$(ConvertTo-XmlText $resumeQuery)</Subscription>
      <Delay>PT1M</Delay>
    </EventTrigger>
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
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <AllowStartOnDemand>true</AllowStartOnDemand>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(ConvertTo-XmlText $Python)</Command>
      <Arguments>-m cc_engine tick</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText $EngineDir)</WorkingDirectory>
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

if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath (Join-Path $EngineDir "cc_engine"))) { throw "cc_engine not found under $EngineDir" }

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Output "Registered scheduled task '$TaskName' (every 10 min, at logon, on resume from sleep)."
