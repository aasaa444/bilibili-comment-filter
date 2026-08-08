import test from "node:test";
import assert from "node:assert/strict";
import {
  applyUidSync,
  createEmptyUidCache,
  deserializeUidCache,
  serializeUidCache,
  shouldHideUid,
} from "../../dist/shared/uid-cache.js";

const hiddenRecord = {
  uid: "1001",
  nicknameSnapshot: "目标账号",
  status: "hidden",
  hidden: true,
  updatedAt: "2026-08-09T00:00:00Z",
};

test("UID snapshot uses UID as key and does not hide exceptions or unknown users", () => {
  const cache = applyUidSync(createEmptyUidCache(), {
    version: 3,
    mode: "snapshot",
    records: [
      hiddenRecord,
      { ...hiddenRecord, uid: "1002", status: "exception", hidden: false },
    ],
  });

  assert.equal(cache.version, 3);
  assert.equal(shouldHideUid(cache, "1001"), true);
  assert.equal(shouldHideUid(cache, "1002"), false);
  assert.equal(shouldHideUid(cache, "9999"), false);
  assert.deepEqual(cache.records["1001"], hiddenRecord);
});

test("UID delta is applied once, while an older response cannot roll back the cache", () => {
  const initial = applyUidSync(createEmptyUidCache(), {
    version: 3,
    mode: "snapshot",
    records: [hiddenRecord],
  });
  const updated = applyUidSync(initial, {
    version: 4,
    mode: "delta",
    upserts: [{ ...hiddenRecord, uid: "2001", status: "review" }],
    removals: ["1001"],
  });
  const stale = applyUidSync(updated, {
    version: 3,
    mode: "delta",
    upserts: [hiddenRecord],
  });

  assert.equal(updated.version, 4);
  assert.equal(shouldHideUid(updated, "1001"), false);
  assert.equal(shouldHideUid(updated, "2001"), true);
  assert.equal(stale.version, 4);
  assert.equal(shouldHideUid(stale, "1001"), false);
});

test("UID cache survives storage serialization for offline filtering", () => {
  const cache = applyUidSync(createEmptyUidCache(), {
    version: 7,
    mode: "snapshot",
    records: [hiddenRecord],
  });

  assert.deepEqual(deserializeUidCache(serializeUidCache(cache)), cache);
});
