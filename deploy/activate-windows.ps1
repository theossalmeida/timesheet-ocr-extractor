[CmdletBinding()]
param(
    [string]$Root = 'C:\ProgramData\Autus',
    [string]$LegacyProjectPath,
    [int]$LegacyTunnelProcessId = 0
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator PowerShell is required.' }
$services = Join-Path $Root 'services'
$definitions = Get-ChildItem $services -Filter 'Autus*.xml.pending'
if ($definitions.Count -lt 2) { throw 'Run install-windows.ps1 -PrepareOnly first.' }
$backup = Join-Path $Root ('service-backups\'+(Get-Date -Format yyyyMMddHHmmss))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Executable @Arguments 2>&1 | ForEach-Object { $_.ToString() }; $result = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
    if ($result -ne 0) { throw "$Executable failed with exit code $result" }
}
function Grant-Private([string]$Path, [string]$ServiceName) {
    Invoke-Checked 'icacls.exe' @($Path,'/inheritance:r','/grant:r','*S-1-5-18:(F)','*S-1-5-32-544:(F)',"NT SERVICE\${ServiceName}:(R)")
}
foreach ($pending in $definitions) {
    [xml]$xml = Get-Content $pending.FullName -Raw
    $name = [string]$xml.service.id
    if ($name -notin @('AutusBackend','AutusFrontend','AutusTunnel')) { throw 'Unexpected service definition.' }
    $existing = Get-Service $name -ErrorAction SilentlyContinue
    $definition = Join-Path $services "$name.xml"
    if ($existing) {
        $details = Get-CimInstance Win32_Service -Filter "Name='$name'"
        if ($details.PathName -notlike "*$services*") { throw 'Existing service is outside the AUTUS installation.' }
        Copy-Item $definition $backup
        Stop-Service $name
    }
    Copy-Item $pending.FullName $definition -Force
    if (-not $existing) { Invoke-Checked (Join-Path $services "$name.exe") @('install') }
    Invoke-Checked 'sc.exe' @('sidtype',$name,'unrestricted')
    foreach ($path in @((Join-Path $Root "logs\$name"),(Join-Path $Root "tmp\$name"))) {
        Invoke-Checked 'icacls.exe' @($path,'/grant:r',"NT SERVICE\${name}:(OI)(CI)(M)")
    }
    if ($name -eq 'AutusBackend') {
        $backend = [string]$xml.service.workingdirectory
        Grant-Private (Join-Path $backend '.env') $name
        $release = Split-Path $backend
    }
    if ($name -eq 'AutusFrontend') {
        Invoke-Checked 'icacls.exe' @((Join-Path ([string]$xml.service.workingdirectory) '.next'),'/grant:r','NT SERVICE\AutusFrontend:(OI)(CI)(M)')
    }
    if ($name -eq 'AutusTunnel') {
        foreach ($file in @('tunnel.token','tunnel.credentials.json')) {
            $path = Join-Path $Root "config\$file"
            if (Test-Path $path) { Grant-Private $path $name }
        }
    }
}
if ($LegacyProjectPath) {
    Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','node.exe') -and $_.CommandLine -and $_.CommandLine.Contains($LegacyProjectPath) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
Start-Service AutusBackend
Start-Service AutusFrontend
if (Get-Service AutusTunnel -ErrorAction SilentlyContinue) { Start-Service AutusTunnel }
$healthy = $false
for ($attempt=0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:3000/api/health' -TimeoutSec 5
        if ($health.status -eq 'ok' -and $health.version -eq '2.0.0') { $healthy=$true; break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $healthy) { throw 'AUTUS v2 did not become healthy. Inspect service logs; previous files remain intact.' }
if ($LegacyTunnelProcessId) {
    $legacy = Get-CimInstance Win32_Process -Filter "ProcessId=$LegacyTunnelProcessId"
    if ($legacy -and $legacy.Name -eq 'cloudflared.exe' -and $legacy.CommandLine -match 'tunnel run timesheet') {
        Stop-Process -Id $LegacyTunnelProcessId
    }
}
Invoke-Checked 'powercfg.exe' @('/change','standby-timeout-ac','0')
Invoke-Checked 'powercfg.exe' @('/change','hibernate-timeout-ac','0')
[IO.File]::WriteAllText((Join-Path $Root 'active-release.txt'),$release)
Get-Service AutusBackend,AutusFrontend,AutusTunnel | Select-Object Name,Status,StartType
Write-Output 'AUTUS v2 is healthy. Verify public access and restart recovery.'
