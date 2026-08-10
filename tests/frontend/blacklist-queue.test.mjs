import test from "node:test";
import assert from "node:assert/strict";
import { mountManagementPage } from "../../dist/web/src/main.js";

class TestHTMLElement {
  constructor(dataset = {}) {
    this.dataset = dataset;
  }

  closest(selector) {
    if (selector === "[data-view]" && this.dataset.view) return this;
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

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function createApi() {
  return {
    getHealth: async () => ({ status: "ready" }),
    getAuthSession: async () => null,
    listTasks: async () => ({ items: [] }),
    listUids: async () => ({ items: [] }),
    listEvidence: async () => ({ items: [] }),
    listReviewActions: async () => ({ items: [] }),
    listSamples: async () => ({ items: [] }),
    listBlacklist: async () => ({
      items: [{
        itemId: "queue-1",
        uid: "9003",
        status: "paused",
        attempts: 2,
        lastError: "selector=.more-actions__trigger",
        errorCategory: "page_structure",
        failureType: "intercepted",
        userMessage: "确认窗口结构未识别，队列已暂停",
        recoveryAction: "请检查 B 站页面结构后点击“恢复”",
        errorAt: "2026-08-10T12:00:00Z",
        updatedAt: "2026-08-10T12:00:00Z",
      }],
    }),
  };
}

test("blacklist queue shows a readable failure and keeps raw details behind disclosure", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const root = new TestRoot();
    mountManagementPage(root, createApi());
    await flush();

    await root.listeners.get("click")({ target: new TestHTMLElement({ view: "blacklist" }) });

    const html = root.content.innerHTML;
    const technicalDetailsIndex = html.indexOf("查看技术详情");
    const rawErrorIndex = html.indexOf("selector=.more-actions__trigger");
    assert.match(html, /确认窗口结构未识别，队列已暂停/);
    assert.match(html, /请检查 B 站页面结构后点击“恢复”/);
    assert.match(html, /data-queue-action="resume"/);
    assert.ok(technicalDetailsIndex >= 0);
    assert.ok(rawErrorIndex > technicalDetailsIndex);
    assert.doesNotMatch(html.slice(0, technicalDetailsIndex), /selector=\.more-actions__trigger/);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});
