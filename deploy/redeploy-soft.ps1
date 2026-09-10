[CmdletBinding()]
param(
    [string]$Root = 'C:\ProgramData\Autus',
    [string]$Branch = 'main',
    [switch]$DryRun
)

# Soft redeploy: pull, swap the backend code into the release that is already
# installed, restart the backend. Keeps the existing virtualenv, so it takes
# seconds instead of rebuilding Python and Node dependencies.
#
# It refuses to run when the change needs more than that - a frontend change
# needs a Next.js build - and installs Python dependencies only when
# requirements.txt actually changed. Use deploy\update-all.bat for everything
# else; that is the full path and the one to fall back on.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Execute este script em uma janela do PowerShell como administrador.'
}

$source = Split-Path -Parent $PSScriptRoot
$releaseFile = Join-Path $Root 'active-release.txt'
if (-not (Test-Path $releaseFile)) { throw "Nenhuma release ativa em $releaseFile. Use deploy\update-all.bat." }
$release = (Get-Content $releaseFile -Raw).Trim()
$releaseBackend = Join-Path $release 'backend'
$runtimePython = Join-Path $releaseBackend '.venv\Scripts\python.exe'
if (-not (Test-Path $runtimePython)) { throw "Virtualenv nao encontrado em $runtimePython. Use deploy\update-all.bat." }

$current = (& git -C $source rev-parse --abbrev-ref HEAD).Trim()
if ($current -ne $Branch) { throw "O repositorio esta em '$current', nao em '$Branch'." }
$dirty = (& git -C $source status --porcelain --untracked-files=no)
if ($dirty) {
    Write-Host $dirty -ForegroundColor Yellow
    throw 'Ha alteracoes locais nao commitadas. Resolva antes de atualizar.'
}

$before = (& git -C $source rev-parse --short HEAD).Trim()
Write-Host "Baixando $Branch..." -ForegroundColor Cyan
& git -C $source fetch origin $Branch
if ($LASTEXITCODE -ne 0) { throw 'git fetch falhou.' }
& git -C $source merge --ff-only "origin/$Branch"
if ($LASTEXITCODE -ne 0) { throw 'git merge --ff-only falhou.' }
$after = (& git -C $source rev-parse --short HEAD).Trim()
if ($before -eq $after) { Write-Host "Ja esta em $after." -ForegroundColor Yellow } else { Write-Host "$before -> $after" -ForegroundColor Green }

# The release folder is named <revision>-<timestamp>, so the revision it was
# built from says what changed since - unless a previous soft redeploy already
# swapped its code, which is what the marker below records.
$revisionMarker = Join-Path $release 'SOFT-REVISION.txt'
if (Test-Path $revisionMarker) { $deployed = (Get-Content $revisionMarker -Raw).Trim() }
else { $deployed = (Split-Path -Leaf $release) -replace '-\d{14}$', '' }
$comparable = $false
if ($deployed) {
    & git -C $source cat-file -e "$deployed^{commit}" 2>$null
    $comparable = ($LASTEXITCODE -eq 0)
}

if ($comparable) {
    $changed = @(& git -C $source diff --name-only "$deployed..HEAD")
    if ($changed | Where-Object { $_ -like 'frontend/*' }) {
        throw 'Esta atualizacao altera o frontend e exige build completo. Use deploy\update-all.bat.'
    }
    if (-not $changed) { Write-Host 'Nenhum arquivo alterado desde a release ativa.' -ForegroundColor Yellow }
} else {
    Write-Host "Nao foi possivel comparar com a release ativa ($deployed): o frontend nao sera atualizado." -ForegroundColor Yellow
}

# Compare before copying - afterwards both files are identical by definition.
$sourceRequirements = Join-Path $source 'backend\requirements.txt'
$releaseRequirements = Join-Path $releaseBackend 'requirements.txt'
$installDependencies = -not (Test-Path $releaseRequirements) -or
    (Get-FileHash $sourceRequirements).Hash -ne (Get-FileHash $releaseRequirements).Hash

if ($DryRun) {
    Write-Host ''
    Write-Host 'DryRun - nada foi alterado.' -ForegroundColor Cyan
    Write-Host "  release ativa : $release"
    Write-Host "  revisao       : $deployed -> $after"
    Write-Host "  dependencias  : $(if ($installDependencies) { 'pip install sera executado' } else { 'inalteradas' })"
    return
}

Write-Host 'Parando AutusBackend...' -ForegroundColor Cyan
Stop-Service AutusBackend -ErrorAction Stop

try {
    # robocopy merges into the existing release folder; Copy-Item -Recurse can
    # nest a directory inside its own copy when the destination already exists.
    # Same exclusions the installer uses: the virtualenv, the private .env and
    # test/bytecode folders never come from the checkout. No /PURGE - deleting
    # by mirror here would put the virtualenv one wrong flag away from erasure.
    robocopy (Join-Path $source 'backend') $releaseBackend /E /NFL /NDL /NJH /NJS /NP `
        /XD '.venv' '__pycache__' '.pytest_cache' 'tests' /XF '.env' | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy falhou com codigo $LASTEXITCODE." }
    $global:LASTEXITCODE = 0
    # Copying does not remove files deleted upstream; clearing bytecode at least
    # stops a stale .pyc from being imported in place of a removed module.
    Get-ChildItem $releaseBackend -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $Root 'config\backend.env') (Join-Path $releaseBackend '.env') -Force

    if ($installDependencies) {
        Write-Host 'requirements.txt mudou: instalando dependencias...' -ForegroundColor Cyan
        & $runtimePython -m pip install --disable-pip-version-check -r $releaseRequirements
        if ($LASTEXITCODE -ne 0) { throw 'pip install falhou.' }
    } else {
        Write-Host 'Dependencias inalteradas: virtualenv reaproveitado.' -ForegroundColor Cyan
    }
} finally {
    Start-Service AutusBackend
}

$healthy = $false
foreach ($attempt in 1..30) {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 5
        if ($health.status -eq 'ok') { $healthy = $true; break }
    } catch { }
}

Get-Service AutusBackend | Format-Table Name, Status, StartType
if (-not $healthy) { throw "Backend nao respondeu /health. Veja $Root\logs, ou use deploy\update-all.bat." }
# The release folder still carries the revision it was built from; record what
# is actually running so the next comparison is against the right commit.
[IO.File]::WriteAllText($revisionMarker, $after)
Write-Host "Backend atualizado para $after e respondendo." -ForegroundColor Green
