---
status: accepted
---

# Chromium 插件与 Python 本机服务

首版采用 Chrome/Edge Manifest V3 与 TypeScript 插件，配套 Python 3.11+、FastAPI、SQLite 和独立后台 Worker；插件负责浏览器权限、视频提交和页面过滤，本机服务负责任务、证据、AI 分析和队列。该组合复用 Python 采集参考的生态，同时让浏览器端和后台职责保持清晰。
