FROM node:22-alpine AS frontend-build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY tsconfig.json ./
COPY shared ./shared
COPY web ./web
COPY extension ./extension
COPY scripts ./scripts
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BILIBILI_FILTER_HOST=0.0.0.0 \
    BILIBILI_FILTER_PORT=8765 \
    BILIBILI_FILTER_DATABASE=/data/bilibili-filter.sqlite3 \
    BILIBILI_FILTER_WEB_ROOT=/app/web/dist \
    BILIBILI_FILTER_BROWSER_HEADLESS=true

WORKDIR /app
COPY pyproject.toml ./
COPY service ./service
RUN pip install --no-cache-dir ".[browser]" \
    && python -m playwright install --with-deps chromium chromium-headless-shell
COPY --from=frontend-build /app/dist/web ./web/dist

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)); raise SystemExit(0 if data.get('status') == 'ready' else 1)"
CMD ["python", "-m", "service.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
