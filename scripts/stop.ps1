$ErrorActionPreference = 'Stop'

function Stop-ProcessTree {
  param([Parameter(Mandatory = $true)][int]$RootPid)

  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $RootPid" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    Stop-ProcessTree -RootPid ([int]$child.ProcessId)
  }
  Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pidPath = Join-Path $repoRoot 'data\service.pid'

if (-not (Test-Path -LiteralPath $pidPath)) {
  Write-Output 'Service PID file was not found; no stop action was taken.'
  exit 0
}

$servicePid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue
$process = if ($servicePid) { Get-Process -Id ([int]$servicePid) -ErrorAction SilentlyContinue } else { $null }
if ($process) {
  Stop-ProcessTree -RootPid $process.Id
  Write-Output "Stopped the project service process PID=$($process.Id)."
} else {
  Write-Output 'The recorded service process no longer exists.'
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
