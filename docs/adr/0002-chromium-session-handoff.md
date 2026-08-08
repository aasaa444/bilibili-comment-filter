---
status: accepted
---

# 通过 Chromium 插件传递 B 站登录态

首版以 Chrome/Edge 为目标，由插件读取当前已登录的 B 站 Cookie 并传给本机采集服务，服务使用该会话获取评论数据。这样符合用户的日常浏览流程，不要求手动导出 Cookie；手动 Cookie 文件作为后续兼容 Firefox 或云端部署的备用入口。

同一登录态也用于初始化或刷新独立 Chromium 工作实例，避免用户在后台实例中重复登录。
