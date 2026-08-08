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
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$pidPath = Join-Path $repoRoot 'data\service.pid'

function Get-ProjectServiceProcess {
  param([Parameter(Mandatory = $true)][int]$ProcessId)

  $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
  if (-not $processInfo) { return $null }
  $expectedPython = [IO.Path]::GetFullPath($pythonPath)
  $actualPython = if ($processInfo.ExecutablePath) { [IO.Path]::GetFullPath($processInfo.ExecutablePath) } else { '' }
  if (-not [string]::Equals($actualPython, $expectedPython, [StringComparison]::OrdinalIgnoreCase)) { return $null }
  $commandLine = [string]$processInfo.CommandLine
  if ($commandLine -notmatch '(?i)(^|\s)-m\s+service\.cli(\s|$)' -or $commandLine -notmatch '(?i)(^|\s)--port\s+8765(\s|$)') { return $null }
  return $processInfo
}

if (-not (Test-Path -LiteralPath $pidPath)) {
  Write-Output 'Service PID file was not found; no stop action was taken.'
  exit 0
}

$servicePid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue
$servicePidValue = 0
$process = if ($servicePid -and [int]::TryParse(([string]$servicePid).Trim(), [ref]$servicePidValue)) {
  Get-ProjectServiceProcess -ProcessId $servicePidValue
} else { $null }
if ($process) {
  Stop-ProcessTree -RootPid $process.Id
  Write-Output "Stopped the project service process PID=$($process.Id)."
} elseif ($servicePid) {
  Write-Output 'The recorded PID does not belong to this project service; no process was stopped.'
} else {
  Write-Output 'The recorded service process no longer exists.'
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
