# 验收报告

日期：2026-08-11

## 结论

当前版本已完成本机服务、Docker Compose 运行时和管理页的可运行验收，并完成批量分析、任务断点、UID 本地隐藏缓存的 fixture 级验收。它可以作为继续接入真实 B 站账号和远程模型服务前的可运行基线，但不能据此宣称已经完成真实 B 站评论采集、真实远程模型判定或官方拉黑。

## 已验证

- Python 服务单元与 API 测试：`.venv\Scripts\python.exe -m pytest -q` 全量通过（199 passed）。
- AI 批处理：按 UID 聚合后分批调用；上下文超限会拆批；单 UID 仍被拒绝时保留为 `uncertain`；支持字符串和分段文本响应；超时按配置重试。
- 昵称样本与模型降级：已发布的昵称正例在模型调用前接入硬规则；远程模型未配置时，昵称/词条规则命中仍保存证据并更新名单，未命中账号保留为 `partial` 等待重试。
- 采集断点：root cursor、评论/回复声明数量、`declared_total` 和 SQLite 重启恢复测试通过。
- 风控边界：API `-352/-412` 与 HTTP `403/412/429` 保留结构化分类并立即暂停采集，不进入普通
  任务自动重试；分页容器、数量、cursor 和结束标记的 malformed 元数据会写入 `failed_items`
  并保留当前 root/reply checkpoint。
- 会话恢复：同步有效 B 站登录态后，只有 `paused + auth_unavailable` 的任务会原子恢复为
  `queued`；其它暂停原因保持不变，并通过后端 API 回归测试验证。
- 旧 SQLite 兼容：启动时为缺少的新列执行增量迁移，旧任务仍可读取。
- 前端：TypeScript 类型检查、构建、Node fixture 测试通过（60 passed）；公开 Shadow DOM fixture 覆盖根评论、楼中楼、
  异步新挂载的嵌套 shadow root 和弹幕不处理。
- 扩展后台认证同步：fixture 验证启动监听器在存在 B 站标签页时同步认证，并验证没有 B 站标签页时完全不读取 Cookie。
- 断点恢复：恢复后的任务会保留并去重此前已记录的失败页，不会用本轮结果覆盖历史采集异常。
- 黑名单队列节奏：默认每个官方拉黑动作至少间隔 60 秒，并叠加 0–30 秒随机抖动；支持通过环境变量调整，配置读取与调度测试通过。
- 任务详情：API 与管理页展示保存数量、声明评论/回复、声明总量、覆盖率和失败项。
- 复核闭环：复核动作可通过 `GET /api/review-actions` 按 UID/证据查询，并在管理页展示历史、操作者和状态变化；撤销会移除本地 UID 并通过增量同步发出 `removed`，加入例外则保留 `exception` 状态。
- 真实协议探针：当前 B 站网页评论组件使用 WBI 签名的 `/x/v2/reply/wbi/main`，签名 key 可从 `/x/web-interface/nav` 的公开 `wbi_img` 字段读取；服务已接入该协议和 `pagination_str={"offset":...}` 不透明游标。对 `BV1eFu36LEt2` 的无 Cookie 真实 transport 冒烟首级返回 20 条、续页游标为 opaque string，并从一个有楼中楼的根评论验证到 3 条回复；尚未完成整区真实收敛，因此任务仍必须保持 `partial`，不能按“完整采集”关闭。
- 既有小规模真实采集记录（发生在 WBI transport 接入前）：普通视频 `BV1z2uH6XEbC` 曾完成只读内存采集并保留异常、断点和 `partial` 结果；该记录不作为当前 WBI 协议已经完整收敛的证明，也未触发 AI 或拉黑操作。
- 启停脚本：PowerShell 语法解析通过；后台启动使用隐藏窗口；停止前校验 PID 对应的 Python 可执行文件、模块和端口参数。
- 启动契约：新增测试覆盖 CLI 默认值/环境变量、Windows 启停参数、健康检查、持久化路径和不写入开机启动。
- Compose 配置：`docker compose config --quiet` 通过；Rancher Desktop Moby daemon 上真实执行 `docker compose build`、`docker compose up -d`，容器健康检查为 `healthy`，`/api/health` 返回 `ready`；认证只返回脱敏诊断状态，具体值随当前同步会话变化，测试不依赖 Cookie 内容。

补充的只读真实会话探针：`BV1z2uH6XEbC` 的完整采集器运行达到 `complete=true`，保存 8 条一级评论和 19 条楼中楼，覆盖率 100%，仅保留一个平台终止空页诊断，没有触发暂停；期间修复了 B 站 `top` 元数据包装器被误解析为评论的问题。更大评论区仍保持独立的全链路收敛验收边界。

同一会话对较大的 `BV1eFu36LEt2` 执行完整只读采集时读取到 206 条一级评论和 604 条回复，平台整区声明总量为 851、楼中楼声明为 627，覆盖率 95.18%，保持 `complete=false, terminal=true`；失败项为一个空楼中楼页和一个终止空一级页。WBI `cursor.all_count` 已按整区总量处理，不再与楼中楼声明重复相加；探针未写入任务数据库，也未消费拉黑队列。

## 仅 fixture 或静态验证

- B 站评论 transport 的固定响应、WBI 签名、动态 key、分页、失败记录和重试已验证；真实冒烟确认当前一级接口的游标链和 `pagination_reply` 形状，但尚未完成同步真实账号后的完整评论区收敛。
- AI transport 使用固定的 HTTP mock；没有调用实际远程 OpenAI-compatible endpoint，也没有验证供应商对 token 计数、`max_tokens` 或响应分段的具体差异。
- 官方拉黑使用替身 executor；没有执行真实 B 站拉黑、批量操作或账号状态变更。
- 浏览器插件使用本地 DOM fixture 验证过滤逻辑；新增 service worker fixture 覆盖当前页面 Cookie 查询、会话同步顺序、权限错误和 Cookie 值脱敏。独立 Chromium 的 fixture 也验证了同步 Cookie 会进入 browser context。随后在真实 Chrome 的普通 B 站视频页验证了 UID `350213094` 的本地评论隐藏，未读取或输出真实 Cookie，也没有执行评论、拉黑和账号操作。

## 当前外部阻断

- 真实 B 站账号、真实评论采集和真实官方拉黑仍需要用户明确安排独立验收窗口；它们不应被 fixture 结果替代。
- 远程 OpenAI-compatible endpoint 尚未配置/验收；当前未进行真实模型分析连接验证。

## 数据与安全边界

- 本报告、测试输出和日志中不包含 Cookie 值。
- 服务的认证会话和任务数据仍按当前设计写入本机 SQLite；部署前应确认数据库文件权限和备份策略。
- 插件只在评论节点上应用本地隐藏标记；当前版本不采集、不分析、不隐藏弹幕。

## T16.5 管理页策略跨模块闭环验收（2026-08-11）

本节只记录 T16.5 固定替身闭环的新增验收；报告前文的协议探针、启动契约和真实会话记录是前序 ticket 的独立证据，不作为本票的真实 B 站或远程模型通过依据。

### 已验证

- 新增 `tests/e2e/test_management_policy_flow.py`，通过公开任务运行接口和任务事件/分析接口，使用固定采集器、固定分析器和记录型拉黑执行器，串联验证：样本发布、任务分析、证据生成、UID 本地隐藏、自动拉黑开关、队列保留、Worker 消费和最终 `blocked` 状态。
- 关闭自动拉黑时，命中 UID 进入 `hidden`，不创建官方队列；`confirm` 明确返回“自动执行官方拉黑已关闭”，不会伪造排队成功。
- 开启自动拉黑后，新命中进入 `queued`；再次关闭开关时队列项仍保留，Worker 不消费；重新开启后队列项完成，替身执行器收到对应 UID，UID 状态变为 `blocked`。
- 新版本样本快照在任务证据中以当前版本号引用，且第二版包含第一版样本；第一版样本和第一版任务证据仍可通过管理 API 查询，历史版本没有被覆盖。完整样本继承兼容旧增量快照的回归测试位于 `tests/service/test_samples.py`。
- 部分采集但已有评论仍进入分析并保留 `partial` 任务状态；没有可分析评论时不启动 AI。非法模型响应记录为分析失败，不伪造任务完成：`tests/service/test_task_observability.py`。
- 队列错误的用户提示、恢复动作、技术详情和状态转换由本票的失败执行器路径以及 `tests/service/test_blacklist_diagnostics.py`、`tests/frontend/blacklist-queue.test.mjs` 覆盖；证据收件箱/详情检查器和样本库可读状态由对应前端 fixture 覆盖。

### 验证命令

- `.venv\Scripts\python.exe -m pytest -q` — 199 passed。
- `npm test` — 60 passed。
- `npm run typecheck` — passed。
- `npm run build` — passed。
- `ruff check tests/e2e/test_management_policy_flow.py service/samples.py tests/service/test_samples.py` — passed。
- `ruff check .` — 基线失败：未修改的 `service/reviews.py` 有 1 个 import 排序问题，
  未修改的 `tests/service/test_blacklist_settings.py` 有 2 个行长问题。
- `git diff --check` — passed。

### 未验证与保留边界

- 本票只完成固定替身和管理 API/前端 fixture 的跨模块验收，不替代真实 B 站评论区完整采集、真实远程模型响应或真实官方拉黑。
- 未关闭或修改既有未完成 Issue；#6–#9、#15、#16 的原有验收责任继续保留。
- 自动拉黑执行器仍未对真实账号产生平台状态变更；真实 B 站环境中的页面结构、登录失效、验证码、风控和平台响应仍需单独安排小规模验收。
