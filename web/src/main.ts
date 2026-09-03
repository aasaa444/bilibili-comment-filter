import { ApiClient, ApiRequestError, type ApiList } from "../../shared/api.js";
import {
  connectionStateFromHealth,
  connectionStateFromRequestError,
  taskViewState,
  type ConnectionState,
} from "../../shared/state.js";
import type {
  AuthSession,
  AnalysisRun,
  BlacklistItem,
  BlacklistSettings,
  Evidence,
  FilterProfile,
  ModelHealth,
  ReviewAction,
  ReviewRecord,
  SampleItem,
  SampleKind,
  SampleSet,
  TaskComment,
  TaskAnalysis,
  TaskEvent,
  UidRecord,
  VideoTask,
} from "../../shared/types.js";
import { mergeSampleItems, parseSampleText } from "./sample-import.js";

type ViewName = "dashboard" | "tasks" | "uids" | "reviews" | "samples" | "blacklist";
type Collection<T> = { items: T[] | null; error?: string };
type EvidenceResultFilter = "uncertain" | "hit" | "all";
const TASK_COMMENTS_PAGE_SIZE = 20;

interface TaskDetailState {
  open: boolean;
  loading: boolean;
  comments: TaskComment[] | null;
  events: TaskEvent[] | null;
  analysis: TaskAnalysis | null;
  commentPage: number;
  error?: string;
  eventsError?: string;
  analysisError?: string;
}

interface ReviewFeedback {
  action: ReviewAction;
  status: "busy" | "success" | "error";
  message?: string;
}

interface ReviewBatchFeedback {
  status: "success" | "error";
  message: string;
}

interface ManagementState {
  activeView: ViewName;
  loading: boolean;
  connection: ConnectionState;
  auth: AuthSession | null;
  model: ModelHealth | null;
  tasks: Collection<VideoTask>;
  taskDetails: Record<string, TaskDetailState>;
  uids: Collection<UidRecord>;
  evidence: Collection<Evidence>;
  dashboardEvidence: Collection<Evidence>;
  reviewActions: Collection<ReviewRecord>;
  samples: Collection<SampleSet>;
  profiles: Collection<FilterProfile>;
  sampleDetails: Record<string, boolean>;
  blacklist: Collection<BlacklistItem>;
  blacklistSettings: BlacklistSettings | null;
  blacklistSettingsError?: string;
  filters: Partial<Record<ViewName, string>>;
  reviewStatus: "pending" | "history" | "all";
  reviewResultFilter: EvidenceResultFilter;
  selectedEvidenceId?: string;
  selectedEvidenceIds: string[];
  evidenceFeedback: Record<string, ReviewFeedback>;
  reviewBatchFeedback?: ReviewBatchFeedback;
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
    auth: null,
    model: null,
    tasks: { items: null },
    taskDetails: {},
    uids: { items: null },
    evidence: { items: null },
    dashboardEvidence: { items: null },
    reviewActions: { items: null },
    samples: { items: null },
    profiles: { items: null },
    sampleDetails: {},
    blacklist: { items: null },
    blacklistSettings: null,
    filters: {},
    reviewStatus: "pending",
    reviewResultFilter: "uncertain",
    selectedEvidenceIds: [],
    evidenceFeedback: {},
    sampleText: "",
    sampleKind: "comment-positive",
    sampleFileItems: [],
  };

  root.innerHTML = renderFrame(state);
  root.addEventListener("click", (event) => {
    closeMoreMenus(event);
    void handleClick(event);
  });
  root.addEventListener("submit", (event) => void handleSubmit(event));
  root.addEventListener("input", handleInput);
  root.addEventListener("change", (event) => void handleChange(event));
  if (typeof document !== "undefined") document.addEventListener("click", closeMoreMenus);
  void loadAll();

  function closeMoreMenus(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLElement && target.closest(".more-menu")) return;
    if (typeof root.querySelectorAll !== "function") return;
    root.querySelectorAll<HTMLDetailsElement>("details.more-menu[open]").forEach((menu) => menu.removeAttribute("open"));
  }

  async function loadAll(preserveNotice = false): Promise<void> {
    state.loading = true;
    if (!preserveNotice) state.notice = undefined;
    state.connection = { kind: "loading", label: "正在连接本机服务" };
    root.innerHTML = renderFrame(state);
    try {
      const health = await api.getHealth();
      state.model = health.model ?? null;
      state.connection = connectionStateFromHealth(health);
      if (state.connection.kind !== "ready") {
        state.loading = false;
        root.innerHTML = renderFrame(state);
        return;
      }
    } catch (error) {
      state.model = null;
      state.connection = connectionStateFromRequestError(asRequestError(error));
      state.loading = false;
      root.innerHTML = renderFrame(state);
      return;
    }

    const requestedEvidenceResult = state.reviewResultFilter === "all" ? undefined : state.reviewResultFilter;
    const [auth, tasks, uids, evidence, dashboardEvidence, reviewActions, samples, profiles, blacklist, blacklistSettings] = await Promise.all([
      api.getAuthSession().catch(() => null),
      loadCollection(() => api.listTasks()),
      loadCollection(() => api.listUids()),
      loadCollection(() => api.listEvidence({ reviewStatus: state.reviewStatus, result: requestedEvidenceResult })),
      state.reviewStatus === "pending" && requestedEvidenceResult === undefined
        ? Promise.resolve(null)
        : loadCollection(() => api.listEvidence({ reviewStatus: "pending" })),
      loadCollection(() => api.listReviewActions()),
      loadCollection(() => api.listSamples()),
      loadCollection(() => api.listProfiles()),
      loadCollection(() => api.listBlacklist()),
      loadBlacklistSettings(api),
    ]);
    state.auth = auth;
    state.tasks = tasks;
    state.uids = uids;
    state.evidence = evidence;
    state.dashboardEvidence = dashboardEvidence ?? evidence;
    const pendingEvidenceIds = new Set(evidence.items?.map((item) => item.evidenceId) ?? []);
    state.selectedEvidenceIds = state.selectedEvidenceIds.filter((id) => pendingEvidenceIds.has(id));
    if (state.selectedEvidenceId && !pendingEvidenceIds.has(state.selectedEvidenceId)) {
      state.selectedEvidenceId = undefined;
    }
    state.reviewActions = reviewActions;
    state.samples = samples;
    state.profiles = profiles;
    state.blacklist = blacklist;
    state.blacklistSettings = blacklistSettings.value;
    state.blacklistSettingsError = blacklistSettings.error;
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
      await runAction(() => api.removeUid(revokeUid.dataset.revokeUid ?? ""), "本地隐藏已撤销");
      return;
    }
    const retryTask = target.closest<HTMLElement>("[data-retry-task]");
    if (retryTask) {
      const task = await runAction(() => api.retryTask(retryTask.dataset.retryTask ?? ""), "任务已重新排队");
      if (task) replaceTask(task);
      return;
    }
    const retryComments = target.closest<HTMLElement>("[data-task-comments-retry]");
    if (retryComments) {
      await loadTaskComments(retryComments.dataset.taskCommentsRetry ?? "");
      return;
    }
    const retryObservability = target.closest<HTMLElement>("[data-task-observability-retry]");
    if (retryObservability) {
      await loadTaskComments(retryObservability.dataset.taskObservabilityRetry ?? "");
      return;
    }
    const commentPageButton = target.closest<HTMLElement>("[data-task-comments-page]");
    if (commentPageButton) {
      const taskId = commentPageButton.dataset.taskCommentsPage ?? "";
      const requestedPage = Number(commentPageButton.dataset.page);
      const detail = state.taskDetails[taskId];
      if (!detail || detail.comments === null || !Number.isInteger(requestedPage)) return;
      detail.commentPage = clampTaskCommentPage(requestedPage, detail.comments.length);
      renderContent();
      return;
    }
    const taskDetailsButton = target.closest<HTMLElement>("[data-task-details]");
    if (taskDetailsButton) {
      const taskId = taskDetailsButton.dataset.taskDetails ?? "";
      const detail = state.taskDetails[taskId] ?? createTaskDetailState(false);
      detail.open = !detail.open;
      state.taskDetails[taskId] = detail;
      renderContent();
      if (detail.open && detail.comments === null && !detail.loading) await loadTaskComments(taskId);
      return;
    }
    const reviewButton = target.closest<HTMLElement>("[data-review-action]");
    if (reviewButton) {
      const evidenceId = reviewButton.dataset.evidenceId ?? "";
      const action = reviewButton.dataset.reviewAction as ReviewAction;
      await runReviewAction(evidenceId, action);
      return;
    }
    const batchReviewButton = target.closest<HTMLElement>("[data-review-batch-action]");
    if (batchReviewButton) {
      await runBatchReview(batchReviewButton.dataset.reviewBatchAction as ReviewAction);
      return;
    }
    const evidenceSelect = target.closest<HTMLElement>("[data-evidence-select]");
    if (evidenceSelect) {
      state.selectedEvidenceId = evidenceSelect.dataset.evidenceSelect;
      renderContent();
      return;
    }
    const evidenceClose = target.closest<HTMLElement>("[data-evidence-close]");
    if (evidenceClose) {
      state.selectedEvidenceId = undefined;
      renderContent();
      return;
    }
    const sampleDetails = target.closest<HTMLElement>("[data-sample-details]");
    if (sampleDetails) {
      const sampleId = sampleDetails.dataset.sampleDetails ?? "";
      state.sampleDetails[sampleId] = !state.sampleDetails[sampleId];
      renderContent();
      return;
    }
    const publishSample = target.closest<HTMLElement>("[data-publish-sample]");
    if (publishSample) {
      await runAction(() => api.publishSample(publishSample.dataset.publishSample ?? ""), "样本版本已发布");
      return;
    }
    const activateProfile = target.closest<HTMLElement>("[data-activate-profile]");
    if (activateProfile) {
      const profileId = activateProfile.dataset.activateProfile ?? "";
      await runAction(() => api.activateProfile(profileId), "当前过滤策略已切换；新建任务将使用该策略");
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
        () => api.createTask({ bvid, videoUrl }),
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
      return;
    }
    if (formName === "profile") {
      const name = String(data.get("profileName") ?? "").trim();
      if (!name) {
        setNotice("error", "请输入策略名称");
        return;
      }
      const splitTerms = (field: string): string[] => String(data.get(field) ?? "")
        .split(/[,，\n]/).map((value) => value.trim()).filter(Boolean);
      const profile = await runAction(() => api.createProfile({
        name,
        description: String(data.get("profileDescription") ?? "").trim(),
        knownTerms: splitTerms("knownTerms"),
        standaloneTerms: splitTerms("standaloneTerms"),
        friendlyExceptions: splitTerms("friendlyExceptions"),
        hostileContext: splitTerms("hostileContext"),
        nicknamePositive: splitTerms("nicknamePositive"),
      }), "过滤策略已创建；切换后新任务会使用它");
      if (profile) await loadAll(true);
    }
  }

  function handleInput(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) return;
    if (target.dataset.filter) {
      state.filters[target.dataset.filter as ViewName] = target.value;
      if (target.dataset.filter === "reviews") {
        const visible = filteredEvidenceItems(state.evidence, target.value, state.reviewResultFilter) ?? [];
        const visibleIds = new Set(visible.map((item) => item.evidenceId));
        state.selectedEvidenceIds = state.selectedEvidenceIds.filter((evidenceId) => visibleIds.has(evidenceId));
        if (state.selectedEvidenceId && !visibleIds.has(state.selectedEvidenceId)) state.selectedEvidenceId = undefined;
      }
      renderContent();
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
    if (target instanceof HTMLInputElement && target.dataset.evidenceSelectToggle) {
      toggleEvidenceSelection(target.dataset.evidenceSelectToggle, target.checked);
      return;
    }
    if (target instanceof HTMLInputElement && target.dataset.evidenceSelectAll) {
      selectVisibleEvidence(target.checked);
      return;
    }
    if (target instanceof HTMLInputElement && target.name === "autoBlacklistEnabled") {
      await toggleBlacklistSettings(target.checked);
      return;
    }
    if (target instanceof HTMLSelectElement && target.name === "reviewStatus") {
      state.reviewStatus = target.value as ManagementState["reviewStatus"];
      state.selectedEvidenceId = undefined;
      state.selectedEvidenceIds = [];
      await loadAll(true);
      return;
    }
    if (target instanceof HTMLSelectElement && target.name === "reviewResultFilter") {
      if (target.value !== "uncertain" && target.value !== "hit" && target.value !== "all") return;
      state.reviewResultFilter = target.value;
      state.selectedEvidenceId = undefined;
      state.selectedEvidenceIds = [];
      await loadAll(true);
      return;
    }
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

  async function toggleBlacklistSettings(enabled: boolean): Promise<void> {
    const method = (api as unknown as {
      updateBlacklistSettings?: (value: boolean) => Promise<BlacklistSettings>;
    }).updateBlacklistSettings;
    if (!method) {
      setNotice("error", "当前本机服务不支持自动拉黑开关");
      return;
    }
    const previous = state.blacklistSettings;
    state.blacklistSettings = {
      enabled,
      mode: enabled ? "local_and_official_queue" : "local_only",
      updatedAt: previous?.updatedAt ?? "",
    };
    renderContent();
    try {
      state.blacklistSettings = await method.call(api, enabled);
      state.notice = {
        kind: "success",
        message: enabled ? "自动官方拉黑已开启；新命中将排入队列" : "自动官方拉黑已关闭；仅执行本地隐藏",
      };
      root.innerHTML = renderFrame(state);
    } catch (error) {
      state.blacklistSettings = previous;
      setNotice("error", messageFromError(error));
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

  async function runReviewAction(
    evidenceId: string,
    action: ReviewAction,
    options: { skipConfirmation?: boolean } = {},
  ): Promise<boolean> {
    if (!evidenceId || state.evidenceFeedback[evidenceId]?.status === "busy") return false;
    if (action === "confirm" && state.blacklistSettings?.enabled !== true) {
      state.evidenceFeedback[evidenceId] = {
        action,
        status: "error",
        message: "自动执行官方拉黑已关闭；请先开启总开关，或选择“仅本地隐藏”",
      };
      renderContent();
      return false;
    }
    if (!options.skipConfirmation && !confirmReviewAction(action, 1, 1)) {
      state.evidenceFeedback[evidenceId] = {
        action,
        status: "error",
        message: "已取消本次操作",
      };
      renderContent();
      return false;
    }
    state.evidenceFeedback[evidenceId] = { action, status: "busy" };
    renderContent();
    try {
      const visibleBeforeReview = filteredEvidenceItems(state.evidence, state.filters.reviews ?? "", state.reviewResultFilter) ?? [];
      const currentIndex = visibleBeforeReview.findIndex((item) => item.evidenceId === evidenceId);
      const nextEvidenceId = currentIndex < 0
        ? undefined
        : visibleBeforeReview[currentIndex + 1]?.evidenceId ?? visibleBeforeReview[currentIndex - 1]?.evidenceId;
      const record = await api.reviewEvidence(evidenceId, action);
      const actions = state.reviewActions.items ?? [];
      state.reviewActions = {
        items: [record, ...actions.filter((item) => item.reviewId !== record.reviewId)],
      };
      state.evidence = {
        items: (state.evidence.items ?? []).filter((item) => item.evidenceId !== evidenceId),
      };
      state.selectedEvidenceIds = state.selectedEvidenceIds.filter((id) => id !== evidenceId);
      state.selectedEvidenceId = nextEvidenceId;
      state.evidenceFeedback[evidenceId] = {
        action,
        status: "success",
        message: "已移入复核历史",
      };
      renderContent();
      return true;
    } catch (error) {
      state.evidenceFeedback[evidenceId] = { action, status: "error", message: messageFromError(error) };
      renderContent();
      return false;
    }
  }

  async function runBatchReview(action: ReviewAction): Promise<void> {
    const visibleItems = filteredEvidenceItems(state.evidence, state.filters.reviews ?? "", state.reviewResultFilter) ?? [];
    const visibleIds = new Set(visibleItems.map((item) => item.evidenceId));
    const ids = state.selectedEvidenceIds.filter((evidenceId) => visibleIds.has(evidenceId));
    if (ids.length === 0) {
      state.reviewBatchFeedback = { status: "error", message: "请先选择至少一条证据" };
      renderContent();
      return;
    }
    if (action === "confirm" && state.blacklistSettings?.enabled !== true) {
      state.reviewBatchFeedback = {
        status: "error",
        message: "自动执行官方拉黑已关闭；请先开启总开关，或选择批量仅本地隐藏",
      };
      renderContent();
      return;
    }
    const uidCount = new Set(
      ids
        .map((evidenceId) => state.evidence.items?.find((item) => item.evidenceId === evidenceId)?.uid)
        .filter(Boolean),
    ).size;
    if (!confirmReviewAction(action, ids.length, uidCount)) {
      state.reviewBatchFeedback = { status: "error", message: "已取消本次批量操作" };
      renderContent();
      return;
    }
    let successCount = 0;
    for (const evidenceId of ids) {
      if (await runReviewAction(evidenceId, action, { skipConfirmation: true })) successCount += 1;
    }
    state.reviewBatchFeedback = {
      status: successCount === ids.length ? "success" : "error",
      message: `批量操作已完成：${successCount}/${ids.length} 条证据，影响 ${uidCount} 个 UID`,
    };
    renderContent();
  }

  function confirmReviewAction(action: ReviewAction, evidenceCount: number, uidCount: number): boolean {
    const confirmDialog = (globalThis as typeof globalThis & {
      confirm?: (message?: string) => boolean;
    }).confirm;
    if (typeof confirmDialog !== "function") return true;
    const actionLabel = reviewActionLabel(action);
    const scope = evidenceCount === 1
      ? "这条证据"
      : `${evidenceCount} 条证据（${uidCount} 个 UID）`;
    const effect = action === "confirm"
      ? "进入官方拉黑队列"
      : action === "hide-only"
        ? "仅保留本地隐藏，不进入官方队列"
        : action === "revoke"
          ? "撤销本地隐藏"
          : actionLabel;
    return confirmDialog(`即将对${scope}执行“${effect}”，是否继续？`);
  }

  function toggleEvidenceSelection(evidenceId: string | undefined, checked: boolean): void {
    if (!evidenceId) return;
    const selected = new Set(state.selectedEvidenceIds);
    if (checked) selected.add(evidenceId);
    else selected.delete(evidenceId);
    state.selectedEvidenceIds = [...selected];
    renderContent();
  }

  function selectVisibleEvidence(checked: boolean): void {
    const items = filteredEvidenceItems(state.evidence, state.filters.reviews ?? "", state.reviewResultFilter);
    if (items === null) return;
    const selected = new Set(state.selectedEvidenceIds);
    for (const item of items) {
      if (checked) selected.add(item.evidenceId);
      else selected.delete(item.evidenceId);
    }
    state.selectedEvidenceIds = [...selected];
    renderContent();
  }

async function loadTaskComments(taskId: string): Promise<void> {
    const detail = state.taskDetails[taskId] ?? createTaskDetailState(true);
    detail.open = true;
    detail.loading = true;
    detail.comments = null;
    detail.events = null;
    detail.analysis = null;
    detail.commentPage = 1;
    detail.error = undefined;
    detail.eventsError = undefined;
    detail.analysisError = undefined;
    state.taskDetails[taskId] = detail;
    renderContent();
    const [comments, events, analysis] = await Promise.all([
      api.listTaskComments(taskId).then((result) => ({ items: result.items })).catch((error) => ({ error })),
      api.listTaskEvents(taskId).then((result) => ({ items: result.items })).catch((error) => ({ error })),
      api.getTaskAnalysis(taskId).then((result) => ({ value: result })).catch((error) => ({ error })),
    ]);
    if ("error" in comments) detail.error = messageFromError(comments.error);
    else detail.comments = comments.items;
    if ("error" in events) detail.eventsError = messageFromError(events.error);
    else detail.events = events.items;
    if ("error" in analysis) detail.analysisError = messageFromError(analysis.error);
    else detail.analysis = analysis.value;
    detail.loading = false;
    renderContent();
  }

  function renderContent(): void {
    const content = root.querySelector<HTMLElement>("[data-content]");
    if (!content) return;
    if (state.activeView === "reviews" && renderReviewRegions()) return;
    const previousInbox = root.querySelector<HTMLElement>("[data-evidence-inbox]");
    const scrollTop = previousInbox?.scrollTop ?? 0;
    content.innerHTML = renderView(state);
    const nextInbox = root.querySelector<HTMLElement>("[data-evidence-inbox]");
    if (nextInbox && scrollTop > 0) nextInbox.scrollTop = scrollTop;
  }

  function renderReviewRegions(): boolean {
    const inbox = root.querySelector<HTMLElement>("[data-evidence-inbox]");
    const inspector = root.querySelector<HTMLElement>("[data-evidence-inspector]");
    const history = root.querySelector<HTMLElement>("[data-review-history]");
    const items = filteredEvidenceItems(state.evidence, state.filters.reviews ?? "", state.reviewResultFilter);
    if (!inbox || !inspector || !history || items === null) return false;

    const scrollTop = inbox.scrollTop;
    inbox.innerHTML = renderEvidenceInboxContents(state, "reviews", state.filters.reviews ?? "");
    inbox.scrollTop = scrollTop;

    const selected = state.evidence.items?.find((item) => item.evidenceId === state.selectedEvidenceId);
    inspector.innerHTML = renderEvidenceInspectorContents(selected, state);
    history.innerHTML = renderReviewHistory(state.reviewActions, state.filters.reviews ?? "", state.evidenceFeedback);
    return true;
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

function createTaskDetailState(open: boolean): TaskDetailState {
  return { open, loading: false, comments: null, events: null, analysis: null, commentPage: 1 };
}

function clampTaskCommentPage(requestedPage: number, commentCount: number): number {
  const pageCount = Math.max(1, Math.ceil(commentCount / TASK_COMMENTS_PAGE_SIZE));
  return Math.min(Math.max(1, requestedPage), pageCount);
}

async function loadCollection<T>(load: () => Promise<ApiList<T>>): Promise<Collection<T>> {
  try {
    return { items: (await load()).items };
  } catch (error) {
    return { items: null, error: messageFromError(error) };
  }
}

async function loadBlacklistSettings(api: ApiClient): Promise<{ value: BlacklistSettings | null; error?: string }> {
  const method = (api as unknown as { getBlacklistSettings?: () => Promise<BlacklistSettings> }).getBlacklistSettings;
  if (!method) return { value: null };
  try {
    return { value: await method.call(api) };
  } catch (error) {
    return { value: null, error: messageFromError(error) };
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
      return `${renderBlacklistControlStatus(state.blacklistSettings, state.blacklistSettingsError, false)}${renderBlacklist(state)}`;
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
  const evidenceItems = state.dashboardEvidence.items;
  const queueItems = state.blacklist.items;
  return `
    <section class="hero-strip">
      <div><p class="eyebrow">当前主动作</p><h3>新建视频任务</h3><p class="muted">提交一个普通 BV 视频，后台会异步采集评论并保留任务状态。</p></div>
      ${renderTaskForm()}
    </section>
    ${renderAuthDiagnostic(state.auth)}
    ${renderModelDiagnostic(state.model)}
    ${renderFilterProfileControl(state.profiles)}
    ${renderBlacklistControlStatus(state.blacklistSettings, state.blacklistSettingsError, true)}
    <section class="stat-grid" aria-label="工作区统计">
      ${renderStat("视频任务", taskItems ? String(taskItems.length) : "未加载", "任务列表")}
      ${renderStat("本地 UID", uidItems ? String(uidItems.length) : "未加载", "名单权威在本机服务")}
      ${renderStat("待复核证据", evidenceItems ? String(evidenceItems.length) : "未加载", "命中与不确定")}
      ${renderStat("拉黑队列", queueItems ? String(queueItems.length) : "未加载", "官方动作")}
    </section>
    <div class="dashboard-columns">
      <section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">任务阶段</p><h3>最近视频任务</h3></div><button class="button button-ghost" data-view="tasks" type="button">查看全部</button></div>${renderTaskCollection(state.tasks, "dashboard", "", state.taskDetails, state.profiles.items ?? [])}</section>
      <section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">需要判断</p><h3>待复核 UID</h3></div><button class="button button-ghost" data-view="reviews" type="button">打开证据</button></div>${renderEvidenceCollection(state, "dashboard")}</section>
    </div>`;
}

function renderModelDiagnostic(model: ModelHealth | null): string {
  if (!model) {
    return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">远程模型诊断</p><h3>模型状态暂不可用</h3></div><span class="status-pill" data-state="error">无法读取</span></div><p class="muted">本地隐藏和任务提交不依赖模型配置；刷新管理页后可重新读取。</p></section>`;
  }
  const ready = model.status === "ready";
  const label = ready ? "模型已配置" : "模型未配置";
  const state = ready ? "ready" : "paused";
  const flag = (value: boolean): string => (value ? "已配置" : "未配置");
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">远程模型诊断</p><h3>AI 分析配置</h3></div><span class="status-pill" data-state="${state}">${label}</span></div><p class="muted">${escapeHtml(model.detail)}</p><p class="muted">地址：${flag(model.baseUrlConfigured)} · 模型名：${flag(model.modelConfigured)} · API Key：${flag(model.apiKeyConfigured)}</p></section>`;
}

function renderFilterProfileControl(collection: Collection<FilterProfile>): string {
  if (collection.items === null) {
    return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">过滤策略</p><h3>当前策略暂不可读取</h3></div><span class="status-pill" data-state="error">无法读取</span></div><p class="muted">${escapeHtml(collection.error ?? "本机服务尚未返回策略数据")}</p></section>`;
  }
  const current = collection.items.find((profile) => profile.isCurrent) ?? collection.items[0];
  if (!current) return renderState("empty", "尚未创建过滤策略", "请创建一条策略后再提交视频任务");
  const profileRows = collection.items.map((profile) => {
    const ruleCount = profile.knownTerms.length + profile.standaloneTerms.length + profile.nicknamePositive.length;
    return `<article class="list-row"><div class="row-main"><strong>${escapeHtml(profile.name)}</strong><span>${escapeHtml(profile.description || "未填写目标描述")}</span><small>规则 ${ruleCount} 条 · 例外 ${profile.friendlyExceptions.length} 条</small></div><div class="row-actions"><span class="status-pill" data-state="${profile.isCurrent ? "ready" : "info"}">${profile.isCurrent ? "当前生效" : "可切换"}</span>${profile.isCurrent ? "" : `<button class="button button-ghost" data-activate-profile="${escapeHtml(profile.profileId)}" type="button">设为当前</button>`}</div></article>`;
  }).join("");
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">当前过滤策略</p><h3>${escapeHtml(current.name)}</h3><p class="muted">${escapeHtml(current.description)}</p></div><span class="status-pill" data-state="ready">新任务生效</span></div><div class="list">${profileRows}</div><details class="profile-create"><summary>创建通用过滤策略</summary><form class="sample-form" data-form="profile"><div class="form-row"><label>策略名称<input name="profileName" required placeholder="例如：广告评论过滤" /></label><label>目标描述<input name="profileDescription" placeholder="识别什么评论或账号" /></label></div><div class="form-row"><label>关键词<input name="knownTerms" placeholder="逗号或换行分隔" /></label><label>强命中词<input name="standaloneTerms" placeholder="出现即命中" /></label></div><div class="form-row"><label>友军例外<input name="friendlyExceptions" placeholder="优先排除" /></label><label>恶意上下文<input name="hostileContext" placeholder="与关键词组合判定" /></label></div><label>昵称高置信样本<input name="nicknamePositive" placeholder="昵称语义直接表达目标时使用" /></label><button class="button button-primary" type="submit">创建策略</button></form></details></section>`;
}

function renderBlacklistControlStatus(
  settings: BlacklistSettings | null,
  error: string | undefined,
  readonly: boolean,
): string {
  if (!settings) {
    return `<section class="workspace-section blacklist-control" data-control-readonly="${readonly}"><div class="section-heading"><div><p class="eyebrow">官方动作总开关</p><h3>自动执行官方拉黑</h3></div><span class="status-pill" data-state="error">暂不可读取</span></div><p class="muted">${escapeHtml(error ?? "本机服务尚未返回开关状态")}</p></section>`;
  }
  const enabledLabel = settings.enabled ? "已开启 · 命中后排入官方拉黑队列" : "已关闭 · 仅本地隐藏";
  const control = readonly
    ? `<span class="status-pill" data-state="${settings.enabled ? "ready" : "paused"}">${settings.enabled ? "已开启" : "已关闭"}</span>`
    : `<label class="switch-control"><input type="checkbox" name="autoBlacklistEnabled" ${settings.enabled ? "checked" : ""} /><span class="switch-track" aria-hidden="true"></span><span>${settings.enabled ? "开启" : "关闭"}</span></label>`;
  return `<section class="workspace-section blacklist-control" data-control-readonly="${readonly}"><div class="section-heading"><div><p class="eyebrow">官方动作总开关</p><h3>自动执行官方拉黑</h3></div>${control}</div><p class="blacklist-control-mode">${enabledLabel}</p><p class="muted">本地隐藏始终立即生效；${settings.enabled ? "新命中会进入官方队列，已有 queued 项继续消费。" : "不会创建或消费新的官方拉黑任务。已有队列项会保留。"}</p></section>`;
}

function renderAuthDiagnostic(session: AuthSession | null): string {
  if (!session) {
    return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">登录态诊断</p><h3>认证状态暂不可用</h3></div><span class="status-pill" data-state="error">无法读取</span></div><p class="muted">请刷新管理页后重试。</p></section>`;
  }
  const state = session.status === "valid" ? "ready" : session.status === "missing" ? "paused" : "error";
  const label = ({
    valid: "会话有效",
    invalid: "会话已失效",
    missing: "尚未同步",
    verification_failed: "验证失败",
  } as const)[session.status];
  const checkedAt = session.checkedAt ? `最近检查：${session.checkedAt}` : "尚未检查时间";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">登录态诊断</p><h3>后台认证状态</h3></div><span class="status-pill" data-state="${state}">${label}</span></div><p class="muted">${escapeHtml(session.detail)} · ${escapeHtml(checkedAt)}${session.cookiePresent ? " · 已收到会话" : " · 未收到会话"}</p></section>`;
}

function renderTasks(state: ManagementState): string {
  const filter = state.filters.tasks ?? "";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">异步工作单元</p><h3>视频任务</h3></div>${renderFilter("tasks", "搜索任务 ID、BVID 或标题", filter)}</div>${renderTaskForm()}${renderTaskCollection(state.tasks, "tasks", filter, state.taskDetails, state.profiles.items ?? [])}</section>`;
}

function renderUids(state: ManagementState): string {
  const filter = state.filters.uids ?? "";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">UID 是唯一主键</p><h3>全局 UID 名单</h3></div>${renderFilter("uids", "搜索 UID 或昵称快照", filter)}</div>
    <form class="inline-form" data-form="uid"><label>UID<input name="uid" inputmode="numeric" pattern="[0-9]+" required placeholder="例如 123456" /></label><label>昵称快照<input name="nickname" placeholder="可选，仅用于展示" /></label><button class="button button-primary" type="submit">加入本地隐藏</button></form>
    ${renderUidCollection(state.uids, filter)}</section>`;
}

function renderReviews(state: ManagementState): string {
  const filter = state.filters.reviews ?? "";
  const selected = state.evidence.items?.find((item) => item.evidenceId === state.selectedEvidenceId);
  const reviewStatusLabels = {
    pending: "待复核收件箱",
    history: "复核历史证据",
    all: "全部证据",
  } as const;
  const statusSelector = `<label class="review-status-filter">证据范围<select name="reviewStatus">${Object.entries(reviewStatusLabels).map(([value, label]) => `<option value="${value}" ${state.reviewStatus === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>`;
  const reviewResultLabels: Record<EvidenceResultFilter, string> = {
    uncertain: "AI 不确定（优先复核）",
    hit: "AI 已命中",
    all: "全部 AI 结果",
  };
  const resultSelector = `<label class="review-status-filter">AI 判定<select name="reviewResultFilter">${Object.entries(reviewResultLabels).map(([value, label]) => `<option value="${value}" ${state.reviewResultFilter === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>`;
  const description = state.reviewStatus === "pending"
    ? state.reviewResultFilter === "uncertain"
      ? "当前优先处理 AI 不确定证据；完成一项复核后会移入下方历史。"
      : "完成一项复核后，证据会从收件箱移出，并保留在下方复核历史。"
    : "历史与全部视图用于回看已处理证据；主要动作只在待复核收件箱提供。";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">${reviewStatusLabels[state.reviewStatus]}</p><h3>判定复核</h3><p class="muted">${description}</p></div><div class="review-heading-controls">${statusSelector}${resultSelector}${renderFilter("reviews", "搜索 UID、评论或来源视频", filter)}</div></div><div class="review-workbench">${renderEvidenceCollection(state, "reviews", filter)}${renderEvidenceInspector(selected, state)}</div><div class="list-heading"><h3>复核历史</h3></div><div data-review-history>${renderReviewHistory(state.reviewActions, filter, state.evidenceFeedback)}</div></section>`;
}

function renderSamples(state: ManagementState): string {
  const activeProfile = state.profiles.items?.find((profile) => profile.isCurrent);
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">版本化输入</p><h3>样本库</h3><p class="muted">${activeProfile ? `新样本将归入当前策略：${escapeHtml(activeProfile.name)}` : "新样本将归入当前过滤策略"}</p></div></div>
    <form class="sample-form" data-form="sample"><div class="form-row"><label>样本类型<select name="sampleKind"><option value="comment-positive" ${state.sampleKind === "comment-positive" ? "selected" : ""}>评论正例</option><option value="comment-negative" ${state.sampleKind === "comment-negative" ? "selected" : ""}>评论反例</option><option value="nickname-positive" ${state.sampleKind === "nickname-positive" ? "selected" : ""}>昵称正例</option></select></label><label>文件导入<input name="sampleFile" type="file" accept=".txt,.csv,text/plain,text/csv" /></label></div><label>粘贴文本<textarea name="sampleText" rows="6" placeholder="每行一个样本；空行和 # 开头的行会跳过">${escapeHtml(state.sampleText)}</textarea></label><div class="sample-preview" data-sample-preview>${renderSamplePreview(state)}</div><button class="button button-primary" type="submit">创建样本草稿</button></form>
    <div class="list-heading"><h3>版本快照</h3></div>${renderSampleCollection(state.samples, state.sampleDetails)}</section>`;
}

function renderBlacklist(state: ManagementState): string {
  const filter = state.filters.blacklist ?? "";
  return `<section class="workspace-section"><div class="section-heading"><div><p class="eyebrow">明确命中后的官方动作</p><h3>拉黑队列</h3></div>${renderFilter("blacklist", "搜索 UID 或队列 ID", filter)}</div>${renderBlacklistCollection(state.blacklist, filter)}</section>`;
}

function renderTaskForm(): string {
  return `<form class="task-form" data-form="task"><label>视频 URL<input name="videoUrl" type="url" placeholder="https://www.bilibili.com/video/BV..." required /></label><label>BVID<input name="bvid" placeholder="可从 URL 自动提取" /></label><p class="muted">任务名称将从 B 站视频元数据读取，浏览器页签标题不会作为原标题保存。</p><button class="button button-primary" type="submit">创建视频任务</button></form>`;
}

function renderTaskCollection(collection: Collection<VideoTask>, view: "dashboard" | "tasks", filter = "", taskDetails: Record<string, TaskDetailState> = {}, profiles: FilterProfile[] = []): string {
  if (collection.items === null) return collection.error ? renderState("error", "任务列表加载失败", collection.error) : renderState("loading", "正在载入任务", "");
  const items = collection.items.filter((task) => `${task.taskId} ${task.bvid} ${task.title}`.toLowerCase().includes(filter.toLowerCase()));
  if (items.length === 0) return renderState(filter ? "filtered-empty" : "empty", filter ? "当前筛选没有匹配任务" : "还没有视频任务", filter ? "清除筛选或回到完整集合" : "从上方提交一个普通 BV 视频");
  return `<div class="list" data-list="${view}">${items.slice(0, view === "dashboard" ? 5 : undefined).map((task) => renderTaskRow(task, taskDetails[task.taskId], profiles)).join("")}</div>`;
}

function renderTaskRow(task: VideoTask, detail?: TaskDetailState, profiles: FilterProfile[] = []): string {
  const view = taskViewState(task.status);
  const isOpen = detail?.open === true;
  const canRetry = task.status === "failed" || task.status === "partial" || task.status === "paused";
  return `<article class="task-row" data-state="${view.kind}"><div class="list-row"><div class="row-main"><strong>${escapeHtml(task.title || task.bvid)}</strong><span class="mono">${escapeHtml(task.taskId)} · ${escapeHtml(task.bvid)}</span><small>${escapeHtml(profileLabel(profiles, task.profileId))} · ${escapeHtml(task.phase ?? "")} ${task.progress === undefined ? "" : `${task.progress}%`}</small></div><div class="row-actions"><span class="status-pill" data-state="${view.kind}">${view.label}</span><button class="button button-ghost" data-task-details="${escapeHtml(task.taskId)}" type="button" aria-expanded="${isOpen ? "true" : "false"}">${isOpen ? "收起详情" : "查看任务详情"}</button>${canRetry ? `<button class="button button-ghost" data-retry-task="${escapeHtml(task.taskId)}" type="button">重试</button>` : ""}</div></div>${isOpen ? renderTaskDetail(task, detail) : ""}</article>`;
}

function renderTaskDetail(task: VideoTask, detail: TaskDetailState): string {
  const failedMarkup = renderTaskFailures(task.failedItems);
  let commentsMarkup: string;
  if (detail.loading) {
    commentsMarkup = renderState("loading", "正在载入任务评论", "只更新当前任务详情");
  } else if (detail.error) {
    commentsMarkup = `<div class="task-comments-error">${renderState("error", "评论加载失败", detail.error)}<button class="button button-ghost" data-task-comments-retry="${escapeHtml(task.taskId)}" type="button">重试加载评论</button></div>`;
  } else if (!detail.comments || detail.comments.length === 0) {
    commentsMarkup = renderState("empty", "暂无已保存评论", "该任务没有可展示的根评论或楼中楼");
  } else {
    commentsMarkup = renderTaskComments(task.taskId, detail.comments, detail.commentPage);
  }
  const observabilityMarkup = `<div class="task-observability-grid">${renderTaskAnalysis(task, detail.analysis, detail.analysisError, detail.loading)}${renderTaskTimeline(task, detail.events, detail.eventsError, detail.loading)}</div>`;
  const taskIssueMarkup = task.error || task.errorCode ? renderTaskIssue(task.errorCode, task.error) : "";
  const taskIssueState = task.errorCode === "collection_incomplete" ? "info" : "error";
  const taskIssueLabel = task.errorCode ? taskErrorLabel(task.errorCode) : "任务有失败项";
  return `<section class="task-detail" data-task-detail="${escapeHtml(task.taskId)}" aria-label="任务详情"><div class="task-detail-heading"><div><p class="eyebrow">采集结果</p><h4>任务详情</h4></div>${taskIssueMarkup ? `<span class="status-pill" data-state="${taskIssueState}">${escapeHtml(taskIssueLabel)}</span>` : `<span class="status-pill" data-state="ready">已保存结果</span>`}</div><div class="task-stat-grid"><div class="task-stat"><span>保存根评论</span><strong>${formatTaskNumber(task.collectedComments)}</strong></div><div class="task-stat"><span>保存楼中楼</span><strong>${formatTaskNumber(task.replyCount)}</strong></div><div class="task-stat"><span>置顶评论</span><strong>${formatTaskNumber(task.pinnedComments)}</strong></div><div class="task-stat"><span>覆盖率</span><strong>${formatTaskCoverage(task.coverage)}</strong></div><div class="task-stat"><span>请求页数</span><strong>${formatTaskNumber(task.requestedPages)}</strong></div><div class="task-stat"><span>声明评论 / 回复</span><strong>${formatTaskNumber(task.declaredComments)} / ${formatTaskNumber(task.declaredReplies)}</strong></div><div class="task-stat"><span>声明总量</span><strong>${formatTaskNumber(task.declaredTotal)}</strong></div></div><div class="task-failures"><h5>失败项</h5>${failedMarkup}</div>${taskIssueMarkup}${observabilityMarkup}<div class="task-comments-section"><div class="section-heading"><div><p class="eyebrow">评论明细</p><h5>根评论与楼中楼</h5></div></div>${commentsMarkup}</div></section>`;
}

interface TaskFailureSummary {
  label: string;
  count: number;
}

function renderTaskFailures(failedItems: string[] | undefined): string {
  if (failedItems === undefined) return `<p class="muted">未提供</p>`;
  if (failedItems.length === 0) return `<p class="muted">无</p>`;
  const summaries = summarizeTaskFailures(failedItems);
  const summaryMarkup = summaries
    .map((summary) => `<li><span>${escapeHtml(summary.label)}</span><strong>${summary.count} 条</strong></li>`)
    .join("");
  const rawMarkup = `<details class="task-failure-raw"><summary>查看原始失败项（${failedItems.length} 条）</summary><ul class="task-failure-list">${failedItems.map((item) => `<li class="mono">${escapeHtml(item)}</li>`).join("")}</ul></details>`;
  return `<ul class="task-failure-summary">${summaryMarkup}</ul>${rawMarkup}`;
}

function summarizeTaskFailures(failedItems: string[]): TaskFailureSummary[] {
  const counts = new Map<string, number>();
  for (const item of failedItems) {
    const label = taskFailureLabel(item);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()].map(([label, count]) => ({ label, count }));
}

function taskFailureLabel(item: string): string {
  const normalized = item.trim().toLowerCase();
  if (normalized.startsWith("inconsistent_root_item:")) {
    if (normalized.includes("missing_comment_id")) return "根评论缺少评论 ID";
    if (normalized.includes(":not_object")) return "根评论结构异常";
    return "根评论数据异常";
  }
  if (normalized.startsWith("empty_reply_page:")) return "楼中楼空页";
  if (normalized.startsWith("empty_root_page:")) return "根评论空页";
  if (normalized.startsWith("reply_page_failed:")) return "楼中楼采集失败";
  return "其他采集异常";
}

function renderTaskIssue(errorCode: string | undefined, errorMessage: string | undefined): string {
  const label = errorCode ? taskErrorLabel(errorCode) : "任务有失败项";
  const message = taskErrorMessage(errorCode, errorMessage);
  const rawMarkup = errorCode || errorMessage
    ? `<details class="task-raw-diagnostic"><summary>查看内部错误信息</summary><dl><div><dt>错误码</dt><dd class="mono">${escapeHtml(errorCode ?? "未提供")}</dd></div><div><dt>原始信息</dt><dd>${escapeHtml(errorMessage ?? "未提供")}</dd></div></dl></details>`
    : "";
  const state = errorCode === "collection_incomplete" ? "task-error task-error-info" : "task-error";
  return `<div class="${state}"><strong>${escapeHtml(label)}</strong><p>${escapeHtml(message)}</p>${rawMarkup}</div>`;
}

function taskErrorLabel(errorCode: string): string {
  return ({
    collection_incomplete: "采集未完整",
    collection_failed: "评论采集失败",
    collection_paused: "采集已暂停",
    auth_unavailable: "登录态不可用",
    invalid_model_response: "AI 返回格式无法识别",
    model_unavailable: "AI 服务暂不可用",
    analysis_failed: "AI 分析失败",
  } as Record<string, string>)[errorCode] ?? "任务处理失败";
}

function taskErrorMessage(errorCode: string | undefined, errorMessage: string | undefined): string {
  if (errorCode === "collection_incomplete") {
    return errorMessage?.includes("were analyzed")
      ? "采集没有覆盖全部评论，但已保存的评论已经继续交给 AI 分析。"
      : "采集没有覆盖全部评论，当前已保存内容未能继续分析。";
  }
  if (errorCode === "invalid_model_response") {
    return "模型返回内容无法解析为有效 JSON，请检查模型输出格式和最大输出长度。";
  }
  if (errorCode === "model_unavailable") return "模型服务暂时不可用，已保留采集结果，稍后可以重试。";
  if (errorCode === "auth_unavailable") return "B 站登录态不可用，采集已暂停；更新登录态后再重试。";
  return errorMessage || "任务未能完成，请查看处理时间线。";
}

function renderTaskAnalysis(
  task: VideoTask,
  analysis: TaskAnalysis | null,
  error: string | undefined,
  loading: boolean,
): string {
  const taskId = task.taskId;
  if (error) {
    return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">AI 运行记录</p><h5>AI 分析摘要</h5></div><span class="status-pill" data-state="error">读取失败</span></div>${renderState("error", "AI 分析记录加载失败", error)}<button class="button button-ghost" data-task-observability-retry="${escapeHtml(taskId)}" type="button">重试读取诊断</button></section>`;
  }
  if (analysis === null) {
    const detail = loading
      ? ""
      : isTerminalTask(task)
        ? "任务在诊断记录启用前完成，本机没有保存 AI 分析记录，无法从历史数据补写。"
        : "该任务尚未进入分析阶段";
    const title = loading ? "正在读取 AI 分析记录" : isTerminalTask(task) ? "历史任务未启用诊断" : "暂无 AI 分析记录";
    return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">AI 运行记录</p><h5>AI 分析摘要</h5></div>${!loading && isTerminalTask(task) ? `<span class="status-pill" data-state="info">历史任务</span>` : ""}</div>${renderState(loading ? "loading" : "empty", title, detail)}</section>`;
  }
  const run = analysis.latest;
  if (!run) {
    const isLegacy = isTerminalTask(task) && analysis.attempts.length === 0;
    return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">AI 运行记录</p><h5>AI 分析摘要</h5></div><span class="status-pill" data-state="info">${isLegacy ? "历史任务" : "未开始"}</span></div>${renderState("empty", isLegacy ? "历史任务未启用诊断" : "AI 尚未开始", isLegacy ? "任务在诊断记录启用前完成，无法从历史数据补写。" : "采集阶段没有把任务交给模型")}</section>`;
  }
  const runState = run.status === "completed" ? "ready" : run.status === "failed" ? "error" : run.status === "unavailable" ? "paused" : "processing";
  const errorMarkup = run.errorMessage
    ? renderTaskIssue(run.errorCode, run.errorMessage)
    : "";
  return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">AI 运行记录 · 第 ${run.attempt + 1} 次尝试</p><h5>AI 分析摘要</h5></div><span class="status-pill" data-state="${runState}">${analysisStatusLabel(run.status)}</span></div><div class="task-analysis-grid"><div class="task-stat"><span>模型</span><strong class="mono">${escapeHtml(run.model ?? "未记录")}</strong></div><div class="task-stat"><span>样本版本</span><strong class="mono">${escapeHtml(run.sampleVersion ?? "未使用")}</strong></div><div class="task-stat"><span>分析批次</span><strong>${run.batchCount}</strong></div><div class="task-stat"><span>分析 UID</span><strong>${run.accountCount}</strong></div><div class="task-stat"><span>命中</span><strong>${run.hitCount}</strong></div><div class="task-stat"><span>不确定</span><strong>${run.uncertainCount}</strong></div><div class="task-stat"><span>非目标</span><strong>${run.nonTargetCount}</strong></div><div class="task-stat"><span>证据</span><strong>${run.evidenceCount}</strong></div></div>${analysis.attempts.length > 1 ? `<p class="muted">已保留 ${analysis.attempts.length} 次分析尝试的摘要。</p>` : ""}${errorMarkup}</section>`;
}

function renderTaskTimeline(
  task: VideoTask,
  events: TaskEvent[] | null,
  error: string | undefined,
  loading: boolean,
): string {
  const taskId = task.taskId;
  if (error) {
    return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">任务诊断</p><h5>处理时间线</h5></div><span class="status-pill" data-state="error">读取失败</span></div>${renderState("error", "时间线加载失败", error)}<button class="button button-ghost" data-task-observability-retry="${escapeHtml(taskId)}" type="button">重试读取诊断</button></section>`;
  }
  if (events === null) {
    return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">任务诊断</p><h5>处理时间线</h5></div></div>${loading ? renderState("loading", "正在读取处理时间线", "") : renderState("empty", "暂无处理记录", "")}</section>`;
  }
  if (events.length === 0) {
    const isLegacy = isTerminalTask(task);
    return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">任务诊断</p><h5>处理时间线</h5></div>${isLegacy ? `<span class="status-pill" data-state="info">历史任务</span>` : ""}</div>${renderState("empty", isLegacy ? "历史任务未启用诊断" : "暂无处理记录", isLegacy ? "没有可回溯的处理记录；对可重试任务点击“重试”后会生成完整记录。" : "任务还没有开始执行")}</section>`;
  }
  return `<section class="task-observability-panel"><div class="section-heading"><div><p class="eyebrow">任务诊断</p><h5>处理时间线</h5></div><span class="status-pill" data-state="info">${events.length} 条记录</span></div><ol class="task-timeline">${events.map((event) => `<li class="task-timeline-item"><div class="task-timeline-meta"><span class="status-pill" data-state="${event.status === "failed" ? "error" : event.status === "succeeded" ? "ready" : "info"}">${escapeHtml(taskEventLabel(event.eventType))}</span><span>${escapeHtml(taskPhaseLabel(event.phase))}</span><time datetime="${escapeAttribute(event.createdAt)}">${escapeHtml(event.createdAt)}</time></div><p>${escapeHtml(taskEventMessage(event))}</p>${renderTaskEventDetails(event.details)}</li>`).join("")}</ol></section>`;
}

function renderTaskEventDetails(details: Record<string, unknown>): string {
  const entries = Object.entries(details);
  if (entries.length === 0) return "";
  const visible = entries.filter(([key, value]) => isVisibleTaskDetail(key, value));
  const visibleMarkup = visible.length > 0
    ? `<dl class="task-event-details">${visible.map(([key, value]) => `<div><dt>${escapeHtml(taskDetailLabel(key))}</dt><dd>${escapeHtml(formatTaskDetailValue(key, value))}</dd></div>`).join("")}</dl>`
    : "";
  const rawMarkup = `<details class="task-raw-diagnostic"><summary>查看原始诊断数据</summary><pre class="mono">${escapeHtml(formatRawTaskDetails(details))}</pre></details>`;
  return `${visibleMarkup}${rawMarkup}`;
}

const VISIBLE_TASK_DETAIL_KEYS = new Set([
  "account_count",
  "batch_count",
  "evidence_count",
  "hit_count",
  "uncertain_count",
  "non_target_count",
  "partial_result_count",
  "error_code",
  "error_message",
  "response_valid",
  "sample_version",
  "requested_pages",
  "saved_comments",
  "saved_replies",
  "declared_comments",
  "declared_replies",
  "declared_total",
  "coverage",
  "failed_item_count",
  "complete",
  "terminal",
  "analysis_continues",
  "collection_complete",
  "result_count",
  "model",
  "attempt",
  "from",
  "to",
  "reason",
]);

function isVisibleTaskDetail(key: string, value: unknown): boolean {
  return VISIBLE_TASK_DETAIL_KEYS.has(key) && (typeof value !== "object" || value === null);
}

function formatTaskDetailValue(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value === null || value === undefined) return "未提供";
  if (key === "coverage" && typeof value === "number") return formatTaskCoverage(value);
  if ((key === "from" || key === "to") && typeof value === "string") return taskStatusLabel(value);
  if (key === "error_code" && typeof value === "string") return taskErrorLabel(value);
  return String(value);
}

function formatRawTaskDetails(details: Record<string, unknown>): string {
  try {
    return JSON.stringify(details, null, 2);
  } catch {
    return "原始诊断数据无法格式化";
  }
}

function taskEventMessage(event: TaskEvent): string {
  const details = event.details;
  switch (event.eventType) {
    case "task_created":
      return "任务已创建，等待开始采集";
    case "task_started":
      return "开始执行任务";
    case "retry_scheduled":
      return "重试已排队";
    case "task_transition":
      return `任务状态：${taskStatusLabel(details.from)} → ${taskStatusLabel(details.to)}`;
    case "phase_started":
      return event.phase === "collecting" ? "开始采集评论" : event.phase === "analyzing" ? "开始 AI 分析" : "处理阶段已开始";
    case "collection_progress":
      return "采集器返回当前进度";
    case "collection_saved":
      return "已保存评论和续采进度";
    case "collection_paused":
      return "采集已暂停，等待恢复条件";
    case "collection_incomplete":
      return details.analysis_continues === true || (taskDetailNumber(details, "saved_comments") ?? 0) + (taskDetailNumber(details, "saved_replies") ?? 0) > 0
        ? "采集未完整；已保存评论继续进入 AI 分析"
        : "采集未完整；没有可分析评论，因此未启动 AI";
    case "collection_failed":
      return "评论采集失败";
    case "analysis_started":
      return `准备批量分析${taskDetailNumber(details, "account_count") === undefined ? " UID" : ` ${taskDetailNumber(details, "account_count")} 个 UID`}`;
    case "model_batch":
      return event.status === "succeeded"
        ? `AI 批次已全部返回${taskDetailNumber(details, "batch_count") === undefined ? "" : `，共 ${taskDetailNumber(details, "batch_count")} 批`}`
        : "开始发送 AI 批次";
    case "model_response":
      return `AI 返回结果已通过格式校验${taskDetailNumber(details, "result_count") === undefined ? "" : `，得到 ${taskDetailNumber(details, "result_count")} 个 UID`}`;
    case "analysis_completed":
      return `AI 分析完成：命中 ${taskDetailNumber(details, "hit_count") ?? 0}，不确定 ${taskDetailNumber(details, "uncertain_count") ?? 0}，非目标 ${taskDetailNumber(details, "non_target_count") ?? 0}`;
    case "analysis_failed":
      return `AI 分析失败${typeof details.error_code === "string" ? `：${taskErrorLabel(details.error_code)}` : ""}`;
    default:
      return /[\u3400-\u9fff]/.test(event.message) ? event.message : taskEventLabel(event.eventType);
  }
}

function taskDetailNumber(details: Record<string, unknown>, key: string): number | undefined {
  return typeof details[key] === "number" && Number.isFinite(details[key]) ? details[key] as number : undefined;
}

function taskStatusLabel(value: unknown): string {
  return ({ queued: "排队", collecting: "采集", analyzing: "AI 分析", completed: "完成", partial: "部分完成", failed: "失败", paused: "暂停" } as Record<string, string>)[String(value)] ?? String(value ?? "未提供");
}

function analysisStatusLabel(status: AnalysisRun["status"]): string {
  return ({ running: "分析中", completed: "分析成功", failed: "分析失败", unavailable: "模型不可用", not_started: "未进入 AI" } as Record<string, string>)[status] ?? status;
}

function taskEventLabel(eventType: string): string {
  return ({ task_created: "任务创建", task_started: "开始执行", retry_scheduled: "已排队重试", task_transition: "状态变更", phase_started: "阶段开始", phase_failed: "阶段失败", collection_progress: "采集进度", collection_saved: "采集已保存", collection_paused: "采集暂停", collection_incomplete: "采集未完整", collection_failed: "采集失败", analysis_started: "分析开始", model_batch: "模型批次", model_response: "模型响应", analysis_completed: "分析完成", analysis_failed: "分析失败" } as Record<string, string>)[eventType] ?? eventType;
}

function taskPhaseLabel(phase: string): string {
  return ({ queued: "排队", collecting: "采集", analyzing: "AI 分析", completed: "完成", task: "任务" } as Record<string, string>)[phase] ?? phase;
}

function taskDetailLabel(key: string): string {
  return ({ account_count: "账号数", batch_count: "批次数", evidence_count: "证据数", hit_count: "命中", uncertain_count: "不确定", non_target_count: "非目标", partial_result_count: "部分结果", error_code: "错误类型", error_type: "错误类型", error_message: "错误信息", response_valid: "响应有效", sample_version: "样本版本", requested_pages: "请求页数", saved_comments: "保存根评论", saved_replies: "保存回复", declared_comments: "声明评论", declared_replies: "声明回复", declared_total: "声明总量", coverage: "覆盖率", failed_item_count: "失败项数", complete: "采集完成", terminal: "采集终止", analysis_continues: "继续分析", collection_complete: "采集完整", result_count: "结果数", model: "模型", attempt: "尝试次数", from: "原状态", to: "新状态", reason: "原因" } as Record<string, string>)[key] ?? key;
}

function isTerminalTask(task: VideoTask): boolean {
  return task.status === "ready" || task.status === "partial" || task.status === "failed";
}

function renderTaskComments(taskId: string, comments: TaskComment[], requestedPage: number): string {
  const pageCount = Math.max(1, Math.ceil(comments.length / TASK_COMMENTS_PAGE_SIZE));
  const page = clampTaskCommentPage(requestedPage, comments.length);
  const startIndex = (page - 1) * TASK_COMMENTS_PAGE_SIZE;
  const pageItems = comments.slice(startIndex, startIndex + TASK_COMMENTS_PAGE_SIZE);
  const endIndex = startIndex + pageItems.length;
  const pagination = pageCount > 1
    ? `<nav class="task-comments-pagination" aria-label="评论分页"><button class="button button-quiet" data-task-comments-page="${escapeHtml(taskId)}" data-page="${page - 1}" type="button" aria-label="上一页" ${page === 1 ? "disabled" : ""}>上一页</button><span aria-live="polite">第 ${page} / ${pageCount} 页</span><button class="button button-quiet" data-task-comments-page="${escapeHtml(taskId)}" data-page="${page + 1}" type="button" aria-label="下一页" ${page === pageCount ? "disabled" : ""}>下一页</button></nav>`
    : "";
  return `<div class="task-comments-toolbar"><span class="task-comments-meta">显示 ${startIndex + 1}-${endIndex} 条，共 ${comments.length} 条评论 · 每页 ${TASK_COMMENTS_PAGE_SIZE} 条</span>${pagination}</div><div class="task-comments">${pageItems.map(renderTaskComment).join("")}</div>`;
}

function renderTaskComment(comment: TaskComment): string {
  const isReply = comment.parentId !== null || comment.level === "reply";
  const relation = isReply ? "楼中楼回复" : "根评论";
  const context = comment.context.length > 0 ? comment.context.join("\n") : "未提供";
  return `<article class="task-comment ${isReply ? "task-comment-reply" : ""}"><div class="task-comment-heading"><div><strong>${relation}</strong><span class="mono">UID ${escapeHtml(comment.uid)}</span><span>${escapeHtml(comment.nickname || "无昵称")}</span></div><div class="row-actions">${comment.isPinned ? `<span class="status-pill" data-state="info">置顶</span>` : ""}<span class="status-pill" data-state="${isReply ? "info" : "ready"}">${isReply ? "回复" : "根评论"}</span></div></div><p class="task-comment-content">${escapeHtml(comment.content)}</p><dl><div><dt>上下文</dt><dd>${escapeHtml(context)}</dd></div><div><dt>评论 ID</dt><dd class="mono">${escapeHtml(comment.commentId)}</dd></div><div><dt>父评论</dt><dd class="mono">${escapeHtml(comment.parentId ?? "无")}</dd></div></dl></article>`;
}

function formatTaskNumber(value: number | undefined): string {
  return value === undefined ? "未提供" : String(value);
}

function formatTaskCoverage(value: number | undefined): string {
  if (value === undefined) return "未提供";
  const percent = value >= 0 && value <= 1 ? value * 100 : value;
  return `${Number.isInteger(percent) ? percent : percent.toFixed(1)}%`;
}

function renderUidCollection(collection: Collection<UidRecord>, filter: string): string {
  if (collection.items === null) return collection.error ? renderState("error", "UID 名单加载失败", collection.error) : renderState("loading", "正在载入 UID 名单", "");
  const items = collection.items.filter((item) => `${item.uid} ${item.nicknameSnapshot}`.toLowerCase().includes(filter.toLowerCase()));
  if (items.length === 0) return renderState(filter ? "filtered-empty" : "empty", filter ? "当前筛选没有匹配 UID" : "还没有本地隐藏 UID", filter ? "清除筛选或回到完整集合" : "添加一个 UID 后会同步到扩展缓存");
  return `<div class="list">${items.map((item) => `<article class="list-row"><div class="row-main"><strong class="mono">${escapeHtml(item.uid)}</strong><span>${escapeHtml(item.nicknameSnapshot || "无昵称快照")}</span><small>更新于 ${escapeHtml(item.updatedAt || "未提供")}</small></div><div class="row-actions"><span class="status-pill" data-state="${item.status}">${uidStatusLabel(item.status)}${item.hidden ? " · 本地隐藏" : ""}</span>${item.hidden ? `<button class="button button-ghost" data-revoke-uid="${escapeHtml(item.uid)}" type="button">撤销隐藏</button>` : ""}</div></article>`).join("")}</div>`;
}

function renderEvidenceCollection(state: ManagementState, view: "dashboard" | "reviews", filter = ""): string {
  const evidenceCollection = view === "dashboard" ? state.dashboardEvidence : state.evidence;
  const items = filteredEvidenceItems(evidenceCollection, filter, view === "reviews" ? state.reviewResultFilter : "all");
  const isInbox = view === "reviews" && state.reviewStatus === "pending";
  const resultFilter = view === "reviews" ? state.reviewResultFilter : "all";
  if (items === null) {
    return evidenceCollection.error
      ? renderState("error", "证据加载失败", evidenceCollection.error)
      : renderState("loading", "正在载入证据", "");
  }
  if (items.length === 0) {
    const resultEmptyTitle = resultFilter === "uncertain"
      ? "当前没有 AI 不确定证据"
      : resultFilter === "hit"
        ? "当前没有 AI 已命中证据"
        : "待复核收件箱为空";
    const resultEmptyDetail = resultFilter === "all"
      ? "已处理证据会保留在下方复核历史"
      : "可以切换 AI 判定筛选查看其他证据";
    const emptyState = renderState(
      filter ? "filtered-empty" : "empty",
      filter ? "当前筛选没有匹配证据" : resultEmptyTitle,
      filter ? "清除筛选或回到完整集合" : resultEmptyDetail,
    );
    if (!isInbox) return emptyState;
    return `<div class="evidence-inbox" data-evidence-inbox><div class="evidence-inbox-heading"><span>待处理收件箱 · 0 条证据</span></div>${renderEvidenceInboxToolbar(state, items)}${emptyState}</div>`;
  }
  const visibleItems = view === "dashboard" ? items.slice(0, 4) : items;
  const toolbar = isInbox ? renderEvidenceInboxToolbar(state, items) : "";
  return `<div class="evidence-inbox" data-evidence-inbox><div class="evidence-inbox-heading"><span>${view === "reviews" ? `当前筛选 ${items.length} 条证据` : `最近 ${visibleItems.length} 条证据`}</span>${view === "dashboard" ? `<span class="muted">主要字段已直接显示</span>` : ""}</div>${toolbar}<div class="evidence-list">${visibleItems.map((item) => renderEvidenceRow(item, state, view)).join("")}</div></div>`;
}

function filteredEvidenceItems(
  collection: Collection<Evidence>,
  filter: string,
  resultFilter: EvidenceResultFilter = "all",
): Evidence[] | null {
  if (collection.items === null) return null;
  const normalizedFilter = filter.toLowerCase();
  return collection.items.filter((item) => (
    (resultFilter === "all" || item.result === resultFilter)
    && `${item.uid} ${item.nicknameSnapshot} ${item.commentText} ${item.sourceVideo ?? ""} ${item.videoId} ${item.signals.join(" ")}`
      .toLowerCase()
      .includes(normalizedFilter)
  ));
}

function renderEvidenceInboxContents(state: ManagementState, view: "dashboard" | "reviews", filter: string): string {
  const markup = renderEvidenceCollection(state, view, filter);
  const openTag = `<div class="evidence-inbox" data-evidence-inbox>`;
  if (!markup.startsWith(openTag)) return markup;
  const closeIndex = markup.lastIndexOf("</div>");
  return closeIndex > openTag.length ? markup.slice(openTag.length, closeIndex) : markup;
}

function renderEvidenceInboxToolbar(state: ManagementState, items: Evidence[]): string {
  const visibleIds = new Set(items.map((item) => item.evidenceId));
  const selectedIds = state.selectedEvidenceIds.filter((evidenceId) => visibleIds.has(evidenceId));
  const selectedCount = selectedIds.length;
  const uidCount = selectedEvidenceUidCount(state, selectedIds);
  const allSelected = items.length > 0 && items.every((item) => selectedIds.includes(item.evidenceId));
  const hasBusySelection = selectedIds.some((evidenceId) => state.evidenceFeedback[evidenceId]?.status === "busy");
  const feedback = state.reviewBatchFeedback
    ? `<span class="evidence-batch-feedback" data-state="${state.reviewBatchFeedback.status}">${escapeHtml(state.reviewBatchFeedback.message)}</span>`
    : "";
  const blacklistDisabled = state.blacklistSettings?.enabled !== true;
  const action = (value: ReviewAction, label: string, className = "button button-quiet", requiresBlacklist = false) => `<button class="${className}" data-review-batch-action="${value}" type="button" ${selectedCount === 0 || hasBusySelection || (requiresBlacklist && blacklistDisabled) ? "disabled" : ""}${requiresBlacklist && blacklistDisabled ? " title=\"请先在拉黑队列页面开启总开关\"" : ""}>${label}</button>`;
  return `<div class="evidence-inbox-toolbar"><label class="evidence-select-all"><input type="checkbox" data-evidence-select-all="true" ${allSelected ? "checked" : ""} ${items.length === 0 ? "disabled" : ""} /><span>全选当前筛选（${items.length}）</span></label><span class="evidence-selection-summary">已选 ${selectedCount} 条证据 · ${uidCount} 个 UID</span><div class="evidence-batch-actions">${action("confirm", blacklistDisabled ? "批量确认官方拉黑（先开启总开关）" : "批量确认官方拉黑", "button button-primary", true)}${action("hide-only", "批量仅本地隐藏")}${action("revoke", "批量撤销隐藏", "button button-danger")}</div>${feedback}</div>`;
}

function selectedEvidenceUidCount(state: ManagementState, evidenceIds: string[]): number {
  return new Set(
    evidenceIds
      .map((evidenceId) => state.evidence.items?.find((item) => item.evidenceId === evidenceId)?.uid)
      .filter((uid): uid is string => Boolean(uid)),
  ).size;
}

function renderEvidenceRow(evidence: Evidence, state: ManagementState, view: "dashboard" | "reviews"): string {
  const selected = state.selectedEvidenceIds.includes(evidence.evidenceId);
  const human = evidenceHumanState(evidence, state);
  const feedback = state.evidenceFeedback[evidence.evidenceId];
  const isInbox = view === "reviews" && state.reviewStatus === "pending";
  const selection = isInbox
    ? `<label class="evidence-row-checkbox"><span class="sr-only">选择 ${escapeHtml(evidence.uid)}</span><input type="checkbox" data-evidence-select-toggle="${escapeHtml(evidence.evidenceId)}" ${selected ? "checked" : ""} ${feedback?.status === "busy" ? "disabled" : ""} /></label>`
    : "";
  const inspectButton = view === "reviews"
    ? `<button class="button button-ghost evidence-inspect-button" data-evidence-select="${escapeHtml(evidence.evidenceId)}" type="button">${state.selectedEvidenceId === evidence.evidenceId ? "已选中详情" : "查看详情"}</button>`
    : `<button class="button button-ghost evidence-inspect-button" data-view="reviews" type="button">去复核</button>`;
  const actions = isInbox ? renderEvidenceActionControls(evidence, state) : "";
  return `<article class="evidence-row ${selected ? "is-selected" : ""}" data-evidence-row="${escapeHtml(evidence.evidenceId)}"><div class="evidence-row-layout">${selection}<div class="evidence-row-content"><div class="evidence-row-heading"><div class="evidence-identity"><strong class="mono">${escapeHtml(evidence.uid)}</strong><span>${escapeHtml(evidence.nicknameSnapshot || "无昵称快照")}</span></div><div class="evidence-statuses"><span class="status-pill" data-state="${evidence.result === "hit" ? "ready" : "processing"}">AI · ${evidence.result === "hit" ? "命中" : "不确定"}</span><span class="status-pill" data-state="${human.state}">人工 · ${escapeHtml(human.label)}</span></div></div><div class="evidence-row-meta"><span class="mono">${escapeHtml(profileLabel(state.profiles.items ?? [], evidence.profileId))}</span><span class="mono">置信度 ${formatEvidenceConfidence(evidence.confidence)}</span><span>${escapeHtml(evidence.videoId || "来源视频未提供")}</span><span>${escapeHtml(evidence.createdAt || "时间未提供")}</span></div><p class="evidence-summary">${escapeHtml(evidence.commentText || "未提供评论摘要")}</p><div class="evidence-signal-line"><span class="muted">命中信号</span><span>${escapeHtml(evidence.signals.length > 0 ? evidence.signals.join(" · ") : evidence.signal ?? "未提供")}</span></div><div class="evidence-row-footer"><span class="muted">${escapeHtml(human.detail)}</span><div class="row-actions">${inspectButton}${actions}</div></div>${feedbackMarkup(evidence, feedback)}</div></div></article>`;
}

function renderEvidenceActionControls(evidence: Evidence, state: ManagementState): string {
  const feedback = state.evidenceFeedback[evidence.evidenceId];
  const disabled = feedback?.status === "busy" ? "disabled" : "";
  const blacklistDisabled = state.blacklistSettings?.enabled !== true;
  const button = (action: ReviewAction, label: string, className = "button button-quiet", requiresBlacklist = false) => `<button class="${className}" data-review-action="${action}" data-evidence-id="${escapeHtml(evidence.evidenceId)}" type="button" ${disabled || (requiresBlacklist && blacklistDisabled) ? "disabled" : ""}${requiresBlacklist && blacklistDisabled ? " title=\"请先在拉黑队列页面开启总开关\"" : ""}>${label}</button>`;
  return `${button("confirm", blacklistDisabled ? "确认官方拉黑（先开启总开关）" : "确认官方拉黑", "button button-primary", true)}${button("hide-only", "仅本地隐藏")}${button("revoke", "撤销本地隐藏", "button button-quiet")}<details class="more-menu"><summary>更多操作</summary><div class="more-menu-items">${button("exception", "加入例外", "button button-danger")}${button("positive-sample", "标记显著样例", "button button-ghost")}</div></details>`;
}

function feedbackMarkup(evidence: Evidence, feedback: ReviewFeedback | undefined): string {
  if (!feedback) return "";
  if (feedback.status === "busy") {
    return `<span class="evidence-feedback" data-state="processing" data-evidence-busy="${escapeHtml(evidence.evidenceId)}">处理中 · ${escapeHtml(reviewActionLabel(feedback.action))}</span>`;
  }
  return `<span class="evidence-feedback" data-state="${feedback.status === "success" ? "ready" : "error"}">${escapeHtml(feedback.message ?? (feedback.status === "success" ? "已记录" : "操作失败"))}</span>`;
}

function evidenceHumanState(evidence: Evidence, state: ManagementState): { label: string; detail: string; state: string } {
  const latest = (state.reviewActions.items ?? [])
    .filter((item) => item.evidenceId === evidence.evidenceId)
    .sort((left, right) => right.createdAt.localeCompare(left.createdAt))[0];
  if (latest) {
    const mapped = {
      keep: { label: "历史保留判定", detail: "历史记录中的保留判定，不会触发官方拉黑", state: "info" },
      confirm: { label: "已确认拉黑", detail: "人工已确认该 UID 进入官方拉黑流程", state: "ready" },
      revoke: { label: "已撤销", detail: "人工已撤销本地隐藏", state: "error" },
      "hide-only": { label: "仅隐藏", detail: "人工保留本地隐藏，不进入官方动作", state: "info" },
      exception: { label: "例外", detail: "人工已加入例外名单", state: "info" },
      "positive-sample": { label: "已标记样例", detail: "人工已沉淀为显著样例", state: "info" },
    } as Record<ReviewAction, { label: string; detail: string; state: string }>;
    return mapped[latest.action];
  }
  const uid = state.uids.items?.find((item) => item.uid === evidence.uid);
  if (!uid) return { label: "未复核", detail: "等待人工复核", state: "info" };
  if (uid.status === "queued") return { label: "未复核", detail: "本地隐藏已生效 · 官方拉黑已排队", state: "info" };
  if (uid.status === "blocked") return { label: "未复核", detail: "本地隐藏已生效 · 官方拉黑已完成", state: "ready" };
  return { label: "未复核", detail: "本地隐藏已生效 · 等待人工复核", state: "info" };
}

function formatEvidenceConfidence(value: number | undefined): string {
  if (value === undefined) return "未提供";
  const percent = value >= 0 && value <= 1 ? value * 100 : value;
  return `${Number.isInteger(percent) ? percent : percent.toFixed(1)}%`;
}

function renderEvidenceInspector(evidence: Evidence | undefined, state: ManagementState): string {
  if (!evidence) {
    return `<aside class="evidence-inspector evidence-inspector-empty" data-evidence-inspector><p class="eyebrow">详情检查器</p><h4>选择一条证据</h4><p class="muted">列表直接承担主要复核；选中后，这里显示完整评论、楼层上下文和模型证据。</p></aside>`;
  }
  const human = evidenceHumanState(evidence, state);
  const comments = evidence.comments.length > 0
    ? evidence.comments.map(renderEvidenceComment).join("")
    : `<li class="evidence-thread-item"><p>${escapeHtml(evidence.commentText || "未提供评论正文")}</p><span class="muted">未提供完整评论数组</span></li>`;
  return `<aside class="evidence-inspector" data-evidence-inspector><div class="evidence-inspector-heading"><div><p class="eyebrow">详情检查器</p><h4>${escapeHtml(evidence.uid)} · ${escapeHtml(evidence.nicknameSnapshot || "无昵称快照")}</h4></div><button class="button button-ghost" data-evidence-close="true" type="button">关闭详情</button></div><div class="evidence-inspector-status"><span class="status-pill" data-state="${evidence.result === "hit" ? "ready" : "processing"}">AI · ${evidence.result === "hit" ? "命中" : "不确定"} · ${formatEvidenceConfidence(evidence.confidence)}</span><span class="status-pill" data-state="${human.state}">人工 · ${escapeHtml(human.label)}</span></div><section class="evidence-inspector-section"><h5>完整评论与楼层上下文</h5><ol class="evidence-thread">${comments}</ol></section><section class="evidence-inspector-section"><h5>判定证据</h5><dl><div><dt>来源视频</dt><dd>${evidence.sourceVideo ? `<a href="${escapeAttribute(evidence.sourceVideo)}" target="_blank" rel="noreferrer">${escapeHtml(evidence.videoId || evidence.sourceVideo)}</a>` : escapeHtml(evidence.videoId || "未提供")}</dd></div><div><dt>评论链接</dt><dd>${evidence.commentUrl ? `<a href="${escapeAttribute(evidence.commentUrl)}" target="_blank" rel="noreferrer">打开评论原址</a>` : "未提供"}</dd></div><div><dt>命中信号</dt><dd>${escapeHtml(evidence.signals.length > 0 ? evidence.signals.join(" · ") : evidence.signal ?? "未提供")}</dd></div><div><dt>模型理由</dt><dd>${escapeHtml(evidence.modelReason ?? "未提供")}</dd></div><div><dt>模型版本</dt><dd class="mono">${escapeHtml(evidence.modelVersion ?? "未提供")}</dd></div><div><dt>样本版本</dt><dd class="mono">${escapeHtml(evidence.sampleVersion ?? "未使用")}</dd></div><div><dt>规则版本</dt><dd class="mono">${escapeHtml(evidence.ruleVersion ?? "未提供")}</dd></div><div><dt>判定时间</dt><dd>${escapeHtml(evidence.createdAt || "未提供")}</dd></div></dl></section><div class="action-row evidence-inspector-actions">${renderEvidenceActionControls(evidence, state)}</div>${feedbackMarkup(evidence, state.evidenceFeedback[evidence.evidenceId])}</aside>`;
}

function renderEvidenceInspectorContents(evidence: Evidence | undefined, state: ManagementState): string {
  const markup = renderEvidenceInspector(evidence, state);
  const openIndex = markup.indexOf(">") + 1;
  const closeIndex = markup.lastIndexOf("</aside>");
  return openIndex > 0 && closeIndex > openIndex ? markup.slice(openIndex, closeIndex) : markup;
}

function renderEvidenceComment(comment: TaskComment): string {
  const context = comment.context.length > 0
    ? `<div class="evidence-comment-context"><span>楼层上下文</span><p>${escapeHtml(comment.context.join("\n"))}</p></div>`
    : "";
  const source = comment.commentUrl
    ? `<a href="${escapeAttribute(comment.commentUrl)}" target="_blank" rel="noreferrer">打开评论原址</a>`
    : "评论链接未提供";
  return `<li class="evidence-thread-item" data-level="${escapeAttribute(comment.level)}"><div class="evidence-comment-meta"><strong>${escapeHtml(comment.nickname || "无昵称")}</strong><span class="mono">${escapeHtml(comment.uid || "未知 UID")}</span><span class="status-pill" data-state="info">${comment.parentId ? "楼中楼" : "根评论"}</span></div><p class="evidence-comment-full">${escapeHtml(comment.content || "未提供评论正文")}</p>${context}<small>${source}</small></li>`;
}

function renderReviewHistory(
  collection: Collection<ReviewRecord>,
  filter: string,
  feedbackByEvidence: Record<string, ReviewFeedback> = {},
): string {
  if (collection.items === null) {
    return collection.error
      ? renderState("error", "复核历史加载失败", collection.error)
      : renderState("loading", "正在载入复核历史", "");
  }
  const items = collection.items.filter((item) =>
    `${item.uid} ${item.evidenceId} ${item.action} ${item.actor ?? ""}`
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  if (items.length === 0) {
    return renderState(
      filter ? "filtered-empty" : "empty",
      filter ? "当前筛选没有匹配复核记录" : "还没有复核记录",
      filter ? "清除筛选或查看完整历史" : "对证据执行操作后会在这里留下历史",
    );
  }
  return `<div class="list">${items.map((item) => { const feedback = feedbackByEvidence[item.evidenceId]; const feedbackMarkup = feedback?.status === "success" ? `<span class="evidence-feedback" data-state="ready" data-evidence-feedback="${escapeHtml(item.evidenceId)}">${escapeHtml(feedback.message ?? "已移入复核历史")}</span>` : ""; return `<article class="list-row"><div class="row-main"><strong class="mono">${escapeHtml(item.uid)}</strong><span>${reviewActionLabel(item.action)} · ${escapeHtml(item.actor ?? "local-user")}</span><small>${escapeHtml(item.createdAt || "未提供")} · 证据 ${escapeHtml(item.evidenceId)}</small>${feedbackMarkup}</div><div class="row-actions"><span class="status-pill" data-state="info">${escapeHtml(item.previousStatus ?? "无")} → ${escapeHtml(item.nextStatus ?? "无")}</span></div></article>`; }).join("")}</div>`;
}

function renderSampleCollection(collection: Collection<SampleSet>, expanded: Record<string, boolean> = {}): string {
  if (collection.items === null) return collection.error ? renderState("error", "样本库加载失败", collection.error) : renderState("loading", "正在载入样本库", "");
  if (collection.items.length === 0) return renderState("empty", "还没有样本版本", "通过文本或文件导入后创建草稿");
  const current = collection.items.find((sample) => sample.isCurrent);
  const currentMarkup = current
    ? `<div class="sample-current" data-sample-current="${escapeHtml(current.sampleId)}"><strong>当前生效版本 · samples-v${current.version}</strong><span>${current.items.length} 条样本会进入后续 AI 分析</span></div>`
    : `<div class="sample-current sample-current-empty"><strong>当前没有已发布版本</strong><span>发布一个草稿后，才会成为后续 AI 分析的样本快照</span></div>`;
  const rows = collection.items.map((sample) => {
    const isOpen = expanded[sample.sampleId] === true;
    const preview = sample.items.slice(0, 2).map((item) => `<li><span class="status-pill" data-state="info">${sampleKindLabel(item.kind)}</span><span>${escapeHtml(item.text)}</span></li>`).join("");
    const previewMore = sample.items.length > 2 ? `<li class="muted">还有 ${sample.items.length - 2} 条，点击“查看全部样本”展开</li>` : "";
    const details = isOpen
      ? `<section class="sample-details" data-sample-detail-panel="${escapeHtml(sample.sampleId)}"><ol>${sample.items.map((item, index) => `<li class="sample-detail-item"><span class="sample-detail-index">${index + 1}</span><div><p>${escapeHtml(item.text)}</p><small>${sampleKindLabel(item.kind)} · ${sampleLabelLabel(item.label)} · 来源：${sampleSourceLabel(item.source)}</small></div></li>`).join("")}</ol></section>`
      : "";
    return `<article class="list-row sample-row" data-sample-row="${escapeHtml(sample.sampleId)}"><div class="row-main"><div class="sample-row-heading"><strong>samples-v${sample.version}</strong><span class="status-pill" data-state="${sampleStatusState(sample)}">${sampleStatusLabel(sample)}</span></div><span>${sampleSetKindLabel(sample.kind)} · ${sample.items.length} 条样本</span><small>创建于 ${escapeHtml(sample.createdAt || "未提供")} · 发布时间：${escapeHtml(sample.publishedAt || "未发布")}</small><small class="mono">${escapeHtml(sample.sampleId)}</small><ul class="sample-preview-list">${preview || `<li class="muted">暂无正文</li>`}${previewMore}</ul></div><div class="row-actions"><button class="button button-ghost" data-sample-details="${escapeHtml(sample.sampleId)}" type="button" aria-expanded="${isOpen ? "true" : "false"}">${isOpen ? "收起样本详情" : "查看全部样本"}</button>${sample.status === "draft" ? `<button class="button button-primary" data-publish-sample="${escapeHtml(sample.sampleId)}" type="button">发布版本</button>` : ""}</div>${details}</article>`;
  }).join("");
  return `${currentMarkup}<div class="list sample-list">${rows}</div>`;
}

function renderBlacklistCollection(collection: Collection<BlacklistItem>, filter: string): string {
  if (collection.items === null) return collection.error ? renderState("error", "拉黑队列加载失败", collection.error) : renderState("loading", "正在载入队列", "");
  const items = collection.items.filter((item) => `${item.uid} ${item.itemId}`.toLowerCase().includes(filter.toLowerCase()));
  if (items.length === 0) return renderState(filter ? "filtered-empty" : "empty", filter ? "当前筛选没有匹配队列项" : "拉黑队列为空", filter ? "清除筛选或回到完整集合" : "明确命中后才会生成官方拉黑任务");
  return `<div class="list">${items.map((item) => {
    const statusState = item.status === "completed"
      ? "ready"
      : item.status === "failed" || item.status === "blocked"
        ? "error"
        : item.status === "processing" ? "processing" : item.status;
    const action = item.status === "queued" || item.status === "processing"
      ? `<button class="button button-ghost" data-queue-action="pause" data-item-id="${escapeHtml(item.itemId)}" type="button">暂停</button>`
      : item.status === "paused"
        ? `<button class="button button-ghost" data-queue-action="resume" data-item-id="${escapeHtml(item.itemId)}" type="button">恢复</button>`
        : item.status === "failed" || item.status === "blocked"
          ? `<button class="button button-ghost" data-queue-action="retry" data-item-id="${escapeHtml(item.itemId)}" type="button">重试</button>`
          : "";
    return `<article class="list-row blacklist-row"><div class="row-main"><strong class="mono">${escapeHtml(item.uid)}</strong><span class="mono">${escapeHtml(item.itemId)}</span><small>${item.attempts} 次尝试</small>${renderBlacklistDiagnostic(item)}</div><div class="row-actions"><span class="status-pill" data-state="${statusState}">${blacklistStatusLabel(item.status)}</span>${action}</div></article>`;
  }).join("")}</div>`;
}

function renderSamplePreview(state: ManagementState): string {
  const items = mergeSampleItems(parseSampleText(state.sampleText, state.sampleKind), state.sampleFileItems);
  if (items.length === 0) return `<p class="muted">预览为空；创建前会再次去重。</p>`;
  return `<p class="preview-count">预览 ${items.length} 条，按“类型 + 文本”去重</p><ol>${items.slice(0, 8).map((item) => `<li><span class="status-pill" data-state="info">${sampleKindLabel(item.kind)}</span> ${escapeHtml(item.text)}</li>`).join("")}${items.length > 8 ? `<li class="muted">其余 ${items.length - 8} 条将在发布请求中一并提交</li>` : ""}</ol>`;
}

function renderFilter(name: ViewName, placeholder: string, value: string): string {
  return `<label class="filter-field"><span class="sr-only">${placeholder}</span><input data-filter="${name}" value="${escapeAttribute(value)}" placeholder="${placeholder}" /></label>`;
}

function profileLabel(profiles: FilterProfile[], profileId: string | undefined): string {
  if (!profileId) return "默认过滤策略";
  return profiles.find((profile) => profile.profileId === profileId)?.name ?? profileId;
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

function renderBlacklistDiagnostic(item: BlacklistItem): string {
  const hasFailure = item.status === "failed" || item.status === "paused" || item.status === "blocked";
  if (!hasFailure && !item.userMessage && !item.lastError) return "";
  const userMessage = item.userMessage ?? blacklistFallbackMessage(item.status);
  const recoveryAction = item.recoveryAction ?? blacklistFallbackRecovery(item.status);
  const category = item.errorCategory ?? "unknown";
  return `<div class="blacklist-diagnostic"><strong>${escapeHtml(userMessage)}</strong><p>${escapeHtml(recoveryAction)}</p><details class="blacklist-technical-details"><summary>查看技术详情</summary><dl><div><dt>错误类别</dt><dd>${escapeHtml(blacklistErrorCategoryLabel(category))} <span class="mono">(${escapeHtml(category)})</span></dd></div><div><dt>失败类型</dt><dd class="mono">${escapeHtml(item.failureType ?? "未提供")}</dd></div><div><dt>尝试次数</dt><dd>${escapeHtml(item.attempts)}</dd></div><div><dt>失败时间</dt><dd>${escapeHtml((item.errorAt ?? item.updatedAt) || "未提供")}</dd></div><div><dt>原始错误</dt><dd class="mono">${escapeHtml(item.lastError ?? "未提供")}</dd></div></dl></details></div>`;
}

function blacklistFallbackMessage(status: BlacklistItem["status"]): string {
  return status === "paused"
    ? "拉黑操作已暂停，请查看技术详情"
    : status === "blocked"
      ? "B 站平台拦截了拉黑操作，请查看技术详情"
      : "拉黑操作失败，请查看技术详情";
}

function blacklistFallbackRecovery(status: BlacklistItem["status"]): string {
  return status === "paused" ? "确认环境恢复后点击“恢复”" : "检查技术详情后点击“重试”";
}

function blacklistErrorCategoryLabel(category: string): string {
  return ({
    authentication: "登录态失效",
    captcha_or_risk: "验证码或风控",
    page_structure: "页面结构变化",
    platform_interception: "平台拦截",
    network: "临时网络",
    browser_environment: "浏览器环境",
    unknown: "未知错误",
  } as Record<string, string>)[category] ?? "未分类错误";
}

function sampleStatusLabel(sample: SampleSet): string {
  if (sample.isCurrent) return "当前生效";
  if (sample.status === "draft") return "草稿";
  return "历史版本";
}

function sampleStatusState(sample: SampleSet): string {
  if (sample.isCurrent) return "ready";
  if (sample.status === "draft") return "info";
  return "paused";
}

function sampleSetKindLabel(kind: SampleSet["kind"]): string {
  return ({ comment: "评论样本", nickname: "昵称样本", mixed: "评论 + 昵称" })[kind];
}

function sampleKindLabel(kind: SampleKind): string {
  return ({ "comment-positive": "评论正例", "comment-negative": "评论反例", "nickname-positive": "昵称正例" })[kind];
}

function sampleLabelLabel(label: string | undefined): string {
  return label === "negative" ? "反例" : label === "positive" ? "正例" : label || "未标注";
}

function sampleSourceLabel(source: SampleItem["source"]): string {
  return ({ manual: "手工导入", file: "文件导入", review: "复核样本" })[source ?? "manual"];
}

function reviewActionLabel(action: ReviewAction): string {
  return ({
    keep: "历史保留判定",
    confirm: "确认官方拉黑",
    revoke: "撤销隐藏",
    "hide-only": "仅保留隐藏",
    exception: "加入例外",
    "positive-sample": "标记显著样例",
  })[action];
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
