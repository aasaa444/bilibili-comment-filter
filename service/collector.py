from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .tasks import VideoTask


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


class BilibiliCommentTransport:
    """HTTP boundary for Bilibili's public comment read endpoints.

    The transport only reads comment pages. Authentication cookies are supplied by the
    service-owned session provider and are never returned by this adapter.
    """

    def __init__(
        self,
        cookies_provider: Callable[[], dict[str, str] | None],
        *,
        base_url: str = "https://api.bilibili.com",
        timeout: float = 20.0,
    ) -> None:
        self.cookies_provider = cookies_provider
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._video_oid_cache: dict[str, str] = {}

    def fetch_root_page(self, video_id: str, page: int) -> dict[str, Any]:
        oid = self._resolve_oid(video_id)
        return self._get(
            "/x/v2/reply",
            {"type": 1, "oid": oid, "pn": page, "ps": 20, "sort": 2},
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
        response = self.client.get(path, params=params, cookies=cookies)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "Bilibili comment request failed")
        return payload.get("data") or {}


class BilibiliCommentCollector:
    def __init__(self, transport: CommentTransport) -> None:
        self.transport = transport

    def collect(self, task: VideoTask, checkpoint: CollectionCheckpoint) -> CollectionResult:
        comments: list[CommentRecord] = []
        seen_ids: set[str] = set()
        failed_items: list[str] = []
        reply_pages = dict(checkpoint.reply_pages)
        root_page = checkpoint.root_page
        requested_pages = 0
        declared_comments = 0
        declared_replies = 0
        pinned_comments = 0
        declared_reply_by_root: dict[str, int] = {}

        while True:
            requested_pages += 1
            try:
                payload = self.transport.fetch_root_page(task.video_id, root_page)
            except Exception:
                failed_items.append(f"root_page:{root_page}")
                return self._result(
                    comments,
                    CollectionCheckpoint(root_page, reply_pages, False),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    declared_replies,
                    failed_items,
                )

            root_items = list(payload.get("replies") or [])
            top_items = payload.get("top")
            if isinstance(top_items, dict):
                root_items.append(top_items)
            elif isinstance(top_items, list):
                root_items.extend(top_items)
            declared_comments = max(declared_comments, _declared_count(payload))
            root_ids = [self._comment_id(item) for item in root_items]
            if root_ids and all(comment_id in seen_ids for comment_id in root_ids):
                failed_items.append(f"duplicate_root_page:{root_page}")
                return self._result(
                    comments,
                    CollectionCheckpoint(root_page, reply_pages, False),
                    requested_pages,
                    pinned_comments,
                    declared_comments,
                    declared_replies,
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
                    continue
                seen_ids.add(comment.comment_id)
                comments.append(comment)
                pinned_comments += int(comment.is_pinned)
                root_id = comment.comment_id
                if int(item.get("rcount") or 0) <= 0 and not item.get("replies"):
                    continue
                reply_page = reply_pages.get(root_id, 1)
                while True:
                    try:
                        reply_payload = self.transport.fetch_replies(
                            task.video_id, root_id, reply_page
                        )
                    except Exception:
                        failed_items.append(f"reply:{root_id}:{reply_page}")
                        return self._result(
                            comments,
                            CollectionCheckpoint(
                                root_page, {**reply_pages, root_id: reply_page}, False
                            ),
                            requested_pages,
                            pinned_comments,
                            declared_comments,
                            declared_replies,
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
                            CollectionCheckpoint(
                                root_page, {**reply_pages, root_id: reply_page}, False
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
                            continue
                        seen_ids.add(parsed.comment_id)
                        comments.append(parsed)
                    if _has_more(reply_payload, reply_page):
                        reply_page += 1
                        reply_pages[root_id] = reply_page
                        continue
                    reply_pages.pop(root_id, None)
                    break
            if _has_more(payload, root_page):
                root_page += 1
                continue
            return self._result(
                comments,
                CollectionCheckpoint(root_page + 1, reply_pages, True),
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


def _declared_count(payload: dict[str, Any]) -> int:
    if payload.get("declared_count") is not None:
        return int(payload["declared_count"])
    page = payload.get("page") or {}
    return int(page.get("count") or 0)


def _has_more(payload: dict[str, Any], page: int) -> bool:
    if "has_more" in payload:
        return bool(payload["has_more"])
    page_info = payload.get("page") or {}
    page_count = int(page_info.get("page_count") or 0)
    return page_count > page
