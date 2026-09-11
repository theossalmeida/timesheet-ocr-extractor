[CmdletBinding()]
param(
    [string]$Root = 'C:\ProgramData\Autus',
    [string]$Origin = 'https://timesheet.theosantoro.dev',
    [string]$PythonExe = 'C:\Program Files\Python312\python.exe',
    [string]$NodeExe = 'C:\Program Files\nodejs\node.exe',
    [string]$TesseractExe = 'C:\Program Files\Tesseract-OCR\tesseract.exe',
    [string]$CloudflaredExe = 'C:\Program Files\cloudflared\cloudflared.exe',
    [string]$TunnelTokenFile,
    [string]$TunnelConfigFile,
    [string]$LegacyProjectPath,
    [switch]$UseExistingTunnelService,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer in an Administrator PowerShell window.'
}
if ($Origin -notmatch '^https://[a-zA-Z0-9.-]+(:[0-9]+)?$') { throw 'Origin must be an HTTPS origin without a trailing slash.' }
if (-not $UseExistingTunnelService -and -not $TunnelTokenFile -and -not $TunnelConfigFile) { throw 'Provide -TunnelTokenFile, -TunnelConfigFile, or -UseExistingTunnelService.' }
$source = Split-Path -Parent $PSScriptRoot
$revisionFile = Join-Path $source 'RELEASE'
if (Test-Path $revisionFile) { $revision = (Get-Content $revisionFile -Raw).Trim() }
else { $revision = (& git -C $source rev-parse --short HEAD).Trim(); if ($LASTEXITCODE -ne 0) { throw 'Cannot read the Git revision.' } }
$release = Join-Path $Root "releases\$revision-$(Get-Date -Format yyyyMMddHHmmss)"
$backend = Join-Path $release 'backend'
$frontend = Join-Path $release 'frontend'
$config = Join-Path $Root 'config'
$services = Join-Path $Root 'services'
$logs = Join-Path $Root 'logs'
$temporary = Join-Path $Root 'tmp'
foreach ($binary in @($PythonExe, $NodeExe, $TesseractExe)) {
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "Required executable missing: $binary" }
}
if (-not $UseExistingTunnelService -and -not (Test-Path -LiteralPath $CloudflaredExe)) { throw 'cloudflared.exe is missing.' }
$pythonVersion = & $PythonExe -c 'import sys; print(sys.version_info >= (3,12))'
if ($pythonVersion -ne 'True') { throw 'Python 3.12 or later is required.' }
$nodeVersion = (& $NodeExe --version).TrimStart('v').Split('.')[0]
if ([int]$nodeVersion -lt 22) { throw 'Install a currently supported Node.js release (22 or newer).' }
if ($UseExistingTunnelService) {
    $existing = Get-Service -Name Cloudflared -ErrorAction SilentlyContinue
    if (-not $existing) { throw 'The existing Cloudflared Windows service was not found.' }
}
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Executable @Arguments 2>&1 | ForEach-Object { $_.ToString() }; $resultCode = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previousPreference }
    if ($resultCode -ne 0) { throw "$Executable failed with exit code $resultCode" }
}
function Set-PrivateFile([string]$Path, [string]$ServiceName) {
    Invoke-Checked 'icacls.exe' @($Path, '/inheritance:r', '/grant:r', '*S-1-5-18:(F)', '*S-1-5-32-544:(F)')
    if ($ServiceName) { Invoke-Checked 'icacls.exe' @($Path, '/grant:r', "NT SERVICE\${ServiceName}:(R)") }
}
function Set-EnvValue([string]$Text, [string]$Key, [string]$Value) {
    return ([regex]::Replace($Text, "(?m)^$Key=.*\r?\n?", '')).TrimEnd() + "`r`n$Key=$Value`r`n"
}
foreach ($directory in @($backend, $frontend, $config, $services, $logs, $temporary)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
Invoke-Checked 'icacls.exe' @($Root, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)(F)', '*S-1-5-32-544:(OI)(CI)(F)', '*S-1-5-19:(OI)(CI)(RX)')
$environmentPath = Join-Path $config 'backend.env'
if (-not (Test-Path $environmentPath)) {
    $sourceEnv = Join-Path $source 'backend\.env'
    if (Test-Path $sourceEnv) { Copy-Item $sourceEnv $environmentPath }
    else { [IO.File]::WriteAllText($environmentPath, '') }
}
Set-PrivateFile $environmentPath ''
$environment = [IO.File]::ReadAllText($environmentPath)
if ($environment -notmatch '(?m)^DATABASE_URL=.+') {
    $secretFile = Join-Path $source 'secreties.txt'
    if (-not (Test-Path $secretFile)) { throw 'Set DATABASE_URL in config\backend.env or provide secreties.txt in the repository root.' }
    $match = [regex]::Match([IO.File]::ReadAllText($secretFile), 'postgres(?:ql)?://[^\s''"]+')
    if (-not $match.Success) { throw 'No PostgreSQL connection string found.' }
    $environment = Set-EnvValue $environment 'DATABASE_URL' $match.Value
}
if ($environment -notmatch '(?m)^BOOTSTRAP_TOKEN=.+') {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes)
    $random.Dispose()
    $bootstrap = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $environment = Set-EnvValue $environment 'BOOTSTRAP_TOKEN' $bootstrap
}
$environment = Set-EnvValue $environment 'APP_ORIGIN' $Origin
$environment = Set-EnvValue $environment 'COOKIE_SECURE' 'true'
$environment = Set-EnvValue $environment 'ENVIRONMENT' 'production'
$environment = Set-EnvValue $environment 'CORS_ORIGINS' "[`"$Origin`"]"
$environment = Set-EnvValue $environment 'TESSERACT_CMD' ($TesseractExe.Replace('\','/'))
[IO.File]::WriteAllText($environmentPath, $environment, [Text.UTF8Encoding]::new($false))
Get-ChildItem (Join-Path $source 'backend') -Force | Where-Object { $_.Name -notin @('.venv','.env','__pycache__','.pytest_cache','tests') } | Copy-Item -Destination $backend -Recurse -Force
Invoke-Checked $PythonExe @('-m','venv',(Join-Path $backend '.venv'))
$runtimePython = Join-Path $backend '.venv\Scripts\python.exe'
Invoke-Checked $runtimePython @('-m','pip','install','--disable-pip-version-check','-r',(Join-Path $backend 'requirements.txt'))
$npm = Join-Path (Split-Path $NodeExe) 'npm.cmd'
$env:NEXT_TELEMETRY_DISABLED = '1'
$env:BACKEND_URL = 'http://127.0.0.1:8000'
Push-Location (Join-Path $source 'frontend')
try {
    Invoke-Checked $npm @('ci','--no-fund')
    Invoke-Checked $npm @('run','build')
} finally { Pop-Location }
Copy-Item (Join-Path $source 'frontend\.next\standalone\*') $frontend -Recurse -Force
Get-ChildItem (Join-Path $source 'frontend\.next\standalone') -Force | Where-Object { $_.Name.StartsWith('.') } | Copy-Item -Destination $frontend -Recurse -Force
Copy-Item (Join-Path $source 'frontend\.next\static') (Join-Path $frontend '.next\static') -Recurse -Force
if (Test-Path (Join-Path $source 'frontend\public')) { Copy-Item (Join-Path $source 'frontend\public') (Join-Path $frontend 'public') -Recurse -Force }
$wrapper = Join-Path $services 'WinSW-x64.exe'
if (-not (Test-Path $wrapper)) {
    Invoke-WebRequest 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe' -OutFile $wrapper -UseBasicParsing
}
if ((Get-FileHash $wrapper -Algorithm SHA256).Hash -ne '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA') {
    throw 'WinSW checksum verification failed.'
}
function Write-Service([string]$Name, [string]$Executable, [string]$Arguments, [string]$Directory, [string]$ExtraEnvironment) {
    $serviceLog = Join-Path $logs $Name
    $serviceTemp = Join-Path $temporary $Name
    New-Item -ItemType Directory -Path $serviceLog,$serviceTemp -Force | Out-Null
    $escape = { param($value) [Security.SecurityElement]::Escape($value) }
    $xml = @"
<service>
  <id>$Name</id>
  <name>$Name</name>
  <description>AUTUS production service</description>
  <executable>$(& $escape $Executable)</executable>
  <arguments>$(& $escape $Arguments)</arguments>
  <workingdirectory>$(& $escape $Directory)</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <serviceaccount><domain>NT AUTHORITY</domain><user>LocalService</user></serviceaccount>
  <onfailure action="restart" delay="10 sec" />
  <onfailure action="restart" delay="30 sec" />
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>180 sec</stoptimeout>
  <stopparentprocessfirst>true</stopparentprocessfirst>
  <logpath>$(& $escape $serviceLog)</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>7</keepFiles></log>
  <env name="TEMP" value="$(& $escape $serviceTemp)" />
  <env name="TMP" value="$(& $escape $serviceTemp)" />
  $ExtraEnvironment
</service>
"@
    [xml]$checked = $xml
    $exe = Join-Path $services "$Name.exe"
    if (-not (Test-Path $exe)) { Copy-Item $wrapper $exe }
    $definition = Join-Path $services "$Name.xml"
    [IO.File]::WriteAllText("$definition.pending",$checked.OuterXml,[Text.UTF8Encoding]::new($false))
    if ($PrepareOnly) { return }
    $existing = Get-Service $Name -ErrorAction SilentlyContinue
    if ($existing) {
        $serviceInfo = Get-CimInstance Win32_Service -Filter "Name='$Name'"
        if ($serviceInfo.PathName -notlike "*$services*") { throw "Refusing to replace an unrelated $Name service." }
        Stop-Service $Name -ErrorAction Stop
    }
    Move-Item "$definition.pending" $definition -Force
    if (-not $existing) { Invoke-Checked $exe @('install') }
    Invoke-Checked 'sc.exe' @('sidtype',$Name,'unrestricted')
    Invoke-Checked 'icacls.exe' @($serviceLog,'/grant:r',"NT SERVICE\${Name}:(OI)(CI)(M)")
    Invoke-Checked 'icacls.exe' @($serviceTemp,'/grant:r',"NT SERVICE\${Name}:(OI)(CI)(M)")
}
$runtimeEnv = '<env name="PYTHONUNBUFFERED" value="1" /><env name="PYTHONDONTWRITEBYTECODE" value="1" /><env name="OMP_THREAD_LIMIT" value="2" />'
Write-Service 'AutusBackend' $runtimePython '-m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --limit-concurrency 16 --timeout-graceful-shutdown 180 --no-proxy-headers --no-access-log' $backend $runtimeEnv
Write-Service 'AutusFrontend' $NodeExe 'server.js' $frontend '<env name="NODE_ENV" value="production" /><env name="HOSTNAME" value="127.0.0.1" /><env name="PORT" value="3001" /><env name="NEXT_TELEMETRY_DISABLED" value="1" />'
if (-not $UseExistingTunnelService -and $TunnelConfigFile) {
    $originalConfig = [IO.File]::ReadAllText($TunnelConfigFile)
    $credentialMatch = [regex]::Match($originalConfig, '(?m)^credentials-file:\s*(.+)$')
    if (-not $credentialMatch.Success) { throw 'Tunnel configuration must include credentials-file.' }
    $credentialSource = $credentialMatch.Groups[1].Value.Trim().Trim([char]34).Trim([char]39)
    $credentialTarget = Join-Path $config 'tunnel.credentials.json'
    Copy-Item -LiteralPath $credentialSource -Destination $credentialTarget -Force
    Set-PrivateFile $credentialTarget ''
    $tunnelId = (Get-Content $credentialTarget -Raw | ConvertFrom-Json).TunnelID
    $newConfig = [regex]::Replace($originalConfig, '(?m)^credentials-file:.*$', 'credentials-file: '+$credentialTarget)
    $newConfig = [regex]::Replace($newConfig, '(?m)^tunnel:.*$', 'tunnel: '+$tunnelId)
    $newConfig = $newConfig.Replace('http://localhost:', 'http://127.0.0.1:')
    $configTarget = Join-Path $config 'tunnel.yml'
    [IO.File]::WriteAllText($configTarget,$newConfig,[Text.UTF8Encoding]::new($false))
    Write-Service 'AutusTunnel' $CloudflaredExe "tunnel --config `"$configTarget`" --no-autoupdate run" $Root ''
} elseif (-not $UseExistingTunnelService) {
    $tunnelToken = Join-Path $config 'tunnel.token'
    Copy-Item -LiteralPath $TunnelTokenFile -Destination $tunnelToken -Force
    Set-PrivateFile $tunnelToken ''
    Write-Service 'AutusTunnel' $CloudflaredExe "tunnel --no-autoupdate run --token-file `"$tunnelToken`"" $Root ''
}
$backendEnv = Join-Path $backend '.env'
Copy-Item $environmentPath $backendEnv -Force
Set-PrivateFile $backendEnv ''
if ($PrepareOnly) {
    Write-Output "Prepared release: $release. Service XML files end in .pending; no services started."
    return
}
Set-PrivateFile $backendEnv 'AutusBackend'
if (-not $UseExistingTunnelService) {
    if ($TunnelConfigFile) { Set-PrivateFile $credentialTarget 'AutusTunnel' }
    else { Set-PrivateFile $tunnelToken 'AutusTunnel' }
}
Invoke-Checked 'icacls.exe' @((Join-Path $frontend '.next'),'/grant:r','NT SERVICE\AutusFrontend:(OI)(CI)(M)')
if ($LegacyProjectPath) {
    Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','node.exe') -and $_.CommandLine -and $_.CommandLine.Contains($LegacyProjectPath) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
Start-Service AutusBackend
Start-Service AutusFrontend
if ($UseExistingTunnelService) {
    Set-Service Cloudflared -StartupType Automatic
    Start-Service Cloudflared
} else { Start-Service AutusTunnel }
Invoke-Checked 'powercfg.exe' @('/change','standby-timeout-ac','0')
Invoke-Checked 'powercfg.exe' @('/change','hibernate-timeout-ac','0')
$healthy = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:3001/api/health' -TimeoutSec 5
        if ($health.status -eq 'ok' -and $health.version -eq '2.0.0') { $healthy = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $healthy) { throw "Services installed but health check failed. Inspect $logs." }
[IO.File]::WriteAllText((Join-Path $Root 'active-release.txt'),$release)
Write-Output "AUTUS is healthy at $Origin. Services run independently of login."
Write-Output "Bootstrap code is stored privately in $environmentPath. Validate external access and logout behavior."
