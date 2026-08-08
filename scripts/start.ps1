$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$dataPath = Join-Path $repoRoot 'data'
$logPath = Join-Path $dataPath 'logs'
$pidPath = Join-Path $dataPath 'service.pid'
$webIndexPath = Join-Path $repoRoot 'dist\web\index.html'

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
  if ($existingPid -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
    Write-Output "Service is already running: http://127.0.0.1:8765/"
    exit 0
  }
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

$stdoutPath = Join-Path $logPath 'service.out.log'
$stderrPath = Join-Path $logPath 'service.err.log'
$arguments = @('-m', 'service.cli', 'serve', '--host', '127.0.0.1', '--port', '8765')
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
$process.Id | Set-Content -LiteralPath $pidPath -Encoding ascii
Start-Sleep -Seconds 1
if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
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
  Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
  throw "Service did not become healthy at /api/health. Check $stderrPath for details."
}

Write-Output "Service started in the background: http://127.0.0.1:8765/"
Write-Output "Logs: $logPath"
Write-Output "The browser was not opened and startup registration was not changed."
