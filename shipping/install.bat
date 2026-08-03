@echo off
REM Eden OE Synth Installer - double-click entry (Windows)
REM This runs the PowerShell installer with the right policy.
title Eden OE Synth Installer
echo.
echo  === Eden OE Synth Installer ===
echo  This will install the runtime, ask for your API key,
echo  and birth your synthetic person. ~5-10 minutes.
echo.
pause
powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
echo  Installer finished. Press any key to close.
pause >nul
