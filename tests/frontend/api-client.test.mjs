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

test("task comments request the task-scoped endpoint and normalize comment DTOs", async () => {
  const calls = [];
  const client = new ApiClient("http://127.0.0.1:8000", mockFetch({
    "/api/tasks/task-1/comments": {
      items: [
        {
          comment_id: "root-1",
          uid: "1001",
          nickname: "根评论用户",
          content: "根评论正文",
          video_id: "BV1abCdefGh1",
          comment_url: "https://www.bilibili.com/./root-1",
          root_id: "root-1",
          parent_id: null,
          level: "root",
          created_at: 1700000000,
          is_pinned: true,
          context: ["视频标题", "根评论正文"],
        },
        {
          comment_id: "reply-1",
          uid: "1002",
          nickname: "回复用户",
          content: "楼中楼正文",
          video_id: "BV1abCdefGh1",
          comment_url: "https://www.bilibili.com/./reply-1",
          root_id: "root-1",
          parent_id: "root-1",
          level: "reply",
          created_at: 1700000001,
          is_pinned: false,
          context: ["根评论正文", "楼中楼正文"],
        },
      ],
    },
  }, calls));

  const comments = await client.listTaskComments("task-1");

  assert.equal(calls[0].input, "http://127.0.0.1:8000/api/tasks/task-1/comments");
  assert.deepEqual(comments.items, [
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
  ]);
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

test("UID delta sync accepts the backend items/removed envelope", async () => {
  const client = new ApiClient("http://127.0.0.1:8000", mockFetch({
    "/api/uids/sync": {
      mode: "delta",
      version: 4,
      items: [{ uid: "1002", state: "review", nickname: "增量昵称" }],
      removed: ["1001"],
    },
  }, []));

  const sync = await client.syncUids(3);

  assert.deepEqual(sync.upserts, [{
    uid: "1002",
    nicknameSnapshot: "增量昵称",
    status: "review",
    hidden: true,
    updatedAt: "",
  }]);
  assert.deepEqual(sync.removals, ["1001"]);
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
