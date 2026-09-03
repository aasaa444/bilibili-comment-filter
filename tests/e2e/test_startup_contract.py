from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _flatten(lines: str) -> str:
    return " ".join(line.strip().rstrip("\\") for line in lines.splitlines()).replace("  ", " ")


def test_fixture_startup_creates_persistent_database_and_reports_ready(tmp_path) -> None:
    database = tmp_path / "persistent" / "bilibili-filter.sqlite3"

    with TestClient(
        create_app(
            db_path=database,
            worker_available=True,
            start_background_worker=False,
        )
    ) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["database"] == {
        "status": "ready",
        "detail": "SQLite connection is available",
    }
    assert response.json()["worker"]["status"] == "ready"
    assert database.is_file()


def test_cli_worker_toggle_is_part_of_the_startup_contract() -> None:
    cli_source = _read("service/cli.py")

    assert "BILIBILI_FILTER_WORKER_ENABLED" in cli_source
    assert "start_background_worker=_env_bool(" in cli_source


def test_windows_start_and_stop_scripts_define_a_safe_local_contract() -> None:
    start = _read("scripts/start.ps1")
    stop = _read("scripts/stop.ps1")

    assert "$ErrorActionPreference = 'Stop'" in start
    assert "$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path" in start
    assert "$pythonPath = Join-Path $repoRoot '.venv\\Scripts\\python.exe'" in start
    assert "$dataPath = Join-Path $repoRoot 'data'" in start
    assert "$logPath = Join-Path $dataPath 'logs'" in start
    assert "$pidPath = Join-Path $dataPath 'service.pid'" in start
    assert "New-Item -ItemType Directory -Path $dataPath -Force" in start
    assert "New-Item -ItemType Directory -Path $logPath -Force" in start
    assert (
        "Start-Process -FilePath $pythonPath -ArgumentList $arguments "
        "-WorkingDirectory $repoRoot -WindowStyle Hidden"
    ) in start
    assert (
        "$arguments = @('-m', 'service.cli', 'serve', '--host', '127.0.0.1', '--port', '8765')"
    ) in start
    assert "Invoke-RestMethod -UseBasicParsing -Uri 'http://127.0.0.1:8765/api/health'" in start
    assert "if ($health.status -eq 'ready')" in start
    assert "Stop-ProcessTree -RootPid $process.Id" in start
    assert "The browser was not opened and startup registration was not changed." in start

    assert "$pidPath = Join-Path $repoRoot 'data\\service.pid'" in stop
    assert "if (-not (Test-Path -LiteralPath $pidPath))" in stop
    assert "Get-ProjectServiceProcess -ProcessId $servicePidValue" in stop
    assert "Stop-ProcessTree -RootPid $process.Id" in stop
    assert (
        "The recorded PID does not belong to this project service; no process was stopped."
    ) in stop
    assert "Remove-Item -LiteralPath $pidPath -Force" in stop


def test_windows_start_and_stop_scripts_parse_when_powershell_is_available() -> None:
    powershell = None
    for name in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
        powershell = shutil.which(name)
        if powershell:
            break
    if powershell is None:
        pytest.skip("PowerShell is not installed")

    command = r"""
$tokens = $null
$errors = $null
foreach ($path in @($env:STARTUP_CONTRACT_START, $env:STARTUP_CONTRACT_STOP)) {
  [System.Management.Automation.Language.Parser]::ParseFile(
    $path, [ref]$tokens, [ref]$errors
  ) | Out-Null
  if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_.Message }
    exit 1
  }
}
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO_ROOT,
        env={
            **dict(os.environ),
            "STARTUP_CONTRACT_START": str(REPO_ROOT / "scripts/start.ps1"),
            "STARTUP_CONTRACT_STOP": str(REPO_ROOT / "scripts/stop.ps1"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_dockerfile_declares_runtime_health_and_persistence_contract() -> None:
    dockerfile = _flatten(_read("Dockerfile"))

    assert "FROM node:22-alpine AS frontend-build" in dockerfile
    assert "FROM python:3.12-slim" in dockerfile
    assert "COPY --from=frontend-build /app/dist/web ./web/dist" in dockerfile
    assert "BILIBILI_FILTER_HOST=0.0.0.0" in dockerfile
    assert "BILIBILI_FILTER_PORT=8765" in dockerfile
    assert "BILIBILI_FILTER_DATABASE=/data/bilibili-filter.sqlite3" in dockerfile
    assert "BILIBILI_FILTER_WEB_ROOT=/app/web/dist" in dockerfile
    assert "RUN mkdir -p /data" in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile
    assert "EXPOSE 8765" in dockerfile
    assert (
        "HEALTHCHECK --interval=30s --timeout=5s --start-period=15s "
        "--retries=3 CMD python -c"
    ) in dockerfile
    assert "http://127.0.0.1:8765/api/health" in dockerfile
    assert "data.get('status') == 'ready'" in dockerfile
    assert (
        'CMD ["python", "-m", "service.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]'
        in dockerfile
    )


def test_compose_declares_environment_health_and_named_persistence_contract() -> None:
    compose = _flatten(_read("docker-compose.yml"))
    env_example = _read(".env.example")

    assert "env_file:" in compose
    assert "path: .env" in compose
    assert "required: false" in compose
    assert "BILIBILI_FILTER_HOST: 0.0.0.0" in compose
    assert "BILIBILI_FILTER_DATABASE: /data/bilibili-filter.sqlite3" in compose
    assert "BILIBILI_FILTER_WEB_ROOT: /app/web/dist" in compose
    assert 'BILIBILI_FILTER_BROWSER_HEADLESS: "true"' in compose
    assert '"127.0.0.1:8765:8765"' in compose
    assert "test: - CMD - python - -c" in compose
    assert "http://127.0.0.1:8765/api/health" in compose
    assert "data.get('status') == 'ready'" in compose
    assert "interval: 30s" in compose
    assert "timeout: 5s" in compose
    assert "start_period: 15s" in compose
    assert "retries: 3" in compose
    assert "- bilibili_filter_data:/data" in compose
    assert "bilibili_filter_data:" in compose
    assert "restart: unless-stopped" in compose

    for variable in (
        "BILIBILI_FILTER_HOST",
        "BILIBILI_FILTER_PORT",
        "BILIBILI_FILTER_DATABASE",
        "BILIBILI_FILTER_WEB_ROOT",
        "BILIBILI_FILTER_LOG_LEVEL",
        "BILIBILI_FILTER_OPENAI_BASE_URL",
        "BILIBILI_FILTER_OPENAI_API_KEY",
        "BILIBILI_FILTER_OPENAI_MODEL",
        "BILIBILI_FILTER_OPENAI_CONTEXT_TOKENS",
        "BILIBILI_FILTER_OPENAI_MAX_OUTPUT_TOKENS",
        "BILIBILI_FILTER_OPENAI_MAX_BATCH_ACCOUNTS",
        "BILIBILI_FILTER_OPENAI_TIMEOUT_SECONDS",
        "BILIBILI_FILTER_WORKER_ENABLED",
        "BILIBILI_FILTER_BROWSER_HEADLESS",
    ):
        assert f"{variable}=" in env_example


def test_compose_config_is_valid_without_starting_a_container() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is not installed")

    result = subprocess.run(
        [docker, "compose", "config", "--quiet"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
