import test from "node:test";
import assert from "node:assert/strict";
import {
  connectionStateFromHealth,
  connectionStateFromRequestError,
  taskViewState,
} from "../../dist/shared/state.js";

test("connection state only becomes ready for an explicit ready health response", () => {
  assert.equal(connectionStateFromHealth({ status: "ready" }).kind, "ready");
  assert.equal(connectionStateFromHealth({ status: "degraded", detail: "worker unavailable" }).kind, "error");
  assert.equal(connectionStateFromHealth({ status: "unavailable" }).kind, "offline");
  assert.equal(connectionStateFromHealth(null).kind, "error");
});

test("HTTP failures expose permission denied separately and retain recovery text", () => {
  assert.equal(connectionStateFromRequestError({ status: 403, message: "需要授权" }).kind, "permission-denied");
  assert.equal(connectionStateFromRequestError({ status: 503, message: "服务不可用" }).kind, "offline");
  assert.equal(connectionStateFromRequestError({ status: 500, message: "请求失败" }).kind, "error");
});

test("task states preserve queued, processing, ready, error and paused semantics", () => {
  assert.equal(taskViewState("queued").kind, "queued");
  assert.equal(taskViewState("processing").kind, "processing");
  assert.equal(taskViewState("ready").kind, "ready");
  assert.equal(taskViewState("partial").kind, "ready");
  assert.equal(taskViewState("failed").kind, "error");
  assert.equal(taskViewState("paused").kind, "paused");
});
