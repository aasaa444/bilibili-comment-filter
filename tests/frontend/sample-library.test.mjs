import test from "node:test";
import assert from "node:assert/strict";
import { mountManagementPage } from "../../dist/web/src/main.js";

class TestHTMLElement {
  constructor(dataset = {}) {
    this.dataset = dataset;
  }

  closest(selector) {
    if (selector === "[data-view]" && this.dataset.view) return this;
    if (selector === "[data-sample-details]" && this.dataset.sampleDetails) return this;
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

function createApi() {
  return {
    getHealth: async () => ({ status: "ready" }),
    getAuthSession: async () => null,
    listTasks: async () => ({ items: [] }),
    listUids: async () => ({ items: [] }),
    listEvidence: async () => ({ items: [] }),
    listReviewActions: async () => ({ items: [] }),
    listSamples: async () => ({
      items: [
        {
          sampleId: "sample-2",
          kind: "comment",
          version: 2,
          status: "published",
          isCurrent: true,
          items: [
            { text: "latest preview", kind: "comment-positive", label: "positive", source: "manual" },
            { text: "latest second", kind: "nickname-positive", label: "positive", source: "review" },
            { text: "latest hidden tail", kind: "comment-negative", label: "negative", source: "file" },
          ],
          createdAt: "2026-08-10T00:00:00Z",
          publishedAt: "2026-08-10T00:01:00Z",
        },
        {
          sampleId: "sample-1",
          kind: "comment",
          version: 1,
          status: "disabled",
          isCurrent: false,
          items: [
            { text: "history preview", kind: "comment-positive", label: "positive", source: "manual" },
            { text: "history second", kind: "comment-positive", label: "positive", source: "manual" },
            { text: "history hidden tail", kind: "comment-positive", label: "positive", source: "manual" },
          ],
          createdAt: "2026-08-09T00:00:00Z",
          publishedAt: "2026-08-09T00:01:00Z",
        },
      ],
    }),
    listBlacklist: async () => ({ items: [] }),
  };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("sample library shows current version, previews, and only expands the selected full snapshot", async () => {
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = TestHTMLElement;
  try {
    const root = new TestRoot();
    mountManagementPage(root, createApi());
    await flush();

    await root.listeners.get("click")({ target: new TestHTMLElement({ view: "samples" }) });

    assert.match(root.content.innerHTML, /data-sample-current="sample-2"/);
    assert.match(root.content.innerHTML, /data-sample-details="sample-1"/);
    assert.match(root.content.innerHTML, /data-sample-details="sample-2"/);
    assert.match(root.content.innerHTML, /latest preview/);
    assert.doesNotMatch(root.content.innerHTML, /latest hidden tail/);

    await root.listeners.get("click")({ target: new TestHTMLElement({ sampleDetails: "sample-2" }) });

    assert.match(root.content.innerHTML, /latest hidden tail/);
    assert.doesNotMatch(root.content.innerHTML, /history hidden tail/);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
  }
});
