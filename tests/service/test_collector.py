from dataclasses import dataclass

from service.collector import (
    BilibiliCommentCollector,
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
            "page": {"count": 2},
            "has_more": page == 1,
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
