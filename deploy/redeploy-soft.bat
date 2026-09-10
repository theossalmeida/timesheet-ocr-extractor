@echo off
rem Redeploy rapido: baixa a revisao e troca so o codigo do backend,
rem reaproveitando o virtualenv ja instalado. Para mudancas de frontend ou
rem de dependencias pesadas, use update-all.bat.
rem Passe -DryRun para ver o que seria feito sem alterar nada.
net session >nul 2>&1
if errorlevel 1 (
    echo Execute este arquivo como administrador.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0redeploy-soft.ps1" %*
if errorlevel 1 (
    echo.
    echo O redeploy rapido falhou. Use update-all.bat para uma instalacao completa.
)
pause
