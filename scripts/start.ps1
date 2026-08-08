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
$dataPath = Join-Path $repoRoot 'data'
$logPath = Join-Path $dataPath 'logs'
$pidPath = Join-Path $dataPath 'service.pid'
$webIndexPath = Join-Path $repoRoot 'dist\web\index.html'

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

New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
New-Item -ItemType Directory -Path $logPath -Force | Out-Null

if (-not (Test-Path -LiteralPath $webIndexPath)) {
  if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw 'npm was not found; install Node.js 22+ and retry.'
  }
  Push-Location $repoRoot
  try {
    npm install
    if ($LASTEXITCODE -ne 0) {
      throw "npm install failed with exit code $LASTEXITCODE."
    }
    npm run build
    if ($LASTEXITCODE -ne 0) {
      throw "npm run build failed with exit code $LASTEXITCODE."
    }
  } finally {
    Pop-Location
  }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
  python -m venv (Join-Path $repoRoot '.venv')
  if ($LASTEXITCODE -ne 0) {
    throw "python -m venv failed with exit code $LASTEXITCODE."
  }
}

& $pythonPath -c "import fastapi, httpx, pydantic, uvicorn, playwright" 2>$null
$dependenciesReady = $LASTEXITCODE -eq 0
if (-not $dependenciesReady) {
  & $pythonPath -m pip install --upgrade pip
  if ($LASTEXITCODE -ne 0) {
    throw "pip upgrade failed with exit code $LASTEXITCODE."
  }
  & $pythonPath -m pip install -e ".[dev,browser]"
  if ($LASTEXITCODE -ne 0) {
    throw "Project dependency installation failed with exit code $LASTEXITCODE."
  }
}

& $pythonPath -m playwright install chromium chromium-headless-shell
if ($LASTEXITCODE -ne 0) {
  throw "Playwright Chromium installation failed."
}

if (Test-Path -LiteralPath $pidPath) {
  $existingPid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue
  $existingPidValue = 0
  if ($existingPid -and [int]::TryParse(([string]$existingPid).Trim(), [ref]$existingPidValue)) {
    if (Get-ProjectServiceProcess -ProcessId $existingPidValue) {
      Write-Output "Service is already running: http://127.0.0.1:8765/"
      exit 0
    }
  }
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

$stdoutPath = Join-Path $logPath 'service.out.log'
$stderrPath = Join-Path $logPath 'service.err.log'
$arguments = @('-m', 'service.cli', 'serve', '--host', '127.0.0.1', '--port', '8765')
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
$process.Id | Set-Content -LiteralPath $pidPath -Encoding ascii
Start-Sleep -Seconds 1
if (-not (Get-ProjectServiceProcess -ProcessId $process.Id)) {
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
  throw "Service exited during startup. Check $stderrPath for details."
}

$healthy = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
  try {
    $health = Invoke-RestMethod -UseBasicParsing -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
    if ($health.status -eq 'ready') {
      $healthy = $true
      break
    }
  } catch {
    # The server may still be binding its port.
  }
  Start-Sleep -Milliseconds 500
}
if (-not $healthy) {
  Stop-ProcessTree -RootPid $process.Id
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
  throw "Service did not become healthy at /api/health. Check $stderrPath for details."
}

Write-Output "Service started in the background: http://127.0.0.1:8765/"
Write-Output "Logs: $logPath"
Write-Output "The browser was not opened and startup registration was not changed."
