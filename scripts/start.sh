#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_path="$repo_root/.venv/bin/python"

mkdir -p "$repo_root/data/logs"
if [ ! -f "$repo_root/dist/web/index.html" ]; then
  command -v npm >/dev/null 2>&1 || { printf '%s\n' '未找到 npm；请安装 Node.js 22+ 后重试。' >&2; exit 1; }
  (cd "$repo_root" && npm install && npm run build)
fi
if [ ! -x "$python_path" ]; then
  python3 -m venv "$repo_root/.venv"
  if ! [ -x "$python_path" ]; then
    printf '%s\n' 'python3 -m venv failed.' >&2
    exit 1
  fi
fi

if ! "$python_path" -c 'import fastapi, httpx, pydantic, uvicorn, playwright' >/dev/null 2>&1; then
  "$python_path" -m pip install --upgrade pip
  "$python_path" -m pip install -e '.[dev,browser]'
fi
if ! "$python_path" -c 'import fastapi, httpx, pydantic, uvicorn, playwright' >/dev/null 2>&1; then
  printf '%s\n' 'Project dependency installation failed.' >&2
  exit 1
fi
"$python_path" -m playwright install chromium chromium-headless-shell

if [ -f "$repo_root/data/service.pid" ]; then
  existing_pid=$(cat "$repo_root/data/service.pid" || true)
  if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
    printf '%s\n' '服务已在运行: http://127.0.0.1:8765/'
    exit 0
  fi
  rm -f "$repo_root/data/service.pid"
fi

nohup "$python_path" -m service.cli serve --host 127.0.0.1 --port 8765 \
  >"$repo_root/data/logs/service.out.log" \
  2>"$repo_root/data/logs/service.err.log" &
service_pid=$!
printf '%s\n' "$service_pid" > "$repo_root/data/service.pid"
sleep 1
if ! kill -0 "$service_pid" 2>/dev/null; then
  rm -f "$repo_root/data/service.pid"
  printf '%s\n' 'Service exited during startup; check data/logs/service.err.log.' >&2
  exit 1
fi
healthy=false
for _ in $(seq 1 30); do
  if "$python_path" -c 'import json, urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=2)); raise SystemExit(0 if data.get("status") == "ready" else 1)' >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 0.5
done
if [ "$healthy" != true ]; then
  kill "$service_pid" 2>/dev/null || true
  rm -f "$repo_root/data/service.pid"
  printf '%s\n' 'Service did not become healthy at /api/health; check data/logs/service.err.log.' >&2
  exit 1
fi
printf '%s\n' '服务已在后台启动: http://127.0.0.1:8765/'
printf '%s\n' '未自动打开浏览器，也未注册开机启动。'
