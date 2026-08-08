import { createEmptyUidCache, type UidCache } from "../../shared/uid-cache.js";
import type { RuntimeResponse } from "./messages.js";
import { createCommentFilterController } from "./comment-filter.js";
import { getVideoIdentity } from "./video.js";

const video = getVideoIdentity(window.location.href, document.title);
if (video) {
  const controller = createCommentFilterController({ root: document, cache: createEmptyUidCache() });
  controller.start();

  chrome.runtime.onMessage.addListener((message: unknown) => {
    if (!isCacheUpdate(message)) return false;
    controller.setCache(message.cache);
    return false;
  });

  void requestInitialCache(controller);
}

async function requestInitialCache(controller: ReturnType<typeof createCommentFilterController>): Promise<void> {
  const response = await new Promise<RuntimeResponse | undefined>((resolve) => {
    chrome.runtime.sendMessage({ type: "GET_UID_CACHE" }, (result: RuntimeResponse | undefined) => {
      resolve(result);
    });
  });
  if (response?.ok && "cache" in response) controller.setCache(response.cache);
}

function isCacheUpdate(value: unknown): value is { type: "UID_CACHE_UPDATED"; cache: UidCache } {
  if (!value || typeof value !== "object") return false;
  const candidate = value as { type?: unknown; cache?: unknown };
  return candidate.type === "UID_CACHE_UPDATED" && Boolean(candidate.cache);
}
