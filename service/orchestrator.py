from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .analyzer import (
    AccountBundle,
    AnalysisDecision,
    AnalysisResult,
    AnalyzerInvalidResponseError,
    AnalyzerUnavailableError,
    BatchAnalyzer,
    CommentForAnalysis,
    SampleSet,
)
from .auth import AuthService
from .blacklist import BlacklistQueueService
from .collector import (
    BilibiliAuthenticationError,
    CollectionCheckpoint,
    CollectionResult,
    CollectionStats,
    CommentCollector,
)
from .persistence import CommentStore, EvidenceStore
from .registry import UidNotFoundError, UidRegistry
from .tasks import TaskProgress, TaskStatus, TaskStore, VideoTask


@dataclass(frozen=True)
class TaskRunSummary:
    task_id: str
    status: TaskStatus
    analyzed_count: int
    evidence_count: int
    queue_count: int
    error_code: str | None = None
    error_message: str | None = None


class TaskOrchestrator:
    """Coordinates the public task seam without owning external protocol details."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        uid_registry: UidRegistry,
        collector: CommentCollector,
        analyzer: BatchAnalyzer,
        queue: BlacklistQueueService,
        comment_store: CommentStore,
        evidence_store: EvidenceStore,
        auth_service: AuthService | None = None,
        sample_provider: Callable[[], SampleSet] | None = None,
    ) -> None:
        self.task_store = task_store
        self.uid_registry = uid_registry
        self.collector = collector
        self.analyzer = analyzer
        self.queue = queue
        self.comment_store = comment_store
        self.evidence_store = evidence_store
        self.auth_service = auth_service
        self.sample_provider = sample_provider or (lambda: SampleSet("samples-empty", ()))

    def run(self, task_id: str) -> TaskRunSummary:
        task = self.task_store.get(task_id)
        if task.status is TaskStatus.COMPLETED:
            return self._summary(task)
        if self.auth_service is not None and not self.auth_service.is_valid():
            paused = self.task_store.transition(
                task_id,
                TaskStatus.PAUSED,
                error_code="auth_unavailable",
                error_message="A valid Bilibili session is required before collection can start",
            )
            return self._summary(paused)

        collecting = self.task_store.transition(task_id, TaskStatus.COLLECTING)
        checkpoint_data = self.task_store.checkpoint(task_id)
        checkpoint = CollectionCheckpoint(
            root_page=int(checkpoint_data.get("root_page", 1)),
            reply_pages={
                str(key): int(value)
                for key, value in dict(checkpoint_data.get("replies", {})).items()
            },
            complete=bool(checkpoint_data.get("complete", False)),
            requested_pages=int(checkpoint_data.get("requested_pages", 0)),
            declared_comments=int(checkpoint_data.get("declared_comments", 0)),
            declared_total=(
                int(checkpoint_data["declared_total"])
                if checkpoint_data.get("declared_total") is not None
                else None
            ),
            declared_reply_counts={
                str(key): int(value)
                for key, value in dict(checkpoint_data.get("declared_reply_counts", {})).items()
            },
            root_cursor=(
                int(checkpoint_data["root_cursor"])
                if checkpoint_data.get("root_cursor") is not None
                else None
            ),
        )
        if checkpoint.complete:
            declared_replies = sum(checkpoint.declared_reply_counts.values())
            total_declared = (
                checkpoint.declared_total
                if checkpoint.declared_total is not None
                else checkpoint.declared_comments + declared_replies
            )
            collection = CollectionResult(
                comments=(),
                checkpoint=checkpoint,
                stats=CollectionStats(
                    requested_pages=checkpoint.requested_pages,
                    declared_comments=checkpoint.declared_comments,
                    declared_replies=declared_replies,
                    coverage=1.0 if total_declared else 0.0,
                ),
                complete=True,
            )
        else:
            try:
                collection = self.collector.collect(collecting, checkpoint)
            except BilibiliAuthenticationError as exc:
                if self.auth_service is not None:
                    self.auth_service.mark_invalid(str(exc))
                paused = self.task_store.transition(
                    task_id,
                    TaskStatus.PAUSED,
                    error_code="auth_unavailable",
                    error_message=str(exc),
                )
                return self._summary(paused)
            except Exception as exc:
                partial = self.task_store.transition(
                    task_id,
                    TaskStatus.PARTIAL,
                    error_code="collection_failed",
                    error_message=str(exc),
                )
                return self._summary(partial)
        self.comment_store.save_many(task_id, collection.comments)
        saved_comments, saved_replies, pinned_comments = self.comment_store.stats_for_task(task_id)
        declared_comments = max(
            checkpoint.declared_comments,
            collection.checkpoint.declared_comments,
            collection.stats.declared_comments,
        )
        declared_replies = sum(collection.checkpoint.declared_reply_counts.values())
        if not declared_replies:
            declared_replies = max(
                sum(checkpoint.declared_reply_counts.values()),
                collection.stats.declared_replies,
            )
        declared_total = (
            collection.checkpoint.declared_total
            if collection.checkpoint.declared_total is not None
            else checkpoint.declared_total
        )
        total_declared = (
            declared_total
            if declared_total is not None
            else declared_comments + declared_replies
        )
        coverage = (
            min(1.0, (saved_comments + saved_replies) / total_declared)
            if total_declared
            else collection.stats.coverage
        )
        progress = TaskProgress(
            requested_pages=max(
                collection.stats.requested_pages, collection.checkpoint.requested_pages
            ),
            saved_comments=saved_comments,
            saved_replies=saved_replies,
            pinned_comments=pinned_comments,
            declared_comments=declared_comments,
            declared_replies=declared_replies,
            declared_total=declared_total,
            coverage=coverage,
            failed_items=collection.failed_items,
        )
        self.task_store.update_progress(
            task_id,
            progress,
            checkpoint={
                "root_page": collection.checkpoint.root_page,
                "replies": collection.checkpoint.reply_pages,
                "complete": collection.complete
                and (not total_declared or saved_comments + saved_replies >= total_declared),
                "requested_pages": collection.checkpoint.requested_pages,
                "declared_comments": collection.checkpoint.declared_comments,
                "declared_total": collection.checkpoint.declared_total,
                "declared_reply_counts": collection.checkpoint.declared_reply_counts,
                "root_cursor": collection.checkpoint.root_cursor,
            },
        )
        collection_complete = collection.complete and (
            not total_declared or saved_comments + saved_replies >= total_declared
        )
        if not collection_complete:
            partial = self.task_store.transition(
                task_id,
                TaskStatus.PARTIAL,
                error_code="collection_incomplete",
                error_message="Collection stopped before all pages were available",
            )
            return self._summary(partial)

        self.task_store.transition(task_id, TaskStatus.ANALYZING)
        accounts = self._group_accounts(self.comment_store.list_for_task(task_id))
        try:
            analysis = self.analyzer.analyze(accounts, self.sample_provider())
        except AnalyzerUnavailableError as exc:
            self._apply_analysis_results(task, accounts, exc.partial_results)
            partial = self.task_store.transition(
                task_id,
                TaskStatus.PARTIAL,
                error_code="model_unavailable",
                error_message=str(exc),
            )
            return self._summary(partial)
        except AnalyzerInvalidResponseError as exc:
            self._apply_analysis_results(task, accounts, exc.partial_results)
            failed = self.task_store.transition(
                task_id,
                TaskStatus.FAILED,
                error_code="invalid_model_response",
                error_message=str(exc),
            )
            return self._summary(failed)
        except Exception as exc:
            failed = self.task_store.transition(
                task_id,
                TaskStatus.FAILED,
                error_code="analysis_failed",
                error_message=str(exc),
            )
            return self._summary(failed)

        self._apply_analysis_results(task, accounts, analysis.results)
        completed = self.task_store.transition(task_id, TaskStatus.COMPLETED)
        return self._summary(completed, analyzed_count=len(analysis.results))

    def _apply_analysis_results(
        self,
        task: VideoTask,
        accounts: tuple[AccountBundle, ...],
        results: tuple[AnalysisResult, ...],
    ) -> None:
        account_map = {account.uid: account for account in accounts}
        for result in results:
            if result.decision is AnalysisDecision.NON_TARGET:
                continue
            account = account_map.get(result.uid)
            if account is None:
                continue
            evidence, _ = self.evidence_store.save_if_absent(
                task_id=task.task_id,
                video_id=task.video_id,
                account_comments=tuple(
                    comment_snapshot_to_record(account, comment_id)
                    for comment_id in result.evidence_comment_ids
                    if comment_id in {comment.comment_id for comment in account.comments}
                ),
                result=result,
            )
            target_state = "queued" if result.decision is AnalysisDecision.HIT else "review"
            self._apply_uid_result(
                uid=result.uid,
                nickname=account.nickname,
                target_state=target_state,
                evidence_id=evidence.evidence_id,
            )

    def _apply_uid_result(
        self, *, uid: str, nickname: str | None, target_state: str, evidence_id: str
    ) -> None:
        from .models import UidState

        desired = UidState(target_state)
        try:
            current = self.uid_registry.get(uid)
        except UidNotFoundError:
            current, _ = self.uid_registry.add(uid=uid, nickname=nickname, state=desired)
        if current.state is UidState.EXCEPTION or current.state is UidState.BLOCKED:
            return
        if desired is UidState.QUEUED:
            if current.state is not UidState.QUEUED:
                self.uid_registry.update(uid=uid, state=desired, nickname=nickname)
            self.queue.enqueue(uid=uid, evidence_id=evidence_id)
        elif (
            current.state
            in {
                UidState.HIDDEN,
                UidState.FAILED,
                UidState.PAUSED,
                UidState.REVIEW,
            }
            and current.state is not desired
        ):
            self.uid_registry.update(uid=uid, state=desired, nickname=nickname)

    def _summary(self, task: VideoTask, analyzed_count: int = 0) -> TaskRunSummary:
        return TaskRunSummary(
            task_id=task.task_id,
            status=task.status,
            analyzed_count=analyzed_count,
            evidence_count=self.evidence_store.count_for_task(task.task_id),
            queue_count=len(self.queue.list()),
            error_code=task.error_code,
            error_message=task.error_message,
        )

    @staticmethod
    def _group_accounts(comments: tuple[object, ...]) -> tuple[AccountBundle, ...]:
        grouped: OrderedDict[str, list[object]] = OrderedDict()
        nicknames: dict[str, str | None] = {}
        for comment in comments:
            grouped.setdefault(comment.uid, []).append(comment)
            nicknames.setdefault(comment.uid, comment.nickname)
        return tuple(
            AccountBundle(
                uid=uid,
                nickname=nicknames[uid],
                comments=tuple(
                    CommentForAnalysis(
                        comment_id=comment.comment_id,
                        content=comment.content,
                        root_id=comment.root_id,
                        parent_id=comment.parent_id,
                        context=comment.context,
                        comment_url=comment.comment_url,
                        video_id=comment.video_id,
                        level=comment.level,
                        created_at=comment.created_at,
                        is_pinned=comment.is_pinned,
                    )
                    for comment in account_comments
                ),
            )
            for uid, account_comments in grouped.items()
        )


def comment_snapshot_to_record(account: AccountBundle, comment_id: str):
    comment = next(comment for comment in account.comments if comment.comment_id == comment_id)
    from .collector import CommentRecord

    return CommentRecord(
        comment_id=comment.comment_id,
        uid=account.uid,
        nickname=account.nickname,
        content=comment.content,
        video_id=comment.video_id,
        comment_url=comment.comment_url,
        root_id=comment.root_id,
        parent_id=comment.parent_id,
        level=comment.level,
        created_at=comment.created_at,
        is_pinned=comment.is_pinned,
        context=comment.context,
    )
