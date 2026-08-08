import test from "node:test";
import assert from "node:assert/strict";
import { ApiClient } from "../../dist/shared/api.js";

function mockFetch(responses, calls) {
  return async (input, init = {}) => {
    calls.push({ input, init });
    const path = new URL(input).pathname;
    const payload = responses[path] ?? {};
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
}

test("task creation maps the frontend DTO to backend video_url/title fields", async () => {
  const calls = [];
  const client = new ApiClient("http://127.0.0.1:8000", mockFetch({
    "/api/tasks": {
      task_id: "task-1",
      video_url: "https://www.bilibili.com/video/BV1abCdefGh1",
      title: "任务标题",
      status: "queued",
    },
  }, calls));

  const task = await client.createTask({
    bvid: "BV1abCdefGh1",
    videoUrl: "https://www.bilibili.com/video/BV1abCdefGh1",
    title: "任务标题",
  });

  assert.equal(task.bvid, "BV1abCdefGh1");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    video_url: "https://www.bilibili.com/video/BV1abCdefGh1",
    title: "任务标题",
  });
});

test("UID sync maps backend state/nickname to the cache DTO and keeps the version query", async () => {
  const calls = [];
  const client = new ApiClient("http://127.0.0.1:8000", mockFetch({
    "/api/uids/sync": {
      version: 9,
      uids: [{ uid: "1001", state: "review", nickname: "昵称快照" }],
    },
  }, calls));

  const sync = await client.syncUids(8);

  assert.equal(new URL(calls[0].input).search, "?since=8");
  assert.deepEqual(sync.records, [{
    uid: "1001",
    nicknameSnapshot: "昵称快照",
    status: "review",
    hidden: true,
    updatedAt: "",
  }]);
});

test("UID writes use backend state/nickname names", async () => {
  const calls = [];
  const client = new ApiClient("http://127.0.0.1:8000", mockFetch({
    "/api/uids": { uid: "1001", state: "hidden", nickname: "目标" },
  }, calls));

  await client.createUid({ uid: "1001", nicknameSnapshot: "目标", status: "hidden", hidden: true });

  assert.deepEqual(JSON.parse(calls[0].init.body), {
    uid: "1001",
    nickname: "目标",
    state: "hidden",
  });
});
