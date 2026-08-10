import test from "node:test";
import assert from "node:assert/strict";
import { ApiClient } from "../../dist/shared/api.js";

test("API client keeps readable blacklist diagnostics and technical fields", async () => {
  const client = new ApiClient("http://127.0.0.1:8765", async () => new Response(JSON.stringify({
    items: [{
      item_id: "queue-1",
      uid: "9003",
      status: "paused",
      attempts: 2,
      last_error: "selector=.more-actions__trigger",
      error_category: "page_structure",
      failure_type: "intercepted",
      user_message: "确认窗口结构未识别，队列已暂停",
      recovery_action: "请检查 B 站页面结构后点击“恢复”",
      error_at: "2026-08-10T12:00:00Z",
      updated_at: "2026-08-10T12:00:00Z",
    }],
  }), { status: 200, headers: { "content-type": "application/json" } }));

  const response = await client.listBlacklist();

  assert.deepEqual(response.items[0], {
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
  });
});
