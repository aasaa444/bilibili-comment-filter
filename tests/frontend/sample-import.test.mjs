import test from "node:test";
import assert from "node:assert/strict";
import { mergeSampleItems, parseSampleText } from "../../dist/web/src/sample-import.js";

test("sample text preview trims blank lines and deduplicates within the selected kind", () => {
  assert.deepEqual(
    parseSampleText("  第一条  \n\n第二条\n第一条\n# 注释", "comment-positive"),
    [
      { text: "第一条", kind: "comment-positive", source: "manual" },
      { text: "第二条", kind: "comment-positive", source: "manual" },
    ],
  );
});

test("sample merge preserves different labels while removing exact duplicates", () => {
  assert.deepEqual(
    mergeSampleItems(
      [{ text: "同一表达", kind: "comment-positive", source: "manual" }],
      [
        { text: "同一表达", kind: "comment-positive", source: "file" },
        { text: "同一表达", kind: "comment-negative", source: "file" },
      ],
    ),
    [
      { text: "同一表达", kind: "comment-positive", source: "manual" },
      { text: "同一表达", kind: "comment-negative", source: "file" },
    ],
  );
});
