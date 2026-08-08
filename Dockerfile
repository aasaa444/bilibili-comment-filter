FROM node:22-alpine AS frontend-build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY tsconfig.json ./
COPY shared ./shared
COPY web ./web
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BILIBILI_FILTER_HOST=0.0.0.0 \
    BILIBILI_FILTER_PORT=8765 \
    BILIBILI_FILTER_DATABASE=/data/bilibili-filter.sqlite3 \
    BILIBILI_FILTER_WEB_ROOT=/app/web/dist

WORKDIR /app
COPY pyproject.toml ./
COPY service ./service
RUN pip install --no-cache-dir ".[browser]" \
    && playwright install --with-deps chromium
COPY --from=frontend-build /app/dist/web ./web/dist

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "-m", "service.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
