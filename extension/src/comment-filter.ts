import type { UidCache } from "../../shared/uid-cache.js";
import { shouldHideUid } from "../../shared/uid-cache.js";

const COMMENT_AREA_SELECTOR =
  "#comment, #comment-container, .comment-container, [data-comment-section=\"comments\"], bili-comments";
const COMMENT_NODE_SELECTOR = [
  "bili-comment-renderer",
  "bili-comment-reply-renderer",
  "#comment .reply-item",
  "#comment .sub-reply-item",
  "#comment [data-comment-id]",
  "#comment-container .reply-item",
  "#comment-container .sub-reply-item",
  "#comment-container [data-comment-id]",
  ".comment-container .reply-item",
  ".comment-container .sub-reply-item",
  ".comment-container [data-comment-id]",
  "[data-comment-section=\"comments\"] .reply-item",
  "[data-comment-section=\"comments\"] .sub-reply-item",
  "[data-comment-section=\"comments\"] [data-comment-id]",
].join(", ");
const BILIBILI_COMMENT_TAGS = new Set([
  "bili-comment-renderer",
  "bili-comment-reply-renderer",
]);
const USER_SELECTOR = [
  "[data-user-id]",
  "[data-uid]",
  "[data-mid]",
  "[data-user-card-mid]",
  "[data-user-profile-id]",
  "a[href*=\"space.bilibili.com/\"]",
].join(", ");
const FILTER_MARKER = "data-bcf-filter-hidden";
const ORIGINAL_HIDDEN_MARKER = "data-bcf-original-hidden";
const ORIGINAL_ARIA_MARKER = "data-bcf-original-aria-hidden";

export interface MutationObserverLike {
  observe(target: Node, options?: MutationObserverInit): void;
  disconnect(): void;
}

export type ObserverFactory = (callback: MutationCallback) => MutationObserverLike;

export interface CommentFilterOptions {
  root: ParentNode;
  cache: UidCache;
  observerFactory?: ObserverFactory;
}

export interface FilterStats {
  scanned: number;
  hidden: number;
  restored: number;
}

export interface CommentFilterController {
  start(): FilterStats;
  stop(): void;
  refresh(): FilterStats;
  setCache(cache: UidCache): FilterStats;
}

export function createCommentFilterController(options: CommentFilterOptions): CommentFilterController {
  let cache = options.cache;
  let observers: MutationObserverLike[] = [];
  let observedRoots = new Set<Node>();
  const observerFactory = options.observerFactory ?? ((callback) => new MutationObserver(callback));

  const onMutations: MutationCallback = () => {
    refresh();
    observeOpenShadowRoots();
  };

  function start(): FilterStats {
    const stats = refresh();
    observeOpenShadowRoots();
    return stats;
  }

  function stop(): void {
    for (const observer of observers) observer.disconnect();
    observers = [];
    observedRoots = new Set<Node>();
  }

  function refresh(): FilterStats {
    return filterCommentDom(options.root, cache);
  }

  function setCache(nextCache: UidCache): FilterStats {
    cache = nextCache;
    return refresh();
  }

  function observeOpenShadowRoots(): void {
    for (const root of collectOpenRoots(options.root)) {
      if (observedRoots.has(root as Node)) continue;
      const observer = observerFactory(onMutations);
      observer.observe(root as Node, { childList: true, subtree: true });
      observers.push(observer);
      observedRoots.add(root as Node);
    }
  }

  return { start, stop, refresh, setCache };
}

export function filterCommentDom(root: ParentNode, cache: UidCache): FilterStats {
  const nodes = findCommentNodes(root);
  let hidden = 0;
  let restored = 0;
  for (const node of nodes) {
    const uid = extractCommentUid(node);
    const shouldBeHidden = uid !== null && shouldHideUid(cache, uid);
    if (shouldBeHidden) {
      if (hideNode(node)) hidden += 1;
    } else if (restoreNode(node)) {
      restored += 1;
    }
  }
  return { scanned: nodes.length, hidden, restored };
}

export function findCommentNodes(root: ParentNode): HTMLElement[] {
  const nodes = Array.from(
    new Set(
      collectOpenRoots(root).flatMap((queryRoot) =>
        Array.from(queryRoot.querySelectorAll<HTMLElement>(COMMENT_NODE_SELECTOR)),
      ),
    ),
  );
  return nodes.filter((node) => isSupportedCommentNode(node));
}

export function extractCommentUid(node: Element): string | null {
  const candidates = [
    node,
    ...collectOpenRoots(node).flatMap((queryRoot) => Array.from(queryRoot.querySelectorAll(USER_SELECTOR))),
  ];
  for (const candidate of candidates) {
    for (const attribute of [
      "data-user-id",
      "data-uid",
      "data-mid",
      "data-user-card-mid",
      "data-user-profile-id",
    ]) {
      const value = candidate.getAttribute(attribute);
      if (isUid(value)) return value;
    }
    const href = candidate.getAttribute("href");
    const linkMatch = href?.match(/space\.bilibili\.com\/(\d+)/i);
    if (linkMatch && isUid(linkMatch[1])) return linkMatch[1];
  }
  return null;
}

function isSupportedCommentNode(node: Element): node is HTMLElement {
  if (BILIBILI_COMMENT_TAGS.has(node.tagName.toLowerCase())) return true;
  if (!node.closest(COMMENT_AREA_SELECTOR)) return false;
  if (node.closest(".danmaku, #danmaku, [data-danmaku]")) return false;
  return true;
}

function collectOpenRoots(root: ParentNode): ParentNode[] {
  const roots: ParentNode[] = [root];
  const pending: ParentNode[] = [root];
  const ownShadowRoot = (root as ParentNode & { shadowRoot?: ShadowRoot | null }).shadowRoot;
  if (ownShadowRoot) {
    roots.push(ownShadowRoot);
    pending.push(ownShadowRoot);
  }
  while (pending.length > 0) {
    const current = pending.shift() as ParentNode;
    for (const element of Array.from(current.querySelectorAll<HTMLElement>("*"))) {
      if (!element.shadowRoot) continue;
      if (roots.includes(element.shadowRoot)) continue;
      roots.push(element.shadowRoot);
      pending.push(element.shadowRoot);
    }
  }
  return roots;
}

function hideNode(node: HTMLElement): boolean {
  if (node.hasAttribute(FILTER_MARKER)) return false;
  node.setAttribute(FILTER_MARKER, "true");
  node.setAttribute(ORIGINAL_HIDDEN_MARKER, String(node.hidden));
  const originalAriaHidden = node.getAttribute("aria-hidden");
  if (originalAriaHidden === null) {
    node.setAttribute(ORIGINAL_ARIA_MARKER, "__missing__");
  } else {
    node.setAttribute(ORIGINAL_ARIA_MARKER, originalAriaHidden);
  }
  node.hidden = true;
  node.setAttribute("aria-hidden", "true");
  return true;
}

function restoreNode(node: HTMLElement): boolean {
  if (!node.hasAttribute(FILTER_MARKER)) return false;
  node.hidden = node.getAttribute(ORIGINAL_HIDDEN_MARKER) === "true";
  const originalAriaHidden = node.getAttribute(ORIGINAL_ARIA_MARKER);
  if (originalAriaHidden === "__missing__" || originalAriaHidden === null) {
    node.removeAttribute("aria-hidden");
  } else {
    node.setAttribute("aria-hidden", originalAriaHidden);
  }
  node.removeAttribute(FILTER_MARKER);
  node.removeAttribute(ORIGINAL_HIDDEN_MARKER);
  node.removeAttribute(ORIGINAL_ARIA_MARKER);
  return true;
}

function isUid(value: string | null | undefined): value is string {
  return Boolean(value && /^\d+$/.test(value));
}
