import test from "node:test";
import assert from "node:assert/strict";
import { getVideoIdentity, isSupportedVideoUrl } from "../../dist/extension/src/video.js";

test("only ordinary desktop Bilibili BV video URLs are accepted", () => {
  assert.equal(isSupportedVideoUrl("https://www.bilibili.com/video/BV1abCdefGh1"), true);
  assert.equal(isSupportedVideoUrl("https://www.bilibili.com/video/BV1abCdefGh1?p=2"), true);
  assert.equal(isSupportedVideoUrl("https://m.bilibili.com/video/BV1abCdefGh1"), false);
  assert.equal(isSupportedVideoUrl("https://www.bilibili.com/read/cv123"), false);
  assert.equal(isSupportedVideoUrl("https://www.bilibili.com/video/av123"), false);
});

test("video identity keeps the stable BV id and display title", () => {
  assert.deepEqual(
    getVideoIdentity("https://www.bilibili.com/video/BV1abCdefGh1?from_spmid=main", "标题来自页面"),
    {
      bvid: "BV1abCdefGh1",
      url: "https://www.bilibili.com/video/BV1abCdefGh1",
      title: "标题来自页面",
    },
  );
  assert.equal(getVideoIdentity("https://www.bilibili.com/video/av123", "不支持"), null);
});
