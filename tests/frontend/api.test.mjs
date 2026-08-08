import test from "node:test";
import assert from "node:assert/strict";
import { ApiClient } from "../../dist/shared/api.js";

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("API client normalizes completed tasks, nested progress and evidence decisions", async () => {
  const client = new ApiClient("http://127.0.0.1:8765", async (input) => {
    assert.equal(input, "http://127.0.0.1:8765/api/tasks");
    return jsonResponse({
      items: [{
        task_id: "task-1",
        video_id: "BV1example01",
        video_url: "https://www.bilibili.com/video/BV1example01",
        status: "completed",
        progress: { saved_comments: 4, saved_replies: 2, coverage: 1 },
      }],
    });
  });

  const tasks = await client.listTasks();

  assert.equal(tasks.items[0].status, "ready");
  assert.equal(tasks.items[0].progress, 100);
  assert.equal(tasks.items[0].collectedComments, 4);
  assert.equal(tasks.items[0].replyCount, 2);
});

test("API client maps review actions and cookie sessions to backend payloads", async () => {
  const requests = [];
  const client = new ApiClient("http://127.0.0.1:8765", async (input, init) => {
    requests.push({ input, init });
    if (String(input).includes("/auth/session")) return jsonResponse({ status: "valid", detail: "ok" });
    return jsonResponse({
      action_id: "review-1",
      evidence_id: "evidence-1",
      uid: "1001",
      action: "highlight",
      before_state: "review",
      after_state: "review",
      actor: "local-user",
      created_at: "2026-08-09T00:00:00Z",
    });
  });

  await client.saveAuthSession({
    source: "extension",
    origin: "https://www.bilibili.com/video/BV1example01",
    cookies: [{ name: "SESSDATA", value: "secret", domain: ".bilibili.com", path: "/" }],
  });
  const review = await client.reviewEvidence("evidence-1", "positive-sample");

  const authBody = JSON.parse(requests[0].init.body);
  const reviewBody = JSON.parse(requests[1].init.body);
  assert.deepEqual(authBody.cookies, { SESSDATA: "secret" });
  assert.equal(reviewBody.action, "highlight");
  assert.equal(review.action, "positive-sample");
});

test("API client keeps versioned samples and cancelled blacklist items", async () => {
  const client = new ApiClient("http://127.0.0.1:8765", async (input) => {
    if (String(input).endsWith("/api/samples")) {
      return jsonResponse({
        items: [{
          sample_id: "sample-1",
          kind: "comment",
          version: "samples-v1",
          status: "published",
          items: [{ label: "negative", content: "反例评论" }],
          created_at: "2026-08-09T00:00:00Z",
        }],
      });
    }
    return jsonResponse({
      items: [{
        item_id: "queue-1",
        uid: "1001",
        status: "cancelled",
        attempts: 0,
        updated_at: "2026-08-09T00:00:00Z",
      }],
    });
  });

  const samples = await client.listSamples();
  const blacklist = await client.listBlacklist();

  assert.equal(samples.items[0].version, 1);
  assert.equal(samples.items[0].items[0].kind, "comment-negative");
  assert.equal(blacklist.items[0].status, "cancelled");
});
