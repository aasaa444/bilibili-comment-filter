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

export interface Evidence {
  evidenceId: string;
  uid: string;
  nicknameSnapshot: string;
  result: "hit" | "uncertain";
  commentText: string;
  threadContext?: string;
  sourceVideo?: string;
  commentUrl?: string;
  signal?: string;
  modelReason?: string;
  confidence?: number;
  modelVersion?: string;
  createdAt: string;
}

export type ReviewAction = "keep" | "revoke" | "hide-only" | "exception" | "positive-sample";

export interface ReviewRecord {
  reviewId: string;
  evidenceId: string;
  uid: string;
  action: ReviewAction;
  previousStatus: UidStatus | null;
  nextStatus: UidStatus | null;
  createdAt: string;
}

export type SampleKind = "comment-positive" | "comment-negative" | "nickname-positive";

export interface SampleItem {
  text: string;
  kind: SampleKind;
  source?: "manual" | "file" | "review";
}

export interface SampleSet {
  sampleId: string;
  version: number;
  status: "draft" | "published" | "disabled";
  items: SampleItem[];
  createdAt: string;
  publishedAt?: string;
}

export interface BlacklistItem {
  itemId: string;
  uid: string;
  status: "queued" | "processing" | "blocked" | "failed" | "completed" | "paused" | "cancelled";
  attempts: number;
  lastError?: string;
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
  title?: string;
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
