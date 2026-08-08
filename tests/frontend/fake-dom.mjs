export class FakeElement {
  constructor({ tagName = "div", id = "", classes = [], attributes = {}, text = "", shadowRoot = null } = {}) {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.className = classes.join(" ");
    this.attributes = new Map(Object.entries(attributes));
    if (id) this.attributes.set("id", id);
    if (classes.length) this.attributes.set("class", this.className);
    this.children = [];
    this.parentElement = null;
    this.shadowRoot = shadowRoot;
    this.hidden = false;
    this.textContent = text;
  }

  append(...children) {
    for (const child of children) {
      child.parentElement = this;
      this.children.push(child);
    }
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "id") this.id = String(value);
    if (name === "class") this.className = String(value);
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  matches(selector) {
    return selector
      .split(",")
      .map((part) => part.trim())
      .some((part) => this.matchesSelectorPart(part));
  }

  matchesSelectorPart(selector) {
    const parts = selector.split(/\s+/).filter(Boolean);
    const target = parts.at(-1);
    if (!this.matchesSimple(target)) return false;
    let ancestor = this.parentElement;
    for (let index = parts.length - 2; index >= 0; index -= 1) {
      while (ancestor && !ancestor.matchesSimple(parts[index])) {
        ancestor = ancestor.parentElement;
      }
      if (!ancestor) return false;
      ancestor = ancestor.parentElement;
    }
    return true;
  }

  matchesSimple(selector) {
    if (selector === "*") return true;
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
    const attrMatch = selector.match(/^([^\[]+)?\[([^=\]]+)(?:=["']?([^\]"']+)["']?)?\]$/);
    if (attrMatch) {
      const [, tagName, name, expected] = attrMatch;
      if (tagName && this.tagName.toLowerCase() !== tagName.toLowerCase()) return false;
      if (!this.hasAttribute(name)) return false;
      return expected === undefined || this.getAttribute(name) === expected;
    }
    const classContains = selector.match(/^\[class\*=["']([^"']+)["']\]$/);
    if (classContains) return this.className.includes(classContains[1]);
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }

  closest(selector) {
    let current = this;
    while (current) {
      if (current.matches(selector)) return current;
      current = current.parentElement;
    }
    return null;
  }

  querySelectorAll(selector) {
    const found = [];
    const visit = (element) => {
      for (const child of element.children) {
        if (child.matches(selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

export function createComment(uid, { reply = false } = {}) {
  return new FakeElement({
    classes: [reply ? "sub-reply-item" : "reply-item"],
    attributes: {
      "data-comment-id": `${reply ? "reply" : "root"}-${uid}`,
      "data-user-id": uid,
    },
  });
}

export function createShadowComment(uid, { reply = false } = {}) {
  const comment = new FakeElement({
    tagName: reply ? "bili-comment-reply-renderer" : "bili-comment-renderer",
  });
  const shadowRoot = new FakeElement({ tagName: "shadow-root" });
  const user = new FakeElement({
    tagName: "a",
    id: "user-avatar",
    attributes: {
      "data-user-profile-id": uid,
      href: `//space.bilibili.com/${uid}`,
    },
  });
  shadowRoot.append(user);
  comment.shadowRoot = shadowRoot;
  return comment;
}

export function createBilibiliCommentsFixture({ blockedUid, ordinaryUid }) {
  const comments = new FakeElement({ tagName: "bili-comments" });
  const commentsShadow = new FakeElement({ tagName: "shadow-root" });
  const thread = new FakeElement({ tagName: "bili-comment-thread-renderer" });
  const threadShadow = new FakeElement({ tagName: "shadow-root" });
  const rootComment = createShadowComment(blockedUid);
  const reply = createShadowComment(blockedUid, { reply: true });
  const ordinary = createShadowComment(ordinaryUid);

  threadShadow.append(rootComment, reply, ordinary);
  thread.shadowRoot = threadShadow;
  commentsShadow.append(thread);
  comments.shadowRoot = commentsShadow;

  return { comments, thread, rootComment, reply, ordinary };
}

export class FakeMutationObserver {
  constructor(callback) {
    this.callback = callback;
    this.observed = false;
  }

  observe() {
    this.observed = true;
  }

  disconnect() {
    this.observed = false;
  }

  emit(mutations) {
    if (this.observed) this.callback(mutations, this);
  }
}
