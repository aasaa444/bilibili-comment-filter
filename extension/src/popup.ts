import type { ConnectionState } from "../../shared/state.js";
import type { VideoTask } from "../../shared/types.js";
import type { PopupState, RuntimeMessage, RuntimeResponse } from "./messages.js";

const connectionElement = document.querySelector<HTMLElement>("#connection-status");
const connectionDetailElement = document.querySelector<HTMLElement>("#connection-detail");
const videoTitleElement = document.querySelector<HTMLElement>("#video-title");
const videoIdElement = document.querySelector<HTMLElement>("#video-id");
const unsupportedElement = document.querySelector<HTMLElement>("#unsupported");
const submitButton = document.querySelector<HTMLButtonElement>("#submit-video");
const refreshButton = document.querySelector<HTMLButtonElement>("#refresh-state");
const cacheSummaryElement = document.querySelector<HTMLElement>("#cache-summary");
const taskElement = document.querySelector<HTMLElement>("#task-state");
const errorElement = document.querySelector<HTMLElement>("#popup-error");

let currentState: PopupState | null = null;

void loadState();
submitButton?.addEventListener("click", () => void submitCurrentVideo());
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
  renderPopup(response.popup);
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
  if (submitButton) {
    submitButton.disabled = !state.video || state.connection.kind !== "ready";
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
  if (submitButton && busy) submitButton.disabled = true;
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
