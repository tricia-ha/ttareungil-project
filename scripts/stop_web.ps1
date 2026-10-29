param([string]$ProjectRoot)
$ErrorActionPreference='Stop'
$root=[System.IO.Path]::GetFullPath($ProjectRoot)
$private=Join-Path (Split-Path $root -Parent) ((Split-Path $root -Leaf)+'_local')
$cache=Join-Path $root 'cache\runtime'
if (Test-Path -LiteralPath (Join-Path $private 'cache')) {$cache=Join-Path $private 'cache\runtime'}
foreach ($name in @('web_tunnel.json','web_server.json')) {
  $record=Join-Path $cache $name
  if (-not (Test-Path -LiteralPath $record)) {continue}
  $info=Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
  if ([System.IO.Path]::GetFullPath($info.root) -ne $root) {throw 'Server path mismatch'}
  $process=Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$info.pid)"
  if (-not $process) {continue}
  $expected=Join-Path $root 'scripts\serve.py'
  if ($name -eq 'web_tunnel.json') {$expected=$info.binary}
  if (-not $process.CommandLine -or $process.CommandLine.IndexOf($expected,[System.StringComparison]::OrdinalIgnoreCase) -lt 0) {throw 'The saved PID belongs to another process. It was not stopped.'}
  Stop-Process -Id ([int]$info.pid)
  Write-Output "Stopped $name"
}
