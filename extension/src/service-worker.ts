import { ApiClient, ApiRequestError } from "../../shared/api.js";
import { applyUidSync, createEmptyUidCache, deserializeUidCache, serializeUidCache } from "../../shared/uid-cache.js";
import { connectionStateFromHealth, connectionStateFromRequestError, type ConnectionState } from "../../shared/state.js";
import type { AuthCookie, AuthSession, VideoTask } from "../../shared/types.js";
import type { RuntimeMessage, RuntimeResponse, PopupState } from "./messages.js";
import { getVideoIdentity } from "./video.js";

const CACHE_STORAGE_KEY = "uid-cache-v1";
const CONNECTION_STORAGE_KEY = "service-connection-v1";
const LAST_TASK_STORAGE_KEY = "last-submitted-task-v1";

let memoryCache: ReturnType<typeof createEmptyUidCache> | null = null;
let memoryConnection: ConnectionState = { kind: "loading", label: "正在连接本机服务" };
let memoryLastTask: VideoTask | undefined;
let backgroundAuthSync: Promise<AuthSession | undefined> | undefined;

interface AuthSessionSyncResult {
  auth: AuthSession;
  connection: ConnectionState;
}

interface InternalAuthSessionSyncResult extends AuthSessionSyncResult {
  cookieValues: string[];
}

export async function readCachedUidCache(): Promise<ReturnType<typeof createEmptyUidCache>> {
  if (memoryCache) return memoryCache;
  const stored = await storageGet(CACHE_STORAGE_KEY);
  if (typeof stored === "string") {
    try {
      memoryCache = deserializeUidCache(stored);
      return memoryCache;
    } catch {
      // A corrupt offline cache cannot be used as an authority; start empty and wait for a real sync.
    }
  }
  memoryCache = createEmptyUidCache();
  return memoryCache;
}

export async function syncUidCache(client = new ApiClient()): Promise<{
  synced: boolean;
  cache: ReturnType<typeof createEmptyUidCache>;
  connection: ConnectionState;
}> {
  const existing = await readCachedUidCache();
  try {
    const health = await client.getHealth();
    const healthState = connectionStateFromHealth(health);
    if (healthState.kind !== "ready") {
      memoryConnection = healthState;
      await persistConnection(memoryConnection);
      return { synced: false, cache: existing, connection: memoryConnection };
    }

    let sync = await client.syncUids(existing.version > 0 ? existing.version : undefined);
    let next = applyUidSync(existing, sync);
    if (next.requiresSnapshot) {
      sync = await client.syncUids();
      next = applyUidSync(existing, { ...sync, mode: "snapshot" });
    }
    memoryCache = next;
    memoryConnection = healthState;
    await storageSet(CACHE_STORAGE_KEY, serializeUidCache(next));
    await persistConnection(memoryConnection);
    await broadcastCache(next);
    return { synced: true, cache: next, connection: memoryConnection };
  } catch (error) {
    memoryConnection = connectionStateFromRequestError(asRequestError(error));
    await persistConnection(memoryConnection);
    return { synced: false, cache: existing, connection: memoryConnection };
  }
}

export async function getPopupState(tab?: chrome.tabs.Tab): Promise<PopupState> {
  const activeTab = tab ?? (await getActiveTab());
  const video = getVideoIdentity(activeTab?.url ?? "", activeTab?.title ?? "");
  const authSync = syncAuthSessionInBackground();
  const syncResult = await syncUidCache();
  await authSync;
  const cache = syncResult.cache;
  const lastTask = await readLastTask();
  return {
    video,
    connection: memoryConnection,
    authSyncAvailable: isBilibiliPageUrl(activeTab?.url ?? ""),
    cache: {
      available: cache.version > 0,
      version: cache.version,
      count: Object.keys(cache.records).length,
      lastSyncedAt: cache.lastSyncedAt,
    },
    task: lastTask && (!video || lastTask.bvid === video.bvid) ? lastTask : undefined,
  };
}

export async function submitCurrentVideo(expectedBvid?: string): Promise<VideoTask> {
  const tab = await getActiveTab();
  const video = getVideoIdentity(tab?.url ?? "", tab?.title ?? "");
  if (!video) throw new ApiRequestError(400, "当前页面不是首版支持的普通 B 站视频页");
  if (expectedBvid && expectedBvid !== video.bvid) {
    throw new ApiRequestError(409, "当前视频已变化，请重新打开插件弹窗");
  }

  const client = new ApiClient();
  let cookieValues: string[] = [];
  try {
    const authSync = await synchronizeAuthSession(tab, video.url, client);
    cookieValues = authSync.cookieValues;
    const task = await client.createTask({
      bvid: video.bvid,
      videoUrl: video.url,
    });
    memoryLastTask = task;
    await storageSet(LAST_TASK_STORAGE_KEY, task);
    return task;
  } catch (error) {
    const normalized = redactCookieValues(asRequestError(error), cookieValues);
    memoryConnection = connectionStateFromRequestError(normalized);
    await persistConnection(memoryConnection);
    throw normalized;
  }
}

export async function syncAuthSession(): Promise<AuthSessionSyncResult> {
  const tab = await getActiveTab();
  const tabUrl = tab?.url ?? "";
  if (!isBilibiliPageUrl(tabUrl)) {
    throw new ApiRequestError(400, "当前页面不是 B 站页面，无法同步登录态");
  }

  const video = getVideoIdentity(tabUrl, tab?.title ?? "");
  return sanitizeAuthSessionResult(
    await synchronizeAuthSession(tab, video?.url ?? tabUrl, new ApiClient()),
  );
}

export function syncAuthSessionInBackground(): Promise<AuthSession | undefined> {
  if (backgroundAuthSync) return backgroundAuthSync;

  const run = (async (): Promise<AuthSession | undefined> => {
    try {
      const tab = await getBilibiliTab();
      const tabUrl = tab?.url ?? "";
      if (!tab || !isBilibiliPageUrl(tabUrl)) return undefined;
      const video = getVideoIdentity(tabUrl, tab.title ?? "");
      const result = await synchronizeAuthSession(
        tab,
        video?.url ?? tabUrl,
        new ApiClient(),
      );
      return result.auth;
    } catch (error) {
      const normalized = asRequestError(error);
      memoryConnection = connectionStateFromRequestError(normalized);
      await persistConnection(memoryConnection);
      return undefined;
    }
  })();

  backgroundAuthSync = run;
  void run.then(
    () => {
      if (backgroundAuthSync === run) backgroundAuthSync = undefined;
    },
    () => {
      if (backgroundAuthSync === run) backgroundAuthSync = undefined;
    },
  );
  return run;
}

export async function handleRuntimeMessage(message: RuntimeMessage): Promise<RuntimeResponse> {
  try {
    switch (message.type) {
      case "GET_UID_CACHE":
        return { ok: true, cache: await readCachedUidCache() };
      case "SYNC_UID_CACHE": {
        const result = await syncUidCache();
        return { ok: true, ...result };
      }
      case "GET_POPUP_STATE":
        return { ok: true, popup: await getPopupState() };
      case "SYNC_AUTH_SESSION":
        return { ok: true, ...(await syncAuthSession()) };
      case "SUBMIT_CURRENT_VIDEO":
        return { ok: true, task: await submitCurrentVideo(message.expectedBvid) };
    }
  } catch (error) {
    const normalized = asRequestError(error);
    return { ok: false, error: { status: normalized.status, message: normalized.message } };
  }
}

async function broadcastCache(cache: ReturnType<typeof createEmptyUidCache>): Promise<void> {
  if (typeof chrome === "undefined") return;
  try {
    const tabs = await tabsQuery({ url: ["https://www.bilibili.com/video/*", "https://bilibili.com/video/*"] });
    await Promise.all(
      tabs
        .filter((tab) => tab.id !== undefined)
        .map(
          (tab) =>
            new Promise<void>((resolve) => {
              try {
                chrome.tabs.sendMessage(tab.id as number, { type: "UID_CACHE_UPDATED", cache }, () => resolve());
              } catch {
                resolve();
              }
            }),
        ),
    );
  } catch {
    // Cache synchronization is authoritative; notifying an already-closed tab is best effort.
  }
}

async function readLastTask(): Promise<VideoTask | undefined> {
  if (memoryLastTask) return memoryLastTask;
  const stored = await storageGet(LAST_TASK_STORAGE_KEY);
  if (stored && typeof stored === "object") {
    memoryLastTask = stored as VideoTask;
  }
  return memoryLastTask;
}

async function persistConnection(connection: ConnectionState): Promise<void> {
  await storageSet(CONNECTION_STORAGE_KEY, connection);
}

async function synchronizeAuthSession(
  tab: chrome.tabs.Tab | null,
  origin: string,
  client: ApiClient,
): Promise<InternalAuthSessionSyncResult> {
  let cookieValues: string[] = [];
  try {
    const health = await client.getHealth();
    const connection = connectionStateFromHealth(health);
    memoryConnection = connection;
    if (connection.kind !== "ready") {
      await persistConnection(connection);
      const detail = "detail" in connection ? connection.detail : undefined;
      throw new ApiRequestError(503, detail ?? "本机服务尚未就绪");
    }

    const cookies = await readBilibiliCookies(tab?.url ?? origin);
    cookieValues = cookies.map((cookie) => cookie.value).filter((value) => value.length > 0);
    const auth = normalizeAuthSession(await client.saveAuthSession({ source: "extension", origin, cookies }), cookieValues);
    const safeConnection = redactConnectionValues(connection, cookieValues);
    memoryConnection = safeConnection;
    await persistConnection(safeConnection);
    return { auth, connection: safeConnection, cookieValues };
  } catch (error) {
    const normalized = redactCookieValues(asRequestError(error), cookieValues);
    memoryConnection = connectionStateFromRequestError(normalized);
    await persistConnection(memoryConnection);
    throw normalized;
  }
}

function sanitizeAuthSessionResult(result: InternalAuthSessionSyncResult): AuthSessionSyncResult {
  return { auth: result.auth, connection: result.connection };
}

async function readBilibiliCookies(url: string): Promise<AuthCookie[]> {
  if (typeof chrome === "undefined") return [];
  const cookies = await new Promise<chrome.cookies.Cookie[]>((resolve, reject) => {
    chrome.cookies.getAll({ url }, (result) => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new ApiRequestError(403, error.message ?? "无法读取 B 站会话权限"));
        return;
      }
      resolve(result);
    });
  });
  return cookies.map((cookie) => ({
    name: cookie.name,
    value: cookie.value,
    domain: cookie.domain,
    path: cookie.path,
    expirationDate: cookie.expirationDate,
    secure: cookie.secure,
    httpOnly: cookie.httpOnly,
    sameSite: cookie.sameSite,
  }));
}

function redactCookieValues(error: ApiRequestError, cookieValues: string[]): ApiRequestError {
  const message = redactCookieText(error.message, cookieValues);
  return message === error.message ? error : new ApiRequestError(error.status, message, error.code);
}

function redactConnectionValues(connection: ConnectionState, cookieValues: string[]): ConnectionState {
  if (!("detail" in connection) || !connection.detail) return connection;
  const detail = redactCookieText(connection.detail, cookieValues);
  return detail === connection.detail ? connection : { ...connection, detail };
}

function redactCookieText(text: string, cookieValues: string[]): string {
  return [...new Set(cookieValues)].filter((value) => value.length > 0).reduce(
    (current, value) => current.split(value).join("[Cookie 已隐藏]"),
    text,
  );
}

function normalizeAuthSession(value: unknown, cookieValues: string[]): AuthSession {
  const candidate = asRecord(value);
  const status = candidate.status;
  if (!isAuthStatus(status)) throw new ApiRequestError(502, "认证状态响应格式无效");
  return {
    status,
    detail: redactCookieText(
      typeof candidate.detail === "string" ? candidate.detail : "未提供认证诊断信息",
      cookieValues,
    ),
    checkedAt: redactCookieText(
      typeof candidate.checked_at === "string"
        ? candidate.checked_at
        : typeof candidate.checkedAt === "string" ? candidate.checkedAt : "",
      cookieValues,
    ) || undefined,
    cookiePresent: typeof candidate.cookie_present === "boolean"
      ? candidate.cookie_present
      : typeof candidate.cookiePresent === "boolean" ? candidate.cookiePresent : false,
  };
}

function isAuthStatus(value: unknown): value is AuthSession["status"] {
  return ["valid", "invalid", "missing", "verification_failed"].includes(value as string);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function isBilibiliPageUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    return url.protocol === "https:" && (hostname === "bilibili.com" || hostname.endsWith(".bilibili.com"));
  } catch {
    return false;
  }
}

async function getActiveTab(): Promise<chrome.tabs.Tab | null> {
  if (typeof chrome === "undefined") return null;
  const tabs = await tabsQuery({ active: true, currentWindow: true });
  return tabs[0] ?? null;
}

async function getBilibiliTab(): Promise<chrome.tabs.Tab | null> {
  const activeTab = await getActiveTab();
  if (activeTab && isBilibiliPageUrl(activeTab.url ?? "")) return activeTab;
  const tabs = await tabsQuery({
    url: ["https://www.bilibili.com/*", "https://*.bilibili.com/*"],
  });
  return tabs.find((tab) => isBilibiliPageUrl(tab.url ?? "")) ?? null;
}

function tabsQuery(query: chrome.tabs.QueryInfo): Promise<chrome.tabs.Tab[]> {
  return new Promise((resolve, reject) => {
    chrome.tabs.query(query, (tabs) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new ApiRequestError(0, error.message ?? "无法读取当前标签页"));
      else resolve(tabs);
    });
  });
}

function storageGet(key: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    chrome.storage.local.get(key, (values) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new ApiRequestError(0, error.message ?? "无法读取扩展缓存"));
      else resolve(values[key]);
    });
  });
}

function storageSet(key: string, value: unknown): Promise<void> {
  return new Promise((resolve, reject) => {
    chrome.storage.local.set({ [key]: value }, () => {
      const error = chrome.runtime.lastError;
      if (error) reject(new ApiRequestError(0, error.message ?? "无法保存扩展缓存"));
      else resolve();
    });
  });
}

function asRequestError(error: unknown): ApiRequestError {
  if (error instanceof ApiRequestError) return error;
  return new ApiRequestError(0, error instanceof Error ? error.message : "本机服务请求失败");
}

function registerListeners(): void {
  if (typeof chrome === "undefined") return;
  chrome.runtime.onMessage.addListener((message: RuntimeMessage, _sender, sendResponse) => {
    void handleRuntimeMessage(message).then(sendResponse);
    return true;
  });
  chrome.runtime.onInstalled.addListener(() => {
    void syncUidCache();
    void syncAuthSessionInBackground();
  });
  chrome.runtime.onStartup.addListener(() => {
    void syncUidCache();
    void syncAuthSessionInBackground();
  });
  chrome.alarms?.create("uid-cache-sync", { periodInMinutes: 5 });
  chrome.alarms?.onAlarm.addListener((alarm) => {
    if (alarm.name === "uid-cache-sync") {
      void syncUidCache();
      void syncAuthSessionInBackground();
    }
  });
}

registerListeners();
