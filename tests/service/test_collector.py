from dataclasses import dataclass

import httpx
import pytest

from service.bilibili_wbi import BilibiliWbiSigner
from service.collector import (
    BilibiliAuthenticationError,
    BilibiliCommentCollector,
    BilibiliCommentTransport,
    BilibiliRateLimitError,
    CollectionCheckpoint,
    CommentRecord,
)
from service.db import Database
from service.persistence import CommentStore
from service.tasks import TaskStore

COMPATIBLE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def make_task():
    database = Database(":memory:")
    database.initialize()
    return TaskStore(database).create(video_url="https://www.bilibili.com/video/BV1collector1")[0]


def make_database_and_task(video_id: str):
    database = Database(":memory:")
    database.initialize()
    task, _ = TaskStore(database).create(
        video_url=f"https://www.bilibili.com/video/{video_id}"
    )
    return database, task


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


def test_collector_persists_first_level_fields_and_save_many_retry_is_idempotent() -> None:
    class FirstLevelTransport:
        def fetch_root_page(self, video_id: str, page: int) -> dict:
            assert (video_id, page) == ("BV1collectorfields", 1)
            return {
                "replies": [
                    {
                        "rpid": 601,
                        "member": {"mid": 6001, "uname": "field-user"},
                        "content": {"message": "first-level fixture content"},
                        "ctime": 1700000601,
                        "rcount": 0,
                        "is_top": True,
                    }
                ],
                "page": {"count": 1, "page_count": 1},
                "has_more": False,
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("first-level field fixture has no replies")

    database, task = make_database_and_task("BV1collectorfields")
    expected = CommentRecord(
        comment_id="601",
        uid="6001",
        nickname="field-user",
        content="first-level fixture content",
        created_at=1700000601,
        video_id="BV1collectorfields",
        comment_url="https://www.bilibili.com/video/BV1collectorfields#reply601",
        root_id="601",
        parent_id=None,
        level="root",
        is_pinned=True,
    )
    try:
        result = BilibiliCommentCollector(FirstLevelTransport()).collect(
            task, CollectionCheckpoint()
        )
        assert result.complete is True
        assert result.failed_items == ()
        assert result.comments == (expected,)

        store = CommentStore(database)
        assert store.save_many(task.task_id, result.comments) == 1
        assert store.save_many(task.task_id, result.comments) == 1
        assert store.stats_for_task(task.task_id) == (1, 0, 1)
        assert store.list_for_task(task.task_id) == (expected,)
    finally:
        database.close()


def test_collector_records_empty_root_page_as_explainable_terminal_page() -> None:
    class EmptyRootTransport:
        def __init__(self) -> None:
            self.pages: list[int] = []

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            self.pages.append(page)
            return {
                "replies": [],
                "page": {"count": 0, "page_count": 1},
                "has_more": False,
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("empty root fixture must not request replies")

    transport = EmptyRootTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is True
    assert result.checkpoint.complete is True
    assert result.comments == ()
    assert result.stats.requested_pages == 1
    assert result.stats.saved_comments == 0
    assert result.failed_items == ("empty_root_page:1",)
    assert transport.pages == [1]


def test_collector_records_empty_reply_page_as_incomplete_without_duplicate() -> None:
    class EmptyReplyTransport:
        def __init__(self) -> None:
            self.root_pages: list[int] = []
            self.reply_pages: list[tuple[str, int]] = []

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            self.root_pages.append(page)
            return {
                "replies": [
                    {
                        "rpid": 602,
                        "member": {"mid": 6002, "uname": "reply-root"},
                        "content": {"message": "root with empty reply page"},
                        "ctime": 1700000602,
                        "rcount": 1,
                    }
                ],
                "page": {"count": 1, "page_count": 1},
                "has_more": False,
            }

        def fetch_replies(self, _video_id: str, root_id: str, page: int) -> dict:
            self.reply_pages.append((root_id, page))
            return {
                "replies": [],
                "page": {"count": 0, "page_count": 1},
                "has_more": False,
            }

    transport = EmptyReplyTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.checkpoint.complete is False
    assert [comment.comment_id for comment in result.comments] == ["602"]
    assert result.stats.requested_pages == 2
    assert result.stats.saved_comments == 1
    assert result.stats.saved_replies == 0
    assert result.stats.declared_replies == 1
    assert result.stats.coverage == pytest.approx(0.5)
    assert result.failed_items == ("empty_reply_page:602:1",)
    assert transport.root_pages == [1]
    assert transport.reply_pages == [("602", 1)]


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

    assert resumed.complete is False
    assert resumed.terminal is True
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


def test_collector_requests_next_reply_page_after_full_page_without_metadata() -> None:
    class FullReplyPageTransport:
        def fetch_root_page(self, video_id: str, page: int) -> dict:
            assert (video_id, page) == ("BV1collector1", 1)
            return {
                "replies": [
                    {
                        "rpid": 350,
                        "member": {"mid": 3500, "uname": "root"},
                        "content": {"message": "root"},
                        "rcount": 20,
                    }
                ],
                "page": {"count": 1, "page_count": 1},
            }

        def fetch_replies(self, video_id: str, root_id: str, page: int) -> dict:
            assert (video_id, root_id) == ("BV1collector1", "350")
            if page == 1:
                replies = [
                    {
                        "rpid": 3500 + index,
                        "member": {"mid": 4500 + index, "uname": f"reply-{index}"},
                        "content": {"message": f"reply {index}"},
                        "root": 350,
                        "parent": 350,
                    }
                    for index in range(20)
                ]
                return {"replies": replies}
            assert page == 2
            return {"replies": []}

    result = BilibiliCommentCollector(FullReplyPageTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is True
    assert result.stats.saved_replies == 20
    assert result.stats.requested_pages == 3


def test_collector_records_deleted_comments_and_inconsistent_comment_shapes() -> None:
    class AnomalyTransport:
        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page == 1
            return {
                "replies": [
                    {
                        "rpid": 360,
                        "member": {"mid": 3600, "uname": "valid"},
                        "content": {"message": "kept"},
                    },
                    {
                        "rpid": 361,
                        "member": {"mid": 3601, "uname": "deleted"},
                        "content": {"message": "[该评论已删除]"},
                    },
                    {"rpid": 362, "content": {"message": "missing uid"}},
                ],
                "page": {"count": 3},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("anomaly fixture has no replies")

    result = BilibiliCommentCollector(AnomalyTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.stats.saved_comments == 2
    assert "deleted_comment:361" in result.failed_items
    assert any(
        item.startswith("inconsistent_root_item:1:362:missing_uid")
        for item in result.failed_items
    )


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


def test_collector_treats_zero_cursor_as_end_without_refetching_page() -> None:
    class ZeroCursorTransport:
        root_cursor_mode = True

        def __init__(self) -> None:
            self.pages: list[int] = []

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            self.pages.append(page)
            if len(self.pages) > 1:
                raise AssertionError("a zero cursor must not be requested again")
            return {
                "replies": [
                    {
                        "rpid": 300,
                        "member": {"mid": 3000, "uname": "zero-cursor-user"},
                        "content": {"message": "one root comment"},
                    }
                ],
                "cursor": {"all_count": 1, "next": 0, "is_end": False},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            return {"replies": []}

    transport = ZeroCursorTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is True
    assert transport.pages == [0]
    assert result.stats.saved_comments == 1


def test_collector_uses_opaque_pagination_offset_when_numeric_cursor_is_missing() -> None:
    class OpaqueCursorTransport:
        root_cursor_mode = True

        def __init__(self) -> None:
            self.pages: list[int | str] = []

        def fetch_root_page(self, _video_id: str, page: int | str) -> dict:
            self.pages.append(page)
            if page == 0:
                return {
                    "replies": [
                        {
                            "rpid": 310,
                            "member": {"mid": 3100, "uname": "opaque-user-1"},
                            "content": {"message": "first page"},
                        }
                    ],
                    "cursor": {
                        "all_count": 2,
                        "is_end": False,
                        "pagination_reply": {"next_offset": "opaque-offset"},
                    },
                }
            assert page == "opaque-offset"
            return {
                "replies": [
                    {
                        "rpid": 311,
                        "member": {"mid": 3101, "uname": "opaque-user-2"},
                        "content": {"message": "second page"},
                    }
                ],
                "cursor": {"all_count": 2, "next": 0, "is_end": True},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("opaque cursor fixture has no replies")

    transport = OpaqueCursorTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is True
    assert transport.pages == [0, "opaque-offset"]
    assert [comment.comment_id for comment in result.comments] == ["310", "311"]


def test_collector_prefers_opaque_pagination_offset_when_numeric_cursor_is_present() -> None:
    class BothCursorTransport:
        root_cursor_mode = True

        def __init__(self) -> None:
            self.pages: list[int | str] = []

        def fetch_root_page(self, _video_id: str, page: int | str) -> dict:
            self.pages.append(page)
            if page == 0:
                return {
                    "replies": [
                        {
                            "rpid": 312,
                            "member": {"mid": 3102, "uname": "both-cursors-user-1"},
                            "content": {"message": "first page"},
                        }
                    ],
                    "cursor": {
                        "all_count": 2,
                        "next": 7,
                        "is_end": False,
                        "pagination_reply": {"next_offset": "opaque-offset"},
                    },
                }
            assert page == "opaque-offset"
            return {
                "replies": [
                    {
                        "rpid": 313,
                        "member": {"mid": 3103, "uname": "both-cursors-user-2"},
                        "content": {"message": "second page"},
                    }
                ],
                "cursor": {"all_count": 2, "next": 0, "is_end": True},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("both-cursors fixture has no replies")

    transport = BothCursorTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is True
    assert transport.pages == [0, "opaque-offset"]
    assert [comment.comment_id for comment in result.comments] == ["312", "313"]


def test_collector_keeps_explicit_cursor_continuation_after_empty_root_page() -> None:
    class EmptyRootTransport:
        root_cursor_mode = True

        def __init__(self) -> None:
            self.pages: list[int] = []

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            self.pages.append(page)
            if page == 0:
                return {
                    "replies": [],
                    "cursor": {"all_count": 2, "next": 2, "is_end": False},
                }
            return {
                "replies": [
                    {
                        "rpid": 350,
                        "member": {"mid": 3500, "uname": "after-empty-user"},
                        "content": {"message": "comment after empty page"},
                    }
                ],
                "cursor": {"all_count": 2, "next": 0, "is_end": False},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("empty root page must not start reply collection")

    transport = EmptyRootTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert transport.pages == [0, 2]
    assert result.stats.saved_comments == 1
    assert "empty_root_page:1" in result.failed_items


def test_collector_keeps_explicit_has_more_after_empty_root_page() -> None:
    class EmptyHasMoreTransport:
        root_cursor_mode = False

        def __init__(self) -> None:
            self.pages: list[int] = []

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            self.pages.append(page)
            if page == 1:
                return {
                    "replies": [],
                    "page": {"count": 2, "page_count": 2},
                    "has_more": True,
                }
            return {
                "replies": [
                    {
                        "rpid": 360,
                        "member": {"mid": 3600, "uname": "after-has-more-user"},
                        "content": {"message": "comment after explicit continuation"},
                    }
                ],
                "page": {"count": 2, "page_count": 2},
                "has_more": False,
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            return {"replies": []}

    transport = EmptyHasMoreTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert transport.pages == [1, 2]
    assert result.stats.saved_comments == 1


def test_collector_treats_zero_reply_cursor_as_end_without_refetching_page() -> None:
    class ZeroReplyCursorTransport:
        root_cursor_mode = False

        def __init__(self) -> None:
            self.reply_pages: list[int] = []

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page == 1
            return {
                "replies": [
                    {
                        "rpid": 400,
                        "member": {"mid": 4000, "uname": "root-user"},
                        "content": {"message": "root"},
                        "rcount": 1,
                    }
                ],
                "page": {"count": 1, "page_count": 1},
            }

        def fetch_replies(self, _video_id: str, root_id: str, page: int) -> dict:
            assert root_id == "400"
            self.reply_pages.append(page)
            if len(self.reply_pages) > 1:
                raise AssertionError("a zero reply cursor must not be requested again")
            return {
                "replies": [
                    {
                        "rpid": 401,
                        "mid": 4001,
                        "member": {"mid": 4001, "uname": "reply-user"},
                        "content": {"message": "reply"},
                        "root": 400,
                        "parent": 400,
                    }
                ],
                "cursor": {"all_count": 1, "next": 0, "is_end": False},
            }

    transport = ZeroReplyCursorTransport()
    result = BilibiliCommentCollector(transport).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is True
    assert transport.reply_pages == [1]
    assert result.stats.saved_replies == 1


def test_cursor_total_count_is_not_double_counted_with_reply_counts() -> None:
    class CursorWithRepliesTransport:
        root_cursor_mode = True

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page == 0
            return {
                "replies": [
                    {
                        "rpid": 500,
                        "member": {"mid": 5000, "uname": "root-a"},
                        "content": {"message": "root a"},
                        "rcount": 1,
                    },
                    {
                        "rpid": 501,
                        "member": {"mid": 5001, "uname": "root-b"},
                        "content": {"message": "root b"},
                        "rcount": 1,
                    },
                ],
                "cursor": {"all_count": 4, "next": 0, "is_end": True},
            }

        def fetch_replies(self, _video_id: str, root_id: str, page: int) -> dict:
            assert page == 1
            return {
                "replies": [
                    {
                        "rpid": int(root_id) + 100,
                        "member": {"mid": int(root_id) + 1000, "uname": "reply"},
                        "content": {"message": "reply"},
                        "root": int(root_id),
                        "parent": int(root_id),
                    }
                ],
                "page": {"count": 1},
            }

    task = make_task()
    result = BilibiliCommentCollector(CursorWithRepliesTransport()).collect(
        task, CollectionCheckpoint()
    )

    assert result.complete is True
    assert result.stats.declared_comments == 4
    assert result.stats.declared_replies == 2
    assert result.stats.coverage == 1.0


def test_collector_uses_root_rcount_when_reply_page_has_no_declared_count() -> None:
    class RootReplyCountTransport:
        root_cursor_mode = False

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page == 1
            return {
                "replies": [
                    {
                        "rpid": 550,
                        "mid": 5500,
                        "member": {"mid": 5500, "uname": "root"},
                        "content": {"message": "root"},
                        "rcount": 2,
                    }
                ],
                "page": {"count": 1, "page_count": 1},
            }

        def fetch_replies(self, _video_id: str, root_id: str, page: int) -> dict:
            assert (root_id, page) == ("550", 1)
            return {
                "replies": [
                    {
                        "rpid": 551,
                        "mid": 5501,
                        "member": {"mid": 5501, "uname": "reply"},
                        "content": {"message": "reply"},
                        "root": 550,
                        "parent": 550,
                    }
                ]
            }

    result = BilibiliCommentCollector(RootReplyCountTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.stats.declared_comments == 1
    assert result.stats.declared_replies == 2
    assert result.stats.saved_replies == 1
    assert result.stats.coverage == pytest.approx(2 / 3)


def test_bilibili_transport_resolves_bv_and_passes_cookie_to_comment_requests() -> None:
    requests: list[httpx.Request] = []
    signer = BilibiliWbiSigner("a" * 32, "b" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["cookie"] == "SESSDATA=session-secret"
        assert request.headers["User-Agent"] == COMPATIBLE_USER_AGENT
        if request.url.path in {"/x/v2/reply/wbi/main", "/x/v2/reply/reply"}:
            assert request.headers["Referer"] == (
                "https://www.bilibili.com/video/BV1realvideo/"
            )
        if request.url.path == "/x/web-interface/view":
            assert dict(request.url.params) == {"bvid": "BV1realvideo"}
            return httpx.Response(200, json={"code": 0, "data": {"aid": 7788}})
        if request.url.path == "/x/v2/reply/wbi/main":
            params = dict(request.url.params)
            assert {**params, "w_rid": "", "wts": ""} == {
                "type": "1",
                "oid": "7788",
                "mode": "3",
                "next": "1",
                "ps": "20",
                "web_location": "1315875",
                "w_rid": "",
                "wts": "",
            }
            expected = signer.sign(
                {
                    "type": 1,
                    "oid": "7788",
                    "mode": 3,
                    "next": 1,
                    "ps": 20,
                    "web_location": 1315875,
                },
                timestamp=int(params["wts"]),
            )
            assert params["w_rid"] == expected["w_rid"]
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
            wbi_signer=signer,
        )

        assert transport.fetch_root_page("BV1realvideo", 1) == {"replies": []}
        assert transport.fetch_replies("BV1realvideo", "101", 2) == {"replies": []}
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/x/web-interface/view",
        "/x/v2/reply/wbi/main",
        "/x/v2/reply/reply",
    ]


def test_bilibili_transport_reads_authoritative_video_title() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/x/web-interface/view"
        assert dict(request.url.params) == {"bvid": "BV1eFu36LEt2"}
        assert request.headers["Referer"] == "https://www.bilibili.com/video/BV1eFu36LEt2/"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"aid": 117058427821372, "title": "  B站官方原标题  "},
            },
        )

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

        metadata = transport.fetch_video_metadata("BV1eFu36LEt2")

        assert metadata.video_id == "BV1eFu36LEt2"
        assert metadata.aid == "117058427821372"
        assert metadata.title == "B站官方原标题"
    finally:
        client.close()

    assert len(requests) == 1


def test_bilibili_transport_loads_wbi_keys_from_navigation_auth_failure_payload() -> None:
    requests: list[httpx.Request] = []
    image_key = "a" * 32
    sub_key = "b" * 32
    signer = BilibiliWbiSigner(image_key, sub_key)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {"aid": 7788}})
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": -101,
                    "message": "账号未登录",
                    "data": {
                        "wbi_img": {
                            "img_url": f"https://i0.hdslb.com/bfs/wbi/{image_key}.png",
                            "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub_key}.png",
                        }
                    },
                },
            )
        if request.url.path == "/x/v2/reply/wbi/main":
            params = dict(request.url.params)
            expected = signer.sign(
                {
                    "type": 1,
                    "oid": "7788",
                    "mode": 3,
                    "next": 0,
                    "ps": 20,
                    "web_location": 1315875,
                },
                timestamp=int(params["wts"]),
            )
            assert params["w_rid"] == expected["w_rid"]
            return httpx.Response(200, json={"code": 0, "data": {"replies": []}})
        return httpx.Response(404)

    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: None,
            client=client,
            min_interval=0,
        )

        assert transport.fetch_root_page("BV1realvideo", 0) == {"replies": []}
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/x/web-interface/view",
        "/x/web-interface/nav",
        "/x/v2/reply/wbi/main",
    ]


def test_bilibili_transport_sends_opaque_root_cursor_as_pagination_string() -> None:
    requests: list[httpx.Request] = []
    signer = BilibiliWbiSigner("a" * 32, "b" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/x/v2/reply/wbi/main"
        params = dict(request.url.params)
        assert {**params, "w_rid": "", "wts": ""} == {
            "type": "1",
            "oid": "7788",
            "mode": "3",
            "next": "0",
            "ps": "20",
            "pagination_str": '{"offset":"opaque-offset"}',
            "web_location": "1315875",
            "w_rid": "",
            "wts": "",
        }
        expected = signer.sign(
            {
                "type": 1,
                "oid": "7788",
                "mode": 3,
                "next": 0,
                "ps": 20,
                "pagination_str": '{"offset":"opaque-offset"}',
                "web_location": 1315875,
            },
            timestamp=int(params["wts"]),
        )
        assert params["w_rid"] == expected["w_rid"]
        return httpx.Response(200, json={"code": 0, "data": {"replies": []}})

    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: None,
            client=client,
            min_interval=0,
            wbi_signer=signer,
        )

        assert transport.fetch_root_page("7788", "opaque-offset") == {"replies": []}
    finally:
        client.close()

    assert len(requests) == 1


def test_collector_records_and_stops_on_a_duplicate_root_page() -> None:
    class DuplicateTransport:
        root_cursor_mode = False

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page in {1, 2}
            return {
                "replies": [
                    {
                        "rpid": 901,
                        "member": {"mid": 9001, "uname": "duplicate-user"},
                        "content": {"message": "duplicate page"},
                    }
                ],
                "page": {"count": 2, "page_count": 2},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            return {"replies": []}

    result = BilibiliCommentCollector(DuplicateTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.stats.saved_comments == 1
    assert "duplicate_root_page:2" in result.failed_items


def test_bilibili_transport_accepts_custom_user_agent() -> None:
    requests: list[httpx.Request] = []
    signer = BilibiliWbiSigner("a" * 32, "b" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"replies": []}})

    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: None,
            client=client,
            min_interval=0,
            user_agent="fixture-browser/1.0",
            wbi_signer=signer,
        )

        assert transport.fetch_root_page("12345", 1) == {"replies": []}
    finally:
        client.close()

    assert requests[0].headers["User-Agent"] == "fixture-browser/1.0"


def test_bilibili_transport_preserves_final_rate_limit_reason() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": -352, "message": "风控系统检测到请求异常"})

    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: {"SESSDATA": "fixture"},
            client=client,
            min_interval=0,
            max_retries=1,
            retry_backoff=0,
        )
        with pytest.raises(BilibiliRateLimitError) as raised:
            transport.fetch_root_page("12345", 1)
    finally:
        client.close()

    assert calls == 1
    assert raised.value.code == -352
    assert raised.value.category == "api_rate_limit"
    assert str(raised.value) == "风控系统检测到请求异常"


@pytest.mark.parametrize(
    ("status_code", "category"),
    [(403, "http_blocked"), (412, "http_rate_limit"), (429, "http_rate_limit")],
)
def test_bilibili_transport_classifies_http_interception_without_retry(
    status_code: int, category: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code)

    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: {"SESSDATA": "fixture"},
            client=client,
            min_interval=0,
            max_retries=3,
            retry_backoff=0,
        )
        with pytest.raises(BilibiliRateLimitError) as raised:
            transport.fetch_root_page("12345", 1)
    finally:
        client.close()

    assert calls == 1
    assert raised.value.code == status_code
    assert raised.value.category == category


def test_collector_records_rate_limit_reason_in_failed_item() -> None:
    class RateLimitedTransport:
        def fetch_root_page(self, _video_id: str, _page: int) -> dict:
            raise BilibiliRateLimitError(-352, "风控系统检测到请求异常", category="api_rate_limit")

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("reply collection must not start")

    result = BilibiliCommentCollector(RateLimitedTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.failed_items == (
        "root_page:1:api_rate_limit:-352:风控系统检测到请求异常",
    )
    assert result.pause_reason == "Bilibili collection paused after api_rate_limit (-352)"


def test_collector_records_malformed_root_page_metadata_and_preserves_checkpoint() -> None:
    class MalformedRootMetadataTransport:
        root_cursor_mode = False

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page == 1
            return {"replies": [], "page": {"count": "not-a-number"}}

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("reply collection must not start")

    result = BilibiliCommentCollector(MalformedRootMetadataTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.checkpoint.root_page == 1
    assert result.checkpoint.requested_pages == 1
    assert result.checkpoint.complete is False
    assert result.failed_items == (
        "inconsistent_root_metadata:1:page.count:not_integer",
    )


def test_collector_records_malformed_root_cursor_metadata_and_preserves_checkpoint() -> None:
    class MalformedCursorTransport:
        root_cursor_mode = True

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page == 0
            return {
                "replies": [],
                "cursor": {"all_count": "unknown", "next": "later", "is_end": "false"},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("reply collection must not start")

    result = BilibiliCommentCollector(MalformedCursorTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.checkpoint.root_cursor is None
    assert result.checkpoint.root_page == 1
    assert result.checkpoint.requested_pages == 1
    assert result.failed_items == (
        "inconsistent_root_metadata:1:cursor.all_count:not_integer",
        "inconsistent_root_metadata:1:cursor.next:not_integer",
        "inconsistent_root_metadata:1:cursor.is_end:not_boolean",
    )


def test_collector_records_malformed_reply_metadata_and_preserves_reply_checkpoint() -> None:
    class MalformedReplyMetadataTransport:
        root_cursor_mode = False

        def fetch_root_page(self, _video_id: str, page: int) -> dict:
            assert page == 1
            return {
                "replies": [
                    {
                        "rpid": 700,
                        "member": {"mid": 7000, "uname": "root"},
                        "content": {"message": "root"},
                        "rcount": 1,
                    }
                ],
                "page": {"count": 1, "page_count": 1},
            }

        def fetch_replies(self, _video_id: str, root_id: str, page: int) -> dict:
            assert (root_id, page) == ("700", 1)
            return {"replies": [], "cursor": {"next": "not-a-number"}}

    result = BilibiliCommentCollector(MalformedReplyMetadataTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is False
    assert result.stats.saved_comments == 1
    assert result.checkpoint.reply_pages == {"700": 1}
    assert result.checkpoint.requested_pages == 2
    assert result.failed_items == (
        "inconsistent_reply_metadata:700:1:cursor.next:not_integer",
    )


def test_bilibili_transport_maps_login_required_response_to_authentication_error() -> None:
    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"code": -101, "message": "账号未登录"}
            )
        ),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: {"SESSDATA": "expired"},
            client=client,
            min_interval=0,
        )
        with pytest.raises(BilibiliAuthenticationError, match="账号未登录"):
            transport.fetch_root_page("12345", 1)
    finally:
        client.close()


def test_bilibili_transport_maps_invalid_csrf_response_to_authentication_error() -> None:
    client = httpx.Client(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"code": -111, "message": "csrf check failed"}
            )
        ),
    )
    try:
        transport = BilibiliCommentTransport(
            lambda: {"SESSDATA": "expired"},
            client=client,
            min_interval=0,
        )
        with pytest.raises(BilibiliAuthenticationError, match="csrf check failed"):
            transport.fetch_root_page("12345", 1)
    finally:
        client.close()


def test_collector_does_not_swallow_authentication_error() -> None:
    class ExpiredTransport:
        def fetch_root_page(self, _video_id: str, _page: int) -> dict:
            raise BilibiliAuthenticationError("fixture session expired")

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("reply collection must not start")

    database = Database(":memory:")
    database.initialize()
    try:
        task, _ = TaskStore(database).create(
            video_url="https://www.bilibili.com/video/BV1expiredcollector"
        )
        with pytest.raises(BilibiliAuthenticationError, match="fixture session expired"):
            BilibiliCommentCollector(ExpiredTransport()).collect(
                task, CollectionCheckpoint()
            )
    finally:
        database.close()


def test_collector_preserves_pinned_flag_when_root_is_in_both_lists() -> None:
    class PinnedTransport:
        root_cursor_mode = False

        def fetch_root_page(self, _video_id: str, _page: int) -> dict:
            return {
                "replies": [
                    {
                        "rpid": 100,
                        "mid": 1000,
                        "member": {"mid": 1000, "uname": "pinned-user"},
                        "content": {"message": "top comment"},
                    }
                ],
                "top": {
                    "rpid": 100,
                    "mid": 1000,
                    "member": {"mid": 1000, "uname": "pinned-user"},
                    "content": {"message": "top comment"},
                },
                "page": {"count": 1, "page_count": 1},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            return {"replies": []}

    database = Database(":memory:")
    database.initialize()
    try:
        task, _ = TaskStore(database).create(
            video_url="https://www.bilibili.com/video/BV1pinnedroot"
        )
        result = BilibiliCommentCollector(PinnedTransport()).collect(
            task, CollectionCheckpoint()
        )
    finally:
        database.close()

    assert result.complete is True
    assert len(result.comments) == 1
    assert result.comments[0].is_pinned is True
    assert result.stats.pinned_comments == 1


def test_collector_ignores_bilibili_top_metadata_wrapper() -> None:
    class TopMetadataTransport:
        root_cursor_mode = True

        def fetch_root_page(self, _video_id: str, _page: int) -> dict:
            return {
                "replies": [
                    {
                        "rpid": 101,
                        "mid": 1001,
                        "member": {"mid": 1001, "uname": "ordinary-user"},
                        "content": {"message": "ordinary comment"},
                    }
                ],
                "top": {"admin": None, "upper": None, "vote": None},
                "cursor": {"all_count": 1, "next": 0, "is_end": True},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            raise AssertionError("no replies are expected in this fixture")

    result = BilibiliCommentCollector(TopMetadataTransport()).collect(
        make_task(), CollectionCheckpoint()
    )

    assert result.complete is True
    assert result.failed_items == ()
    assert len(result.comments) == 1
    assert result.stats.pinned_comments == 0


def test_collector_keeps_incomplete_when_declared_count_exceeds_saved_comments() -> None:
    class ShortTransport:
        root_cursor_mode = False

        def fetch_root_page(self, _video_id: str, _page: int) -> dict:
            return {
                "replies": [
                    {
                        "rpid": 200,
                        "mid": 2000,
                        "member": {"mid": 2000, "uname": "short-user"},
                        "content": {"message": "only comment"},
                    }
                ],
                "page": {"count": 2, "page_count": 1},
            }

        def fetch_replies(self, _video_id: str, _root_id: str, _page: int) -> dict:
            return {"replies": []}

    database = Database(":memory:")
    database.initialize()
    try:
        task, _ = TaskStore(database).create(
            video_url="https://www.bilibili.com/video/BV1shortcount"
        )
        result = BilibiliCommentCollector(ShortTransport()).collect(
            task, CollectionCheckpoint()
        )
    finally:
        database.close()

    assert result.complete is False
    assert result.checkpoint.complete is False
    assert result.stats.declared_comments == 2
    assert result.stats.saved_comments == 1
