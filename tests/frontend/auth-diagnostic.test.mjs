import test from "node:test";
import assert from "node:assert/strict";
import { mountManagementPage } from "../../dist/web/src/main.js";

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
}

function createApi(auth) {
  const calls = [];
  return {
    calls,
    getHealth: async () => ({ status: "ready" }),
    getAuthSession: async () => {
      calls.push("getAuthSession");
      return auth;
    },
    listTasks: async () => ({ items: [] }),
    listUids: async () => ({ items: [] }),
    listEvidence: async () => ({ items: [] }),
    listReviewActions: async () => ({ items: [] }),
    listSamples: async () => ({ items: [] }),
    listBlacklist: async () => ({ items: [] }),
  };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

const authFixtures = [
  {
    status: "valid",
    detail: "会话验证通过",
    cookiePresent: true,
    label: "会话有效",
    dataState: "ready",
    sessionNotice: "已收到会话",
    absentNotice: "未收到会话",
  },
  {
    status: "invalid",
    detail: "会话已过期，请重新同步",
    cookiePresent: true,
    label: "会话已失效",
    dataState: "error",
    sessionNotice: "已收到会话",
    absentNotice: "未收到会话",
  },
  {
    status: "missing",
    detail: "扩展尚未同步会话",
    cookiePresent: false,
    label: "尚未同步",
    dataState: "paused",
    sessionNotice: "未收到会话",
    absentNotice: "已收到会话",
  },
  {
    status: "verification_failed",
    detail: "B 站会话验证失败，请重新登录",
    cookiePresent: true,
    label: "验证失败",
    dataState: "error",
    sessionNotice: "已收到会话",
    absentNotice: "未收到会话",
  },
].map((fixture) => ({
  ...fixture,
  checkedAt: "2026-08-09T01:02:03Z",
}));

test("management page renders every authentication diagnostic state", async () => {
  for (const fixture of authFixtures) {
    const root = new TestRoot();
    const api = createApi(fixture);

    mountManagementPage(root, api);
    await flush();

    const html = root.content.innerHTML;
    assert.deepEqual(api.calls, ["getAuthSession"]);
    assert.ok(html.includes(`data-state="${fixture.dataState}">${fixture.label}</span>`), fixture.status);
    assert.ok(html.includes(fixture.detail), fixture.status);
    assert.ok(html.includes(`最近检查：${fixture.checkedAt}`), fixture.status);
    assert.ok(html.includes(fixture.sessionNotice), fixture.status);
    assert.equal(html.includes(fixture.absentNotice), false, fixture.status);
  }
});

test("management page explains when the authentication diagnostic is unavailable", async () => {
  const root = new TestRoot();
  const api = createApi(null);

  mountManagementPage(root, api);
  await flush();

  assert.deepEqual(api.calls, ["getAuthSession"]);
  assert.ok(root.content.innerHTML.includes("认证状态暂不可用"));
  assert.ok(root.content.innerHTML.includes("无法读取"));
  assert.ok(root.content.innerHTML.includes("请刷新管理页后重试。"));
});
