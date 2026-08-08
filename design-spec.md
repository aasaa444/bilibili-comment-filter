# B站评论过滤工具 Design Specification

> Generated from a `design` consultation with `oiloil-ui-ux-guide`.
> Style family: `brand-driven` (reference-led; no third-party brand assets are copied).

## 1. Design direction

- **Product**: 面向个人 B 站用户的评论过滤工具，用浏览器插件提交视频任务，并在本机管理 UID、证据、复核和拉黑队列。
- **Style family**: `brand-driven`。以用户指定的 Pornhub 网站作为黑黄配色和戏谑语气的视觉参考，重新设计本项目的布局、文案和组件。
- **References**: Pornhub 网站的黑底、橙黄色强调和让人会心一笑的品牌语气；不复制 Logo、站名、图片、文案或页面结构。
- **Tone**: 黑色幽默、直白、克制。幽默可以出现在标题和辅助状态文案中，但不能改变 UID 判定、证据或拉黑操作的真实含义。
- **Hard constraints**: 桌面端优先；深色主题优先，首版只验证深色界面；中文；信息密度偏高；管理页和插件弹窗是核心 surface；弹幕不进入界面范围。
- **Locale**: primary `zh-CN`, secondary `[]`
- **Primary surfaces**: 本机管理页、Chromium 插件弹窗。

## 2. Color

### Brand

- `--color-primary`: `#ff9900` — 主要 CTA、当前导航、进度条、重要结果；公开参考 CSS 中的 `#f90` 展开值。
- `--color-primary-hover`: `#ff9f0e` — hover、active 和需要提高注意力的交互反馈。
- `--color-primary-subtle`: `rgba(255, 153, 0, 0.14)` — 当前导航、已隐藏状态和低强度强调背景。
- `--color-secondary`: `#ff9000` — 保留为参考站点中出现的辅助橙色；首版不把它作为第二套 CTA 语义。

### Neutrals (tinted toward true near-black)

- `--color-bg`: `#151515` — 页面主背景。
- `--color-surface`: `#1b1b1b` — 工作面、弹窗内部工作面和必要的状态区域。
- `--color-border`: `#2f2f2f` — 仅用于焦点、分隔或平台默认控件需要边界的场景；`container: none` 下不用于卡片外框。
- `--color-text`: `#ffffff` — 标题、UID、主要结果。
- `--color-text-secondary`: `#c6c6c6` — 正文、解释和次要字段。
- `--color-text-muted`: `#969696` — UID 辅助信息、时间、提示和不可操作的元数据。

### Semantic

- `--color-success`: `#34c759` — 服务连接、完成、已同步。
- `--color-warning`: `#ff9f0e` — 分析中、待复核、暂停提示。
- `--color-error`: `#e44545` — 失败、登录失效、验证码或需要停止的异常。
- `--color-info`: `#3a88e9` — 友军例外、信息说明和非风险状态。

### Dark mode

- V1 以深色界面为唯一验证主题，以上基础 token 即为深色 token。
- 不在首版伪造浅色 token；浅色主题是否支持列为开放问题。

## 3. Typography

| Role | Font | Weights | Source |
|---|---|---|---|
| Heading | `Microsoft YaHei` | 400 / 600 / 700 | Windows system font; fallback `Segoe UI`, `sans-serif` |
| Body | `Microsoft YaHei` | 400 / 600 / 700 | Windows system font; fallback `Segoe UI`, `sans-serif` |
| Mono | `Cascadia Mono` | 400 / 600 | Windows system font; fallback `Consolas`, `monospace` |

### Type scale (px)

`11 / 12 / 14 / 15 / 17 / 20 / 24 / 30`

- `11–12px`: UID、时间、帮助和状态元数据。
- `14–15px`: 常规界面正文、表格行和按钮。
- `17–20px`: 区块标题、任务名称和插件视频标题。
- `24–30px`: 页面标题和关键统计值。

### Body measure

- UI body target: 45–65 个 CJK 字符的等价阅读宽度。
- UI line-height: `1.7`；CJK 文本不使用负字距。
- Heading letter-spacing: `0`。
- UID、任务 ID、视频 BV 号、模型版本和时间戳使用 Mono 字体，不使用装饰性字体。

## 4. Spacing

- Base unit: `4px`
- Allowed scale: `4 / 8 / 12 / 16 / 24 / 32 / 48`
- Density: `compact`
- 常规列表行高度约 `36–40px`；插件弹窗以 `16px` 内边距和 `8–24px` 的纵向节奏为主。
- Off-scale spacing requires a short justification in code comments.

## 5. Radius

- `--radius-sm`: `3px` — 小按钮、导航选中态和状态控件。
- `--radius-md`: `6px` — 工作面、任务条和重复列表区域。
- `--radius-lg`: `10px` — 插件弹窗整体工作面或需要明确聚焦的独立区域。
- `--radius-full`: `9999px` — 状态 pill、开关和圆点，不用于普通容器。

## 6. Elevation / shadow

- `--shadow-sm`: `none`
- `--shadow-md`: `none`
- `--shadow-lg`: `none`
- 采用 flat 视觉。容器之间通过页面背景、工作面色差、留白和排版组织，不使用投影制造层级。

## 7. Motion

- Vocabulary: `minimal`
- Default duration: `120ms` for micro feedback, `160ms` for state changes, `180ms` for overlays。
- Easing: `ease-out`
- Allowed motion patterns: 颜色变化、透明度变化、进度条宽度变化、列表状态的短暂淡入。
- Forbidden: bounce、parallax、持续旋转装饰、自动滚动、会抢焦点的动画和与任务无关的动效。
- 后台任务的进度变化必须通过文本和数值可见，不得只靠动画表达。

## 7a. Container strategy

- **Strategy**: `none`
- **Per-surface overrides**:
  - management dashboard: `none`
  - UID evidence / review: `none`
  - form / settings: `none`
  - Chromium extension popup: `none` inside the popup; popup 外轮廓由浏览器负责
  - long-form content: `N/A — 首版没有长文内容 surface`
- 页面不使用卡片外框、投影或层层嵌套容器。重复对象使用无框列表行，区块使用留白、标题和状态色分组。
- 状态 pill、开关、按钮和选中导航可以使用背景色和 `radius-full` / `radius-sm`；它们不构成页面容器。

## 7b. Icon system

- **Set**: `lucide`
- **Weight**: `regular`
- **Treatment**: `monochrome`
- **Sizes**: `16 / 20 / 24px` baseline; `32 / 48px` for empty states。
- **Primary use color**: `currentColor`; only status or primary action may inherit `--color-primary`。
- **Mixing**: 不混用图标套件；缺失图标使用最接近的 Lucide 图标或省略，不临时引入 emoji、3D 图标或自绘品牌图标。
- 不熟悉或高风险动作必须同时显示文字；“官方拉黑”等动作不能只显示图标。

## 7c. Decoration

| Surface | Gradients | Textures | Motifs |
|---|---|---|---|
| Management dashboard | `none` | `none` | `none` |
| UID evidence / review | `none` | `none` | `none` |
| Form / settings | `none` | `none` | `none` |
| Chromium extension popup | `none` | `none` | `none` |

- 黑色幽默通过真实任务文案和少量辅助状态文案表达，不通过色情图片、品牌模仿、噪点、贴纸、渐变或装饰插画表达。
- 不为“会心一笑”新增独立视觉层；如果幽默文案会让状态、风险或下一步变得不清楚，优先保留业务文案。

## 8. Component conventions

### Buttons

- **Primary**: `#ff9900` background, black text, `6px 14px` padding, `36px` minimum height, `3px` radius。
- **Secondary / quiet**: `#1b1b1b` background, `#c6c6c6` text, same height and radius；不与 primary 并列争夺同等强调。
- **Ghost**: transparent background, `--color-primary` text, only for低风险次要入口。
- **Destructive**: explicit text such as “加入官方拉黑队列” or “撤销隐藏”; use `--color-error` only for danger feedback, never hide the meaning behind jokes。
- **Sizes**: `sm = 32px`, `md = 36px`, `lg = 40px` minimum height。
- Each action must produce a real state change or have a clearly visible disabled/static state; no decorative fake buttons in product UI.

### Inputs

- Default: `#1b1b1b` background, `#c6c6c6` text, no enclosing card.
- Focus: `2px` `--color-primary` outline with `2px` offset; focus cannot rely on color alone。
- Error: `--color-error` outline plus a short recovery message near the field。
- Disabled: `#969696` text at reduced opacity; preserve the reason for disabled state。
- Import and sample forms show format, required fields and current scope before submission。

### Lists and evidence

- Lists identify objects with the minimum stable combination: nickname snapshot + UID + one differentiating signal。
- UID is the primary key; nickname is always labeled as a snapshot and never replaces UID.
- List rows handle recognition and comparison. Evidence detail handles complete comment text, parent/root context, source video, signal, model reason and version.
- Review and queue rows use one dominant action per row. Batch actions show selection count and exact affected scope before submit.

### State coverage

| State | Visible expression | Next step |
|---|---|---|
| loading | 局部进度和“正在连接 / 正在载入” | 等待或查看保留的当前对象 |
| empty | “还没有视频任务 / 还没有待复核 UID” | 提交视频或导入样本 |
| filtered-empty | “当前筛选没有匹配结果” | 清除筛选或回到完整集合 |
| queued | “已排队，后台会继续处理” | 查看队列或暂停 |
| processing | 采集、分析、写入名单的阶段和进度 | 查看任务详情 |
| error | 发生了什么、是否自动重试、如何恢复 | 重试、修复登录态或查看错误 |
| ready | 完成统计、影响范围和可执行下一步 | 复核证据或查看队列 |
| permission-denied | 明确指出本机服务、B 站登录态或浏览器权限缺失 | 连接服务、重新登录或授权 |

## 9. Surfaces (templates)

- **Management dashboard**: 左侧工作区导航 + 右侧主工作区；主动作是“新建视频任务”。首屏先显示当前任务阶段和进度，再显示统计、待复核 UID 和官方拉黑队列摘要。管理页使用紧凑无框列表，最大宽度约 `1440px`。
- **UID evidence / review**: 列表负责 UID 识别和比较；详情展开完整证据。命中、仅隐藏、例外和失败使用稳定状态语义，不因幽默文案改变颜色或动作。
- **Chromium extension popup**: 目标宽度 `360px`；首屏显示当前 B 站视频身份、登录状态、本机服务连接和“提交这个视频”主动作；本页本地隐藏开关和缓存 UID 数量紧随其后；复杂复核入口回到管理页。
- **Form / settings**: 样本导入、模型配置和服务连接按任务分组，默认只显示当前任务必需字段；高级配置按需展开，不与提交任务的主动作混在一起。
- **Marketing landing / long-form content**: `N/A — 本项目首版是个人操作工具，不设计营销首页或长文内容页`。

## 10. Anti-patterns for this project

- 不复制 Pornhub 的 Logo、字标、站名、图片、文案或页面结构；只保留可辨识的黑黄参考和戏谑语气。
- 不使用渐变、噪点、纹理、装饰性插画、色情视觉或与评论过滤无关的品牌化素材。
- 不把页面做成嵌套卡片；`container: none` 下不使用卡片边框、投影或大面积圆角面板。
- 不把“官方拉黑”写成模糊的玩笑，也不使用仅图标的高风险操作。
- 不用昵称、词条或评论文本代替 UID；不在列表中隐藏 UID、来源视频或判定状态。
- 不用持续动画表达后台任务状态；进度必须有文字、数字或阶段。
- 不在同一区域放置多个同等强调的主 CTA；提交视频、确认拉黑和撤销操作必须区分。
- 不把模型提示词、视觉生成过程、设计约束或内部实现说明直接当作用户界面文案。
- 不使用 emoji 作为图标，不混用 Lucide 与其他图标风格。

## 11. Open questions

- 是否在 V1 之后提供浅色主题；当前只验证深色主题。
- 是否为项目设计独立的正式 Logo 和 Manifest 图标；当前使用中性的几何标记，不代表任何第三方品牌。
- 黑色幽默文案的固定词汇表尚未独立版本化；后续应保留“可笑但不影响判断”的边界。
- 管理页在窄屏下的完整表格交互仍需实现阶段验证；插件弹窗的 `360px` 宽度已在业务稿中验证。

## 12. Cross-surface notes

- 管理页与插件弹窗共享 `#151515 / #ff9900` 色彩、微软雅黑、Cascadia Mono、Lucide regular monochrome、紧凑间距和无投影规则。
- 管理页优先扫描、比较和复核；插件弹窗优先识别当前视频、提交任务和显示连接状态。不要把管理页的完整证据表格塞进弹窗。
- 同一状态在两个 surface 中保持同一名称和语义，例如“已隐藏”“待复核”“例外”“后台服务已连接”。
- 插件断开服务时仍可使用已同步 UID 缓存；界面必须显式显示缓存过滤与后台任务之间的区别。

Validated against `business-mockup-1.html` (latest iteration).
