@echo off
rem Atualiza o AUTUS: baixa a revisao mais recente e reinstala os servicos.
rem Execute como administrador (botao direito > Executar como administrador).
net session >nul 2>&1
if errorlevel 1 (
    echo Execute este arquivo como administrador.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-windows.ps1" %*
if errorlevel 1 (
    echo.
    echo A atualizacao falhou. Verifique a mensagem acima e os logs em C:\ProgramData\Autus\logs.
)
pause
