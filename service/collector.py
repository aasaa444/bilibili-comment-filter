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
    declared_total: int | None = None
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
    pause_reason: str | None = None


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


class BilibiliTransportError(RuntimeError):
    """A Bilibili response failed without exposing session material."""

    def __init__(self, code: int | str, message: str, *, category: str = "api") -> None:
        self.code = code
        self.category = category
        self.detail = message
        super().__init__(message)


class BilibiliRateLimitError(BilibiliTransportError):
    """Bilibili rejected a request for rate, risk-control, or overload reasons."""


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
        min_interval: float = 1.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
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
        self._retry_backoff = max(0.0, retry_backoff)
        self._last_request = 0.0

    def fetch_root_page(self, video_id: str, page: int) -> dict[str, Any]:
        oid = self._resolve_oid(video_id)
        return self._get(
            "/x/v2/reply/main",
            {"type": 1, "oid": oid, "mode": 3, "next": page, "ps": 20},
            referer=f"https://www.bilibili.com/video/{video_id}/",
        )

    def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict[str, Any]:
        oid = self._resolve_oid(video_id)
        return self._get(
            "/x/v2/reply/reply",
            {"type": 1, "oid": oid, "root": root_id, "pn": page, "ps": 20},
            referer=f"https://www.bilibili.com/video/{video_id}/",
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

    def _get(
        self,
        path: str,
        params: dict[str, object],
        *,
        referer: str | None = None,
    ) -> dict[str, Any]:
        cookies = self.cookies_provider() or {}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": referer or "https://www.bilibili.com/",
            "User-Agent": self.user_agent,
        }
        if cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in cookies.items()
            )
        retryable_codes = {-352, -412, -509}
        for attempt in range(self._max_retries + 1):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request = time.monotonic()
            try:
                response = self.client.get(path, params=params, headers=headers)
            except httpx.RequestError as exc:
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise BilibiliTransportError(
                    "network",
                    f"Bilibili request failed: {exc}",
                    category="network",
                ) from exc
            if response.status_code in {403, 412, 429}:
                category = {
                    403: "http_blocked",
                    412: "http_rate_limit",
                    429: "http_rate_limit",
                }[response.status_code]
                raise BilibiliRateLimitError(
                    response.status_code,
                    f"Bilibili HTTP {response.status_code}",
                    category=category,
                )
            if response.status_code >= 500:
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise BilibiliTransportError(
                    response.status_code,
                    f"Bilibili HTTP {response.status_code}",
                    category="http_server_error",
                )
            if response.status_code >= 400:
                raise BilibiliTransportError(
                    response.status_code,
                    f"Bilibili HTTP {response.status_code}",
                    category="http_error",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise BilibiliTransportError(
                    "invalid_json",
                    "Bilibili comment request returned invalid JSON",
                    category="protocol",
                ) from exc
            if not isinstance(payload, dict):
                raise BilibiliTransportError(
                    "invalid_payload",
                    "Bilibili comment request returned an invalid JSON object",
                    category="protocol",
                )
            code = payload.get("code")
            if code == 0:
                return payload.get("data") or {}
            if code in {-101, -111}:
                raise BilibiliAuthenticationError(
                    payload.get("message") or "Bilibili session is no longer valid"
                )
            message = str(payload.get("message") or f"Bilibili code {code}")
            if code in {-352, -412}:
                raise BilibiliRateLimitError(code, message, category="api_rate_limit")
            if code in retryable_codes:
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise BilibiliRateLimitError(code, message, category="api_rate_limit")
            raise BilibiliTransportError(code, message)
        raise AssertionError("Bilibili request retry loop did not return or raise")

    def _sleep_before_retry(self, attempt: int) -> None:
        if self._retry_backoff:
            time.sleep(self._retry_backoff * (2**attempt))


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
        declared_total = checkpoint.declared_total
        pinned_comments = 0
        invalid_item = False
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
            except Exception as exc:
                failed_items.append(_format_failed_item(f"root_page:{root_page}", exc))
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
                        declared_total=declared_total,
                    ),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    sum(declared_reply_by_root.values()),
                    failed_items,
                    declared_total=declared_total,
                    pause_reason=_pause_reason(exc),
                )

            if not isinstance(payload, dict):
                failed_items.append(f"inconsistent_root_payload:{root_page}:not_object")
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
                        declared_total=declared_total,
                    ),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    sum(declared_reply_by_root.values()),
                    failed_items,
                    declared_total=declared_total,
                )
            if "replies" not in payload:
                failed_items.append(f"inconsistent_root_payload:{root_page}:missing_replies")
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
                        declared_total=declared_total,
                    ),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    sum(declared_reply_by_root.values()),
                    failed_items,
                    declared_total=declared_total,
                )
            metadata_errors = _metadata_errors(
                payload, f"inconsistent_root_metadata:{root_page}"
            )
            if metadata_errors:
                failed_items.extend(metadata_errors)
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
                        declared_total=declared_total,
                    ),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    sum(declared_reply_by_root.values()),
                    failed_items,
                    declared_total=declared_total,
                )
            raw_root_items = list(payload.get("replies") or [])
            invalid_item = invalid_item or len(raw_root_items) != sum(
                isinstance(item, dict) for item in raw_root_items
            )
            if invalid_item:
                failed_items.append(f"inconsistent_root_item:{root_page}:not_object")
            root_items = [item for item in raw_root_items if isinstance(item, dict)]
            root_items.extend(_pinned_items(payload))
            declared_comments = max(declared_comments, _declared_count(payload))
            source_total = _declared_total(payload)
            if source_total is not None:
                declared_total = max(declared_total or 0, source_total)
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
                        declared_total=declared_total,
                    ),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    sum(declared_reply_by_root.values()),
                    failed_items,
                    declared_total=declared_total,
                )

            if not root_items:
                failed_items.append(f"empty_root_page:{root_page}")

            for item in root_items:
                comment_id = self._comment_id(item)
                try:
                    comment = self._parse_comment(
                        task.video_id,
                        item,
                        root_id=comment_id,
                        parent_id=None,
                        level="root",
                    )
                except ValueError as exc:
                    invalid_item = True
                    failed_items.append(
                        f"inconsistent_root_item:{root_page}:{comment_id or 'unknown'}:{exc}"
                    )
                    continue
                failed_items.extend(self._comment_anomalies(comment, item))
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
                    except Exception as exc:
                        failed_items.append(
                            _format_failed_item(f"reply:{root_id}:{reply_page}", exc)
                        )
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
                                declared_total=declared_total,
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            sum(declared_reply_by_root.values()),
                            failed_items,
                            declared_total=declared_total,
                            pause_reason=_pause_reason(exc),
                        )
                    if not isinstance(reply_payload, dict):
                        failed_items.append(
                            f"inconsistent_reply_payload:{root_id}:{reply_page}:not_object"
                        )
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
                                declared_total=declared_total,
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            sum(declared_reply_by_root.values()),
                            failed_items,
                            declared_total=declared_total,
                        )
                    if "replies" not in reply_payload:
                        failed_items.append(
                            f"inconsistent_reply_payload:{root_id}:{reply_page}:missing_replies"
                        )
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
                                declared_total=declared_total,
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            sum(declared_reply_by_root.values()),
                            failed_items,
                            declared_total=declared_total,
                        )
                    metadata_errors = _metadata_errors(
                        reply_payload,
                        f"inconsistent_reply_metadata:{root_id}:{reply_page}",
                    )
                    if metadata_errors:
                        failed_items.extend(metadata_errors)
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
                                declared_total=declared_total,
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            sum(declared_reply_by_root.values()),
                            failed_items,
                            declared_total=declared_total,
                        )
                    raw_reply_items = list(reply_payload.get("replies") or [])
                    if len(raw_reply_items) != sum(
                        isinstance(item, dict) for item in raw_reply_items
                    ):
                        invalid_item = True
                        failed_items.append(
                            f"inconsistent_reply_item:{root_id}:{reply_page}:not_object"
                        )
                    reply_items = [item for item in raw_reply_items if isinstance(item, dict)]
                    if not reply_items:
                        failed_items.append(f"empty_reply_page:{root_id}:{reply_page}")
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
                                declared_total=declared_total,
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            sum(declared_reply_by_root.values()),
                            failed_items,
                            declared_total=declared_total,
                        )
                    for reply in reply_items:
                        comment_id = self._comment_id(reply)
                        try:
                            parsed = self._parse_comment(
                                task.video_id,
                                reply,
                                root_id=root_id,
                                parent_id=(
                                    str(reply.get("parent"))
                                    if reply.get("parent")
                                    else root_id
                                ),
                                level="reply",
                            )
                        except ValueError as exc:
                            invalid_item = True
                            failed_items.append(
                                f"inconsistent_reply_item:{root_id}:{reply_page}:"
                                f"{comment_id or 'unknown'}:{exc}"
                            )
                            continue
                        failed_items.extend(self._comment_anomalies(parsed, reply))
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
                    if _has_more(reply_payload, reply_page, item_count=len(reply_items)):
                        reply_page += 1
                        reply_pages[root_id] = reply_page
                        continue
                    reply_pages.pop(root_id, None)
                    break
            if _has_more(payload, root_page, item_count=len(raw_root_items)):
                root_page += 1
                root_cursor = _next_cursor(payload)
                continue
            total_declared = _total_declared(
                declared_comments, sum(declared_reply_by_root.values()), declared_total
            )
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
                    ) and not invalid_item,
                    requested_pages,
                    declared_comments,
                    declared_reply_by_root,
                    None,
                    declared_total=declared_total,
                ),
                requested_pages,
                pinned_comments,
                declared_comments,
                sum(declared_reply_by_root.values()),
                failed_items,
                declared_total=declared_total,
            )

    @staticmethod
    def _comment_id(item: dict[str, Any]) -> str:
        raw_id = item.get("rpid") or item.get("id")
        return str(raw_id) if raw_id not in (None, "") else ""

    @staticmethod
    def _parse_comment(
        video_id: str,
        item: dict[str, Any],
        *,
        root_id: str,
        parent_id: str | None,
        level: str,
    ) -> CommentRecord:
        raw_comment_id = item.get("rpid") or item.get("id")
        if raw_comment_id in (None, ""):
            raise ValueError("missing_comment_id")
        comment_id = str(raw_comment_id)
        member = item.get("member")
        if not isinstance(member, dict):
            member = {}
        content = item.get("content")
        if not isinstance(content, dict):
            content = {}
        raw_uid = item.get("mid") or member.get("mid")
        if raw_uid in (None, "", 0, "0"):
            raise ValueError("missing_uid")
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
            uid=str(raw_uid),
            nickname=member.get("uname") or item.get("nickname"),
            content=str(content.get("message") or item.get("message") or ""),
            video_id=video_id,
            comment_url=f"https://www.bilibili.com/video/{video_id}#reply{comment_id}",
            root_id=resolved_root,
            parent_id=resolved_parent,
            level=level,
            created_at=_created_at(item.get("ctime")),
            is_pinned=bool(item.get("is_top") or item.get("top")),
            context=context,
        )

    @staticmethod
    def _comment_anomalies(comment: CommentRecord, item: dict[str, Any]) -> tuple[str, ...]:
        anomalies: list[str] = []
        if not comment.nickname:
            anomalies.append(f"inconsistent_comment:{comment.comment_id}:missing_nickname")
        if (
            item.get("is_deleted")
            or item.get("deleted")
            or item.get("status") == -1
            or comment.content in {
                "[\u8be5\u8bc4\u8bba\u5df2\u5220\u9664]",
                "\u8be5\u8bc4\u8bba\u5df2\u5220\u9664",
            }
        ):
            anomalies.append(f"deleted_comment:{comment.comment_id}")
        return tuple(anomalies)

    @staticmethod
    def _result(
        comments: list[CommentRecord],
        checkpoint: CollectionCheckpoint,
        requested_pages: int,
        pinned_comments: int,
        declared_comments: int,
        declared_replies: int,
        failed_items: list[str],
        *,
        declared_total: int | None = None,
        pause_reason: str | None = None,
    ) -> CollectionResult:
        saved_comments = sum(comment.level == "root" for comment in comments)
        saved_replies = sum(comment.level == "reply" for comment in comments)
        total_saved = saved_comments + saved_replies
        total_declared = _total_declared(declared_comments, declared_replies, declared_total)
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
            complete=checkpoint.complete,
            failed_items=tuple(failed_items),
            pause_reason=pause_reason,
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
        *,
        declared_total: int | None = None,
    ) -> CollectionCheckpoint:
        return CollectionCheckpoint(
            root_page=root_page,
            reply_pages=dict(reply_pages),
            complete=complete,
            requested_pages=requested_pages,
            declared_comments=declared_comments,
            declared_total=declared_total,
            declared_reply_counts=dict(declared_reply_counts),
            root_cursor=root_cursor,
        )


def _declared_count(payload: dict[str, Any]) -> int:
    if payload.get("declared_count") is not None:
        return _coerce_integer(payload["declared_count"], 0) or 0
    page = payload.get("page")
    page = page if isinstance(page, dict) else {}
    page_count = _coerce_integer(page.get("count"), 0) or 0
    if page_count:
        return page_count
    cursor = payload.get("cursor")
    cursor = cursor if isinstance(cursor, dict) else {}
    return _coerce_integer(cursor.get("all_count"), 0) or 0


def _created_at(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_ctime") from exc


def _format_failed_item(prefix: str, error: Exception) -> str:
    if not isinstance(error, BilibiliTransportError):
        return prefix
    detail = " ".join(str(error.detail).split())[:160]
    return f"{prefix}:{error.category}:{error.code}:{detail}"


def _pause_reason(error: Exception) -> str | None:
    if not isinstance(error, BilibiliRateLimitError):
        return None
    return f"Bilibili collection paused after {error.category} ({error.code})"


def _metadata_errors(payload: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    for container_name in ("page", "cursor"):
        container = payload.get(container_name)
        if container is not None and not isinstance(container, dict):
            errors.append(f"{prefix}:{container_name}:not_object")
    for metadata_field in ("declared_count", "declared_total"):
        if metadata_field in payload and not _is_integer_value(payload[metadata_field]):
            errors.append(f"{prefix}:{metadata_field}:not_integer")
    page = payload.get("page")
    if isinstance(page, dict):
        for metadata_field in ("count", "acount", "page_count", "size"):
            if metadata_field in page and not _is_integer_value(page[metadata_field]):
                errors.append(f"{prefix}:page.{metadata_field}:not_integer")
    cursor = payload.get("cursor")
    if isinstance(cursor, dict):
        for metadata_field in ("all_count", "next"):
            if metadata_field in cursor and not _is_integer_value(cursor[metadata_field]):
                errors.append(f"{prefix}:cursor.{metadata_field}:not_integer")
        if "is_end" in cursor and not isinstance(cursor["is_end"], bool):
            errors.append(f"{prefix}:cursor.is_end:not_boolean")
    if "has_more" in payload and not isinstance(payload["has_more"], bool):
        errors.append(f"{prefix}:has_more:not_boolean")
    return errors


def _is_integer_value(value: Any) -> bool:
    if value in (None, ""):
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        try:
            int(value)
        except ValueError:
            return False
        return True
    return False


def _coerce_integer(value: Any, default: int | None) -> int | None:
    if not _is_integer_value(value) or value in (None, ""):
        return default
    return int(value)


def _declared_total(payload: dict[str, Any]) -> int | None:
    if payload.get("declared_total") is not None:
        return _coerce_integer(payload["declared_total"], None)
    cursor = payload.get("cursor")
    cursor = cursor if isinstance(cursor, dict) else {}
    if "all_count" in cursor and cursor.get("all_count") is not None:
        return _coerce_integer(cursor["all_count"], None)
    return None


def _total_declared(
    declared_comments: int, declared_replies: int, declared_total: int | None
) -> int:
    return declared_total if declared_total is not None else declared_comments + declared_replies


def _has_more(payload: dict[str, Any], page: int, *, item_count: int | None = None) -> bool:
    if "has_more" in payload:
        return bool(payload["has_more"])
    cursor = payload.get("cursor")
    cursor = cursor if isinstance(cursor, dict) else {}
    if "is_end" in cursor:
        next_cursor = _coerce_integer(cursor.get("next"), None)
        return not bool(cursor.get("is_end")) and (next_cursor is not None and next_cursor > 0)
    page_info = payload.get("page")
    page_info = page_info if isinstance(page_info, dict) else {}
    page_count = _coerce_integer(page_info.get("page_count"), 0) or 0
    if page_count:
        return page_count > page
    page_size = _coerce_integer(page_info.get("size"), 0) or 0
    total = _coerce_integer(page_info.get("count") or page_info.get("acount"), 0) or 0
    if page_size and total:
        return total > page * page_size
    return item_count is not None and item_count >= 20


def _next_cursor(payload: dict[str, Any]) -> int | None:
    cursor = payload.get("cursor")
    cursor = cursor if isinstance(cursor, dict) else {}
    value = cursor.get("next")
    return _coerce_integer(value, None)


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
