import test from "node:test";
import assert from "node:assert/strict";

const VIDEO_ID = "BV1fixture01";
const VIDEO_URL = `https://www.bilibili.com/video/${VIDEO_ID}`;
const COOKIE_VALUE = "fixture-cookie-value-never-returned";
let fixtureCounter = 0;

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function createFixture({
  tab = { id: 7, url: `${VIDEO_URL}?p=2`, title: "Fixture 普通视频" },
  cookies = [{ name: "SESSDATA", value: COOKIE_VALUE, domain: ".bilibili.com", path: "/" }],
  cookieError,
  fetch,
} = {}) {
  const storage = new Map();
  const tabQueries = [];
  const cookieQueries = [];
  const requests = [];
  const listeners = { message: [], installed: [], startup: [], alarm: [] };
  const runtime = { lastError: undefined };
  const event = (name) => ({ addListener(listener) { listeners[name].push(listener); } });

  const chrome = {
    runtime: {
      ...runtime,
      onMessage: event("message"),
      onInstalled: event("installed"),
      onStartup: event("startup"),
    },
    tabs: {
      query(query, callback) {
        tabQueries.push(query);
        callback(tab ? [tab] : []);
      },
    },
    cookies: {
      getAll(details, callback) {
        cookieQueries.push(details);
        if (cookieError) {
          chrome.runtime.lastError = { message: cookieError };
          callback([]);
          chrome.runtime.lastError = undefined;
          return;
        }
        callback(cookies);
      },
    },
    storage: {
      local: {
        get(key, callback) {
          callback({ [key]: storage.get(key) });
        },
        set(values, callback) {
          for (const [key, value] of Object.entries(values)) storage.set(key, value);
          callback();
        },
      },
    },
    alarms: {
      create() {},
      onAlarm: event("alarm"),
    },
  };

  const fetchImpl = fetch ?? (async ({ path }) => {
    if (path === "/api/health") return jsonResponse({ status: "ready" });
    if (path === "/api/auth/session") return jsonResponse({ status: "valid", detail: "fixture auth" });
    if (path === "/api/tasks") {
      return jsonResponse({
        task_id: "task-fixture",
        video_id: VIDEO_ID,
        video_url: VIDEO_URL,
        title: "Fixture 普通视频",
        status: "queued",
        submitted_at: "2026-08-09T00:00:00Z",
      });
    }
    throw new Error(`Unexpected fixture request: ${path}`);
  });

  return {
    chrome,
    storage,
    tabQueries,
    cookieQueries,
    requests,
    async fetch(input, init = {}) {
      const request = {
        input: String(input),
        init,
        path: new URL(input).pathname,
        body: init.body ? JSON.parse(init.body) : undefined,
      };
      requests.push(request);
      return fetchImpl(request);
    },
  };
}

async function withServiceWorker(options, action) {
  const fixture = createFixture(options);
  const previousChrome = globalThis.chrome;
  const previousFetch = globalThis.fetch;
  globalThis.chrome = fixture.chrome;
  globalThis.fetch = fixture.fetch;
  try {
    const worker = await import(`../../dist/extension/src/service-worker.js?fixture=${fixtureCounter++}`);
    return { result: await action(worker, fixture), fixture };
  } finally {
    globalThis.chrome = previousChrome;
    globalThis.fetch = previousFetch;
  }
}

test("service worker reads the current tab cookies, syncs auth, and creates a task", async () => {
  const { result, fixture } = await withServiceWorker({}, (worker) =>
    worker.handleRuntimeMessage({ type: "SUBMIT_CURRENT_VIDEO", expectedBvid: VIDEO_ID }),
  );

  assert.equal(result.ok, true);
  assert.equal(result.task.taskId, "task-fixture");
  assert.equal(result.task.bvid, VIDEO_ID);
  assert.deepEqual(fixture.tabQueries, [{ active: true, currentWindow: true }]);
  assert.deepEqual(fixture.cookieQueries, [{ url: `${VIDEO_URL}?p=2` }]);
  assert.deepEqual(fixture.requests.map(({ path, init }) => [path, init.method]), [
    ["/api/health", "GET"],
    ["/api/auth/session", "POST"],
    ["/api/tasks", "POST"],
  ]);
  assert.deepEqual(fixture.requests[1].body, {
    source: "extension",
    origin: VIDEO_URL,
    cookies: { SESSDATA: COOKIE_VALUE },
  });
  assert.deepEqual(fixture.requests[2].body, {
    video_url: VIDEO_URL,
    title: "Fixture 普通视频",
  });
  assert.equal(JSON.stringify(result).includes(COOKIE_VALUE), false);
});

test("service worker reports an unsupported current page without reading cookies", async () => {
  const { result, fixture } = await withServiceWorker({
    tab: { id: 7, url: "https://www.bilibili.com/read/cv123", title: "Fixture article" },
    fetch: async () => {
      throw new Error("fetch must not run for an unsupported page");
    },
  }, (worker) => worker.handleRuntimeMessage({ type: "SUBMIT_CURRENT_VIDEO" }));

  assert.equal(result.ok, false);
  assert.equal(result.error.status, 400);
  assert.match(result.error.message, /当前页面不是首版支持/);
  assert.deepEqual(fixture.cookieQueries, []);
  assert.deepEqual(fixture.requests, []);
});

test("service worker reports an unavailable local service before cookie sync", async () => {
  const { result, fixture } = await withServiceWorker({
    fetch: async ({ path }) => {
      assert.equal(path, "/api/health");
      return jsonResponse({ status: "unavailable", detail: "Fixture service unavailable" });
    },
  }, (worker) => worker.handleRuntimeMessage({ type: "SUBMIT_CURRENT_VIDEO" }));

  assert.equal(result.ok, false);
  assert.equal(result.error.status, 503);
  assert.equal(result.error.message, "Fixture service unavailable");
  assert.deepEqual(fixture.cookieQueries, []);
  assert.equal(fixture.requests.length, 1);
});

test("service worker exposes a cookie permission error without starting auth or task requests", async () => {
  const { result, fixture } = await withServiceWorker({
    cookieError: "Fixture cookie permission denied",
    fetch: async ({ path }) => {
      assert.equal(path, "/api/health");
      return jsonResponse({ status: "ready" });
    },
  }, (worker) => worker.handleRuntimeMessage({ type: "SUBMIT_CURRENT_VIDEO" }));

  assert.equal(result.ok, false);
  assert.equal(result.error.status, 403);
  assert.equal(result.error.message, "Fixture cookie permission denied");
  assert.deepEqual(fixture.requests.map(({ path }) => path), ["/api/health"]);
  assert.deepEqual(fixture.cookieQueries, [{ url: `${VIDEO_URL}?p=2` }]);
});

test("service worker redacts cookie values from authentication errors", async () => {
  const { result, fixture } = await withServiceWorker({
    fetch: async ({ path }) => {
      if (path === "/api/health") return jsonResponse({ status: "ready" });
      if (path === "/api/auth/session") {
        return jsonResponse({ detail: `Fixture rejected ${COOKIE_VALUE}` }, 502);
      }
      throw new Error(`Unexpected fixture request: ${path}`);
    },
  }, (worker) => worker.handleRuntimeMessage({ type: "SUBMIT_CURRENT_VIDEO" }));

  assert.equal(result.ok, false);
  assert.equal(result.error.status, 502);
  assert.equal(result.error.message, "Fixture rejected [Cookie 已隐藏]");
  assert.equal(JSON.stringify(result).includes(COOKIE_VALUE), false);
  assert.deepEqual(fixture.requests.map(({ path }) => path), ["/api/health", "/api/auth/session"]);
  assert.deepEqual(fixture.requests[1].body.cookies, { SESSDATA: COOKIE_VALUE });
  assert.equal(JSON.stringify(fixture.storage.get("service-connection-v1")).includes(COOKIE_VALUE), false);
});
