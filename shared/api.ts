import type {
  ApiErrorEnvelope,
  AuthSession,
  AuthSessionRequest,
  BlacklistItem,
  BlacklistSettings,
  CreateTaskRequest,
  CreateUidRequest,
  Evidence,
  HealthResponse,
  SampleSetKind,
  ReviewRecord,
  SampleSet,
  TaskComment,
  TaskStatus,
  UidRecord,
  UidSyncResponse,
  UpdateUidRequest,
  VideoTask,
} from "./types.js";

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8765";

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

export interface ApiList<T> {
  items: T[];
  total?: number;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;

  constructor(baseUrl = DEFAULT_API_BASE_URL, fetchImpl: FetchLike = (input, init) => fetch(input, init)) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.fetchImpl = fetchImpl;
  }

  getHealth(): Promise<HealthResponse> {
    return this.request<unknown>("/api/health").then(normalizeHealth);
  }

  getAuthSession(): Promise<AuthSession> {
    return this.request<unknown>("/api/auth/session").then(normalizeAuthSession);
  }

  listUids(): Promise<ApiList<UidRecord>> {
    return this.requestList("/api/uids", "uids", normalizeUidRecord);
  }

  createUid(payload: CreateUidRequest): Promise<UidRecord> {
    return this.request<unknown>("/api/uids", { method: "POST", body: toCreateUidPayload(payload) }).then(requireUidRecord);
  }

  updateUid(uid: string, payload: UpdateUidRequest): Promise<UidRecord> {
    return this.request<unknown>(`/api/uids/${encodeURIComponent(uid)}`, {
      method: "PATCH",
      body: toUpdateUidPayload(payload),
    }).then(requireUidRecord);
  }

  removeUid(uid: string): Promise<void> {
    return this.request<unknown>(`/api/uids/${encodeURIComponent(uid)}`, {
      method: "DELETE",
    }).then(() => undefined);
  }
  async syncUids(since?: number): Promise<UidSyncResponse> {
    const query = since === undefined ? "" : `?since=${encodeURIComponent(String(since))}`;
    return normalizeUidSync(await this.request<unknown>(`/api/uids/sync${query}`));
  }

  saveAuthSession(payload: AuthSessionRequest): Promise<unknown> {
    return this.request<unknown>("/api/auth/session", { method: "POST", body: toAuthSessionPayload(payload) });
  }

  listTasks(status?: TaskStatus): Promise<ApiList<VideoTask>> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.requestList(`/api/tasks${query}`, "tasks", normalizeVideoTask);
  }

  createTask(payload: CreateTaskRequest): Promise<VideoTask> {
    return this.request<unknown>("/api/tasks", { method: "POST", body: toCreateTaskPayload(payload) }).then(requireVideoTask);
  }

  getTask(taskId: string): Promise<VideoTask> {
    return this.request<unknown>(`/api/tasks/${encodeURIComponent(taskId)}`).then(requireVideoTask);
  }

  listTaskComments(taskId: string): Promise<ApiList<TaskComment>> {
    return this.requestList(`/api/tasks/${encodeURIComponent(taskId)}/comments`, "comments", normalizeTaskComment);
  }

  retryTask(taskId: string): Promise<VideoTask> {
    return this.request<unknown>(`/api/tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST" }).then(requireVideoTask);
  }

  listEvidence(params: {
    taskId?: string;
    uidStatus?: string;
    result?: string;
    reviewStatus?: "pending" | "history" | "all";
  } = {}): Promise<ApiList<Evidence>> {
    const query = new URLSearchParams();
    if (params.taskId) query.set("task_id", params.taskId);
    if (params.uidStatus) query.set("uid_status", params.uidStatus);
    if (params.result) query.set("result", params.result);
    if (params.reviewStatus) query.set("review_status", params.reviewStatus);
    return this.requestList(`/api/reviews${query.size ? `?${query}` : ""}`, "evidence", normalizeEvidence);
  }
  listReviewActions(params: { evidenceId?: string; uid?: string } = {}): Promise<ApiList<ReviewRecord>> {
    const query = new URLSearchParams();
    if (params.evidenceId) query.set("evidence_id", params.evidenceId);
    if (params.uid) query.set("uid", params.uid);
    return this.requestList(
      `/api/review-actions${query.size ? `?${query}` : ""}`,
      "review_actions",
      normalizeReviewRecord,
    );
  }

  reviewEvidence(evidenceId: string, action: string): Promise<ReviewRecord> {
    return this.request<unknown>(`/api/reviews/${encodeURIComponent(evidenceId)}`, {
      method: "POST",
      body: { action: action === "hide-only" ? "hide_only" : action === "positive-sample" ? "highlight" : action },
    }).then(requireReviewRecord);
  }

  listSamples(): Promise<ApiList<SampleSet>> {
    return this.requestList("/api/samples", "samples", normalizeSampleSet);
  }

  createSample(payload: { items: unknown[] }): Promise<SampleSet> {
    const items = payload.items.flatMap((item) => {
      const candidate = asRecord(item);
      const text = stringValue(candidate.text) ?? stringValue(candidate.content);
      if (!text) return [];
      const kind = stringValue(candidate.kind) ?? "comment-positive";
      return [{
        content: text,
        kind: kind === "nickname-positive" ? "nickname" : "comment",
        label: stringValue(candidate.label) ?? (kind === "comment-negative" ? "negative" : "positive"),
        source: candidate.source === "file" || candidate.source === "review" ? candidate.source : "manual",
      }];
    });
    const first = asRecord(payload.items[0]);
    const kind = stringValue(first.kind) ?? "comment-positive";
    return this.request<unknown>("/api/samples", {
      method: "POST",
      body: {
        kind: kind === "nickname-positive" ? "nickname" : "comment",
        label: kind === "comment-negative" ? "negative" : "positive",
        items,
      },
    }).then(requireSampleSet);
  }

  publishSample(sampleId: string): Promise<SampleSet> {
    return this.request<unknown>(`/api/samples/${encodeURIComponent(sampleId)}/publish`, { method: "POST" }).then(requireSampleSet);
  }

  listBlacklist(): Promise<ApiList<BlacklistItem>> {
    return this.requestList("/api/blacklist", "blacklist", normalizeBlacklistItem);
  }

  pauseBlacklist(itemId: string): Promise<BlacklistItem> {
    return this.request<unknown>(`/api/blacklist/${encodeURIComponent(itemId)}/pause`, { method: "POST" }).then(requireBlacklistItem);
  }

  resumeBlacklist(itemId: string): Promise<BlacklistItem> {
    return this.request<unknown>(`/api/blacklist/${encodeURIComponent(itemId)}/resume`, { method: "POST" }).then(requireBlacklistItem);
  }

  retryBlacklist(itemId: string): Promise<BlacklistItem> {
    return this.request<unknown>(`/api/blacklist/${encodeURIComponent(itemId)}/retry`, { method: "POST" }).then(requireBlacklistItem);
  }

  getBlacklistSettings(): Promise<BlacklistSettings> {
    return this.request<unknown>("/api/blacklist/settings").then(requireBlacklistSettings);
  }

  updateBlacklistSettings(enabled: boolean): Promise<BlacklistSettings> {
    return this.request<unknown>("/api/blacklist/settings", {
      method: "PATCH",
      body: { enabled },
    }).then(requireBlacklistSettings);
  }
  private async request<T>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: options.method ?? "GET",
        headers: options.body === undefined ? undefined : { "content-type": "application/json" },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
    } catch (error) {
      throw new ApiRequestError(0, error instanceof Error ? error.message : "无法连接本机服务");
    }

    const payload: unknown = await readResponsePayload(response);

    if (!response.ok) {
      const envelope = asApiErrorEnvelope(payload);
      throw new ApiRequestError(
        response.status,
        envelope.detail ?? envelope.message ?? `本机服务返回 HTTP ${response.status}`,
        envelope.code,
      );
    }
    return payload as T;
  }

  private async requestList<T>(path: string, arrayKey: string, normalize?: (value: unknown) => T | null): Promise<ApiList<T>> {
    const payload = await this.request<unknown>(path);
    if (Array.isArray(payload)) return { items: normalizeItems(payload, normalize) };
    if (payload && typeof payload === "object") {
      const candidate = payload as Record<string, unknown>;
      const items = candidate.items ?? candidate[arrayKey] ?? candidate.records ?? candidate.uids ?? candidate.tasks ?? candidate.evidence ?? candidate.samples ?? candidate.blacklist;
      if (Array.isArray(items)) {
        return {
          items: normalizeItems(items, normalize),
          total: typeof candidate.total === "number" ? candidate.total : undefined,
        };
      }
    }
    return { items: [] };
  }
}

function normalizeItems<T>(items: unknown[], normalize?: (value: unknown) => T | null): T[] {
  if (!normalize) return items as T[];
  return items.map(normalize).filter((item): item is T => item !== null);
}

function normalizeHealth(value: unknown): HealthResponse {
  const candidate = asRecord(value);
  const rawStatus = stringValue(candidate.status);
  const status = rawStatus === "ready" || rawStatus === "ok" || rawStatus === "healthy"
    ? "ready"
    : rawStatus === "degraded"
      ? "degraded"
      : "unavailable";
  return {
    status,
    service: stringValue(candidate.service),
    version: stringValue(candidate.version),
    detail: stringValue(candidate.detail) ?? stringValue(candidate.message),
  };
}

function normalizeAuthSession(value: unknown): AuthSession {
  const candidate = asRecord(value);
  const status = stringValue(candidate.status);
  if (!isAuthStatus(status)) throw new ApiRequestError(502, "认证状态响应格式无效");
  return {
    status,
    detail: stringValue(candidate.detail) ?? "未提供认证诊断信息",
    checkedAt: stringValue(candidate.checked_at) ?? stringValue(candidate.checkedAt),
    cookiePresent: booleanValue(candidate.cookie_present) ?? booleanValue(candidate.cookiePresent) ?? false,
  };
}

function normalizeUidRecord(value: unknown): UidRecord | null {
  const candidate = asRecord(value);
  const uid = stringValue(candidate.uid);
  const status = stringValue(candidate.status) ?? stringValue(candidate.state);
  if (!uid || !isUidStatus(status)) return null;
  return {
    uid,
    nicknameSnapshot: stringValue(candidate.nickname) ?? stringValue(candidate.nickname_snapshot) ?? stringValue(candidate.nicknameSnapshot) ?? "",
    status,
    hidden: booleanValue(candidate.hidden) ?? booleanValue(candidate.local_hidden) ?? status !== "exception",
    updatedAt: stringValue(candidate.updated_at) ?? stringValue(candidate.updatedAt) ?? "",
  };
}

function normalizeUidSync(value: unknown): UidSyncResponse {
  const candidate = asRecord(value);
  const version = numberValue(candidate.version) ?? numericString(candidate.version);
  if (version === null) throw new ApiRequestError(502, "UID 同步响应缺少有效版本");
  const mode = candidate.mode === "delta" ? "delta" : "snapshot";
  const records = normalizeItems(arrayValue(candidate.records) ?? arrayValue(candidate.uids) ?? arrayValue(candidate.items) ?? [], normalizeUidRecord);
  const upserts = normalizeItems(
    arrayValue(candidate.upserts) ?? (mode === "delta" ? arrayValue(candidate.items) : null) ?? [],
    normalizeUidRecord,
  );
  const removals = (
    arrayValue(candidate.removals) ?? arrayValue(candidate.removed) ?? []
  ).filter((uid): uid is string => typeof uid === "string");
  return {
    version,
    mode,
    records: mode === "snapshot" ? records : undefined,
    upserts: mode === "delta" ? upserts : undefined,
    removals: mode === "delta" ? removals : undefined,
    baseVersion: numberValue(candidate.base_version) ?? numberValue(candidate.baseVersion) ?? undefined,
  };
}

function normalizeVideoTask(value: unknown): VideoTask | null {
  const candidate = asRecord(value);
  const taskId = stringValue(candidate.task_id) ?? stringValue(candidate.taskId) ?? stringValue(candidate.id);
  const videoUrl = stringValue(candidate.video_url) ?? stringValue(candidate.videoUrl) ?? "";
  const bvid = stringValue(candidate.bvid) ?? stringValue(candidate.video_id) ?? extractBvid(videoUrl);
  const status = normalizeTaskStatus(candidate.status);
  if (!taskId || !bvid || !isTaskStatus(status)) return null;
  const progressObject = asRecord(candidate.progress);
  const progressValue = numberValue(candidate.progress) ?? numberValue(progressObject.coverage);
  const failedItems = arrayValue(candidate.failed_items) ?? arrayValue(progressObject.failed_items);
  return {
    taskId,
    bvid,
    videoUrl,
    title: stringValue(candidate.title) ?? "",
    status,
    submittedAt: stringValue(candidate.submitted_at) ?? stringValue(candidate.submittedAt) ?? "",
    updatedAt: stringValue(candidate.updated_at) ?? stringValue(candidate.updatedAt),
    progress: progressValue === null ? undefined : Math.round(progressValue * 100),
    phase: stringValue(candidate.phase) ?? (status === "processing" ? "处理中" : undefined),
    collectedComments: numberValue(candidate.collected_comments) ?? numberValue(candidate.collectedComments) ?? numberValue(progressObject.saved_comments) ?? undefined,
    replyCount: numberValue(candidate.reply_count) ?? numberValue(candidate.replyCount) ?? numberValue(progressObject.saved_replies) ?? undefined,
    requestedPages: numberValue(candidate.requested_pages) ?? numberValue(candidate.requestedPages) ?? numberValue(progressObject.requested_pages) ?? undefined,
    pinnedComments: numberValue(candidate.pinned_comments) ?? numberValue(candidate.pinnedComments) ?? numberValue(progressObject.pinned_comments) ?? undefined,
    declaredComments: numberValue(candidate.declared_comments) ?? numberValue(candidate.declaredComments) ?? numberValue(progressObject.declared_comments) ?? undefined,
    declaredReplies: numberValue(candidate.declared_replies) ?? numberValue(candidate.declaredReplies) ?? numberValue(progressObject.declared_replies) ?? undefined,
    declaredTotal: numberValue(candidate.declared_total) ?? numberValue(candidate.declaredTotal) ?? numberValue(progressObject.declared_total) ?? numberValue(progressObject.declaredTotal) ?? undefined,
    coverage: numberValue(candidate.coverage) ?? numberValue(progressObject.coverage) ?? undefined,
    failedItems: failedItems === null ? undefined : stringArray(failedItems),
    errorCode: stringValue(candidate.error_code) ?? stringValue(candidate.errorCode),
    error: stringValue(candidate.error) ?? stringValue(candidate.error_message),
  };
}

function normalizeTaskComment(value: unknown): TaskComment | null {
  const candidate = asRecord(value);
  const commentId = stringValue(candidate.comment_id) ?? stringValue(candidate.commentId) ?? stringValue(candidate.id);
  const uid = stringValue(candidate.uid);
  const rootId = stringValue(candidate.root_id) ?? stringValue(candidate.rootId) ?? commentId;
  const parentId = stringValue(candidate.parent_id) ?? stringValue(candidate.parentId) ?? null;
  if (!commentId || !uid || !rootId) return null;
  return {
    commentId,
    uid,
    nickname: stringValue(candidate.nickname) ?? stringValue(candidate.nickname_snapshot) ?? stringValue(candidate.nicknameSnapshot) ?? "",
    content: stringValue(candidate.content) ?? stringValue(candidate.comment_text) ?? "",
    videoId: stringValue(candidate.video_id) ?? stringValue(candidate.videoId) ?? "",
    commentUrl: stringValue(candidate.comment_url) ?? stringValue(candidate.commentUrl) ?? "",
    rootId,
    parentId,
    level: stringValue(candidate.level) ?? (parentId ? "reply" : "root"),
    createdAt: numberValue(candidate.created_at) ?? numberValue(candidate.createdAt),
    isPinned: booleanValue(candidate.is_pinned) ?? booleanValue(candidate.isPinned) ?? false,
    context: stringArray(candidate.context),
  };
}

function normalizeEvidence(value: unknown): Evidence | null {
  const candidate = asRecord(value);
  const evidenceId = stringValue(candidate.evidence_id) ?? stringValue(candidate.evidenceId) ?? stringValue(candidate.id);
  const uid = stringValue(candidate.uid);
  const rawResult = stringValue(candidate.result) ?? stringValue(candidate.decision);
  const result = rawResult === "hit" || rawResult === "uncertain" ? rawResult : null;
  if (!evidenceId || !uid || !result) return null;
  const comments = (arrayValue(candidate.comments) ?? [])
    .map(normalizeEvidenceComment)
    .filter((item): item is TaskComment => item !== null);
  const rawComments = arrayValue(candidate.comments) ?? [];
  const firstComment = rawComments.length > 0 ? asRecord(rawComments[0]) : {};
  const firstNormalizedComment = comments[0];
  const signals = stringArray(candidate.signals);
  return {
    evidenceId,
    taskId: stringValue(candidate.task_id) ?? stringValue(candidate.taskId) ?? "",
    uid,
    nicknameSnapshot: stringValue(candidate.nickname_snapshot) ?? stringValue(candidate.nicknameSnapshot) ?? stringValue(candidate.nickname) ?? "",
    result,
    videoId: stringValue(candidate.video_id) ?? stringValue(candidate.videoId) ?? firstNormalizedComment?.videoId ?? "",
    comments,
    commentText: stringValue(candidate.comment_text) ?? stringValue(candidate.commentText) ?? firstNormalizedComment?.content ?? stringValue(firstComment.content) ?? "",
    threadContext: stringValue(candidate.thread_context) ?? stringValue(candidate.threadContext) ?? (firstNormalizedComment?.context.join("\n") || stringArray(firstComment.context).join("\n") || undefined),
    sourceVideo: stringValue(candidate.source_video) ?? stringValue(candidate.sourceVideo) ?? (stringValue(candidate.video_id) ? `https://www.bilibili.com/video/${candidate.video_id}` : undefined),
    commentUrl: stringValue(candidate.comment_url) ?? stringValue(candidate.commentUrl) ?? firstNormalizedComment?.commentUrl ?? stringValue(firstComment.comment_url),
    signal: stringValue(candidate.signal) ?? (signals.join(", ") || undefined),
    signals,
    modelReason: stringValue(candidate.model_reason) ?? stringValue(candidate.modelReason) ?? stringValue(candidate.reason),
    confidence: numberValue(candidate.confidence) ?? undefined,
    modelVersion: stringValue(candidate.model_version) ?? stringValue(candidate.modelVersion),
    sampleVersion: stringValue(candidate.sample_version) ?? stringValue(candidate.sampleVersion),
    ruleVersion: stringValue(candidate.rule_version) ?? stringValue(candidate.ruleVersion),
    createdAt: stringValue(candidate.created_at) ?? stringValue(candidate.createdAt) ?? "",
  };
}

function normalizeEvidenceComment(value: unknown): TaskComment | null {
  const candidate = asRecord(value);
  const commentId = stringValue(candidate.comment_id) ?? stringValue(candidate.commentId) ?? stringValue(candidate.id);
  if (!commentId) return null;
  const parentId = stringValue(candidate.parent_id) ?? stringValue(candidate.parentId) ?? null;
  return {
    commentId,
    uid: stringValue(candidate.uid) ?? "",
    nickname: stringValue(candidate.nickname) ?? stringValue(candidate.nickname_snapshot) ?? stringValue(candidate.nicknameSnapshot) ?? "",
    content: stringValue(candidate.content) ?? stringValue(candidate.comment_text) ?? "",
    videoId: stringValue(candidate.video_id) ?? stringValue(candidate.videoId) ?? "",
    commentUrl: stringValue(candidate.comment_url) ?? stringValue(candidate.commentUrl) ?? "",
    rootId: stringValue(candidate.root_id) ?? stringValue(candidate.rootId) ?? commentId,
    parentId,
    level: stringValue(candidate.level) ?? (parentId ? "reply" : "root"),
    createdAt: numberValue(candidate.created_at) ?? numberValue(candidate.createdAt),
    isPinned: booleanValue(candidate.is_pinned) ?? booleanValue(candidate.isPinned) ?? false,
    context: stringArray(candidate.context),
  };
}
function normalizeReviewRecord(value: unknown): ReviewRecord | null {
  const candidate = asRecord(value);
  const reviewId = stringValue(candidate.review_id) ?? stringValue(candidate.reviewId) ?? stringValue(candidate.action_id) ?? stringValue(candidate.id);
  const evidenceId = stringValue(candidate.evidence_id) ?? stringValue(candidate.evidenceId);
  const uid = stringValue(candidate.uid);
  const rawAction = stringValue(candidate.action);
  const action = rawAction === "hide_only"
    ? "hide-only"
    : rawAction === "highlight"
      ? "positive-sample"
      : rawAction;
  if (!reviewId || !evidenceId || !uid || !isReviewAction(action)) return null;
  return {
    reviewId,
    evidenceId,
    uid,
    action,
    previousStatus: isUidStatus(candidate.previous_status) ? candidate.previous_status : isUidStatus(candidate.before_state) ? candidate.before_state : null,
    nextStatus: isUidStatus(candidate.next_status) ? candidate.next_status : isUidStatus(candidate.after_state) ? candidate.after_state : null,
    actor: stringValue(candidate.actor),
    createdAt: stringValue(candidate.created_at) ?? stringValue(candidate.createdAt) ?? "",
  };
}

function normalizeSampleSet(value: unknown): SampleSet | null {
  const candidate = asRecord(value);
  const sampleId = stringValue(candidate.sample_id) ?? stringValue(candidate.sampleId) ?? stringValue(candidate.id);
  const version = numberValue(candidate.version) ?? numericString(candidate.version);
  const status = stringValue(candidate.status);
  if (!sampleId || version === null || !isSampleSetStatus(status)) return null;
  const rawItems = arrayValue(candidate.items) ?? [];
  const items = rawItems.flatMap((item) => {
    const normalized = normalizeSampleItem(item, stringValue(candidate.kind));
    return normalized ? [normalized] : [];
  });
  const rawKind = stringValue(candidate.kind);
  return {
    sampleId,
    kind: isSampleSetKind(rawKind) ? rawKind : inferSampleSetKind(items),
    version,
    status,
    isCurrent: booleanValue(candidate.is_current) ?? booleanValue(candidate.isCurrent) ?? status === "published",
    items,
    createdAt: stringValue(candidate.created_at) ?? stringValue(candidate.createdAt) ?? "",
    publishedAt: stringValue(candidate.published_at) ?? stringValue(candidate.publishedAt),
  };
}

function normalizeSampleItem(value: unknown, sampleKind?: string): SampleSet["items"][number] | null {
  const candidate = asRecord(value);
  const text = stringValue(candidate.text) ?? stringValue(candidate.content);
  const label = stringValue(candidate.label);
  const kind = normalizeSampleItemKind(stringValue(candidate.kind), sampleKind, label);
  if (!text || !isSampleKind(kind)) return null;
  return {
    text,
    kind,
    ...(label ? { label } : {}),
    source: candidate.source === "file" || candidate.source === "review" ? candidate.source : "manual",
  };
}

function normalizeSampleItemKind(value: string | undefined, sampleKind?: string, label?: string): SampleSet["items"][number]["kind"] {
  if (value === "nickname" || value === "nickname-positive") return "nickname-positive";
  if (value === "comment-negative") return "comment-negative";
  if (value === "comment" || value === "comment-positive") return label === "negative" ? "comment-negative" : "comment-positive";
  return sampleKind === "nickname" ? "nickname-positive" : label === "negative" ? "comment-negative" : "comment-positive";
}

function inferSampleSetKind(items: SampleSet["items"]): SampleSetKind {
  const hasNickname = items.some((item) => item.kind === "nickname-positive");
  const hasComment = items.some((item) => item.kind !== "nickname-positive");
  return hasNickname && hasComment ? "mixed" : hasNickname ? "nickname" : "comment";
}

function normalizeBlacklistItem(value: unknown): BlacklistItem | null {
  const candidate = asRecord(value);
  const itemId = stringValue(candidate.item_id) ?? stringValue(candidate.itemId) ?? stringValue(candidate.id);
  const uid = stringValue(candidate.uid);
  const status = stringValue(candidate.status);
  if (!itemId || !uid || !isBlacklistStatus(status)) return null;
  return {
    itemId,
    uid,
    status,
    attempts: numberValue(candidate.attempts) ?? 0,
    lastError: stringValue(candidate.last_error) ?? stringValue(candidate.lastError),
    errorCategory: stringValue(candidate.error_category) ?? stringValue(candidate.errorCategory),
    failureType: stringValue(candidate.failure_type) ?? stringValue(candidate.failureType),
    userMessage: stringValue(candidate.user_message) ?? stringValue(candidate.userMessage),
    recoveryAction: stringValue(candidate.recovery_action) ?? stringValue(candidate.recoveryAction),
    errorAt: stringValue(candidate.error_at) ?? stringValue(candidate.errorAt),
    updatedAt: stringValue(candidate.updated_at) ?? stringValue(candidate.updatedAt) ?? "",
  };
}

function normalizeBlacklistSettings(value: unknown): BlacklistSettings | null {
  const candidate = asRecord(value);
  const enabled = booleanValue(candidate.enabled);
  const mode = candidate.mode === "local_and_official_queue" || candidate.mode === "local_only"
    ? candidate.mode
    : null;
  if (enabled === null || mode === null) return null;
  return {
    enabled,
    mode,
    updatedAt: stringValue(candidate.updated_at) ?? stringValue(candidate.updatedAt) ?? "",
  };
}
function requireNormalized<T>(value: T | null, label: string): T {
  if (value === null) throw new ApiRequestError(502, `${label}响应格式无效`);
  return value;
}

const requireUidRecord = (value: unknown): UidRecord => requireNormalized(normalizeUidRecord(value), "UID");
const requireVideoTask = (value: unknown): VideoTask => requireNormalized(normalizeVideoTask(value), "任务");
const requireReviewRecord = (value: unknown): ReviewRecord => requireNormalized(normalizeReviewRecord(value), "复核");
const requireSampleSet = (value: unknown): SampleSet => requireNormalized(normalizeSampleSet(value), "样本");
const requireBlacklistItem = (value: unknown): BlacklistItem => requireNormalized(normalizeBlacklistItem(value), "队列");
const requireBlacklistSettings = (value: unknown): BlacklistSettings => requireNormalized(normalizeBlacklistSettings(value), "Blacklist settings");

function toCreateUidPayload(payload: CreateUidRequest): Record<string, unknown> {
  return {
    uid: payload.uid,
    nickname: payload.nicknameSnapshot,
    state: payload.status,
  };
}

function toUpdateUidPayload(payload: UpdateUidRequest): Record<string, unknown> {
  return { state: payload.status ?? (payload.hidden === false ? "exception" : payload.hidden === true ? "hidden" : undefined) };
}

function toCreateTaskPayload(payload: CreateTaskRequest): Record<string, unknown> {
  return {
    video_url: payload.videoUrl || `https://www.bilibili.com/video/${payload.bvid}`,
    title: payload.title,
  };
}

function toAuthSessionPayload(payload: AuthSessionRequest): Record<string, unknown> {
  const cookies = Object.fromEntries(payload.cookies.map((cookie) => [cookie.name, cookie.value]));
  return {
    source: payload.source,
    origin: payload.origin,
    cookies,
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function numericString(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const match = value.match(/(\d+)$/);
  if (!match) return null;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) ? parsed : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function arrayValue(value: unknown): unknown[] | null {
  return Array.isArray(value) ? value : null;
}

function isUidStatus(value: unknown): value is UidRecord["status"] {
  return ["hidden", "review", "queued", "blocked", "exception", "failed", "paused"].includes(value as string);
}

function isAuthStatus(value: unknown): value is AuthSession["status"] {
  return ["valid", "invalid", "missing", "verification_failed"].includes(value as string);
}

function isTaskStatus(value: unknown): value is TaskStatus {
  return ["queued", "processing", "ready", "partial", "failed", "paused"].includes(value as string);
}

function normalizeTaskStatus(value: unknown): TaskStatus | undefined {
  if (value === "collecting" || value === "analyzing") return "processing";
  if (value === "completed") return "ready";
  return isTaskStatus(value) ? value : undefined;
}

function isReviewAction(value: unknown): value is ReviewRecord["action"] {
  return ["keep", "confirm", "revoke", "hide-only", "exception", "positive-sample"].includes(value as string);
}

function isSampleKind(value: unknown): value is SampleSet["items"][number]["kind"] {
  return ["comment-positive", "comment-negative", "nickname-positive"].includes(value as string);
}

function isSampleSetKind(value: unknown): value is SampleSetKind {
  return ["comment", "nickname", "mixed"].includes(value as string);
}

function isSampleSetStatus(value: unknown): value is SampleSet["status"] {
  return ["draft", "published", "disabled"].includes(value as string);
}

function isBlacklistStatus(value: unknown): value is BlacklistItem["status"] {
  return ["queued", "processing", "blocked", "failed", "completed", "paused", "cancelled"].includes(value as string);
}

function extractBvid(value: string): string {
  return value.match(/\/video\/(BV[0-9A-Za-z]{10})(?:[/?#]|$)/i)?.[1] ?? "";
}

async function readResponsePayload(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { detail: text } satisfies ApiErrorEnvelope;
  }
}

function asApiErrorEnvelope(payload: unknown): ApiErrorEnvelope {
  if (!payload || typeof payload !== "object") return {};
  const candidate = payload as ApiErrorEnvelope;
  return {
    detail: typeof candidate.detail === "string" ? candidate.detail : undefined,
    message: typeof candidate.message === "string" ? candidate.message : undefined,
    code: typeof candidate.code === "string" ? candidate.code : undefined,
  };
}
