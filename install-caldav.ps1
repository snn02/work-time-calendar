[CmdletBinding()]
param(
 [string]$UserProfilePath=$env:USERPROFILE,
 [string]$PythonCommand='python',
 [string]$ProfileName='work',
 [string]$CalDAVUrl,
 [string]$CalendarName,
 [string]$CredentialTarget='work-time-calendar'
)
$ErrorActionPreference='Stop'
$profilePath=[IO.Path]::GetFullPath($UserProfilePath)
$installPath=Join-Path $profilePath '.agents\tools\work-time-calendar-caldav'
$configPath=Join-Path $profilePath '.agents\config\work-time-calendar.json'
$sourcePath=Join-Path $PSScriptRoot 'caldav-connector'
$exists=Test-Path -LiteralPath $configPath
if ($ProfileName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$') { throw 'Invalid profile name.' }
if ($exists) {
 $config=Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
 if (-not $config.connections) { $config.connections=@{} }
 if ($config.connections.ContainsKey($ProfileName)) {
  $profile=$config.connections[$ProfileName]
  if ($profile.provider -ne 'caldav') { throw 'Existing profile is not CalDAV; refusing to replace it.' }
  if ($CalDAVUrl -and $CalDAVUrl -ne $profile.url) { throw 'Existing URL differs; update personal configuration explicitly first.' }
  if ($CalendarName -and $CalendarName -ne $profile.calendar_name) { throw 'Existing calendar differs; update personal configuration explicitly first.' }
 } else {
  if (-not $CalDAVUrl -or -not $CalendarName) { throw 'New profile needs -CalDAVUrl and -CalendarName.' }
  $profile=@{provider='caldav';url=$CalDAVUrl;calendar_name=$CalendarName;credential_target=$CredentialTarget}
  $config.connections[$ProfileName]=$profile
  if (-not $config.default) { $config.default=$ProfileName }
 }
} else {
 if (-not $CalDAVUrl -or -not $CalendarName) { throw 'First installation needs -CalDAVUrl and -CalendarName. Add the credential to Windows Credential Manager separately.' }
 $profile=@{provider='caldav';url=$CalDAVUrl;calendar_name=$CalendarName;credential_target=$CredentialTarget}
 $config=@{timezone='Europe/Moscow';default=$ProfileName;connections=@{$ProfileName=$profile}}
}
$uri=$null
if (-not [Uri]::TryCreate([string]$profile.url,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'CalDAV URL must be HTTPS with no embedded credentials, query or fragment.' }
if (-not $profile.calendar_name -or -not $profile.credential_target) { throw 'Calendar name and credential target are required.' }
$pythonPath=Join-Path $installPath 'venv\Scripts\python.exe'
New-Item -ItemType Directory -Path $installPath -Force | Out-Null
if (-not (Test-Path -LiteralPath $pythonPath)) { & $PythonCommand -m venv (Join-Path $installPath 'venv'); if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' } }
& $pythonPath -m pip install --disable-pip-version-check -r (Join-Path $sourcePath 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; personal config unchanged.' }
& $pythonPath -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed; personal config unchanged.' }
$connectorPath=Join-Path $installPath 'connector.py'
Copy-Item -LiteralPath (Join-Path $sourcePath 'connector.py') -Destination $connectorPath -Force
if ((Get-FileHash -LiteralPath (Join-Path $sourcePath 'connector.py')).Hash -ne (Get-FileHash -LiteralPath $connectorPath).Hash) { throw 'Connector copy mismatch' }
$profile.python=$pythonPath.Replace('\','/')
$profile.connector=$connectorPath.Replace('\','/')
New-Item -ItemType Directory -Path (Split-Path -Parent $configPath) -Force | Out-Null
[IO.File]::WriteAllText($configPath,($config | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
[pscustomobject]@{Connector=$connectorPath;Python=$pythonPath;Config=$configPath;Default=$config.default;Profile=$ProfileName} | ConvertTo-Json
