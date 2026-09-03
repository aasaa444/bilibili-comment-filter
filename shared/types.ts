export type UidStatus =
  | "hidden"
  | "review"
  | "queued"
  | "blocked"
  | "exception"
  | "failed"
  | "paused";

export interface UidRecord {
  uid: string;
  nicknameSnapshot: string;
  status: UidStatus;
  hidden: boolean;
  updatedAt: string;
}

export interface HealthResponse {
  status: "ready" | "degraded" | "unavailable";
  service?: string;
  version?: string;
  detail?: string;
  model?: ModelHealth;
}

export interface ModelHealth {
  status: string;
  detail: string;
  baseUrlConfigured: boolean;
  modelConfigured: boolean;
  apiKeyConfigured: boolean;
}

export type AuthStatus = "valid" | "invalid" | "missing" | "verification_failed";

export interface AuthSession {
  status: AuthStatus;
  detail: string;
  checkedAt?: string;
  cookiePresent: boolean;
}

export interface UidSyncResponse {
  version: number;
  mode: "snapshot" | "delta";
  records?: UidRecord[];
  upserts?: UidRecord[];
  removals?: string[];
  baseVersion?: number;
}

export type TaskStatus = "queued" | "processing" | "ready" | "partial" | "failed" | "paused";

export interface VideoTask {
  taskId: string;
  bvid: string;
  videoUrl: string;
  title: string;
  status: TaskStatus;
  submittedAt: string;
  updatedAt?: string;
  progress?: number;
  phase?: string;
  collectedComments?: number;
  replyCount?: number;
  requestedPages?: number;
  pinnedComments?: number;
  declaredComments?: number;
  declaredReplies?: number;
  declaredTotal?: number;
  coverage?: number;
  failedItems?: string[];
  errorCode?: string;
  error?: string;
  profileId?: string;
}

export interface TaskComment {
  commentId: string;
  uid: string;
  nickname: string;
  content: string;
  videoId: string;
  commentUrl: string;
  rootId: string;
  parentId: string | null;
  level: string;
  createdAt: number | null;
  isPinned: boolean;
  context: string[];
}

export interface TaskEvent {
  eventId: number;
  taskId: string;
  attempt: number;
  phase: string;
  eventType: string;
  status: string;
  message: string;
  details: Record<string, unknown>;
  createdAt: string;
}

export interface AnalysisRun {
  analysisId: string;
  taskId: string;
  attempt: number;
  status: "running" | "completed" | "failed" | "unavailable" | "not_started" | string;
  model?: string;
  sampleVersion?: string;
  batchCount: number;
  accountCount: number;
  hitCount: number;
  uncertainCount: number;
  nonTargetCount: number;
  evidenceCount: number;
  errorCode?: string;
  errorMessage?: string;
  startedAt?: string;
  completedAt?: string;
}

export interface TaskAnalysis {
  latest: AnalysisRun | null;
  attempts: AnalysisRun[];
}

export interface Evidence {
  evidenceId: string;
  taskId: string;
  uid: string;
  nicknameSnapshot: string;
  result: "hit" | "uncertain";
  videoId: string;
  comments: TaskComment[];
  commentText: string;
  threadContext?: string;
  sourceVideo?: string;
  commentUrl?: string;
  signal?: string;
  signals: string[];
  modelReason?: string;
  confidence?: number;
  modelVersion?: string;
  sampleVersion?: string;
  ruleVersion?: string;
  createdAt: string;
  profileId?: string;
}

export type ReviewAction = "keep" | "confirm" | "revoke" | "hide-only" | "exception" | "positive-sample";

export interface ReviewRecord {
  reviewId: string;
  evidenceId: string;
  uid: string;
  action: ReviewAction;
  previousStatus: UidStatus | null;
  nextStatus: UidStatus | null;
  actor?: string;
  createdAt: string;
}

export type SampleKind = "comment-positive" | "comment-negative" | "nickname-positive";
export type SampleSetKind = "comment" | "nickname" | "mixed";

export interface SampleItem {
  text: string;
  kind: SampleKind;
  label?: string;
  source?: "manual" | "file" | "review";
}

export interface SampleSet {
  sampleId: string;
  kind: SampleSetKind;
  version: number;
  status: "draft" | "published" | "disabled";
  isCurrent: boolean;
  items: SampleItem[];
  createdAt: string;
  publishedAt?: string;
  profileId?: string;
}

export interface FilterProfile {
  profileId: string;
  name: string;
  description: string;
  status: string;
  knownTerms: string[];
  standaloneTerms: string[];
  friendlyExceptions: string[];
  hostileContext: string[];
  nicknamePositive: string[];
  createdAt: string;
  updatedAt: string;
  isCurrent: boolean;
}

export interface BlacklistItem {
  itemId: string;
  uid: string;
  status: "queued" | "processing" | "blocked" | "failed" | "completed" | "paused" | "cancelled";
  attempts: number;
  lastError?: string;
  errorCategory?: string;
  failureType?: string;
  userMessage?: string;
  recoveryAction?: string;
  errorAt?: string;
  updatedAt: string;
}

export interface BlacklistSettings {
  enabled: boolean;
  mode: "local_only" | "local_and_official_queue";
  updatedAt: string;
}

export interface ApiErrorEnvelope {
  detail?: string;
  message?: string;
  code?: string;
}

export interface CreateUidRequest {
  uid: string;
  nicknameSnapshot?: string;
  status?: UidStatus;
  hidden?: boolean;
}

export interface UpdateUidRequest {
  status?: UidStatus;
  hidden?: boolean;
}

export interface CreateTaskRequest {
  bvid: string;
  videoUrl: string;
}

export interface AuthCookie {
  name: string;
  value: string;
  domain: string;
  path: string;
  expirationDate?: number;
  secure?: boolean;
  httpOnly?: boolean;
  sameSite?: string;
}

export interface AuthSessionRequest {
  source: "extension";
  origin: string;
  cookies: AuthCookie[];
}
