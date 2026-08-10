import test from "node:test";
import assert from "node:assert/strict";
import { mountManagementPage } from "../../dist/web/src/main.js";

class TestHTMLElement {
  constructor(dataset = {}, properties = {}) {
    this.dataset = dataset;
    Object.assign(this, properties);
  }

  closest(selector) {
    const keys = {
      "[data-view]": "view",
      "[data-review-action]": "reviewAction",
      "[data-evidence-select]": "evidenceSelect",
      "[data-evidence-close]": "evidenceClose",
      "[data-evidence-select-toggle]": "evidenceSelectToggle",
      "[data-evidence-select-all]": "evidenceSelectAll",
      "[data-review-batch-action]": "reviewBatchAction",
    };
    return keys[selector] && this.dataset[keys[selector]] !== undefined ? this : null;
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

const evidence = {
  evidenceId: "evidence-1",
  taskId: "task-1",
  uid: "1001",
  nicknameSnapshot: "hostile-user",
  result: "hit",
  commentText: "完整根评论",
  threadContext: "根评论上下文",
  sourceVideo: "https://www.bilibili.com/video/BV1example01",
  commentUrl: "https://www.bilibili.com/video/BV1example01#reply-root-1",
  signal: "巴斯特, 明显敌意",
  signals: ["巴斯特", "明显敌意"],
  modelReason: "模型理由",
  confidence: 0.97,
  modelVersion: "remote-model-v1",
  sampleVersion: "samples-v3",
  ruleVersion: "rules-v2",
  videoId: "BV1example01",
  comments: [{
    commentId: "root-1",
    uid: "1001",
    nickname: "hostile-user",
    content: "完整根评论",
    videoId: "BV1example01",
    commentUrl: "https://www.bilibili.com/video/BV1example01#reply-root-1",
    rootId: "root-1",
    parentId: null,
    level: "root",
    createdAt: 1700000000,
    isPinned: false,
    context: ["根评论上下文"],
  }, {
    commentId: "reply-1",
    uid: "1001",
    nickname: "hostile-user",
    content: "完整楼中楼",
    videoId: "BV1example01",
    commentUrl: "https://www.bilibili.com/video/BV1example01#reply-reply-1",
    rootId: "root-1",
    parentId: "root-1",
    level: "reply",
    createdAt: 1700000001,
    isPinned: false,
    context: ["根评论：完整根评论"],
  }],
  createdAt: "2026-08-10T00:00:00Z",
};

function createApi() {
  const calls = [];
  const reviewQueries = [];
  return {
    calls,
    reviewQueries,
    getHealth: async () => ({ status: "ready" }),
    getAuthSession: async () => null,
    listTasks: async () => ({ items: [] }),
    listUids: async () => ({ items: [{ uid: "1001", nicknameSnapshot: "hostile-user", status: "hidden", hidden: true, updatedAt: "" }] }),
    listEvidence: async (params = {}) => {
      reviewQueries.push(params.reviewStatus);
      return { items: [evidence] };
    },
    listReviewActions: async () => ({ items: [] }),
    listSamples: async () => ({ items: [] }),
    listBlacklist: async () => ({ items: [] }),
    getBlacklistSettings: async () => ({ enabled: false, mode: "local_only", updatedAt: "" }),
    reviewEvidence: async (evidenceId, action) => {
      calls.push({ evidenceId, action });
      return {
        reviewId: `review-${calls.length}`,
        evidenceId,
        uid: "1001",
        action,
        previousStatus: "hidden",
        nextStatus: action === "revoke" ? null : action === "exception" ? "exception" : "hidden",
        actor: "local-user",
        createdAt: "2026-08-10T00:00:01Z",
      };
    },
  };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function withFakeDom(callback) {
  const previous = {
    HTMLElement: globalThis.HTMLElement,
    HTMLInputElement: globalThis.HTMLInputElement,
    HTMLSelectElement: globalThis.HTMLSelectElement,
  };
  globalThis.HTMLElement = TestHTMLElement;
  globalThis.HTMLInputElement = TestHTMLElement;
  globalThis.HTMLSelectElement = TestHTMLElement;
  try {
    return await callback();
  } finally {
    globalThis.HTMLElement = previous.HTMLElement;
    globalThis.HTMLInputElement = previous.HTMLInputElement;
    globalThis.HTMLSelectElement = previous.HTMLSelectElement;
  }
}

async function openReviews(root) {
  await root.listeners.get("click")({ target: new TestHTMLElement({ view: "reviews" }) });
}

test("review inbox keeps primary evidence visible and opens full details in a separate inspector", async () => {
  await withFakeDom(async () => {
    const root = new TestRoot();
    mountManagementPage(root, createApi());
    await flush();
    await openReviews(root);

    const html = root.content.innerHTML;
    assert.match(html, /data-evidence-inbox/);
    assert.match(html, /data-evidence-row="evidence-1"/);
    assert.match(html, /AI · 命中/);
    assert.match(html, /人工 · 未复核/);
    assert.match(html, /置信度 97%/);
    assert.match(html, /巴斯特/);
    assert.match(html, /确认官方拉黑/);
    assert.match(html, /更多操作/);
    assert.doesNotMatch(html, /<details class="evidence-row"/);
    assert.doesNotMatch(html, /完整楼中楼/);

    await root.listeners.get("click")({
      target: new TestHTMLElement({ evidenceSelect: "evidence-1" }),
    });

    assert.match(root.content.innerHTML, /data-evidence-inspector/);
    assert.match(root.content.innerHTML, /完整根评论/);
    assert.match(root.content.innerHTML, /完整楼中楼/);
    assert.match(root.content.innerHTML, /samples-v3/);
    assert.match(root.content.innerHTML, /remote-model-v1/);
    assert.match(root.content.innerHTML, /关闭详情/);
  });
});

test("review action reports row-local busy and moves completed evidence to history", async () => {
  await withFakeDom(async () => {
    let resolveReview;
    const reviewPromise = new Promise((resolve) => { resolveReview = resolve; });
    const api = createApi();
    api.getBlacklistSettings = async () => ({ enabled: true, mode: "local_and_official_queue", updatedAt: "" });
    api.reviewEvidence = async (evidenceId, action) => {
      api.calls.push({ evidenceId, action });
      return reviewPromise;
    };
    const root = new TestRoot();
    mountManagementPage(root, api);
    await flush();
    await openReviews(root);

    const clickPromise = root.listeners.get("click")({
      target: new TestHTMLElement({ reviewAction: "confirm", evidenceId: "evidence-1" }),
    });
    await flush();
    assert.match(root.content.innerHTML, /data-evidence-busy="evidence-1"/);
    assert.match(root.content.innerHTML, /处理中/);
    assert.match(root.content.innerHTML, /data-evidence-row="evidence-1"/);

    resolveReview({
      reviewId: "review-1",
      evidenceId: "evidence-1",
      uid: "1001",
      action: "confirm",
      previousStatus: "hidden",
      nextStatus: "hidden",
      actor: "local-user",
      createdAt: "2026-08-10T00:00:01Z",
    });
    await clickPromise;
    await flush();

    assert.doesNotMatch(root.content.innerHTML, /data-evidence-row="evidence-1"/);
    assert.match(root.content.innerHTML, /evidence-1/);
    assert.match(root.content.innerHTML, /data-evidence-feedback="evidence-1"/);
    assert.doesNotMatch(root.content.innerHTML, /正在载入本机数据/);
  });
});

test("review inbox can switch between pending, history, and all evidence without showing inbox actions in history", async () => {
  await withFakeDom(async () => {
    const root = new TestRoot();
    const api = createApi();
    mountManagementPage(root, api);
    await flush();
    await openReviews(root);

    assert.deepEqual(api.reviewQueries, ["pending"]);
    await root.listeners.get("change")({
      target: new TestHTMLElement({}, { name: "reviewStatus", value: "history" }),
    });
    await flush();

    assert.deepEqual(api.reviewQueries, ["pending", "history"]);
    assert.match(root.content.innerHTML, /复核历史证据/);
    assert.doesNotMatch(root.content.innerHTML, /data-review-action=/);
  });
});

test("official review action requires one explicit confirmation and keeps cancellation local", async () => {
  await withFakeDom(async () => {
    const previousConfirm = globalThis.confirm;
    globalThis.confirm = () => false;
    try {
      const api = createApi();
      api.getBlacklistSettings = async () => ({ enabled: true, mode: "local_and_official_queue", updatedAt: "" });
      const root = new TestRoot();
      mountManagementPage(root, api);
      await flush();
      await openReviews(root);

      await root.listeners.get("click")({
        target: new TestHTMLElement({ reviewAction: "confirm", evidenceId: "evidence-1" }),
      });

      assert.deepEqual(api.calls, []);
      assert.match(root.content.innerHTML, /已取消本次操作/);
      assert.match(root.content.innerHTML, /data-evidence-row="evidence-1"/);
    } finally {
      globalThis.confirm = previousConfirm;
    }
  });
});

test("batch toolbar states exact evidence and UID impact", async () => {
  await withFakeDom(async () => {
    const second = { ...evidence, evidenceId: "evidence-2", uid: "1002", nicknameSnapshot: "another-user" };
    const api = createApi();
    api.listEvidence = async () => ({ items: [evidence, second] });
    api.listUids = async () => ({ items: [] });
    const root = new TestRoot();
    mountManagementPage(root, api);
    await flush();
    await openReviews(root);

    await root.listeners.get("change")({
      target: new TestHTMLElement({ evidenceSelectToggle: "evidence-1" }, { checked: true }),
    });
    await root.listeners.get("change")({
      target: new TestHTMLElement({ evidenceSelectToggle: "evidence-2" }, { checked: true }),
    });
    assert.match(root.content.innerHTML, /已选 2 条证据 · 2 个 UID/);

    await root.listeners.get("click")({
      target: new TestHTMLElement({ reviewBatchAction: "hide-only" }),
    });
    await flush();
    assert.deepEqual(api.calls.map((call) => call.action), ["hide-only", "hide-only"]);
    assert.match(root.content.innerHTML, /批量操作已完成/);
  });
});
