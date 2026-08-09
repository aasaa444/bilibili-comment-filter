import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const root = process.cwd();
const dist = path.join(root, "dist");
const webDist = path.join(dist, "web");
const extensionDist = path.join(dist, "extension");

await mkdir(webDist, { recursive: true });
await mkdir(extensionDist, { recursive: true });
await cp(path.join(dist, "shared"), path.join(webDist, "shared"), { recursive: true });
await cp(path.join(dist, "shared"), path.join(extensionDist, "shared"), { recursive: true });

const webIndex = await readFile(path.join(root, "web", "index.html"), "utf8");
await writeFile(
  path.join(webDist, "index.html"),
  webIndex.replace("../dist/web/src/main.js", "./src/main.js"),
  "utf8",
);
await cp(path.join(root, "web", "styles.css"), path.join(webDist, "styles.css"));
await cp(path.join(root, "extension", "manifest.json"), path.join(extensionDist, "manifest.json"));
await cp(path.join(root, "extension", "popup.html"), path.join(extensionDist, "popup.html"));
await cp(path.join(root, "extension", "popup.css"), path.join(extensionDist, "popup.css"));

const contentScriptModules = [
  path.join(dist, "shared", "uid-cache.js"),
  path.join(dist, "extension", "src", "comment-filter.js"),
  path.join(dist, "extension", "src", "video.js"),
  path.join(dist, "extension", "src", "content-script.js"),
];
const contentScriptSources = await Promise.all(
  contentScriptModules.map(async (modulePath) => {
    const source = await readFile(modulePath, "utf8");
    return source
      .replace(/^import\s[^;]+;\s*$/gm, "")
      .replace(/^export\s+/gm, "");
  }),
);
await writeFile(
  path.join(extensionDist, "src", "content-script.js"),
  `(() => {\n${contentScriptSources.join("\n\n")}\n})();\n`,
  "utf8",
);
