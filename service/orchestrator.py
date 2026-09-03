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
    VideoMetadataProvider,
    fetch_official_video_title,
    total_declared_count,
)
from .cursors import normalize_cursor
from .observability import AnalysisRunStore, TaskEventStore
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
        sample_provider: Callable[[str], SampleSet] | None = None,
        video_metadata_provider: VideoMetadataProvider | None = None,
        event_store: TaskEventStore | None = None,
        analysis_run_store: AnalysisRunStore | None = None,
        auto_blacklist_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.task_store = task_store
        self.uid_registry = uid_registry
        self.collector = collector
        self.analyzer = analyzer
        self.queue = queue
        self.comment_store = comment_store
        self.evidence_store = evidence_store
        self.auth_service = auth_service
        self.sample_provider = sample_provider or (
            lambda _profile_id: SampleSet("samples-empty", ())
        )
        self.video_metadata_provider = video_metadata_provider
        self.event_store = event_store
        self.analysis_run_store = analysis_run_store
        self.auto_blacklist_enabled = auto_blacklist_enabled or (lambda: False)

    def run(self, task_id: str) -> TaskRunSummary:
        task = self.task_store.get(task_id)
        title = fetch_official_video_title(self.video_metadata_provider, task.video_id)
        if title is not None and title != task.title:
            task = self.task_store.update_title(task.task_id, title)
        if task.status is TaskStatus.COMPLETED:
            return self._summary(task)
        self._event(
            task,
            phase="queued",
            event_type="task_started",
            status="started",
            message="Task execution started",
            details={"status": task.status.value},
        )
        if self.auth_service is not None and not self.auth_service.is_valid():
            message = "Collection was not started because the Bilibili session is unavailable"
            self._analysis_not_started(task, "auth_unavailable", message)
            self._event(
                task,
                phase="collecting",
                event_type="phase_failed",
                status="failed",
                message=message,
                details={"error_code": "auth_unavailable"},
            )
            paused = self.task_store.transition(
                task_id,
                TaskStatus.PAUSED,
                error_code="auth_unavailable",
                error_message="A valid Bilibili session is required before collection can start",
            )
            return self._summary(paused)

        collecting = self.task_store.transition(task_id, TaskStatus.COLLECTING)
        self._event(
            collecting,
            phase="collecting",
            event_type="phase_started",
            status="started",
            message="Comment collection started",
            details={"attempt": collecting.attempt},
        )
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
            root_cursor=normalize_cursor(checkpoint_data.get("root_cursor")),
        )
        if checkpoint.complete:
            declared_replies = sum(checkpoint.declared_reply_counts.values())
            total_declared = total_declared_count(
                checkpoint.declared_comments,
                declared_replies,
                checkpoint.declared_total,
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
                self._analysis_not_started(task, "auth_unavailable", str(exc))
                self._event(
                    task,
                    phase="collecting",
                    event_type="phase_failed",
                    status="failed",
                    message="Comment collection lost the Bilibili session",
                    details={
                        "error_code": "auth_unavailable",
                        "error_type": exc.__class__.__name__,
                    },
                )
                paused = self.task_store.transition(
                    task_id,
                    TaskStatus.PAUSED,
                    error_code="auth_unavailable",
                    error_message=str(exc),
                )
                return self._summary(paused)
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                self._analysis_not_started(task, "collection_failed", message)
                self._event(
                    task,
                    phase="collecting",
                    event_type="collection_failed",
                    status="failed",
                    message="Comment collection failed",
                    details={"error_type": exc.__class__.__name__, "error_message": message},
                )
                partial = self.task_store.transition(
                    task_id,
                    TaskStatus.PARTIAL,
                    error_code="collection_failed",
                    error_message=message,
                )
                return self._summary(partial)
        self._event(
            task,
            phase="collecting",
            event_type="collection_progress",
            status="succeeded" if collection.complete or collection.terminal else "info",
            message="Comment collection returned a checkpoint",
            details={
                "requested_pages": collection.stats.requested_pages,
                "saved_comments": collection.stats.saved_comments,
                "saved_replies": collection.stats.saved_replies,
                "declared_comments": collection.stats.declared_comments,
                "declared_replies": collection.stats.declared_replies,
                "coverage": collection.stats.coverage,
                "complete": collection.complete,
                "terminal": collection.terminal,
                "failed_item_count": len(collection.failed_items),
            },
        )
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
        total_declared = total_declared_count(
            declared_comments,
            declared_replies,
            declared_total,
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
            declared_total=total_declared,
            coverage=coverage,
            failed_items=tuple(
                dict.fromkeys((*task.progress.failed_items, *collection.failed_items))
            ),
        )
        self.task_store.update_progress(
            task_id,
            progress,
            checkpoint={
                "root_page": collection.checkpoint.root_page,
                "replies": collection.checkpoint.reply_pages,
                "complete": (collection.complete or collection.terminal)
                and (not total_declared or saved_comments + saved_replies >= total_declared),
                "requested_pages": collection.checkpoint.requested_pages,
                "declared_comments": collection.checkpoint.declared_comments,
                "declared_total": total_declared,
                "declared_reply_counts": collection.checkpoint.declared_reply_counts,
                "root_cursor": collection.checkpoint.root_cursor,
            },
        )
        self._event(
            task,
            phase="collecting",
            event_type="collection_saved",
            status="succeeded",
            message="Collected comments and checkpoint were saved",
            details={
                "saved_comments": saved_comments,
                "saved_replies": saved_replies,
                "coverage": coverage,
                "failed_item_count": len(progress.failed_items),
            },
        )
        if collection.pause_reason:
            self._analysis_not_started(
                task,
                "collection_paused",
                "AI analysis was not started because collection was paused",
            )
            self._event(
                task,
                phase="collecting",
                event_type="collection_paused",
                status="failed",
                message="Comment collection was paused before AI analysis",
                details={"reason": collection.pause_reason},
            )
            paused = self.task_store.transition(
                task_id,
                TaskStatus.PAUSED,
                error_code="collection_paused",
                error_message=collection.pause_reason,
            )
            return self._summary(paused)
        collection_complete = (collection.complete or collection.terminal) and (
            not total_declared or saved_comments + saved_replies >= total_declared
        )
        collection_incomplete = not collection_complete
        has_saved_comments = saved_comments + saved_replies > 0
        if collection_incomplete:
            message = (
                "Collection is incomplete; saved comments will still be analyzed"
                if has_saved_comments
                else "Collection is incomplete and no saved comments are available for analysis"
            )
            self._event(
                task,
                phase="collecting",
                event_type="collection_incomplete",
                status="info" if has_saved_comments else "failed",
                message=message,
                details={
                    "coverage": coverage,
                    "saved_comments": saved_comments,
                    "saved_replies": saved_replies,
                    "declared_total": total_declared,
                    "analysis_continues": has_saved_comments,
                },
            )
            if not has_saved_comments:
                self._analysis_not_started(task, "collection_incomplete", message)
                partial = self.task_store.transition(
                    task_id,
                    TaskStatus.PARTIAL,
                    error_code="collection_incomplete",
                    error_message="Collection stopped before any analyzable comments were saved",
                )
                return self._summary(partial)

        analyzing = self.task_store.transition(task_id, TaskStatus.ANALYZING)
        accounts = self._group_accounts(self.comment_store.list_for_task(task_id))
        model_name = self._model_name()
        if self.analysis_run_store is not None:
            self.analysis_run_store.start(
                task_id=task_id,
                attempt=analyzing.attempt,
                account_count=len(accounts),
                model=model_name,
            )
        self._event(
            analyzing,
            phase="analyzing",
            event_type="phase_started",
            status="started",
            message="AI analysis started",
            details={"account_count": len(accounts), "model": model_name},
        )
        self._event(
            analyzing,
            phase="analyzing",
            event_type="analysis_started",
            status="started",
            message="Preparing one or more model batches",
            details={"account_count": len(accounts), "model": model_name},
        )
        samples: SampleSet | None = None
        try:
            samples = self.sample_provider(task.profile_id)
            if self.analysis_run_store is not None:
                self.analysis_run_store.set_sample_version(
                    task_id, analyzing.attempt, samples.version
                )
            self._event(
                analyzing,
                phase="analyzing",
                event_type="model_batch",
                status="started",
                message="Sending account batches to the configured model",
                details={"account_count": len(accounts), "sample_version": samples.version},
            )
            analysis = self.analyzer.analyze(accounts, samples)
        except AnalyzerUnavailableError as exc:
            counts = self._analysis_counts(exc.partial_results)
            self._finish_analysis(
                task=analyzing,
                status="unavailable",
                account_count=len(accounts),
                batch_count=getattr(exc, "batch_count", 0),
                counts=counts,
                model=model_name or getattr(exc, "model", None),
                sample_version=(
                    samples.version if samples is not None else getattr(exc, "sample_version", None)
                ),
                error_code="model_unavailable",
                error_message=str(exc),
            )
            self._analysis_failed_event(
                analyzing,
                error_code="model_unavailable",
                error_message=str(exc),
                partial_result_count=len(exc.partial_results),
                response_issue=False,
            )
            self._apply_analysis_results(task, accounts, exc.partial_results)
            partial = self.task_store.transition(
                task_id,
                TaskStatus.PARTIAL,
                error_code="model_unavailable",
                error_message=str(exc),
            )
            return self._summary(partial)
        except AnalyzerInvalidResponseError as exc:
            counts = self._analysis_counts(exc.partial_results)
            self._finish_analysis(
                task=analyzing,
                status="failed",
                account_count=len(accounts),
                batch_count=getattr(exc, "batch_count", 0),
                counts=counts,
                model=model_name or getattr(exc, "model", None),
                sample_version=(
                    samples.version if samples is not None else getattr(exc, "sample_version", None)
                ),
                error_code="invalid_model_response",
                error_message=str(exc),
            )
            self._analysis_failed_event(
                analyzing,
                error_code="invalid_model_response",
                error_message=str(exc),
                partial_result_count=len(exc.partial_results),
                response_issue=True,
            )
            self._apply_analysis_results(task, accounts, exc.partial_results)
            failed = self.task_store.transition(
                task_id,
                TaskStatus.FAILED,
                error_code="invalid_model_response",
                error_message=str(exc),
            )
            return self._summary(failed)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._finish_analysis(
                task=analyzing,
                status="failed",
                account_count=len(accounts),
                batch_count=0,
                counts=self._analysis_counts(()),
                model=model_name,
                sample_version=samples.version if samples is not None else None,
                error_code="analysis_failed",
                error_message=message,
            )
            self._analysis_failed_event(
                analyzing,
                error_code="analysis_failed",
                error_message=message,
                partial_result_count=0,
                response_issue=False,
            )
            failed = self.task_store.transition(
                task_id,
                TaskStatus.FAILED,
                error_code="analysis_failed",
                error_message=message,
            )
            return self._summary(failed)

        counts = self._analysis_counts(analysis.results)
        self._finish_analysis(
            task=analyzing,
            status="completed",
            account_count=len(accounts),
            batch_count=analysis.batch_count,
            counts=counts,
            model=self._result_model(analysis.results, model_name),
            sample_version=analysis.sample_context.version,
        )
        self._event(
            analyzing,
            phase="analyzing",
            event_type="model_batch",
            status="succeeded",
            message="All model batches returned",
            details={"batch_count": analysis.batch_count, "account_count": len(accounts)},
        )
        self._event(
            analyzing,
            phase="analyzing",
            event_type="model_response",
            status="succeeded",
            message="Model response passed validation",
            details={"result_count": len(analysis.results), "batch_count": analysis.batch_count},
        )
        self._apply_analysis_results(task, accounts, analysis.results)
        final_status = (
            self.task_store.transition(
                task_id,
                TaskStatus.PARTIAL,
                error_code="collection_incomplete",
                error_message=(
                    "Collection stopped before all pages were available; "
                    "saved comments were analyzed"
                ),
            )
            if collection_incomplete
            else self.task_store.transition(task_id, TaskStatus.COMPLETED)
        )
        self._event(
            final_status,
            phase="completed",
            event_type="analysis_completed",
            status="succeeded",
            message=(
                "AI analysis completed for saved comments; collection remains incomplete"
                if collection_incomplete
                else "AI analysis completed"
            ),
            details={
                "account_count": counts["account_count"],
                "batch_count": analysis.batch_count,
                "evidence_count": counts["evidence_count"],
                "hit_count": counts["hit_count"],
                "non_target_count": counts["non_target_count"],
                "uncertain_count": counts["uncertain_count"],
                "collection_complete": not collection_incomplete,
            },
        )
        return self._summary(final_status, analyzed_count=len(analysis.results))

    def _event(
        self,
        task: VideoTask,
        *,
        phase: str,
        event_type: str,
        status: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        if self.event_store is not None:
            self.event_store.append(
                task_id=task.task_id,
                attempt=task.attempt,
                phase=phase,
                event_type=event_type,
                status=status,
                message=message,
                details=details,
            )

    def _analysis_not_started(
        self, task: VideoTask, error_code: str, error_message: str
    ) -> None:
        if self.analysis_run_store is not None:
            self.analysis_run_store.not_started(
                task_id=task.task_id,
                attempt=task.attempt,
                error_code=error_code,
                error_message=error_message,
            )

    def _finish_analysis(
        self,
        *,
        task: VideoTask,
        status: str,
        account_count: int,
        batch_count: int,
        counts: dict[str, int],
        model: str | None,
        sample_version: str | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self.analysis_run_store is not None:
            self.analysis_run_store.finish(
                task_id=task.task_id,
                attempt=task.attempt,
                status=status,
                account_count=account_count,
                batch_count=batch_count,
                hit_count=counts["hit_count"],
                uncertain_count=counts["uncertain_count"],
                non_target_count=counts["non_target_count"],
                evidence_count=counts["evidence_count"],
                model=model,
                sample_version=sample_version,
                error_code=error_code,
                error_message=error_message,
            )

    def _analysis_failed_event(
        self,
        task: VideoTask,
        *,
        error_code: str,
        error_message: str,
        partial_result_count: int,
        response_issue: bool,
    ) -> None:
        response_message = (
            "Model response did not pass validation"
            if response_issue
            else "Model request did not produce a usable result"
        )
        self._event(
            task,
            phase="analyzing",
            event_type="model_response",
            status="failed",
            message=response_message,
            details={
                "error_code": error_code,
                "error_type": "model_response",
                "partial_result_count": partial_result_count,
                "response_valid": False if response_issue else None,
            },
        )
        self._event(
            task,
            phase="analyzing",
            event_type="analysis_failed",
            status="failed",
            message=f"AI analysis failed: {error_message}",
            details={"error_code": error_code, "partial_result_count": partial_result_count},
        )

    @staticmethod
    def _analysis_counts(results: tuple[AnalysisResult, ...]) -> dict[str, int]:
        hit_count = sum(result.decision is AnalysisDecision.HIT for result in results)
        uncertain_count = sum(
            result.decision is AnalysisDecision.UNCERTAIN for result in results
        )
        non_target_count = sum(
            result.decision is AnalysisDecision.NON_TARGET for result in results
        )
        return {
            "account_count": len(results),
            "hit_count": hit_count,
            "uncertain_count": uncertain_count,
            "non_target_count": non_target_count,
            "evidence_count": hit_count + uncertain_count,
        }

    @staticmethod
    def _result_model(results: tuple[AnalysisResult, ...], configured: str | None) -> str | None:
        if configured:
            return configured
        return next((result.model_version for result in results if result.model_version), None)

    def _model_name(self) -> str | None:
        value = getattr(self.analyzer, "model", None)
        return value.strip() if isinstance(value, str) and value.strip() else None

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
                profile_id=task.profile_id,
            )
            target_state = (
                "queued"
                if result.decision is AnalysisDecision.HIT and self.auto_blacklist_enabled()
                else "hidden"
                if result.decision is AnalysisDecision.HIT
                else "review"
            )
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
