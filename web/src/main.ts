import { ApiClient, ApiRequestError, type ApiList } from "../../shared/api.js";
import {
  connectionStateFromHealth,
  connectionStateFromRequestError,
  taskViewState,
  type ConnectionState,
} from "../../shared/state.js";
import type {
  BlacklistItem,
  Evidence,
  ReviewAction,
  SampleItem,
  SampleKind,
  SampleSet,
  UidRecord,
  VideoTask,
} from "../../shared/types.js";
import { mergeSampleItems, parseSampleText } from "./sample-import.js";

type ViewName = "dashboard" | "tasks" | "uids" | "reviews" | "samples" | "blacklist";
type Collection<T> = { items: T[] | null; error?: string };

interface ManagementState {
  activeView: ViewName;
  loading: boolean;
  connection: ConnectionState;
  tasks: Collection<VideoTask>;
  uids: Collection<UidRecord>;
  evidence: Collection<Evidence>;
  samples: Collection<SampleSet>;
  blacklist: Collection<BlacklistItem>;
  filters: Partial<Record<ViewName, string>>;
  sampleText: string;
  sampleKind: SampleKind;
  sampleFileItems: SampleItem[];
  notice?: { kind: "success" | "error"; message: string };
}

const navigation: Array<{ view: ViewName; label: string; hint: string }> = [
  { view: "dashboard", label: "工作台", hint: "任务与健康" },
  { view: "tasks", label: "视频任务", hint: "采集与分析" },
  { view: "uids", label: "UID 名单", hint: "本地隐藏" },
  { view: "reviews", label: "证据复核", hint: "判定与上下文" },
  { view: "samples", label: "样本库", hint: "版本与反馈" },
  { view: "blacklist", label: "拉黑队列", hint: "官方动作" },
];

export function mountManagementPage(root: HTMLElement, api = new ApiClient()): void {
  const state: ManagementState = {
    activeView: "dashboard",
    loading: false,
    connection: { kind: "loading", label: "正在连接本机服务" },
    tasks: { items: null },
    uids: { items: null },
    evidence: { items: null },
    samples: { items: null },
    blacklist: { items: null },
    filters: {},
    sampleText: "",
    sampleKind: "comment-positive",
    sampleFileItems: [],
  };

  root.innerHTML = renderFrame(state);
  root.addEventListener("click", (event) => void handleClick(event));
  root.addEventListener("submit", (event) => void handleSubmit(event));
  root.addEventListener("input", handleInput);
  root.addEventListener("change", (event) => void handleChange(event));
  void loadAll();

  async function loadAll(preserveNotice = false): Promise<void> {
    state.loading = true;
    if (!preserveNotice) state.notice = undefined;
    state.connection = { kind: "loading", label: "正在连接本机服务" };
    root.innerHTML = renderFrame(state);
    try {
      const health = await api.getHealth();
      state.connection = connectionStateFromHealth(health);
      if (state.connection.kind !== "ready") {
        state.loading = false;
        root.innerHTML = renderFrame(state);
        return;
      }
    } catch (error) {
      state.connection = connectionStateFromRequestError(asRequestError(error));
      state.loading = false;
      root.innerHTML = renderFrame(state);
      return;
    }

    const [tasks, uids, evidence, samples, blacklist] = await Promise.all([
      loadCollection(() => api.listTasks()),
      loadCollection(() => api.listUids()),
      loadCollection(() => api.listEvidence()),
      loadCollection(() => api.listSamples()),
      loadCollection(() => api.listBlacklist()),
    ]);
    state.tasks = tasks;
    state.uids = uids;
    state.evidence = evidence;
    state.samples = samples;
    state.blacklist = blacklist;
    state.loading = false;
    root.innerHTML = renderFrame(state);
  }

  async function handleClick(event: Event): Promise<void> {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const navigationButton = target.closest<HTMLElement>("[data-view]");
    if (navigationButton) {
      state.activeView = navigationButton.dataset.view as ViewName;
      state.notice = undefined;
      root.innerHTML = renderFrame(state);
      return;
    }
    if (target.closest("[data-refresh]")) {
      await loadAll();
      return;
    }

    const revokeUid = target.closest<HTMLElement>("[data-revoke-uid]");
    if (revokeUid) {
      await runAction(() => api.updateUid(revokeUid.dataset.revokeUid ?? "", { hidden: false, status: "exception" }), "本地隐藏已撤销");
      return;
    }
    const retryTask = target.closest<HTMLElement>("[data-retry-task]");
    if (retryTask) {
      const task = await runAction(() => api.retryTask(retryTask.dataset.retryTask ?? ""), "任务已重新排队");
      if (task) replaceTask(task);
      return;
    }
    const reviewButton = target.closest<HTMLElement>("[data-review-action]");
    if (reviewButton) {
      const evidenceId = reviewButton.dataset.evidenceId ?? "";
      const action = reviewButton.dataset.reviewAction as ReviewAction;
      await runAction(() => api.reviewEvidence(evidenceId, action), "复核操作已记录");
      return;
    }
    const publishSample = target.closest<HTMLElement>("[data-publish-sample]");
    if (publishSample) {
      await runAction(() => api.publishSample(publishSample.dataset.publishSample ?? ""), "样本版本已发布");
      return;
    }
    const queueAction = target.closest<HTMLElement>("[data-queue-action]");
    if (queueAction) {
      const itemId = queueAction.dataset.itemId ?? "";
      const action = queueAction.dataset.queueAction;
      const operation = action === "pause"
        ? api.pauseBlacklist(itemId)
        : action === "resume"
          ? api.resumeBlacklist(itemId)
          : api.retryBlacklist(itemId);
      await runAction(() => operation, "拉黑队列状态已更新");
    }
  }

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const data = new FormData(form);
    const formName = form.dataset.form;
    if (formName === "task") {
      const videoUrl = String(data.get("videoUrl") ?? "").trim();
      const bvid = String(data.get("bvid") ?? "").trim() || extractBvid(videoUrl);
      if (!bvid) {
        setNotice("error", "请输入普通 B 站 BV 视频 URL 或 BVID");
        return;
      }
      const task = await runAction(
        () => api.createTask({ bvid, videoUrl, title: String(data.get("title") ?? "").trim() || undefined }),
        "视频任务已创建",
      );
      if (task && "taskId" in task) replaceTask(task);
      return;
    }
    if (formName === "uid") {
      const uid = String(data.get("uid") ?? "").trim();
      if (!/^\d+$/.test(uid)) {
        setNotice("error", "UID 必须是数字，昵称只作为展示快照");
        return;
      }
      const record = await runAction(
        () => api.createUid({ uid, nicknameSnapshot: String(data.get("nickname") ?? "").trim(), hidden: true, status: "hidden" }),
        "UID 已加入本地隐藏名单",
      );
      if (record && "uid" in record) replaceUid(record);
      return;
    }
    if (formName === "sample") {
      const textItems = parseSampleText(state.sampleText, state.sampleKind);
      const items = mergeSampleItems(textItems, state.sampleFileItems);
      if (items.length === 0) {
        setNotice("error", "没有可发布的样本内容");
        return;
      }
      const sample = await runAction(() => api.createSample({ items }), "样本草稿已创建");
      if (sample && "sampleId" in sample) {
        state.sampleText = "";
        state.sampleFileItems = [];
        await loadAll();
      }
    }
  }

  function handleInput(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) return;
    if (target.dataset.filter) {
      state.filters[target.dataset.filter as ViewName] = target.value;
      root.querySelector("[data-content]")!.innerHTML = renderView(state);
      return;
    }
    if (target.name === "sampleText") {
      state.sampleText = target.value;
      const preview = root.querySelector<HTMLElement>("[data-sample-preview]");
      if (preview) preview.innerHTML = renderSamplePreview(state);
    }
  }

  async function handleChange(event: Event): Promise<void> {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) return;
    if (target.name === "sampleKind") {
      state.sampleKind = target.value as SampleKind;
      const preview = root.querySelector<HTMLElement>("[data-sample-preview]");
      if (preview) preview.innerHTML = renderSamplePreview(state);
    }
    if (target instanceof HTMLInputElement && target.name === "sampleFile" && target.files?.[0]) {
      const contents = await target.files[0].text();
      state.sampleFileItems = parseSampleText(contents, state.sampleKind, "file");
      const preview = root.querySelector<HTMLElement>("[data-sample-preview]");
      if (preview) preview.innerHTML = renderSamplePreview(state);
    }
  }

  async function runAction<T>(operation: () => Promise<T>, successMessage: string): Promise<T | undefined> {
    try {
      const result = await operation();
      state.notice = { kind: "success", message: successMessage };
      await loadAll(true);
      return result;
    } catch (error) {
      setNotice("error", messageFromError(error));
      return undefined;
    }
  }

  function replaceTask(task: VideoTask): void {
    const items = state.tasks.items ?? [];
    state.tasks = { items: [task, ...items.filter((item) => item.taskId !== task.taskId)] };
    state.activeView = "tasks";
    root.querySelector("[data-content]")!.innerHTML = renderView(state);
  }

  function replaceUid(record: UidRecord): void {
    const items = state.uids.items ?? [];
    state.uids = { items: [record, ...items.filter((item) => item.uid !== record.uid)] };
    state.activeView = "uids";
    root.querySelector("[data-content]")!.innerHTML = renderView(state);
  }

  function setNotice(kind: "success" | "error", message: string): void {
    state.notice = { kind, message };
    root.innerHTML = renderFrame(state);
  }
}

async function loadCollection<T>(load: () => Promise<ApiList<T>>): Promise<Collection<T>> {
  try {
    return { items: (await load()).items };
  } catch (error) {
    return { items: null, error: messageFromError(error) };
  }
}

function renderFrame(state: ManagementState): string {
  return `
    <div class="app-shell">
      <aside class="sidebar">
        <div class="brand-lockup">
          <p class="eyebrow">本机优先</p>
          <h1>评论过滤</h1>
          <p class="muted">只处理普通视频评论，不处理弹幕。</p>
        </div>
        <nav aria-label="工作区导航">
          ${navigation.map((item) => `
            <button class="nav-item ${state.activeView === item.view ? "is-active" : ""}" data-view="${item.view}" type="button">
              <span>${item.label}</span><small>${item.hint}</small>
            </button>`).join("")}
        </nav>
        <button class="button button-quiet refresh-button" data-refresh type="button" ${state.loading ? "disabled" : ""}>刷新数据</button>
      </aside>
      <main class="workspace">
        <header class="workspace-header">
          <div>
            <p class="eyebrow">个人管理页</p>
            <h2>${navigation.find((item) => item.view === state.activeView)?.label ?? "工作台"}</h2>
          </div>
          <div class="service-indicator" data-state="${state.connection.kind}">
            <span class="status-dot" aria-hidden="true"></span>
            <div><strong>${escapeHtml(state.connection.label)}</strong><small>${escapeHtml(("detail" in state.connection ? state.connection.detail : undefined) ?? "")}</small></div>
          </div>
        </header>
        ${state.notice ? `<p class="notice notice-${state.notice.kind}">${escapeHtml(state.notice.message)}</p>` : ""}
        <section data-content>${renderView(state)}</section>
      </main>
    </div>`;
}

function renderView(state: ManagementState): string {
  if (state.connection.kind !== "ready") return renderConnectionBlock(state.connection);
  if (state.loading) return renderState("loading", "正在载入本机数据", "等待 API 返回当前工作区");
  switch (state.activeView) {
    case "dashboard":
      return renderDashboard(state);
    case "tasks":
      return renderTasks(state);
    case "uids":
      return renderUids(state);
    case "reviews":
      return renderReviews(state);
    case "samples":
      return renderSamples(state);
    case "blacklist":
      return renderBlacklist(state);
  }
}

function renderConnectionBlock(connection: ConnectionState): string {
  const detail = ("detail" in connection ? connection.detail : undefined) ?? "请确认本机服务正在运行，并检查浏览器访问权限。";
  if (connection.kind === "loading") return renderState("loading", connection.label, detail);
  if (connection.kind === "permission-denied") return renderState("permission-denied", connection.label, detail);
  if (connection.kind === "offline") return renderState("error", connection.label, detail);
  return renderState("error", connection.label, detail);
}

function renderDashboard(state: ManagementState): string {
  const taskItems = state.tasks.items;
  const uidItems = state.uids.items;
  const evidenceItems = state.evidence.items;
  const queueItems = state.blacklist.items;
  return `
    <section class="hero-strip">
      <div><p class="eyebrow">当前主动作</p><h3>新建视频任务</h3><p class="muted">提交一个普通 BV 视频，后台会异步采集评论并保留任务状态。</p></div>
      ${renderTaskForm()}
    </section>
    <section class="stat-grid" aria-label="工作区统计">
      ${renderStat("视频任务", taskItems ? String(taskItems.length) : "未加载", "任务列表")}
      ${renderStat("本地 UID", uidItems ? String(uidItems.length) : "未加载", "名单权威在本机服务")}
      ${renderStat("待复核证据", evidenceItems ? String(evidenceItems.length) : "未加载", "命中与不确定")}
      ${renderStat("拉黑队列", queueItems ? String(queueItems.length) : "未加载", "官方动作")}
    </section>
    <div class="dashboard-columns">
      <section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">任务阶段</p><h3>最近视频任务</h3></div><button class="button button-ghost" data-view="tasks" type="button">查看全部</button></div>${renderTaskCollection(state.tasks, "dashboard")}</section>
      <section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">需要判断</p><h3>待复核 UID</h3></div><button class="button button-ghost" data-view="reviews" type="button">打开证据</button></div>${renderEvidenceCollection(state.evidence, "dashboard")}</section>
    </div>`;
}

function renderTasks(state: ManagementState): string {
  const filter = state.filters.tasks ?? "";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">异步工作单元</p><h3>视频任务</h3></div>${renderFilter("tasks", "搜索任务 ID、BVID 或标题", filter)}</div>${renderTaskForm()}${renderTaskCollection(state.tasks, "tasks", filter)}</section>`;
}

function renderUids(state: ManagementState): string {
  const filter = state.filters.uids ?? "";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">UID 是唯一主键</p><h3>全局 UID 名单</h3></div>${renderFilter("uids", "搜索 UID 或昵称快照", filter)}</div>
    <form class="inline-form" data-form="uid"><label>UID<input name="uid" inputmode="numeric" pattern="[0-9]+" required placeholder="例如 123456" /></label><label>昵称快照<input name="nickname" placeholder="可选，仅用于展示" /></label><button class="button button-primary" type="submit">加入本地隐藏</button></form>
    ${renderUidCollection(state.uids, filter)}</section>`;
}

function renderReviews(state: ManagementState): string {
  const filter = state.filters.reviews ?? "";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">完整证据</p><h3>判定复核</h3></div>${renderFilter("reviews", "搜索 UID、评论或来源视频", filter)}</div>${renderEvidenceCollection(state.evidence, "reviews", filter)}</section>`;
}

function renderSamples(state: ManagementState): string {
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">版本化输入</p><h3>样本库</h3></div></div>
    <form class="sample-form" data-form="sample"><div class="form-row"><label>样本类型<select name="sampleKind"><option value="comment-positive" ${state.sampleKind === "comment-positive" ? "selected" : ""}>评论正例</option><option value="comment-negative" ${state.sampleKind === "comment-negative" ? "selected" : ""}>评论反例</option><option value="nickname-positive" ${state.sampleKind === "nickname-positive" ? "selected" : ""}>昵称正例</option></select></label><label>文件导入<input name="sampleFile" type="file" accept=".txt,.csv,text/plain,text/csv" /></label></div><label>粘贴文本<textarea name="sampleText" rows="6" placeholder="每行一个样本；空行和 # 开头的行会跳过">${escapeHtml(state.sampleText)}</textarea></label><div class="sample-preview" data-sample-preview>${renderSamplePreview(state)}</div><button class="button button-primary" type="submit">创建样本草稿</button></form>
    <div class="list-heading"><h3>历史版本</h3></div>${renderSampleCollection(state.samples)}</section>`;
}

function renderBlacklist(state: ManagementState): string {
  const filter = state.filters.blacklist ?? "";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">明确命中后的官方动作</p><h3>拉黑队列</h3></div>${renderFilter("blacklist", "搜索 UID 或队列 ID", filter)}</div>${renderBlacklistCollection(state.blacklist, filter)}</section>`;
}

function renderTaskForm(): string {
  return `<form class="task-form" data-form="task"><label>视频 URL<input name="videoUrl" type="url" placeholder="https://www.bilibili.com/video/BV..." required /></label><label>BVID<input name="bvid" placeholder="可从 URL 自动提取" /></label><label>标题快照<input name="title" placeholder="可选" /></label><button class="button button-primary" type="submit">创建视频任务</button></form>`;
}

function renderTaskCollection(collection: Collection<VideoTask>, view: "dashboard" | "tasks", filter = ""): string {
  if (collection.items === null) return collection.error ? renderState("error", "任务列表加载失败", collection.error) : renderState("loading", "正在载入任务", "");
  const items = collection.items.filter((task) => `${task.taskId} ${task.bvid} ${task.title}`.toLowerCase().includes(filter.toLowerCase()));
  if (items.length === 0) return renderState(filter ? "filtered-empty" : "empty", filter ? "当前筛选没有匹配任务" : "还没有视频任务", filter ? "清除筛选或回到完整集合" : "从上方提交一个普通 BV 视频");
  return `<div class="list" data-list="${view}">${items.slice(0, view === "dashboard" ? 5 : undefined).map(renderTaskRow).join("")}</div>`;
}

function renderTaskRow(task: VideoTask): string {
  const view = taskViewState(task.status);
  return `<article class="list-row" data-state="${view.kind}"><div class="row-main"><strong>${escapeHtml(task.title || task.bvid)}</strong><span class="mono">${escapeHtml(task.taskId)} · ${escapeHtml(task.bvid)}</span><small>${escapeHtml(task.phase ?? "")} ${task.progress === undefined ? "" : `${task.progress}%`}</small></div><div class="row-actions"><span class="status-pill" data-state="${view.kind}">${view.label}</span>${task.status === "failed" ? `<button class="button button-ghost" data-retry-task="${escapeHtml(task.taskId)}" type="button">重试</button>` : ""}</div></article>`;
}

function renderUidCollection(collection: Collection<UidRecord>, filter: string): string {
  if (collection.items === null) return collection.error ? renderState("error", "UID 名单加载失败", collection.error) : renderState("loading", "正在载入 UID 名单", "");
  const items = collection.items.filter((item) => `${item.uid} ${item.nicknameSnapshot}`.toLowerCase().includes(filter.toLowerCase()));
  if (items.length === 0) return renderState(filter ? "filtered-empty" : "empty", filter ? "当前筛选没有匹配 UID" : "还没有本地隐藏 UID", filter ? "清除筛选或回到完整集合" : "添加一个 UID 后会同步到扩展缓存");
  return `<div class="list">${items.map((item) => `<article class="list-row"><div class="row-main"><strong class="mono">${escapeHtml(item.uid)}</strong><span>${escapeHtml(item.nicknameSnapshot || "无昵称快照")}</span><small>更新于 ${escapeHtml(item.updatedAt || "未提供")}</small></div><div class="row-actions"><span class="status-pill" data-state="${item.status}">${uidStatusLabel(item.status)}${item.hidden ? " · 本地隐藏" : ""}</span>${item.hidden ? `<button class="button button-ghost" data-revoke-uid="${escapeHtml(item.uid)}" type="button">撤销隐藏</button>` : ""}</div></article>`).join("")}</div>`;
}

function renderEvidenceCollection(collection: Collection<Evidence>, view: "dashboard" | "reviews", filter = ""): string {
  if (collection.items === null) return collection.error ? renderState("error", "证据加载失败", collection.error) : renderState("loading", "正在载入证据", "");
  const items = collection.items.filter((item) => `${item.uid} ${item.commentText} ${item.sourceVideo ?? ""}`.toLowerCase().includes(filter.toLowerCase()));
  if (items.length === 0) return renderState(filter ? "filtered-empty" : "empty", filter ? "当前筛选没有匹配证据" : "还没有待复核 UID", filter ? "清除筛选或回到完整集合" : "不确定结果会在这里等待判断");
  return `<div class="evidence-list">${items.slice(0, view === "dashboard" ? 4 : undefined).map(renderEvidenceRow).join("")}</div>`;
}

function renderEvidenceRow(evidence: Evidence): string {
  return `<details class="evidence-row"><summary><span><strong class="mono">${escapeHtml(evidence.uid)}</strong> ${escapeHtml(evidence.nicknameSnapshot || "无昵称快照")}</span><span class="status-pill" data-state="${evidence.result === "hit" ? "ready" : "processing"}">${evidence.result === "hit" ? "命中" : "不确定"}</span></summary><div class="evidence-body"><p class="evidence-comment">${escapeHtml(evidence.commentText)}</p><dl><div><dt>楼层上下文</dt><dd>${escapeHtml(evidence.threadContext ?? "未提供")}</dd></div><div><dt>来源视频</dt><dd>${evidence.sourceVideo ? `<a href="${escapeAttribute(evidence.sourceVideo)}" target="_blank" rel="noreferrer">${escapeHtml(evidence.sourceVideo)}</a>` : "未提供"}</dd></div><div><dt>评论链接</dt><dd>${evidence.commentUrl ? `<a href="${escapeAttribute(evidence.commentUrl)}" target="_blank" rel="noreferrer">打开来源</a>` : "未提供"}</dd></div><div><dt>命中信号</dt><dd>${escapeHtml(evidence.signal ?? "未提供")}</dd></div><div><dt>模型理由</dt><dd>${escapeHtml(evidence.modelReason ?? "未提供")}</dd></div><div><dt>模型版本</dt><dd class="mono">${escapeHtml(evidence.modelVersion ?? "未提供")}</dd></div></dl><div class="action-row"><button class="button button-quiet" data-review-action="keep" data-evidence-id="${escapeHtml(evidence.evidenceId)}" type="button">保留判定</button><button class="button button-quiet" data-review-action="revoke" data-evidence-id="${escapeHtml(evidence.evidenceId)}" type="button">撤销隐藏</button><button class="button button-quiet" data-review-action="hide-only" data-evidence-id="${escapeHtml(evidence.evidenceId)}" type="button">仅保留隐藏</button><button class="button button-danger" data-review-action="exception" data-evidence-id="${escapeHtml(evidence.evidenceId)}" type="button">加入例外</button><button class="button button-ghost" data-review-action="positive-sample" data-evidence-id="${escapeHtml(evidence.evidenceId)}" type="button">标记显著样例</button></div></div></details>`;
}

function renderSampleCollection(collection: Collection<SampleSet>): string {
  if (collection.items === null) return collection.error ? renderState("error", "样本库加载失败", collection.error) : renderState("loading", "正在载入样本库", "");
  if (collection.items.length === 0) return renderState("empty", "还没有样本版本", "通过文本或文件导入后创建草稿");
  return `<div class="list">${collection.items.map((sample) => `<article class="list-row"><div class="row-main"><strong>版本 ${sample.version}</strong><span>${sample.items.length} 条样本 · ${sample.status}</span><small class="mono">${escapeHtml(sample.sampleId)}</small></div><div class="row-actions">${sample.status === "draft" ? `<button class="button button-primary" data-publish-sample="${escapeHtml(sample.sampleId)}" type="button">发布版本</button>` : `<span class="status-pill" data-state="${sample.status === "published" ? "ready" : "paused"}">${sample.status === "published" ? "已发布" : "已停用"}</span>`}</div></article>`).join("")}</div>`;
}

function renderBlacklistCollection(collection: Collection<BlacklistItem>, filter: string): string {
  if (collection.items === null) return collection.error ? renderState("error", "拉黑队列加载失败", collection.error) : renderState("loading", "正在载入队列", "");
  const items = collection.items.filter((item) => `${item.uid} ${item.itemId}`.toLowerCase().includes(filter.toLowerCase()));
  if (items.length === 0) return renderState(filter ? "filtered-empty" : "empty", filter ? "当前筛选没有匹配队列项" : "拉黑队列为空", filter ? "清除筛选或回到完整集合" : "明确命中后才会生成官方拉黑任务");
  return `<div class="list">${items.map((item) => `<article class="list-row"><div class="row-main"><strong class="mono">${escapeHtml(item.uid)}</strong><span class="mono">${escapeHtml(item.itemId)}</span><small>${item.attempts} 次尝试${item.lastError ? ` · ${escapeHtml(item.lastError)}` : ""}</small></div><div class="row-actions"><span class="status-pill" data-state="${item.status === "completed" ? "ready" : item.status === "failed" || item.status === "blocked" ? "error" : item.status === "processing" ? "processing" : item.status}">${blacklistStatusLabel(item.status)}</span>${item.status === "queued" || item.status === "processing" ? `<button class="button button-ghost" data-queue-action="pause" data-item-id="${escapeHtml(item.itemId)}" type="button">暂停</button>` : ""}${item.status === "paused" ? `<button class="button button-ghost" data-queue-action="resume" data-item-id="${escapeHtml(item.itemId)}" type="button">恢复</button>` : ""}${item.status === "failed" ? `<button class="button button-ghost" data-queue-action="retry" data-item-id="${escapeHtml(item.itemId)}" type="button">重试</button>` : ""}</div></article>`).join("")}</div>`;
}

function renderSamplePreview(state: ManagementState): string {
  const items = mergeSampleItems(parseSampleText(state.sampleText, state.sampleKind), state.sampleFileItems);
  if (items.length === 0) return `<p class="muted">预览为空；创建前会再次去重。</p>`;
  return `<p class="preview-count">预览 ${items.length} 条，按“类型 + 文本”去重</p><ol>${items.slice(0, 8).map((item) => `<li><span class="status-pill" data-state="info">${sampleKindLabel(item.kind)}</span> ${escapeHtml(item.text)}</li>`).join("")}${items.length > 8 ? `<li class="muted">其余 ${items.length - 8} 条将在发布请求中一并提交</li>` : ""}</ol>`;
}

function renderFilter(name: ViewName, placeholder: string, value: string): string {
  return `<label class="filter-field"><span class="sr-only">${placeholder}</span><input data-filter="${name}" value="${escapeAttribute(value)}" placeholder="${placeholder}" /></label>`;
}

function renderStat(label: string, value: string, detail: string): string {
  return `<div class="stat-item"><span>${label}</span><strong>${escapeHtml(value)}</strong><small>${detail}</small></div>`;
}

function renderState(kind: string, title: string, detail: string): string {
  return `<div class="state-block" data-state="${kind}"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`;
}

function extractBvid(value: string): string {
  return value.match(/\/video\/(BV[0-9A-Za-z]{10})(?:[/?#]|$)/i)?.[1] ?? "";
}

function uidStatusLabel(status: UidRecord["status"]): string {
  return ({ hidden: "已隐藏", review: "待复核", queued: "已排队", blocked: "已拉黑", exception: "例外", failed: "失败", paused: "已暂停" })[status];
}

function blacklistStatusLabel(status: BlacklistItem["status"]): string {
  return ({ queued: "已排队", processing: "处理中", blocked: "平台拦截", failed: "失败", completed: "已完成", paused: "已暂停", cancelled: "已取消" })[status];
}

function sampleKindLabel(kind: SampleKind): string {
  return ({ "comment-positive": "评论正例", "comment-negative": "评论反例", "nickname-positive": "昵称正例" })[kind];
}

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，当前对象未被本地伪造更新";
}

function asRequestError(error: unknown): ApiRequestError {
  if (error instanceof ApiRequestError) return error;
  return new ApiRequestError(0, messageFromError(error));
}

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character] ?? character);
}

function escapeAttribute(value: unknown): string {
  return escapeHtml(value);
}

if (typeof document !== "undefined") {
  const root = document.querySelector<HTMLElement>("#app");
  if (root) mountManagementPage(root);
}
