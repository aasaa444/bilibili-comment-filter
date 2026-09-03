import type { ConnectionState } from "../../shared/state.js";
import type { AuthSession, VideoTask } from "../../shared/types.js";
import type { PopupState, RuntimeMessage, RuntimeResponse } from "./messages.js";

const connectionElement = document.querySelector<HTMLElement>("#connection-status");
const connectionDetailElement = document.querySelector<HTMLElement>("#connection-detail");
const videoTitleElement = document.querySelector<HTMLElement>("#video-title");
const videoIdElement = document.querySelector<HTMLElement>("#video-id");
const unsupportedElement = document.querySelector<HTMLElement>("#unsupported");
const submitButton = document.querySelector<HTMLButtonElement>("#submit-video");
const syncAuthButton = document.querySelector<HTMLButtonElement>("#sync-auth-session");
const refreshButton = document.querySelector<HTMLButtonElement>("#refresh-state");
const cacheSummaryElement = document.querySelector<HTMLElement>("#cache-summary");
const taskElement = document.querySelector<HTMLElement>("#task-state");
const authStatusElement = document.querySelector<HTMLElement>("#auth-status");
const errorElement = document.querySelector<HTMLElement>("#popup-error");

let currentState: PopupState | null = null;
let currentAuthSession: AuthSession | null = null;

void loadState();
submitButton?.addEventListener("click", () => void submitCurrentVideo());
syncAuthButton?.addEventListener("click", () => void syncAuthSession());
refreshButton?.addEventListener("click", () => void loadState());

async function loadState(): Promise<void> {
  setBusy(true);
  clearError();
  const response = await sendMessage({ type: "GET_POPUP_STATE" });
  if (!response?.ok || !("popup" in response)) {
    renderConnection({ kind: "error", label: "连接请求失败", detail: responseErrorMessage(response) ?? "没有收到扩展响应" });
    renderUnsupported();
    setBusy(false);
    return;
  }
  currentState = response.popup;
  currentAuthSession = null;
  renderPopup(response.popup);
  setBusy(false);
}

async function syncAuthSession(): Promise<void> {
  if (!currentState || currentState.connection.kind !== "ready" || currentState.authSyncAvailable === false) return;
  setBusy(true);
  clearError();
  const response = await sendMessage({ type: "SYNC_AUTH_SESSION" });
  if (!response?.ok || !("auth" in response)) {
    currentAuthSession = null;
    renderAuthStatus(null, responseErrorMessage(response) ?? "登录态同步失败，当前状态已保留");
    showError(responseErrorMessage(response) ?? "登录态同步失败，当前状态已保留");
    setBusy(false);
    return;
  }
  currentAuthSession = response.auth;
  currentState = { ...currentState, connection: response.connection };
  renderPopup(currentState);
  setBusy(false);
}

async function submitCurrentVideo(): Promise<void> {
  if (!currentState?.video || currentState.connection.kind !== "ready") return;
  setBusy(true);
  clearError();
  const response = await sendMessage({ type: "SUBMIT_CURRENT_VIDEO", expectedBvid: currentState.video.bvid });
  if (!response?.ok || !("task" in response)) {
    showError(responseErrorMessage(response) ?? "任务提交失败，当前状态已保留");
    setBusy(false);
    return;
  }
  currentState = { ...currentState, task: response.task };
  renderPopup(currentState);
  setBusy(false);
}

function renderPopup(state: PopupState): void {
  renderConnection(state.connection);
  if (!state.video) {
    renderUnsupported();
  } else {
    unsupportedElement?.toggleAttribute("hidden", true);
    if (videoTitleElement) videoTitleElement.textContent = state.video.title || "当前视频未提供标题";
    if (videoIdElement) videoIdElement.textContent = state.video.bvid;
  }
  if (cacheSummaryElement) {
    cacheSummaryElement.textContent = state.cache.available
      ? `已同步 ${state.cache.count} 条 UID，版本 ${state.cache.version}`
      : "尚无可用 UID 缓存；服务断开时不会假装已同步";
  }
  renderTask(state.task);
  renderAuthStatus(currentAuthSession);
  if (submitButton) {
    submitButton.disabled = !state.video || state.connection.kind !== "ready";
  }
  if (syncAuthButton) {
    syncAuthButton.disabled = state.connection.kind !== "ready" || state.authSyncAvailable === false;
  }
}

function renderConnection(state: ConnectionState): void {
  connectionElement?.setAttribute("data-state", state.kind);
  if (connectionElement) connectionElement.textContent = state.label;
  if (connectionDetailElement) connectionDetailElement.textContent = ("detail" in state ? state.detail : undefined) ?? "";
}

function renderUnsupported(): void {
  unsupportedElement?.toggleAttribute("hidden", false);
  if (videoTitleElement) videoTitleElement.textContent = "当前页面不在首版支持范围";
  if (videoIdElement) videoIdElement.textContent = "仅支持已登录桌面端普通 BV 视频页";
  if (submitButton) submitButton.disabled = true;
  if (syncAuthButton) syncAuthButton.disabled = true;
}

function renderTask(task: VideoTask | undefined): void {
  if (!taskElement) return;
  if (!task) {
    taskElement.textContent = "还没有提交当前视频任务";
    taskElement.setAttribute("data-state", "empty");
    return;
  }
  taskElement.setAttribute("data-state", task.status);
  taskElement.textContent = `任务 ${task.taskId}：${task.status}${task.phase ? `，${task.phase}` : ""}`;
}

function setBusy(busy: boolean): void {
  if (refreshButton) refreshButton.disabled = busy;
  if (busy) {
    if (submitButton) submitButton.disabled = true;
    if (syncAuthButton) syncAuthButton.disabled = true;
    return;
  }
  if (currentState) {
    if (submitButton) submitButton.disabled = !currentState.video || currentState.connection.kind !== "ready";
    if (syncAuthButton) syncAuthButton.disabled = currentState.connection.kind !== "ready" || currentState.authSyncAvailable === false;
  }
}

function renderAuthStatus(auth: AuthSession | null, fallback?: string): void {
  if (!authStatusElement) return;
  if (!auth) {
    authStatusElement.setAttribute("data-state", "empty");
    authStatusElement.textContent = fallback ?? "尚未同步当前 B 站登录态";
    return;
  }
  const label = auth.status === "valid"
    ? "登录态有效"
    : auth.status === "missing"
      ? "尚未读取到登录态"
      : auth.status === "invalid"
        ? "登录态已失效"
        : "登录态验证失败";
  authStatusElement.setAttribute("data-state", auth.status === "valid" ? "ready" : auth.status === "missing" ? "paused" : "error");
  authStatusElement.textContent = `${label}：${auth.detail}`;
}

function showError(message: string): void {
  if (!errorElement) return;
  errorElement.hidden = false;
  errorElement.textContent = message;
}

function clearError(): void {
  if (!errorElement) return;
  errorElement.hidden = true;
  errorElement.textContent = "";
}

function sendMessage(message: RuntimeMessage): Promise<RuntimeResponse | undefined> {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response: RuntimeResponse | undefined) => resolve(response));
  });
}

function responseErrorMessage(response: RuntimeResponse | undefined): string | undefined {
  return response && !response.ok ? response.error.message : undefined;
}
