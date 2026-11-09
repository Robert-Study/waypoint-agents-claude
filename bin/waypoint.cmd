@echo off
rem waypoint: the Waypoint Core command. install.ps1 puts this folder (the clone's bin folder) on the user
rem Path once; uninstall.ps1 takes it off again. It runs scripts\waypoint.cmd next door. It holds no paths,
rem so it reads the same in every code page. No 'call' prefix: it would expand percent signs a second time and
rem double carets in the arguments. As the last line, this hands control over, so the exit code comes back.
"%~dp0..\scripts\waypoint.cmd" %*
