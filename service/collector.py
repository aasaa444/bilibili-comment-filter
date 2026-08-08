from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import httpx

from .tasks import VideoTask

DEFAULT_BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class CommentRecord:
    comment_id: str
    uid: str
    nickname: str | None
    content: str
    video_id: str
    comment_url: str
    root_id: str
    parent_id: str | None
    level: str
    created_at: int | None
    is_pinned: bool
    context: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectionCheckpoint:
    root_page: int = 1
    reply_pages: dict[str, int] = field(default_factory=dict)
    complete: bool = False
    requested_pages: int = 0
    declared_comments: int = 0
    declared_reply_counts: dict[str, int] = field(default_factory=dict)
    root_cursor: int | None = None


@dataclass(frozen=True)
class CollectionStats:
    requested_pages: int = 0
    saved_comments: int = 0
    saved_replies: int = 0
    pinned_comments: int = 0
    declared_comments: int = 0
    declared_replies: int = 0
    coverage: float = 0.0


@dataclass(frozen=True)
class CollectionResult:
    comments: tuple[CommentRecord, ...]
    checkpoint: CollectionCheckpoint
    stats: CollectionStats
    complete: bool
    failed_items: tuple[str, ...] = ()


class CommentCollector(Protocol):
    def collect(self, task: VideoTask, checkpoint: CollectionCheckpoint) -> CollectionResult:
        """Collect comments through a replaceable external protocol boundary."""


class CommentTransport(Protocol):
    def fetch_root_page(self, video_id: str, page: int) -> dict[str, Any]:
        """Fetch one first-level comment page."""

    def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict[str, Any]:
        """Fetch one reply page for a root comment."""


class BilibiliAuthenticationError(RuntimeError):
    """The comment endpoint rejected the current session as unauthenticated."""


class BilibiliCommentTransport:
    """HTTP boundary for Bilibili's public comment read endpoints.

    The transport only reads comment pages. Authentication cookies are supplied by the
    service-owned session provider and are never returned by this adapter.
    """

    root_cursor_mode = True

    def __init__(
        self,
        cookies_provider: Callable[[], dict[str, str] | None],
        *,
        base_url: str = "https://api.bilibili.com",
        timeout: float = 20.0,
        min_interval: float = 0.5,
        max_retries: int = 3,
        client: httpx.Client | None = None,
        user_agent: str = DEFAULT_BILIBILI_USER_AGENT,
    ) -> None:
        self.cookies_provider = cookies_provider
        self.user_agent = user_agent
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.bilibili.com/",
                "User-Agent": self.user_agent,
            },
        )
        self._video_oid_cache: dict[str, str] = {}
        self._min_interval = max(0.0, min_interval)
        self._max_retries = max(0, max_retries)
        self._last_request = 0.0

    def fetch_root_page(self, video_id: str, page: int) -> dict[str, Any]:
        oid = self._resolve_oid(video_id)
        return self._get(
            "/x/v2/reply/main",
            {"type": 1, "oid": oid, "mode": 3, "next": page, "ps": 20},
        )

    def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict[str, Any]:
        oid = self._resolve_oid(video_id)
        return self._get(
            "/x/v2/reply/reply",
            {"type": 1, "oid": oid, "root": root_id, "pn": page, "ps": 20},
        )

    def _resolve_oid(self, video_id: str) -> str:
        if not video_id.upper().startswith("BV"):
            return video_id
        cached = self._video_oid_cache.get(video_id)
        if cached:
            return cached
        payload = self._get("/x/web-interface/view", {"bvid": video_id})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        aid = data.get("aid") if isinstance(data, dict) else None
        if aid is None:
            raise RuntimeError("Bilibili video metadata did not contain an aid")
        resolved = str(aid)
        self._video_oid_cache[video_id] = resolved
        return resolved

    def _get(self, path: str, params: dict[str, object]) -> dict[str, Any]:
        cookies = self.cookies_provider() or {}
        headers = {"User-Agent": self.user_agent}
        if cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in cookies.items()
            )
        retryable_codes = {-352, -412, -509}
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request = time.monotonic()
            response = self.client.get(path, params=params, headers=headers)
            if response.status_code == 412 or response.status_code >= 500:
                last_error = RuntimeError(f"Bilibili HTTP {response.status_code}")
                if attempt < self._max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                response.raise_for_status()
            response.raise_for_status()
            payload = response.json()
            code = payload.get("code")
            if code == 0:
                return payload.get("data") or {}
            if code == -101:
                raise BilibiliAuthenticationError(
                    payload.get("message") or "Bilibili session is no longer valid"
                )
            if code in retryable_codes and attempt < self._max_retries:
                last_error = RuntimeError(payload.get("message") or f"Bilibili code {code}")
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(payload.get("message") or "Bilibili comment request failed")
        raise RuntimeError(f"Bilibili request failed: {last_error}") from last_error


class BilibiliCommentCollector:
    def __init__(self, transport: CommentTransport) -> None:
        self.transport = transport

    def collect(self, task: VideoTask, checkpoint: CollectionCheckpoint) -> CollectionResult:
        comments: list[CommentRecord] = []
        seen_ids: set[str] = set()
        comment_indexes: dict[str, int] = {}
        failed_items: list[str] = []
        reply_pages = dict(checkpoint.reply_pages)
        declared_reply_by_root = dict(checkpoint.declared_reply_counts)
        root_page = checkpoint.root_page
        root_cursor = checkpoint.root_cursor
        requested_pages = checkpoint.requested_pages
        declared_comments = checkpoint.declared_comments
        pinned_comments = 0
        fresh_checkpoint = (
            checkpoint.root_page == 1
            and checkpoint.root_cursor is None
            and not checkpoint.reply_pages
            and checkpoint.requested_pages == 0
        )

        while True:
            requested_pages += 1
            try:
                request_page = (
                    root_cursor
                    if root_cursor is not None
                    else 0
                    if getattr(self.transport, "root_cursor_mode", False)
                    else root_page
                )
                payload = self.transport.fetch_root_page(task.video_id, request_page)
            except BilibiliAuthenticationError:
                raise
            except Exception:
                failed_items.append(f"root_page:{root_page}")
                return self._result(
                    comments,
                    self._checkpoint(
                        root_page,
                        reply_pages,
                        False,
                        requested_pages,
                        declared_comments,
                        declared_reply_by_root,
                        root_cursor,
                    ),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    sum(declared_reply_by_root.values()),
                    failed_items,
                )

            root_items = list(payload.get("replies") or [])
            root_items.extend(_pinned_items(payload))
            declared_comments = max(declared_comments, _declared_count(payload))
            root_ids = [self._comment_id(item) for item in root_items]
            if root_ids and all(comment_id in seen_ids for comment_id in root_ids):
                failed_items.append(f"duplicate_root_page:{root_page}")
                return self._result(
                    comments,
                    self._checkpoint(
                        root_page,
                        reply_pages,
                        False,
                        requested_pages,
                        declared_comments,
                        declared_reply_by_root,
                        root_cursor,
                    ),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    sum(declared_reply_by_root.values()),
                    failed_items,
                )

            for item in root_items:
                comment = self._parse_comment(
                    task.video_id,
                    item,
                    root_id=self._comment_id(item),
                    parent_id=None,
                    level="root",
                )
                if comment.comment_id in seen_ids:
                    index = comment_indexes[comment.comment_id]
                    existing = comments[index]
                    if comment.is_pinned and not existing.is_pinned:
                        comments[index] = replace(existing, is_pinned=True)
                        pinned_comments += 1
                    continue
                seen_ids.add(comment.comment_id)
                comment_indexes[comment.comment_id] = len(comments)
                comments.append(comment)
                pinned_comments += int(comment.is_pinned)
                root_id = comment.comment_id
                if int(item.get("rcount") or 0) <= 0 and not item.get("replies"):
                    continue
                reply_page = reply_pages.get(root_id, 1)
                while True:
                    try:
                        requested_pages += 1
                        reply_payload = self.transport.fetch_replies(
                            task.video_id, root_id, reply_page
                        )
                    except BilibiliAuthenticationError:
                        raise
                    except Exception:
                        failed_items.append(f"reply:{root_id}:{reply_page}")
                        return self._result(
                            comments,
                            self._checkpoint(
                                root_page,
                                {**reply_pages, root_id: reply_page},
                                False,
                                requested_pages,
                                declared_comments,
                                declared_reply_by_root,
                                root_cursor,
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            sum(declared_reply_by_root.values()),
                            failed_items,
                        )
                    reply_items = list(reply_payload.get("replies") or [])
                    declared_reply_by_root[root_id] = max(
                        declared_reply_by_root.get(root_id, 0), _declared_count(reply_payload)
                    )
                    reply_ids = [self._comment_id(reply) for reply in reply_items]
                    if reply_ids and all(reply_id in seen_ids for reply_id in reply_ids):
                        failed_items.append(f"duplicate_reply_page:{root_id}:{reply_page}")
                        return self._result(
                            comments,
                            self._checkpoint(
                                root_page,
                                {**reply_pages, root_id: reply_page},
                                False,
                                requested_pages,
                                declared_comments,
                                declared_reply_by_root,
                                root_cursor,
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            sum(declared_reply_by_root.values()),
                            failed_items,
                        )
                    for reply in reply_items:
                        parsed = self._parse_comment(
                            task.video_id,
                            reply,
                            root_id=root_id,
                            parent_id=str(reply.get("parent")) if reply.get("parent") else root_id,
                            level="reply",
                        )
                        if parsed.comment_id in seen_ids:
                            index = comment_indexes[parsed.comment_id]
                            existing = comments[index]
                            if parsed.is_pinned and not existing.is_pinned:
                                comments[index] = replace(existing, is_pinned=True)
                                pinned_comments += 1
                            continue
                        seen_ids.add(parsed.comment_id)
                        comment_indexes[parsed.comment_id] = len(comments)
                        comments.append(parsed)
                    if _has_more(reply_payload, reply_page):
                        reply_page += 1
                        reply_pages[root_id] = reply_page
                        continue
                    reply_pages.pop(root_id, None)
                    break
            if _has_more(payload, root_page):
                root_page += 1
                root_cursor = _next_cursor(payload)
                continue
            total_declared = declared_comments + sum(declared_reply_by_root.values())
            total_saved = len(comments)
            return self._result(
                comments,
                self._checkpoint(
                    root_page + 1,
                    reply_pages,
                    (
                        not fresh_checkpoint
                        or not total_declared
                        or total_saved >= total_declared
                    ),
                    requested_pages,
                    declared_comments,
                    declared_reply_by_root,
                    None,
                ),
                requested_pages,
                pinned_comments,
                declared_comments,
                sum(declared_reply_by_root.values()),
                failed_items,
            )

    @staticmethod
    def _comment_id(item: dict[str, Any]) -> str:
        return str(item.get("rpid") or item.get("id"))

    @staticmethod
    def _parse_comment(
        video_id: str,
        item: dict[str, Any],
        *,
        root_id: str,
        parent_id: str | None,
        level: str,
    ) -> CommentRecord:
        member = item.get("member") or {}
        content = item.get("content") or {}
        comment_id = str(item.get("rpid") or item.get("id"))
        raw_root = item.get("root")
        raw_parent = item.get("parent")
        resolved_root = str(raw_root) if raw_root not in (None, 0, "0") else root_id
        resolved_parent = str(raw_parent) if raw_parent not in (None, 0, "0") else parent_id
        context = tuple(
            value
            for value in (item.get("root_content"), item.get("parent_content"))
            if isinstance(value, str) and value
        )
        return CommentRecord(
            comment_id=comment_id,
            uid=str(item.get("mid") or member.get("mid") or "0"),
            nickname=member.get("uname") or item.get("nickname"),
            content=str(content.get("message") or item.get("message") or ""),
            video_id=video_id,
            comment_url=f"https://www.bilibili.com/video/{video_id}#reply{comment_id}",
            root_id=resolved_root,
            parent_id=resolved_parent,
            level=level,
            created_at=int(item["ctime"]) if item.get("ctime") is not None else None,
            is_pinned=bool(item.get("is_top") or item.get("top")),
            context=context,
        )

    @staticmethod
    def _result(
        comments: list[CommentRecord],
        checkpoint: CollectionCheckpoint,
        requested_pages: int,
        pinned_comments: int,
        declared_comments: int,
        declared_replies: int,
        failed_items: list[str],
    ) -> CollectionResult:
        saved_comments = sum(comment.level == "root" for comment in comments)
        saved_replies = sum(comment.level == "reply" for comment in comments)
        total_saved = saved_comments + saved_replies
        total_declared = declared_comments + declared_replies
        coverage = (
            min(1.0, total_saved / total_declared) if total_declared else float(bool(total_saved))
        )
        return CollectionResult(
            comments=tuple(comments),
            checkpoint=checkpoint,
            stats=CollectionStats(
                requested_pages=requested_pages,
                saved_comments=saved_comments,
                saved_replies=saved_replies,
                pinned_comments=pinned_comments,
                declared_comments=declared_comments,
                declared_replies=declared_replies,
                coverage=coverage,
            ),
            complete=checkpoint.complete and not failed_items,
            failed_items=tuple(failed_items),
        )

    @staticmethod
    def _checkpoint(
        root_page: int,
        reply_pages: dict[str, int],
        complete: bool,
        requested_pages: int,
        declared_comments: int,
        declared_reply_counts: dict[str, int],
        root_cursor: int | None = None,
    ) -> CollectionCheckpoint:
        return CollectionCheckpoint(
            root_page=root_page,
            reply_pages=dict(reply_pages),
            complete=complete,
            requested_pages=requested_pages,
            declared_comments=declared_comments,
            declared_reply_counts=dict(declared_reply_counts),
            root_cursor=root_cursor,
        )


def _declared_count(payload: dict[str, Any]) -> int:
    if payload.get("declared_count") is not None:
        return int(payload["declared_count"])
    page = payload.get("page") or {}
    page_count = int(page.get("count") or 0)
    if page_count:
        return page_count
    cursor = payload.get("cursor") or {}
    return int(cursor.get("all_count") or 0)


def _has_more(payload: dict[str, Any], page: int) -> bool:
    if "has_more" in payload:
        return bool(payload["has_more"])
    cursor = payload.get("cursor") or {}
    if "is_end" in cursor:
        return not bool(cursor.get("is_end")) and cursor.get("next") is not None
    page_info = payload.get("page") or {}
    page_count = int(page_info.get("page_count") or 0)
    if page_count:
        return page_count > page
    page_size = int(page_info.get("size") or 0)
    total = int(page_info.get("count") or page_info.get("acount") or 0)
    return bool(page_size and total and total > page * page_size)


def _next_cursor(payload: dict[str, Any]) -> int | None:
    cursor = payload.get("cursor") or {}
    value = cursor.get("next")
    return int(value) if value is not None else None


def _pinned_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("top", "top_replies"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    upper = payload.get("upper") or {}
    if isinstance(upper, dict) and isinstance(upper.get("top"), dict):
        candidates.append(upper["top"])
    return [{**item, "is_top": True} for item in candidates]
