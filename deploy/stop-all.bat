@echo off
powershell -NoProfile -Command "Stop-Service AutusTunnel,AutusFrontend,AutusBackend -ErrorAction Stop"
if errorlevel 1 echo Execute este arquivo como administrador.
