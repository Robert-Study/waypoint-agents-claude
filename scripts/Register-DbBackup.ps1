# Registers a nightly Task Scheduler job that backs up the Waypoint engine database.
# -DryRun prints the task it would add and changes nothing.
param([switch]$DryRun, [string]$At = "02:30")
$repo = Split-Path -Parent $PSScriptRoot
$py = $(. (Join-Path $PSScriptRoot "waypoint_config.ps1"); Get-WaypointPython)
$script = Join-Path $repo "scripts\backup_db.py"
$name = "Waypoint-DbBackup"
if ($DryRun) {
  Write-Output "Would register task '$name' daily at $At :"
  Write-Output "  $py `"$script`" --keep 14"
  exit 0
}
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$script`" --keep 14" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Description "Nightly backup of the Waypoint engine database (keeps 14)." -Force | Out-Null
Write-Output "Registered '$name' daily at $At"
