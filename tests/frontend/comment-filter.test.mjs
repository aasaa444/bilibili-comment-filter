import test from "node:test";
import assert from "node:assert/strict";
import { applyUidSync, createEmptyUidCache } from "../../dist/shared/uid-cache.js";
import { createCommentFilterController } from "../../dist/extension/src/comment-filter.js";
import {
  FakeElement,
  FakeMutationObserver,
  createComment,
  createBilibiliCommentsFixture,
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

test("comment filtering traverses the observed nested Bilibili Shadow DOM and keeps danmaku untouched", () => {
  const document = new FakeElement({ tagName: "document" });
  const blockedUid = String(900_000_001);
  const ordinaryUid = String(900_000_002);
  const fixture = createBilibiliCommentsFixture({ blockedUid, ordinaryUid });
  const danmaku = new FakeElement({
    classes: ["danmaku"],
    attributes: { "data-user-id": blockedUid },
  });
  document.append(fixture.comments, danmaku);

  assert.equal(fixture.comments.shadowRoot.querySelector("bili-comment-thread-renderer"), fixture.thread);
  assert.equal(fixture.thread.shadowRoot.querySelector("bili-comment-renderer"), fixture.rootComment);
  assert.equal(fixture.thread.shadowRoot.querySelector("bili-comment-reply-renderer"), fixture.reply);
  const rootAvatar = fixture.rootComment.shadowRoot.querySelector("[data-user-profile-id]");
  const replyAvatar = fixture.reply.shadowRoot.querySelector("[data-user-profile-id]");
  assert.equal(rootAvatar.tagName, "A");
  assert.equal(rootAvatar.id, "user-avatar");
  assert.equal(rootAvatar.getAttribute("data-user-profile-id"), blockedUid);
  assert.equal(replyAvatar.id, "user-avatar");
  assert.equal(replyAvatar.getAttribute("data-user-profile-id"), blockedUid);

  const cache = applyUidSync(createEmptyUidCache(), {
    version: 1,
    mode: "snapshot",
    records: [{
      uid: blockedUid,
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
  assert.equal(fixture.rootComment.hidden, true);
  assert.equal(fixture.reply.hidden, true);
  assert.equal(fixture.ordinary.hidden, false);
  assert.equal(danmaku.hidden, false);
});

test("async comments in a newly mounted nested Shadow DOM are filtered and observed", () => {
  const document = new FakeElement({ tagName: "document" });
  const blockedUid = String(900_000_011);
  const ordinaryUid = String(900_000_012);
  const fixture = createBilibiliCommentsFixture({ blockedUid, ordinaryUid });
  document.append(fixture.comments);

  const cache = applyUidSync(createEmptyUidCache(), {
    version: 1,
    mode: "snapshot",
    records: [{
      uid: blockedUid,
      nicknameSnapshot: "目标账号",
      status: "hidden",
      hidden: true,
      updatedAt: "2026-08-09T00:00:00Z",
    }],
  });
  const observers = [];
  const controller = createCommentFilterController({
    root: document,
    cache,
    observerFactory: (callback) => {
      const observer = new FakeMutationObserver(callback);
      observers.push(observer);
      return observer;
    },
  });

  controller.start();
  const commentsShadowObserver = observers.find(
    (observer) => observer.target === fixture.comments.shadowRoot,
  );
  assert.ok(commentsShadowObserver);

  const lateThread = new FakeElement({ tagName: "bili-comment-thread-renderer" });
  const lateThreadShadow = new FakeElement({ tagName: "shadow-root" });
  const lateComment = createShadowComment(blockedUid);
  lateThreadShadow.append(lateComment);
  lateThread.shadowRoot = lateThreadShadow;
  fixture.comments.shadowRoot.append(lateThread);

  commentsShadowObserver.emit([{ type: "childList", addedNodes: [lateThread] }]);

  assert.equal(lateComment.hidden, true);
  const lateThreadObserver = observers.find((observer) => observer.target === lateThreadShadow);
  assert.ok(lateThreadObserver);

  const secondLateComment = createShadowComment(blockedUid, { reply: true });
  lateThreadShadow.append(secondLateComment);
  lateThreadObserver.emit([{ type: "childList", addedNodes: [secondLateComment] }]);

  assert.equal(secondLateComment.hidden, true);
});
