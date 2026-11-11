@echo off
rem ===== Add this Windows PC's GPU to the Home AI server =====
rem Download this file from the AI server (http://SERVER:8090/add-this-pc.bat) - it has the server address filled in.
rem Just double-click. Click "Yes" when Windows asks for permission.
title Add this PC's GPU to the AI server
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set SERVER=__SERVER_IP__
if "%SERVER:~0,2%"=="__" (
  echo This copy has no server address. Download it from http://YOUR-AI-SERVER:8090/add-this-pc.bat instead.
  pause
  exit /b 1
)
echo Downloading worker installer from the AI server...
powershell -NoProfile -Command "Invoke-WebRequest http://%SERVER%:8090/add-worker-windows.ps1 -OutFile $env:TEMP\add-worker-windows.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\add-worker-windows.ps1" -Server %SERVER%
echo.
echo Done. If you see WORKER-OK above, this PC's GPU is now part of the AI server.
pause
