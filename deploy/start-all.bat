@echo off
powershell -NoProfile -Command "Start-Service AutusBackend,AutusFrontend,AutusTunnel -ErrorAction Stop; Get-Service AutusBackend,AutusFrontend,AutusTunnel"
if errorlevel 1 echo Execute este arquivo como administrador.
