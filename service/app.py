from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .analyzer import (
    BatchAnalyzer,
    OpenAICompatibleBatchAnalyzer,
    OpenAICompatibleTransport,
)
from .auth import AuthService, AuthVerifier, BilibiliAuthVerifier
from .blacklist import (
    BlacklistExecutor,
    BlacklistQueueError,
    BlacklistQueueService,
    PlaywrightBlacklistExecutor,
)
from .collector import (
    BilibiliCommentCollector,
    BilibiliCommentTransport,
    CommentCollector,
)
from .db import Database
from .models import (
    AuthCookie,
    AuthSessionRequest,
    AuthSessionResponse,
    BlacklistListResponse,
    BlacklistResponse,
    ComponentHealth,
    EvidenceListResponse,
    EvidenceResponse,
    HealthResponse,
    ReviewActionRequest,
    ReviewActionResponse,
    SampleImportRequest,
    SampleListResponse,
    SampleResponse,
    TaskCreateRequest,
    TaskListResponse,
    TaskProgressResponse,
    TaskResponse,
    UidCreateRequest,
    UidListResponse,
    UidPatchRequest,
    UidRecordResponse,
    UidState,
    UidSyncResponse,
)
from .orchestrator import TaskOrchestrator
from .persistence import CommentStore, EvidenceNotFoundError, EvidenceStore
from .registry import InvalidUidTransitionError, UidNotFoundError, UidRegistry
from .reviews import ReviewService
from .samples import SampleStore
from .tasks import (
    InvalidTaskTransitionError,
    TaskNotFoundError,
    TaskStore,
    UnsupportedVideoError,
    VideoTask,
)
from .worker import BackgroundWorker

DEFAULT_DB_PATH = Path(
    os.getenv(
        "BILIBILI_FILTER_DATABASE",
        os.getenv("BILIBILI_FILTER_DB", "data/bilibili-filter.sqlite3"),
    )
)


def create_app(
    *,
    db_path: str | Path | None = None,
    auth_verifier: AuthVerifier | None = None,
    worker_available: bool = True,
    collector: CommentCollector | None = None,
    analyzer: BatchAnalyzer | None = None,
    blacklist_executor: BlacklistExecutor | None = None,
    web_root: str | Path | None = None,
    start_background_worker: bool = False,
) -> FastAPI:
    database = Database(db_path or DEFAULT_DB_PATH)
    database.initialize()
    auth_service = AuthService(database, auth_verifier or BilibiliAuthVerifier())
    uid_registry = UidRegistry(database)
    task_store = TaskStore(database)
    comment_store = CommentStore(database)
    evidence_store = EvidenceStore(database)
    sample_store = SampleStore(database)
    queue = BlacklistQueueService(database, uid_registry)
    collector = collector or BilibiliCommentCollector(
        BilibiliCommentTransport(database.latest_auth_cookies)
    )
    analyzer = analyzer or OpenAICompatibleBatchAnalyzer(
        transport=OpenAICompatibleTransport(
            base_url=os.getenv(
                "BILIBILI_FILTER_OPENAI_BASE_URL",
                os.getenv("BILIBILI_FILTER_MODEL_URL", "http://127.0.0.1:11434/v1"),
            ),
            api_key=os.getenv(
                "BILIBILI_FILTER_OPENAI_API_KEY", os.getenv("BILIBILI_FILTER_MODEL_KEY")
            ),
            model=os.getenv(
                "BILIBILI_FILTER_OPENAI_MODEL", os.getenv("BILIBILI_FILTER_MODEL", "local-model")
            ),
        ),
        model=os.getenv(
            "BILIBILI_FILTER_OPENAI_MODEL", os.getenv("BILIBILI_FILTER_MODEL", "local-model")
        ),
    )
    orchestrator = TaskOrchestrator(
        task_store=task_store,
        uid_registry=uid_registry,
        collector=collector,
        analyzer=analyzer,
        queue=queue,
        comment_store=comment_store,
        evidence_store=evidence_store,
        auth_service=auth_service,
        sample_provider=sample_store.current,
    )
    executor = blacklist_executor or PlaywrightBlacklistExecutor(
        cookies_provider=database.latest_auth_cookies
    )
    worker = BackgroundWorker(
        task_store=task_store,
        orchestrator=orchestrator,
        queue=queue,
        executor=executor,
    )
    review_service = ReviewService(
        database=database,
        evidence_store=evidence_store,
        uid_registry=uid_registry,
        queue=queue,
        sample_store=sample_store,
    )

    app = FastAPI(title="Bilibili Comment Filter Service", version="0.1.0")
    app.state.database = database
    app.state.auth_service = auth_service
    app.state.uid_registry = uid_registry
    app.state.task_store = task_store
    app.state.comment_store = comment_store
    app.state.evidence_store = evidence_store
    app.state.sample_store = sample_store
    app.state.review_service = review_service
    app.state.blacklist_queue = queue
    app.state.orchestrator = orchestrator
    app.state.blacklist_executor = executor
    app.state.worker = worker
    app.state.start_background_worker = start_background_worker
    app.state.worker_available = worker_available

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def start_worker() -> None:
        if app.state.start_background_worker and app.state.worker_available:
            worker.start()

    @app.on_event("shutdown")
    def stop_worker() -> None:
        worker.stop()
        database.close()

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        database_ready, database_detail = database.check()
        worker_ready = app.state.worker_available and worker.available
        auth_verification, _, _ = auth_service.current()
        worker_detail = "Task worker is available" if worker_ready else "Task worker is unavailable"
        service_status = "ready" if database_ready and worker_ready else "unavailable"
        return HealthResponse(
            status=service_status,
            database=ComponentHealth(
                status="ready" if database_ready else "unavailable",
                detail=database_detail,
            ),
            worker=ComponentHealth(
                status="ready" if worker_ready else "unavailable", detail=worker_detail
            ),
            auth=ComponentHealth(
                status=auth_verification.status.value,
                detail=auth_verification.detail,
            ),
        )

    @app.get("/api/auth/session", response_model=AuthSessionResponse)
    def get_auth_session() -> AuthSessionResponse:
        verification, checked_at, cookie_present = auth_service.current()
        return AuthSessionResponse(
            status=verification.status,
            detail=verification.detail,
            checked_at=checked_at,
            cookie_present=cookie_present,
        )

    @app.post("/api/auth/session", response_model=AuthSessionResponse)
    def post_auth_session(request: AuthSessionRequest) -> AuthSessionResponse:
        verification, checked_at = auth_service.synchronize(
            cookies=normalize_cookies(request.cookies), source=request.source
        )
        return AuthSessionResponse(
            status=verification.status,
            detail=verification.detail,
            checked_at=checked_at,
            cookie_present=True,
        )

    @app.get("/api/uids/sync", response_model=UidSyncResponse)
    def sync_uids(since: int = 0) -> UidSyncResponse:
        if since < 0:
            raise HTTPException(status_code=422, detail="since must be non-negative")
        mode, version, items = uid_registry.sync(since)
        return UidSyncResponse(
            mode=mode,
            version=version,
            items=[uid_response(item) for item in items],
            removed=[],
        )

    @app.get("/api/uids", response_model=UidListResponse)
    def list_uids(state: UidState | None = None) -> UidListResponse:
        return UidListResponse(
            items=[uid_response(item) for item in uid_registry.list(state=state)],
            version=uid_registry.version(),
        )

    @app.post("/api/uids", response_model=UidRecordResponse)
    def create_uid(request: UidCreateRequest) -> UidRecordResponse:
        record, created = uid_registry.add(
            uid=request.uid, nickname=request.nickname, state=request.state
        )
        response = uid_response(record)
        return JSONResponse(
            status_code=201 if created else 200,
            content=response.model_dump(mode="json"),
        )

    @app.patch("/api/uids/{uid}", response_model=UidRecordResponse)
    def patch_uid(uid: str, request: UidPatchRequest) -> UidRecordResponse:
        try:
            record = uid_registry.update(uid=uid, state=request.state, nickname=request.nickname)
        except UidNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"UID {uid} was not found") from exc
        except InvalidUidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return uid_response(record)

    @app.get("/api/tasks", response_model=TaskListResponse)
    def list_tasks() -> TaskListResponse:
        return TaskListResponse(items=[task_response(task) for task in task_store.list()])

    @app.post("/api/tasks", response_model=TaskResponse)
    def create_task(request: TaskCreateRequest) -> object:
        try:
            task, created = task_store.create(video_url=request.video_url, title=request.title)
        except UnsupportedVideoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response = task_response(task)
        return JSONResponse(
            status_code=201 if created else 200,
            content=response.model_dump(mode="json"),
        )

    @app.get("/api/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        try:
            return task_response(task_store.get(task_id))
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Task {task_id} was not found") from exc

    @app.post("/api/tasks/{task_id}/retry", response_model=TaskResponse)
    def retry_task(task_id: str) -> TaskResponse:
        try:
            return task_response(task_store.retry(task_id))
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Task {task_id} was not found") from exc
        except InvalidTaskTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/run", response_model=TaskResponse)
    def run_task(task_id: str) -> TaskResponse:
        try:
            orchestrator.run(task_id)
            return task_response(task_store.get(task_id))
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Task {task_id} was not found") from exc

    @app.get("/api/tasks/{task_id}/comments", response_model=object)
    def list_task_comments(task_id: str) -> object:
        try:
            task_store.get(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Task {task_id} was not found") from exc
        return {
            "items": [comment_response(comment) for comment in comment_store.list_for_task(task_id)]
        }

    @app.get("/api/reviews", response_model=EvidenceListResponse)
    def list_reviews(
        task_id: str | None = None,
        uid: str | None = None,
        result: str | None = None,
    ) -> EvidenceListResponse:
        from .analyzer import AnalysisDecision

        decision = None
        if result:
            try:
                decision = AnalysisDecision(result)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail="result must be hit or uncertain"
                ) from exc
        return EvidenceListResponse(
            items=[
                evidence_response(item)
                for item in evidence_store.list(task_id=task_id, uid=uid, decision=decision)
            ]
        )

    @app.post("/api/reviews/{evidence_id}", response_model=ReviewActionResponse)
    def apply_review(evidence_id: str, request: ReviewActionRequest) -> ReviewActionResponse:
        try:
            result = review_service.apply(
                evidence_id=evidence_id,
                action=request.action,
                actor=request.actor,
            )
        except EvidenceNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"Evidence {evidence_id} was not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ReviewActionResponse(
            action_id=result.action_id,
            evidence_id=result.evidence_id,
            uid=result.uid,
            action=result.action,
            before_state=result.before_state,
            after_state=result.after_state,
            actor=result.actor,
            created_at=result.created_at,
        )

    @app.get("/api/samples", response_model=SampleListResponse)
    def list_samples() -> object:
        return JSONResponse(
            content={"items": [sample_response(item) for item in sample_store.list()]}
        )

    @app.post("/api/samples", response_model=SampleResponse)
    def create_sample(request: SampleImportRequest) -> object:
        items = [(item.content, item.label or request.label) for item in request.items]
        if request.text:
            items.extend((line, request.label) for line in request.text.splitlines())
        kind = "nickname" if request.kind == "nickname" else "comment"
        try:
            sample = sample_store.create(kind=kind, label=request.label, items=items)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(status_code=201, content=sample_response(sample))

    @app.post("/api/samples/{sample_id}/publish", response_model=SampleResponse)
    def publish_sample(sample_id: str) -> object:
        try:
            sample = sample_store.publish(sample_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Sample {sample_id} was not found"
            ) from exc
        return JSONResponse(content=sample_response(sample))

    @app.get("/api/blacklist", response_model=BlacklistListResponse)
    def list_blacklist() -> object:
        return JSONResponse(content={"items": [blacklist_response(item) for item in queue.list()]})

    @app.post("/api/blacklist/process", response_model=BlacklistResponse | None)
    def process_blacklist() -> object:
        item = queue.process_next(app.state.blacklist_executor)
        return JSONResponse(content=blacklist_response(item) if item is not None else None)

    @app.post("/api/blacklist/{item_id}/pause", response_model=BlacklistResponse)
    def pause_blacklist(item_id: str) -> object:
        try:
            item = queue.pause(item_id)
        except BlacklistQueueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(content=blacklist_response(item))

    @app.post("/api/blacklist/{item_id}/resume", response_model=BlacklistResponse)
    def resume_blacklist(item_id: str) -> object:
        try:
            item = queue.resume(item_id)
        except BlacklistQueueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(content=blacklist_response(item))

    @app.post("/api/blacklist/{item_id}/retry", response_model=BlacklistResponse)
    def retry_blacklist(item_id: str) -> object:
        try:
            item = queue.retry(item_id)
        except BlacklistQueueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(content=blacklist_response(item))

    if web_root is not None and Path(web_root).is_dir():
        app.mount("/", StaticFiles(directory=Path(web_root), html=True), name="web")

    return app


def normalize_cookies(cookies: dict[str, str] | list[AuthCookie]) -> dict[str, str]:
    if isinstance(cookies, dict):
        return cookies
    return {cookie.name: cookie.value for cookie in cookies}


def comment_response(comment: object) -> dict[str, object]:
    return {
        "comment_id": comment.comment_id,
        "uid": comment.uid,
        "nickname": comment.nickname,
        "content": comment.content,
        "video_id": comment.video_id,
        "comment_url": comment.comment_url,
        "root_id": comment.root_id,
        "parent_id": comment.parent_id,
        "level": comment.level,
        "created_at": comment.created_at,
        "is_pinned": comment.is_pinned,
        "context": list(comment.context),
    }


def evidence_response(record: object) -> EvidenceResponse:
    return EvidenceResponse(
        evidence_id=record.evidence_id,
        task_id=record.task_id,
        uid=record.uid,
        decision=record.decision,
        nickname=record.nickname,
        video_id=record.video_id,
        comment_ids=list(record.comment_ids),
        comments=list(record.comments),
        signals=list(record.signals),
        reason=record.reason,
        confidence=record.confidence,
        model_version=record.model_version,
        sample_version=record.sample_version,
        rule_version=record.rule_version,
        created_at=record.created_at,
    )


def sample_response(record: object) -> dict[str, object]:
    kind = "nickname-positive" if record.kind == "nickname" else "comment-positive"
    return {
        "sample_id": record.sample_id,
        "version": int(record.version) if record.version.isdigit() else record.version,
        "status": record.status,
        "items": [
            {
                "text": item.content,
                "kind": kind,
                "source": "review" if record.label == "review" else "manual",
                "content": item.content,
                "label": item.label,
            }
            for item in record.items
        ],
        "created_at": record.created_at.isoformat(),
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "duplicate_count": record.duplicate_count,
    }


def blacklist_response(item: object) -> dict[str, object]:
    return {
        "item_id": item.item_id,
        "uid": item.uid,
        "evidence_id": item.evidence_id,
        "status": item.status,
        "attempts": item.attempts,
        "last_error": item.last_error,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


def uid_response(record: object) -> UidRecordResponse:
    return UidRecordResponse(
        uid=record.uid,
        nickname=record.nickname,
        state=record.state,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def task_response(task: VideoTask) -> TaskResponse:
    return TaskResponse(
        task_id=task.task_id,
        video_id=task.video_id,
        video_url=task.video_url,
        title=task.title,
        status=task.status,
        submitted_at=task.submitted_at,
        updated_at=task.updated_at,
        attempt=task.attempt,
        error_code=task.error_code,
        error_message=task.error_message,
        progress=TaskProgressResponse(
            requested_pages=task.progress.requested_pages,
            saved_comments=task.progress.saved_comments,
            saved_replies=task.progress.saved_replies,
            pinned_comments=task.progress.pinned_comments,
            declared_comments=task.progress.declared_comments,
            declared_replies=task.progress.declared_replies,
            coverage=task.progress.coverage,
            failed_items=list(task.progress.failed_items),
        ),
    )


app = create_app()
