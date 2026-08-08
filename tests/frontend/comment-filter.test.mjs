import test from "node:test";
import assert from "node:assert/strict";
import { applyUidSync, createEmptyUidCache } from "../../dist/shared/uid-cache.js";
import { createCommentFilterController } from "../../dist/extension/src/comment-filter.js";
import {
  FakeElement,
  FakeMutationObserver,
  createComment,
  createShadowComment,
} from "./fake-dom.mjs";

function createCommentPage() {
  const document = new FakeElement({ tagName: "document" });
  const commentSection = new FakeElement({ id: "comment" });
  const rootComment = createComment("1001");
  const reply = createComment("1001", { reply: true });
  const unknownComment = createComment("9001");
  const danmaku = new FakeElement({
    classes: ["danmaku"],
    attributes: { "data-user-id": "1001" },
  });
  commentSection.append(rootComment, reply, unknownComment);
  document.append(commentSection, danmaku);
  return { document, commentSection, rootComment, reply, unknownComment, danmaku };
}

test("comment filtering hides matching roots and replies but never touches danmaku", () => {
  const page = createCommentPage();
  const cache = applyUidSync(createEmptyUidCache(), {
    version: 1,
    mode: "snapshot",
    records: [{
      uid: "1001",
      nicknameSnapshot: "目标账号",
      status: "hidden",
      hidden: true,
      updatedAt: "2026-08-09T00:00:00Z",
    }],
  });
  let observer;
  const controller = createCommentFilterController({
    root: page.document,
    cache,
    observerFactory: (callback) => {
      observer = new FakeMutationObserver(callback);
      return observer;
    },
  });

  controller.start();

  assert.equal(page.rootComment.hidden, true);
  assert.equal(page.reply.hidden, true);
  assert.equal(page.unknownComment.hidden, false);
  assert.equal(page.danmaku.hidden, false);
  assert.equal(observer.observed, true);
});

test("async comments are filtered after re-render and become visible after revocation", () => {
  const page = createCommentPage();
  let observer;
  const controller = createCommentFilterController({
    root: page.document,
    cache: createEmptyUidCache(),
    observerFactory: (callback) => {
      observer = new FakeMutationObserver(callback);
      return observer;
    },
  });

  controller.start();
  const lateComment = createComment("1001");
  page.commentSection.append(lateComment);
  controller.setCache(applyUidSync(createEmptyUidCache(), {
    version: 2,
    mode: "snapshot",
    records: [{
      uid: "1001",
      nicknameSnapshot: "目标账号",
      status: "review",
      hidden: true,
      updatedAt: "2026-08-09T00:00:00Z",
    }],
  }));
  observer.emit([{ type: "childList", addedNodes: [lateComment] }]);
  assert.equal(lateComment.hidden, true);

  controller.setCache(createEmptyUidCache());
  const reRenderedComment = createComment("1001");
  page.commentSection.append(reRenderedComment);
  observer.emit([{ type: "childList", addedNodes: [reRenderedComment] }]);
  assert.equal(reRenderedComment.hidden, false);
  assert.equal(lateComment.hidden, false);
});

test("comment filtering traverses Bilibili open Shadow DOM and keeps danmaku untouched", () => {
  const document = new FakeElement({ tagName: "document" });
  const comments = new FakeElement({ tagName: "bili-comments" });
  const rootComment = createShadowComment("1001");
  const reply = createShadowComment("1001", { reply: true });
  const ordinary = createShadowComment("9001");
  const commentsShadow = new FakeElement({ tagName: "shadow-root" });
  commentsShadow.append(rootComment, reply, ordinary);
  comments.shadowRoot = commentsShadow;
  const danmaku = new FakeElement({
    classes: ["danmaku"],
    attributes: { "data-user-id": "1001" },
  });
  document.append(comments, danmaku);

  const cache = applyUidSync(createEmptyUidCache(), {
    version: 1,
    mode: "snapshot",
    records: [{
      uid: "1001",
      nicknameSnapshot: "目标账号",
      status: "hidden",
      hidden: true,
      updatedAt: "2026-08-09T00:00:00Z",
    }],
  });

  const controller = createCommentFilterController({
    root: document,
    cache,
    observerFactory: (callback) => new FakeMutationObserver(callback),
  });
  const stats = controller.start();

  assert.equal(stats.scanned, 3);
  assert.equal(stats.hidden, 2);
  assert.equal(rootComment.hidden, true);
  assert.equal(reply.hidden, true);
  assert.equal(ordinary.hidden, false);
  assert.equal(danmaku.hidden, false);
});
