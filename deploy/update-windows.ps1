[CmdletBinding()]
param(
    [string]$PythonExe = 'C:\ProgramData\Autus\runtimes\python\python.exe',
    [string]$NodeExe = 'C:\Program Files\nodejs\node.exe',
    [string]$CloudflaredExe = 'C:\ProgramData\Autus\runtimes\cloudflared.exe',
    [string]$TunnelConfigFile = 'C:\Users\theoa\.cloudflared\config.yml',
    [string]$Branch = 'main'
)

# Pulls the reviewed revision and rebuilds AUTUS in one step. The installer
# stops the services itself, so the pull runs first: a failed pull then costs
# no downtime at all.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Execute este script em uma janela do PowerShell como administrador.'
}

$source = Split-Path -Parent $PSScriptRoot
Write-Host "Repositorio: $source" -ForegroundColor Cyan

$before = (& git -C $source rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Nao foi possivel ler a revisao atual em $source." }

# Local edits to tracked files would be silently carried into the release.
$dirty = (& git -C $source status --porcelain --untracked-files=no)
if ($dirty) {
    Write-Host $dirty -ForegroundColor Yellow
    throw 'Ha alteracoes locais nao commitadas. Resolva antes de atualizar.'
}

$current = (& git -C $source rev-parse --abbrev-ref HEAD).Trim()
if ($current -ne $Branch) {
    throw "O repositorio esta em '$current', nao em '$Branch'. Use -Branch $current se for intencional."
}

Write-Host "Baixando $Branch..." -ForegroundColor Cyan
& git -C $source fetch origin $Branch
if ($LASTEXITCODE -ne 0) { throw 'git fetch falhou.' }
& git -C $source merge --ff-only "origin/$Branch"
if ($LASTEXITCODE -ne 0) { throw 'git merge --ff-only falhou. Verifique a branch local.' }

$after = (& git -C $source rev-parse --short HEAD).Trim()
if ($before -eq $after) {
    Write-Host "Ja esta em $after. Reinstalando mesmo assim." -ForegroundColor Yellow
} else {
    Write-Host "$before -> $after" -ForegroundColor Green
    & git -C $source log --oneline "$before..$after"
}

# Reuse the tunnel exactly as it is installed today: an existing Cloudflared
# service must not be replaced by a second connector.
$installer = Join-Path $PSScriptRoot 'install-windows.ps1'
$arguments = @{ PythonExe = $PythonExe; NodeExe = $NodeExe; CloudflaredExe = $CloudflaredExe }
if (Get-Service -Name 'Cloudflared' -ErrorAction SilentlyContinue) {
    Write-Host 'Servico Cloudflared existente sera reaproveitado.' -ForegroundColor Cyan
    $arguments['UseExistingTunnelService'] = $true
} else {
    $arguments['TunnelConfigFile'] = $TunnelConfigFile
}

Write-Host 'Reinstalando servicos...' -ForegroundColor Cyan
# The installer throws on failure (its own ErrorActionPreference is Stop), and
# that propagates here. Its exit code is not a verdict - trailing native calls
# leave their own value in $LASTEXITCODE - so health is checked below instead.
& $installer @arguments

# The installer starts the services; confirm they actually answer.
$healthy = $false
foreach ($attempt in 1..30) {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 5
        if ($health.status -eq 'ok') { $healthy = $true; break }
    } catch { }
}

Get-Service AutusBackend,AutusFrontend -ErrorAction SilentlyContinue | Format-Table Name,Status,StartType
if (-not $healthy) {
    throw 'Backend nao respondeu /health. Veja C:\ProgramData\Autus\logs.'
}
Write-Host "AUTUS atualizado para $after e respondendo." -ForegroundColor Green
Write-Host 'Confira o app publico em https://timesheet.theosantoro.dev' -ForegroundColor Green
