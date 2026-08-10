import test from "node:test";
import assert from "node:assert/strict";
import { mountManagementPage } from "../../dist/web/src/main.js";

class TestHTMLElement {
  constructor(dataset = {}) {
    Object.assign(this, dataset);
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

function createApi() {
  const calls = [];
  let settings = {
    enabled: false,
    mode: "local_only",
    updatedAt: "2026-08-10T00:00:00Z",
  };
  return {
    calls,
    getHealth: async () => ({ status: "ready" }),
    getAuthSession: async () => null,
    listTasks: async () => ({ items: [] }),
    listUids: async () => ({ items: [] }),
    listEvidence: async () => ({ items: [] }),
    listReviewActions: async () => ({ items: [] }),
    listSamples: async () => ({ items: [] }),
    listBlacklist: async () => ({ items: [] }),
    getBlacklistSettings: async () => settings,
    updateBlacklistSettings: async (enabled) => {
      calls.push(`update:${enabled}`);
      settings = {
        enabled,
        mode: enabled ? "local_and_official_queue" : "local_only",
        updatedAt: "2026-08-10T00:00:01Z",
      };
      return settings;
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

test("dashboard only reports the blacklist switch while the queue owns the control", async () => {
  await withFakeDom(async () => {
    const root = new TestRoot();
    mountManagementPage(root, createApi());
    await flush();

    assert.match(root.content.innerHTML, /data-control-readonly="true"/);
    assert.match(root.content.innerHTML, /已关闭/);
    assert.doesNotMatch(root.content.innerHTML, /name="autoBlacklistEnabled"/);

    await root.listeners.get("click")({ target: new TestHTMLElement({ view: "blacklist" }) });

    assert.match(root.content.innerHTML, /data-control-readonly="false"/);
    assert.match(root.content.innerHTML, /name="autoBlacklistEnabled"/);
    assert.match(root.content.innerHTML, /仅本地隐藏/);
  });
});

test("queue switch persists the enabled mode and reports the local/official boundary", async () => {
  await withFakeDom(async () => {
    const root = new TestRoot();
    const api = createApi();
    mountManagementPage(root, api);
    await flush();
    await root.listeners.get("click")({ target: new TestHTMLElement({ view: "blacklist" }) });

    await root.listeners.get("change")({
      target: new TestHTMLElement({ name: "autoBlacklistEnabled", checked: true }),
    });
    await flush();

    assert.deepEqual(api.calls, ["update:true"]);
    assert.match(root.content.innerHTML, /已开启/);
    assert.match(root.content.innerHTML, /命中后排入官方拉黑队列/);
  });
});
