# 验收报告

日期：2026-08-09

## 结论

当前版本已完成本机服务、Docker Compose 运行时和管理页的可运行验收，并完成批量分析、任务断点、UID 本地隐藏缓存的 fixture 级验收。它可以作为继续接入真实 B 站账号和模型服务前的可运行基线，但不能据此宣称已经完成真实 B 站评论采集、真实模型判定或官方拉黑。

## 已验证

- Python 服务单元与 API 测试：`python -m pytest -q` 全量通过（122 passed）。
- AI 批处理：按 UID 聚合后分批调用；上下文超限会拆批；单 UID 仍被拒绝时保留为 `uncertain`；支持字符串和分段文本响应；超时按配置重试。
- 采集断点：root cursor、评论/回复声明数量、`declared_total` 和 SQLite 重启恢复测试通过。
- 风控边界：API `-352/-412` 与 HTTP `403/412/429` 保留结构化分类并立即暂停采集，不进入普通
  任务自动重试；分页容器、数量、cursor 和结束标记的 malformed 元数据会写入 `failed_items`
  并保留当前 root/reply checkpoint。
- 旧 SQLite 兼容：启动时为缺少的新列执行增量迁移，旧任务仍可读取。
- 前端：TypeScript 类型检查、构建、Node fixture 测试通过（34 passed）；公开 Shadow DOM fixture 覆盖根评论、楼中楼、
  异步新挂载的嵌套 shadow root 和弹幕不处理。
- 任务详情：API 与管理页展示保存数量、声明评论/回复、声明总量、覆盖率和失败项。
- 复核闭环：复核动作可通过 `GET /api/review-actions` 按 UID/证据查询，并在管理页展示历史、操作者和状态变化。
- 真实协议探针：在已同步会话下对 `BV1eFu36LEt2` 做了元数据探针，一级接口按 `next=0,2,...,12` 返回 19/20 条后以空页和 `is_end=true` 结束，接口 `all_count=811`，去重后保存 199 条一级评论；把 `cursor.pagination_reply.next_offset` 作为 `pagination_str={"offset":...}` 重放仍返回空终止页。当前任务同时保存 544 条楼中楼回复，覆盖率约 54.9%，状态为 `partial`，因此 #7/#8 仍不能按“完整采集”关闭。
- 启停脚本：PowerShell 语法解析通过；后台启动使用隐藏窗口；停止前校验 PID 对应的 Python 可执行文件、模块和端口参数。
- 启动契约：新增测试覆盖 CLI 默认值/环境变量、Windows 启停参数、健康检查、持久化路径和不写入开机启动。
- Compose 配置：`docker compose config --quiet` 通过；Rancher Desktop Moby daemon 上真实执行 `docker compose build`、`docker compose up -d`，容器健康检查为 `healthy`，`/api/health` 返回 `ready`；认证只返回脱敏诊断状态，具体值随当前同步会话变化，测试不依赖 Cookie 内容。

## 仅 fixture 或静态验证

- B 站评论 transport 的固定响应、分页、失败记录和重试已验证；真实探针确认了当前一级接口的游标链和 `pagination_reply` 形状，但平台返回的可采集条数低于 `all_count`，没有完成同步真实账号后的完整评论区收敛。
- AI transport 使用固定的 HTTP mock；没有调用实际 OpenAI-compatible endpoint，也没有验证供应商对 token 计数、`max_tokens` 或响应分段的具体差异。
- 官方拉黑使用替身 executor；没有执行真实 B 站拉黑、批量操作或账号状态变更。
- 浏览器插件使用本地 DOM fixture 验证过滤逻辑；新增 service worker fixture 覆盖当前页面 Cookie 查询、会话同步顺序、权限错误和 Cookie 值脱敏。独立 Chromium 的 fixture 也验证了同步 Cookie 会进入 browser context。随后在真实 Chrome 的普通 B 站视频页验证了 UID `350213094` 的本地评论隐藏，未读取或输出真实 Cookie，也没有执行评论、拉黑和账号操作。

## 当前外部阻断

- 真实 B 站账号、真实评论采集和真实官方拉黑仍需要用户明确安排独立验收窗口；它们不应被 fixture 结果替代。
- 模型服务的真实端点仍需要在本机或配置的远程服务上做一次小规模、可回滚的连接验收。

## 数据与安全边界

- 本报告、测试输出和日志中不包含 Cookie 值。
- 服务的认证会话和任务数据仍按当前设计写入本机 SQLite；部署前应确认数据库文件权限和备份策略。
- 插件只在评论节点上应用本地隐藏标记；当前版本不采集、不分析、不隐藏弹幕。
