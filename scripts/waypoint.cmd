@echo off
rem waypoint: runs scripts\waypoint_cli.py from any folder, also when called through a forwarding shim
rem (for example the product's bin\waypoint.cmd, which install.ps1 puts on the user Path).
rem Python: %WAYPOINT_PYTHON% (fast path, no PowerShell), else Get-WaypointPython in waypoint_config.ps1:
rem the python key in the settings file, then the original install's venv if it exists, then py -3, then python.
rem No labels or goto, so the file works with LF or CRLF line endings.
setlocal
set "WP_DIR=%~dp0"
set "WP_CFG=%~dp0waypoint_config.ps1"
set "WP_PY=%WAYPOINT_PYTHON%"
if not defined WP_PY for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ". $env:WP_CFG; Get-WaypointPython"`) do set "WP_PY=%%P"
if not defined WP_PY (
  echo waypoint: no Python 3.10 or newer found; install Python or set WAYPOINT_PYTHON to python.exe 1>&2
  exit /b 1
)
"%WP_PY%" "%WP_DIR%waypoint_cli.py" %*
exit /b %ERRORLEVEL%
