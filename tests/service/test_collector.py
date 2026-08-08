from dataclasses import dataclass

import httpx

from service.collector import (
    BilibiliCommentCollector,
    BilibiliCommentTransport,
    CollectionCheckpoint,
)
from service.db import Database
from service.tasks import TaskStore


def make_task():
    database = Database(":memory:")
    database.initialize()
    return TaskStore(database).create(video_url="https://www.bilibili.com/video/BV1collector1")[0]


@dataclass
class FixedTransport:
    def fetch_root_page(self, video_id: str, page: int) -> dict:
        assert video_id == "BV1collector1"
        assert page == 1
        return {
            "replies": [
                {
                    "rpid": 101,
                    "member": {"mid": 1001, "uname": "alpha"},
                    "content": {"message": "root comment"},
                    "ctime": 1700000000,
                    "rcount": 1,
                    "is_top": True,
                }
            ],
            "page": {"count": 1},
            "has_more": False,
        }

    def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict:
        assert (video_id, root_id, page) == ("BV1collector1", "101", 1)
        return {
            "replies": [
                {
                    "rpid": 102,
                    "member": {"mid": 1002, "uname": "beta"},
                    "content": {"message": "reply comment"},
                    "ctime": 1700000001,
                    "root": 101,
                    "parent": 101,
                }
            ],
            "page": {"count": 1},
            "has_more": False,
        }


def test_collector_returns_roots_pinned_comments_and_replies() -> None:
    task = make_task()
    result = BilibiliCommentCollector(FixedTransport()).collect(task, CollectionCheckpoint())

    assert result.complete is True
    assert [comment.comment_id for comment in result.comments] == ["101", "102"]
    assert result.comments[0].is_pinned is True
    assert result.comments[1].root_id == "101"
    assert result.comments[1].parent_id == "101"
    assert result.stats.saved_comments == 1
    assert result.stats.saved_replies == 1
    assert result.stats.pinned_comments == 1
    assert result.stats.coverage == 1.0


class ResumableTransport:
    def __init__(self) -> None:
        self.fail_page_two = True

    def fetch_root_page(self, video_id: str, page: int) -> dict:
        if page == 2 and self.fail_page_two:
            raise TimeoutError("temporary page failure")
        return {
            "replies": [
                {
                    "rpid": 200 + page,
                    "member": {"mid": 2000 + page, "uname": f"user-{page}"},
                    "content": {"message": f"page {page}"},
                    "ctime": 1700000000 + page,
                    "rcount": 0,
                }
            ],
            "page": {"num": page, "size": 1, "count": 2},
        }

    def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict:
        raise AssertionError("No replies are expected in this fixture")


def test_collector_returns_checkpoint_and_resumes_after_page_failure() -> None:
    task = make_task()
    transport = ResumableTransport()
    collector = BilibiliCommentCollector(transport)

    partial = collector.collect(task, CollectionCheckpoint())

    assert partial.complete is False
    assert partial.checkpoint.root_page == 2
    assert partial.failed_items == ("root_page:2",)
    transport.fail_page_two = False

    resumed = collector.collect(task, partial.checkpoint)

    assert resumed.complete is True
    assert [comment.comment_id for comment in resumed.comments] == ["202"]


class PaginatedReplyTransport:
    def fetch_root_page(self, video_id: str, page: int) -> dict:
        assert page == 1
        return {
            "replies": [
                {
                    "rpid": 300,
                    "member": {"mid": 3000, "uname": "root"},
                    "content": {"message": "root"},
                    "rcount": 2,
                }
            ],
            "page": {"num": 1, "size": 20, "count": 1},
        }

    def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict:
        assert root_id == "300"
        return {
            "replies": [
                {
                    "rpid": 300 + page,
                    "member": {"mid": 3000 + page, "uname": f"reply-{page}"},
                    "content": {"message": f"reply {page}"},
                    "root": 300,
                    "parent": 300,
                }
            ],
            "page": {"num": page, "size": 1, "count": 2},
        }


def test_collector_follows_bilibili_num_size_count_reply_pages() -> None:
    task = make_task()
    result = BilibiliCommentCollector(PaginatedReplyTransport()).collect(
        task, CollectionCheckpoint()
    )

    assert result.complete is True
    assert [comment.comment_id for comment in result.comments] == ["300", "301", "302"]
    assert result.stats.saved_replies == 2
    assert result.stats.declared_replies == 2
    assert result.stats.requested_pages == 3
    assert result.stats.coverage == 1.0


class CursorTransport:
    def __init__(self) -> None:
        self.pages: list[int] = []

    def fetch_root_page(self, video_id: str, page: int) -> dict:
        self.pages.append(page)
        assert page in {1, 7}
        return {
            "replies": [
                {
                    "rpid": 400 + len(self.pages),
                    "member": {"mid": 4000 + len(self.pages), "uname": "cursor-user"},
                    "content": {"message": "cursor page"},
                    "rcount": 0,
                }
            ],
            "cursor": {
                "all_count": 2,
                "next": 7 if page == 1 else 0,
                "is_end": page != 1,
            },
        }

    def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict:
        raise AssertionError("No replies are expected in this fixture")


def test_collector_follows_cursor_and_declared_all_count() -> None:
    task = make_task()
    transport = CursorTransport()
    result = BilibiliCommentCollector(transport).collect(task, CollectionCheckpoint())

    assert result.complete is True
    assert transport.pages == [1, 7]
    assert result.stats.saved_comments == 2
    assert result.stats.declared_comments == 2
    assert result.stats.requested_pages == 2
    assert result.stats.coverage == 1.0


def test_bilibili_transport_resolves_bv_and_passes_cookie_to_comment_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["cookie"] == "SESSDATA=session-secret"
        if request.url.path == "/x/web-interface/view":
            assert dict(request.url.params) == {"bvid": "BV1realvideo"}
            return httpx.Response(200, json={"code": 0, "data": {"aid": 7788}})
        if request.url.path == "/x/v2/reply/main":
            assert dict(request.url.params) == {
                "type": "1",
                "oid": "7788",
                "mode": "3",
                "next": "1",
                "ps": "20",
            }
            return httpx.Response(200, json={"code": 0, "data": {"replies": []}})
        if request.url.path == "/x/v2/reply/reply":
            assert dict(request.url.params) == {
                "type": "1",
                "oid": "7788",
                "root": "101",
                "pn": "2",
                "ps": "20",
            }
            return httpx.Response(200, json={"code": 0, "data": {"replies": []}})
        return httpx.Response(404)

    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: {"SESSDATA": "session-secret"},
            client=client,
            min_interval=0,
        )

        assert transport.fetch_root_page("BV1realvideo", 1) == {"replies": []}
        assert transport.fetch_replies("BV1realvideo", "101", 2) == {"replies": []}
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/x/web-interface/view",
        "/x/v2/reply/main",
        "/x/v2/reply/reply",
    ]
