import test from "node:test";
import assert from "node:assert/strict";
import { mountManagementPage } from "../../dist/web/src/main.js";

class TestHTMLElement {
  constructor(dataset = {}) {
    this.dataset = dataset;
  }

  closest(selector) {
    if (selector === "[data-task-details]" && this.dataset.taskDetails) return this;
    if (selector === "[data-task-comments-retry]" && this.dataset.taskCommentsRetry) return this;
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

function createTask(taskId, title) {
  return {
    taskId,
    bvid: "BV1abCdefGh1",
    videoUrl: "https://www.bilibili.com/video/BV1abCdefGh1",
    title,
    status: "ready",
    submittedAt: "2026-08-09T00:00:00Z",
    collectedComments: 4,
    replyCount: 2,
    requestedPages: 3,
    pinnedComments: 1,
    declaredComments: 5,
    declaredReplies: 3,
    declaredTotal: 8,
    coverage: 0.8,
    failedItems: ["reply:root-1:2"],
  };
}

function createApi({ comments, commentError } = {}) {
  const calls = [];
  return {
    calls,
    getHealth: async () => ({ status: "ready" }),
    getAuthSession: async () => null,
    listTasks: async () => ({ items: [createTask("task-1", "任务一"), createTask("task-2", "任务二")] }),
    listUids: async () => ({ items: [] }),
    listEvidence: async () => ({ items: [] }),
    listSamples: async () => ({ items: [] }),
    listBlacklist: async () => ({ items: [] }),
    listTaskComments: async (taskId) => {
      calls.push(taskId);
      if (commentError) throw new Error(commentError);
      return { items: comments ?? [] };
    },
  };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

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
    assert.match(root.content.innerHTML, /reply:root-1:2/);
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
