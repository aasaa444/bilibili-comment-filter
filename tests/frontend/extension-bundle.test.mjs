import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("built content script is executable as a classic Chrome content script", async () => {
  const contentScript = await readFile(
    path.join(projectRoot, "dist/extension/src/content-script.js"),
    "utf8",
  );

  assert.doesNotThrow(
    () => new Function(contentScript),
    "Chrome content_scripts cannot parse an ES module import",
  );
  assert.doesNotMatch(contentScript, /^\s*import\s/m);
  assert.doesNotMatch(contentScript, /^\s*export\s/m);
});
