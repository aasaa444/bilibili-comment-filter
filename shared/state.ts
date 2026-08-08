import type { HealthResponse, TaskStatus } from "./types.js";

export type ConnectionState =
  | { kind: "loading"; label: string }
  | { kind: "ready"; label: string; detail?: string }
  | { kind: "offline"; label: string; detail?: string }
  | { kind: "error"; label: string; detail?: string }
  | { kind: "permission-denied"; label: string; detail?: string };

export type TaskViewState =
  | { kind: "queued"; label: string }
  | { kind: "processing"; label: string }
  | { kind: "ready"; label: string }
  | { kind: "paused"; label: string }
  | { kind: "error"; label: string };

export function connectionStateFromHealth(health: HealthResponse | null): ConnectionState {
  if (!health) return { kind: "error", label: "连接请求失败", detail: "没有收到本机服务的健康响应" };
  if (health.status === "ready") {
    return { kind: "ready", label: "后台服务已连接", detail: health.detail };
  }
  if (health.status === "unavailable") {
    return { kind: "offline", label: "后台服务未连接", detail: health.detail };
  }
  return { kind: "error", label: "后台服务未就绪", detail: health.detail ?? "服务或 Worker 尚未可用" };
}

export function connectionStateFromRequestError(error: { status?: number; message?: string }): ConnectionState {
  const detail = error.message ?? "请求没有完成";
  if (error.status === 401 || error.status === 403) {
    return { kind: "permission-denied", label: "需要本机或 B 站权限", detail };
  }
  if (error.status === 0 || error.status === 502 || error.status === 503 || error.status === 504) {
    return { kind: "offline", label: "后台服务未连接", detail };
  }
  return { kind: "error", label: "连接请求失败", detail };
}

export function taskViewState(status: TaskStatus): TaskViewState {
  switch (status) {
    case "queued":
      return { kind: "queued", label: "已排队" };
    case "processing":
      return { kind: "processing", label: "处理中" };
    case "ready":
    case "partial":
      return { kind: "ready", label: status === "partial" ? "部分完成" : "已完成" };
    case "paused":
      return { kind: "paused", label: "已暂停" };
    case "failed":
      return { kind: "error", label: "失败" };
  }
}
