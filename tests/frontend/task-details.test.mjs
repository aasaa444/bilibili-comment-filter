import test from "node:test";
import assert from "node:assert/strict";
import { mountManagementPage } from "../../dist/web/src/main.js";

class TestHTMLElement {
  constructor(dataset = {}) {
    this.dataset = dataset;
  }

  closest(selector) {
    if (selector === "[data-task-details]" && this.dataset.taskDetails) return this;
    if (selector === "[data-retry-task]" && this.dataset.retryTask) return this;
    if (selector === "[data-task-comments-retry]" && this.dataset.taskCommentsRetry) return this;
    if (selector === "[data-task-comments-page]" && this.dataset.taskCommentsPage) return this;
    return null;
  }
}

class TestRoot {
  constructor() {
    this.content = { innerHTML: "" };
    this.listeners = new Map();
    this.html = "";
  }

  set innerHTML(value) {
    this.html = value;
    const match = value.match(/<section data-content>([\s\S]*)<\/section>/);
    if (match) this.content.innerHTML = match[1];
  }

  get innerHTML() {
    return this.html;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  querySelector(selector) {
    return selector === "[data-content]" ? this.content : null;
  }
}

function createTask(taskId, title, status = "ready") {
  return {
    taskId,
    bvid: "BV1abCdefGh1",
    videoUrl: "https://www.bilibili.com/video/BV1abCdefGh1",
    title,
    status,
    submittedAt: "2026-08-09T00:00:00Z",
    collectedComments: 4,
    replyCount: 2,
    requestedPages: 3,
    pinnedComments: 1,
    declaredComments: 5,
    declaredReplies: 3,
    declaredTotal: 8,
    coverage: 0.8,
    failedItems: [
      "inconsistent_root_item:1:unknown:missing_comment_id",
      "inconsistent_root_item:2:not_object",
      "empty_reply_page:root-1:2",
    ],
  };
}

function createApi({ comments, commentError, events = [], analysis = { latest: null, attempts: [] }, health = { status: "ready" }, tasks } = {}) {
  const calls = [];
  return {
    calls,
    getHealth: async () => health,
    getAuthSession: async () => null,
    listTasks: async () => ({ items: tasks ?? [createTask("task-1", "任务一"), createTask("task-2", "任务二")] }),
    listUids: async () => ({ items: [] }),
    listEvidence: async () => ({ items: [] }),
    listSamples: async () => ({ items: [] }),
    listBlacklist: async () => ({ items: [] }),
    listTaskComments: async (taskId) => {
      calls.push(taskId);
      if (commentError) throw new Error(commentError);
      return { items: comments ?? [] };
    },
    listTaskEvents: async () => ({ items: events }),
    getTaskAnalysis: async () => analysis,
  };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("management dashboard shows model configuration without blocking local work", async () => {
  const root = new TestRoot();
  const api = createApi({
    health: {
      status: "ready",
      model: {
        status: "unconfigured",
        detail: "Remote model is not configured; local hiding remains available.",
        baseUrlConfigured: false,
        modelConfigured: false,
        apiKeyConfigured: false,
      },
    },
  });

  mountManagementPage(root, api);
  await flush();

  assert.match(root.innerHTML, /AI 分析配置/);
  assert.match(root.innerHTML, /模型未配置/);
  assert.match(root.innerHTML, /local hiding remains available/);
});

test("partial, paused, and failed tasks expose a retry action", async () => {
  const root = new TestRoot();
  const api = createApi({
    tasks: [
      createTask("task-partial", "partial task", "partial"),
      createTask("task-paused", "paused task", "paused"),
      createTask("task-failed", "failed task", "failed"),
      createTask("task-ready", "ready task", "ready"),
    ],
  });

  mountManagementPage(root, api);
  await flush();

  assert.match(root.innerHTML, /data-retry-task="task-partial"/);
  assert.match(root.innerHTML, /data-retry-task="task-paused"/);
  assert.match(root.innerHTML, /data-retry-task="task-failed"/);
  assert.doesNotMatch(root.innerHTML, /data-retry-task="task-ready"/);
});

test("task rows expose a detail entry and render normalized root/reply comments with stats", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const root = new TestRoot();
    const api = createApi({
      comments: [
        {
          commentId: "root-1",
          uid: "1001",
          nickname: "根评论用户",
          content: "根评论正文",
          videoId: "BV1abCdefGh1",
          commentUrl: "https://www.bilibili.com/./root-1",
          rootId: "root-1",
          parentId: null,
          level: "root",
          createdAt: 1700000000,
          isPinned: true,
          context: ["视频标题", "根评论正文"],
        },
        {
          commentId: "reply-1",
          uid: "1002",
          nickname: "回复用户",
          content: "楼中楼正文",
          videoId: "BV1abCdefGh1",
          commentUrl: "https://www.bilibili.com/./reply-1",
          rootId: "root-1",
          parentId: "root-1",
          level: "reply",
          createdAt: 1700000001,
          isPinned: false,
          context: ["根评论正文", "楼中楼正文"],
        },
      ],
    });

    mountManagementPage(root, api);
    await flush();
    assert.match(root.innerHTML, /data-task-details="task-1"/);
    assert.match(root.innerHTML, /data-task-details="task-2"/);

    await root.listeners.get("click")({ target: new TestHTMLElement({ taskDetails: "task-1" }) });
    await flush();

    assert.deepEqual(api.calls, ["task-1"]);
    assert.match(root.content.innerHTML, /保存根评论/);
    assert.match(root.content.innerHTML, /覆盖率/);
    assert.match(root.content.innerHTML, /80%/);
    assert.match(root.content.innerHTML, /声明总量/);
    assert.match(root.content.innerHTML, />8</);
    assert.match(root.content.innerHTML, /失败项/);
    assert.match(root.content.innerHTML, /根评论缺少评论 ID/);
    assert.match(root.content.innerHTML, /根评论结构异常/);
    assert.match(root.content.innerHTML, /楼中楼空页/);
    assert.match(root.content.innerHTML, /查看原始失败项/);
    assert.match(root.content.innerHTML, /inconsistent_root_item:1:unknown:missing_comment_id/);
    assert.match(root.content.innerHTML, /根评论/);
    assert.match(root.content.innerHTML, /楼中楼回复/);
    assert.match(root.content.innerHTML, /1001/);
    assert.match(root.content.innerHTML, /根评论用户/);
    assert.match(root.content.innerHTML, /根评论正文/);
    assert.match(root.content.innerHTML, /视频标题/);
    assert.match(root.content.innerHTML, /置顶/);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});

test("task details show AI summary counts and the execution timeline", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const root = new TestRoot();
    const api = createApi({
      events: [
        {
          eventId: 1,
          taskId: "task-1",
          attempt: 0,
          phase: "analyzing",
          eventType: "analysis_completed",
          status: "succeeded",
          message: "AI analysis completed",
          details: { hit_count: 1, non_target_count: 2, checkpoint: { root_page: 3 } },
          createdAt: "2026-08-10T00:00:00Z",
        },
      ],
      analysis: {
        latest: {
          analysisId: "analysis-1",
          taskId: "task-1",
          attempt: 0,
          status: "completed",
          model: "fixture-model",
          sampleVersion: "samples-v1",
          batchCount: 2,
          accountCount: 3,
          hitCount: 1,
          uncertainCount: 0,
          nonTargetCount: 2,
          evidenceCount: 1,
        },
        attempts: [],
      },
    });

    mountManagementPage(root, api);
    await flush();
    await root.listeners.get("click")({ target: new TestHTMLElement({ taskDetails: "task-1" }) });
    await flush();

    assert.match(root.content.innerHTML, /AI 分析摘要/);
    assert.match(root.content.innerHTML, /fixture-model/);
    assert.match(root.content.innerHTML, /非目标/);
    assert.match(root.content.innerHTML, /处理时间线/);
    assert.match(root.content.innerHTML, /分析完成/);
    assert.match(root.content.innerHTML, /AI 分析完成/);
    assert.match(root.content.innerHTML, /查看原始诊断数据/);
    assert.doesNotMatch(root.content.innerHTML, /AI analysis completed/);
    const observabilityIndex = root.content.innerHTML.indexOf('<div class="task-observability-grid">');
    const commentsIndex = root.content.innerHTML.indexOf('<div class="task-comments-section">');
    assert.ok(observabilityIndex >= 0);
    assert.ok(commentsIndex > observabilityIndex);
    assert.doesNotMatch(
      root.content.innerHTML.slice(commentsIndex),
      /AI 分析摘要|处理时间线/,
    );
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});

test("legacy terminal tasks explain why diagnostics are unavailable", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const root = new TestRoot();
    const api = createApi({
      tasks: [createTask("task-legacy", "历史任务", "ready")],
      comments: [],
      events: [],
      analysis: { latest: null, attempts: [] },
    });

    mountManagementPage(root, api);
    await flush();
    await root.listeners.get("click")({ target: new TestHTMLElement({ taskDetails: "task-legacy" }) });
    await flush();

    assert.match(root.content.innerHTML, /历史任务未启用诊断/);
    assert.match(root.content.innerHTML, /无法从历史数据补写/);
    assert.match(root.content.innerHTML, /没有可回溯的处理记录/);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});

test("task details keep an AI parsing failure visible", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const root = new TestRoot();
    const api = createApi({
      analysis: {
        latest: {
          analysisId: "analysis-2",
          taskId: "task-1",
          attempt: 0,
          status: "failed",
          batchCount: 1,
          accountCount: 27,
          hitCount: 0,
          uncertainCount: 0,
          nonTargetCount: 0,
          evidenceCount: 0,
          errorCode: "invalid_model_response",
          errorMessage: "Model response was not valid JSON",
        },
        attempts: [],
      },
    });

    mountManagementPage(root, api);
    await flush();
    await root.listeners.get("click")({ target: new TestHTMLElement({ taskDetails: "task-1" }) });
    await flush();

    assert.match(root.content.innerHTML, /分析失败/);
    assert.match(root.content.innerHTML, /invalid_model_response/);
    assert.match(root.content.innerHTML, /Model response was not valid JSON/);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});

test("task comment errors remain visible without removing other task rows", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const root = new TestRoot();
    const api = createApi({ commentError: "评论服务返回 503" });

    mountManagementPage(root, api);
    await flush();
    await root.listeners.get("click")({ target: new TestHTMLElement({ taskDetails: "task-1" }) });
    await flush();

    assert.match(root.content.innerHTML, /评论加载失败/);
    assert.match(root.content.innerHTML, /评论服务返回 503/);
    assert.match(root.content.innerHTML, /任务二/);
    assert.deepEqual(api.calls, ["task-1"]);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});

test("task comments render one page at a time and support next-page navigation", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const comments = Array.from({ length: 45 }, (_, index) => ({
      commentId: `comment-${index + 1}`,
      uid: String(2000 + index),
      nickname: `用户${index + 1}`,
      content: `评论第 ${index + 1} 条`,
      videoId: "BV1abCdefGh1",
      commentUrl: `https://www.bilibili.com/./comment-${index + 1}`,
      rootId: `comment-${index + 1}`,
      parentId: null,
      level: "root",
      createdAt: 1700000000 + index,
      isPinned: false,
      context: [],
    }));
    const root = new TestRoot();
    const api = createApi({ comments });

    mountManagementPage(root, api);
    await flush();
    await root.listeners.get("click")({ target: new TestHTMLElement({ taskDetails: "task-1" }) });
    await flush();

    assert.match(root.content.innerHTML, /显示 1-20 条，共 45 条评论/);
    assert.match(root.content.innerHTML, /评论第 1 条/);
    assert.doesNotMatch(root.content.innerHTML, /评论第 21 条/);
    assert.match(root.content.innerHTML, /第 1 \/ 3 页/);

    await root.listeners.get("click")({
      target: new TestHTMLElement({ taskCommentsPage: "task-1", page: "2" }),
    });

    assert.match(root.content.innerHTML, /显示 21-40 条，共 45 条评论/);
    assert.match(root.content.innerHTML, /评论第 21 条/);
    assert.doesNotMatch(root.content.innerHTML, /评论第 1 条/);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});
