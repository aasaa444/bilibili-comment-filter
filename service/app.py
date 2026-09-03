from __future__ import annotations

import math
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analyzer import (
    AccountBundle,
    AnalysisBatchResult,
    AnalyzerUnavailableError,
    BatchAnalyzer,
    OpenAICompatibleBatchAnalyzer,
    OpenAICompatibleTransport,
    SampleSet,
    evaluate_rule_results,
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
    VideoMetadataProvider,
    fetch_official_video_title,
)
from .db import Database
from .models import (
    AnalysisRunResponse,
    AuthCookie,
    AuthSessionRequest,
    AuthSessionResponse,
    AuthStatus,
    BlacklistListResponse,
    BlacklistResponse,
    BlacklistSettingsRequest,
    BlacklistSettingsResponse,
    CommentListResponse,
    ComponentHealth,
    EvidenceListResponse,
    EvidenceResponse,
    EvidenceReviewStatus,
    FilterProfileCreateRequest,
    FilterProfileListResponse,
    FilterProfileResponse,
    HealthResponse,
    ModelHealth,
    ReviewActionRequest,
    ReviewActionResponse,
    SampleImportRequest,
    SampleListResponse,
    SampleResponse,
    TaskAnalysisResponse,
    TaskCreateRequest,
    TaskEventListResponse,
    TaskEventResponse,
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
from .observability import AnalysisRunStore, TaskEventStore
from .orchestrator import TaskOrchestrator
from .persistence import CommentStore, EvidenceNotFoundError, EvidenceStore
from .profiles import FilterProfileStore
from .registry import InvalidUidTransitionError, UidNotFoundError, UidRegistry
from .reviews import ReviewService
from .samples import NewSampleItem, SampleStore
from .settings import BlacklistAutomationSettings, SettingsStore
from .tasks import (
    InvalidTaskTransitionError,
    TaskNotFoundError,
    TaskStore,
    UnsupportedVideoError,
    VideoTask,
)
from .worker import BackgroundWorker, WorkerConfig

DEFAULT_DB_PATH = Path(
    os.getenv(
        "BILIBILI_FILTER_DATABASE",
        os.getenv("BILIBILI_FILTER_DB", "data/bilibili-filter.sqlite3"),
    )
)


class UnconfiguredBatchAnalyzer:
    """Fail clearly when no remote OpenAI-compatible model is configured."""

    def analyze(
        self, _accounts: tuple[AccountBundle, ...], _samples: SampleSet
    ) -> AnalysisBatchResult:
        rule_results = evaluate_rule_results(_accounts, _samples)
        raise AnalyzerUnavailableError(
            "Remote OpenAI-compatible model is not configured; set "
            "BILIBILI_FILTER_OPENAI_BASE_URL and BILIBILI_FILTER_OPENAI_MODEL",
            partial_results=rule_results,
        )


def create_app(
    *,
    db_path: str | Path | None = None,
    auth_verifier: AuthVerifier | None = None,
    worker_available: bool = True,
    collector: CommentCollector | None = None,
    video_metadata_provider: VideoMetadataProvider | None = None,
    analyzer: BatchAnalyzer | None = None,
    blacklist_executor: BlacklistExecutor | None = None,
    web_root: str | Path | None = None,
    start_background_worker: bool = False,
) -> FastAPI:
    database = Database(db_path or DEFAULT_DB_PATH)
    database.initialize()
    auth_service = AuthService(database, auth_verifier or BilibiliAuthVerifier())
    task_event_store = TaskEventStore(database)
    analysis_run_store = AnalysisRunStore(database)
    uid_registry = UidRegistry(database)
    task_store = TaskStore(database, event_store=task_event_store)
    comment_store = CommentStore(database)
    evidence_store = EvidenceStore(database)
    profile_store = FilterProfileStore(database)
    sample_store = SampleStore(database, profile_store)
    settings_store = SettingsStore(database)
    settings_store.get_blacklist_automation()
    queue = BlacklistQueueService(database, uid_registry)
    if collector is None:
        transport = BilibiliCommentTransport(database.latest_auth_cookies)
        collector = BilibiliCommentCollector(transport)
        video_metadata_provider = video_metadata_provider or transport
    model_base_url = _env_optional_text("BILIBILI_FILTER_OPENAI_BASE_URL")
    model_api_key = _env_optional_text("BILIBILI_FILTER_OPENAI_API_KEY")
    model_name = _env_optional_text("BILIBILI_FILTER_OPENAI_MODEL")
    model_health = _model_health(model_base_url, model_name, model_api_key)
    if analyzer is None:
        if model_base_url and model_name:
            analyzer = OpenAICompatibleBatchAnalyzer(
                transport=OpenAICompatibleTransport(
                    base_url=model_base_url,
                    api_key=model_api_key,
                    model=model_name,
                    timeout=_env_positive_float(
                        "BILIBILI_FILTER_OPENAI_TIMEOUT_SECONDS", 120.0
                    ),
                    max_output_tokens=_env_positive_int(
                        "BILIBILI_FILTER_OPENAI_MAX_OUTPUT_TOKENS", 4096
                    ),
                ),
                model=model_name,
                context_budget=_env_positive_int(
                    "BILIBILI_FILTER_OPENAI_CONTEXT_TOKENS", 100000
                ),
                max_batch_accounts=_env_positive_int(
                    "BILIBILI_FILTER_OPENAI_MAX_BATCH_ACCOUNTS", 32
                ),
            )
        else:
            analyzer = UnconfiguredBatchAnalyzer()
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
        video_metadata_provider=video_metadata_provider,
        event_store=task_event_store,
        analysis_run_store=analysis_run_store,
        auto_blacklist_enabled=settings_store.is_blacklist_automation_enabled,
    )
    executor = blacklist_executor or PlaywrightBlacklistExecutor(
        cookies_provider=database.latest_auth_cookies
    )
    worker = BackgroundWorker(
        task_store=task_store,
        orchestrator=orchestrator,
        queue=queue,
        executor=executor,
        config=WorkerConfig(
            queue_interval=_env_nonnegative_float(
                "BILIBILI_FILTER_BLACKLIST_INTERVAL_SECONDS", 60.0
            ),
            queue_jitter=_env_nonnegative_float(
                "BILIBILI_FILTER_BLACKLIST_JITTER_SECONDS", 30.0
            ),
        ),
        auto_blacklist_enabled=settings_store.is_blacklist_automation_enabled,
    )
    review_service = ReviewService(
        database=database,
        evidence_store=evidence_store,
        uid_registry=uid_registry,
        queue=queue,
        sample_store=sample_store,
        settings_store=settings_store,
    )
    title_refresh_attempted: set[str] = set()

    def refresh_task_title(task: VideoTask) -> VideoTask:
        if task.title_source == "bilibili" or task.task_id in title_refresh_attempted:
            return task
        title_refresh_attempted.add(task.task_id)
        title = fetch_official_video_title(video_metadata_provider, task.video_id)
        if title is None:
            return task
        return task_store.update_title(task.task_id, title)

    app = FastAPI(title="Bilibili Comment Filter Service", version="0.1.0")
    app.state.database = database
    app.state.auth_service = auth_service
    app.state.uid_registry = uid_registry
    app.state.task_store = task_store
    app.state.task_event_store = task_event_store
    app.state.analysis_run_store = analysis_run_store
    app.state.comment_store = comment_store
    app.state.evidence_store = evidence_store
    app.state.sample_store = sample_store
    app.state.profile_store = profile_store
    app.state.settings_store = settings_store
    app.state.blacklist_settings = settings_store
    app.state.review_service = review_service
    app.state.blacklist_queue = queue
    app.state.orchestrator = orchestrator
    app.state.blacklist_executor = executor
    app.state.worker = worker
    app.state.start_background_worker = start_background_worker
    app.state.worker_available = worker_available
    app.state.model_health = model_health

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
        worker_ready = app.state.worker_available and (
            worker.running if app.state.start_background_worker else True
        )
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
            model=model_health,
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
        normalized_cookies = normalize_cookies(request.cookies)
        verification, checked_at = auth_service.synchronize(
            cookies=normalized_cookies, source=request.source
        )
        if verification.status is AuthStatus.VALID:
            task_store.retry_auth_unavailable()
        return AuthSessionResponse(
            status=verification.status,
            detail=verification.detail,
            checked_at=checked_at,
            cookie_present=bool(normalized_cookies),
        )

    @app.get("/api/uids/sync", response_model=UidSyncResponse)
    def sync_uids(since: int = 0) -> UidSyncResponse:
        if since < 0:
            raise HTTPException(status_code=422, detail="since must be non-negative")
        mode, version, items, removed = uid_registry.sync(since)
        return UidSyncResponse(
            mode=mode,
            version=version,
            items=[uid_response(item) for item in items],
            removed=removed,
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

    @app.delete("/api/uids/{uid}", status_code=204)
    def delete_uid(uid: str) -> Response:
        queue.cancel_for_uid(uid)
        uid_registry.remove(uid)
        return Response(status_code=204)

    @app.get("/api/tasks", response_model=TaskListResponse)
    def list_tasks() -> TaskListResponse:
        return TaskListResponse(
            items=[task_response(refresh_task_title(task)) for task in task_store.list()]
        )

    @app.get("/api/profiles", response_model=FilterProfileListResponse)
    def list_profiles() -> FilterProfileListResponse:
        return FilterProfileListResponse(
            items=[filter_profile_response(profile) for profile in profile_store.list()]
        )

    @app.get("/api/profiles/current", response_model=FilterProfileResponse)
    def get_current_profile() -> FilterProfileResponse:
        return filter_profile_response(profile_store.current())

    @app.post("/api/profiles", response_model=FilterProfileResponse, status_code=201)
    def create_profile(request: FilterProfileCreateRequest) -> FilterProfileResponse:
        try:
            profile = profile_store.create(
                name=request.name,
                description=request.description,
                known_terms=tuple(request.known_terms),
                standalone_terms=tuple(request.standalone_terms),
                friendly_exceptions=tuple(request.friendly_exceptions),
                hostile_context=tuple(request.hostile_context),
                nickname_positive=tuple(request.nickname_positive),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return filter_profile_response(profile)

    @app.post("/api/profiles/{profile_id}/activate", response_model=FilterProfileResponse)
    def activate_profile(profile_id: str) -> FilterProfileResponse:
        try:
            profile_store.activate(profile_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Filter profile {profile_id} was not found"
            ) from exc
        return filter_profile_response(profile_store.current())

    @app.post("/api/tasks", response_model=TaskResponse)
    def create_task(request: TaskCreateRequest) -> object:
        try:
            video_id = task_store.video_id_from_url(request.video_url)
            title = fetch_official_video_title(video_metadata_provider, video_id)
            task, created = task_store.create(
                video_url=request.video_url,
                title=title,
                title_source="bilibili" if title else "pending",
                profile_id=profile_store.current().profile_id,
            )
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
            return task_response(refresh_task_title(task_store.get(task_id)))
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

    @app.get("/api/tasks/{task_id}/comments", response_model=CommentListResponse)
    def list_task_comments(task_id: str) -> CommentListResponse:
        try:
            task_store.get(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Task {task_id} was not found") from exc
        return CommentListResponse(
            items=[
                comment_response(comment) for comment in comment_store.list_for_task(task_id)
            ]
        )

    @app.get("/api/tasks/{task_id}/events", response_model=TaskEventListResponse)
    def list_task_events(task_id: str) -> TaskEventListResponse:
        try:
            task_store.get(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Task {task_id} was not found") from exc
        return TaskEventListResponse(
            items=[task_event_response(event) for event in task_event_store.list_for_task(task_id)]
        )

    @app.get("/api/tasks/{task_id}/analysis", response_model=TaskAnalysisResponse)
    def get_task_analysis(task_id: str) -> TaskAnalysisResponse:
        try:
            task_store.get(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Task {task_id} was not found") from exc
        attempts = [
            analysis_run_response(run)
            for run in analysis_run_store.list_for_task(task_id)
        ]
        return TaskAnalysisResponse(latest=attempts[0] if attempts else None, attempts=attempts)

    @app.get("/api/reviews", response_model=EvidenceListResponse)
    def list_reviews(
        task_id: str | None = None,
        uid: str | None = None,
        result: str | None = None,
        review_status: EvidenceReviewStatus = EvidenceReviewStatus.PENDING,
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
                for item in evidence_store.list(
                    task_id=task_id,
                    uid=uid,
                    decision=decision,
                    review_status=review_status,
                )
            ]
        )

    @app.get("/api/review-actions", response_model=list[ReviewActionResponse])
    def list_review_actions(
        evidence_id: str | None = None,
        uid: str | None = None,
    ) -> list[ReviewActionResponse]:
        return [
            ReviewActionResponse(
                action_id=record.action_id,
                evidence_id=record.evidence_id,
                uid=record.uid,
                action=record.action,
                before_state=record.before_state,
                after_state=record.after_state,
                actor=record.actor,
                created_at=record.created_at,
            )
            for record in review_service.list(evidence_id=evidence_id, uid=uid)
        ]

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
        kind = "nickname" if request.kind == "nickname" else "comment"
        items = [
            NewSampleItem(
                content=item.content,
                label=item.label or request.label,
                kind=_storage_sample_kind(item.kind) if item.kind else kind,
                source=item.source or "manual",
            )
            for item in request.items
        ]
        if request.text:
            items.extend(
                NewSampleItem(line, request.label, kind, "manual")
                for line in request.text.splitlines()
            )
        try:
            profile_id = request.profile_id or profile_store.current().profile_id
            profile_store.get(profile_id)
            sample = sample_store.create(
                kind=kind, label=request.label, items=items, profile_id=profile_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Filter profile {exc.args[0]} was not found",
            ) from exc
        return JSONResponse(status_code=201, content=sample_response(sample))

    @app.post("/api/samples/{sample_id}/publish", response_model=SampleResponse)
    def publish_sample(sample_id: str) -> object:
        try:
            sample = sample_store.publish(sample_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Sample {sample_id} was not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(content=sample_response(sample))

    @app.get("/api/blacklist", response_model=BlacklistListResponse)
    def list_blacklist() -> object:
        return JSONResponse(content={"items": [blacklist_response(item) for item in queue.list()]})

    @app.get("/api/blacklist/settings", response_model=BlacklistSettingsResponse)
    def get_blacklist_settings() -> BlacklistSettingsResponse:
        return blacklist_settings_response(settings_store.get_blacklist_automation())

    @app.patch("/api/blacklist/settings", response_model=BlacklistSettingsResponse)
    def patch_blacklist_settings(request: BlacklistSettingsRequest) -> BlacklistSettingsResponse:
        return blacklist_settings_response(settings_store.set_blacklist_automation(request.enabled))

    @app.post("/api/blacklist/process", response_model=BlacklistResponse | None)
    def process_blacklist() -> object:
        if not settings_store.is_blacklist_automation_enabled():
            raise HTTPException(
                status_code=409,
                detail="自动执行官方拉黑已关闭；当前模式为仅本地隐藏",
            )
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


def _env_positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_positive_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _env_nonnegative_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


def _env_optional_text(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _model_health(
    base_url: str | None, model_name: str | None, api_key: str | None
) -> ModelHealth:
    base_url_configured = bool(base_url)
    model_configured = bool(model_name)
    api_key_configured = bool(api_key)
    if base_url_configured and model_configured:
        key_detail = (
            "API Key 已设置"
            if api_key_configured
            else "API Key 未设置；目标端点可能不需要密钥"
        )
        return ModelHealth(
            status="ready",
            detail=f"远程 OpenAI-compatible 模型已配置（{key_detail}）",
            base_url_configured=True,
            model_configured=True,
            api_key_configured=api_key_configured,
        )

    missing = []
    if not base_url_configured:
        missing.append("BILIBILI_FILTER_OPENAI_BASE_URL")
    if not model_configured:
        missing.append("BILIBILI_FILTER_OPENAI_MODEL")
    return ModelHealth(
        status="unconfigured",
        detail=(
            "远程 OpenAI-compatible 模型未配置；缺少 "
            + "、".join(missing)
            + "。本地 UID 隐藏和任务提交仍可用。"
        ),
        base_url_configured=base_url_configured,
        model_configured=model_configured,
        api_key_configured=api_key_configured,
    )


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
        profile_id=record.profile_id,
    )


def sample_response(record: object) -> dict[str, object]:
    return {
        "sample_id": record.sample_id,
        "kind": record.kind,
        "version": record.version,
        "status": record.status,
        "label": record.label,
        "items": [
            {
                "text": item.content,
                "kind": (
                    "nickname-positive"
                    if item.kind == "nickname"
                    else "comment-negative"
                    if item.label == "negative"
                    else "comment-positive"
                ),
                "source": item.source,
                "content": item.content,
                "label": item.label,
            }
            for item in record.items
        ],
        "created_at": record.created_at.isoformat(),
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "duplicate_count": record.duplicate_count,
        "is_current": record.is_current,
        "profile_id": record.profile_id,
    }


def filter_profile_response(profile: object) -> FilterProfileResponse:
    catalog = profile.catalog
    return FilterProfileResponse(
        profile_id=profile.profile_id,
        name=profile.name,
        description=profile.description,
        status=profile.status,
        known_terms=list(catalog.known_terms),
        standalone_terms=list(catalog.standalone_terms),
        friendly_exceptions=list(catalog.friendly_exceptions),
        hostile_context=list(catalog.hostile_context),
        nickname_positive=list(catalog.nickname_positive),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        is_current=profile.is_current,
    )


def _storage_sample_kind(value: str | None) -> str:
    return "nickname" if value in {"nickname", "nickname-positive"} else "comment"


def blacklist_response(item: object) -> dict[str, object]:
    return {
        "item_id": item.item_id,
        "uid": item.uid,
        "evidence_id": item.evidence_id,
        "status": item.status,
        "attempts": item.attempts,
        "last_error": item.last_error,
        "error_category": item.error_category,
        "failure_type": item.failure_type,
        "user_message": item.user_message,
        "recovery_action": item.recovery_action,
        "error_at": item.error_at.isoformat() if item.error_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


def blacklist_settings_response(settings: BlacklistAutomationSettings) -> BlacklistSettingsResponse:
    return BlacklistSettingsResponse(
        enabled=settings.enabled,
        mode=settings.mode,
        updated_at=settings.updated_at,
    )


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
        profile_id=task.profile_id,
        progress=TaskProgressResponse(
            requested_pages=task.progress.requested_pages,
            saved_comments=task.progress.saved_comments,
            saved_replies=task.progress.saved_replies,
            pinned_comments=task.progress.pinned_comments,
            declared_comments=task.progress.declared_comments,
            declared_replies=task.progress.declared_replies,
            declared_total=task.progress.declared_total,
            coverage=task.progress.coverage,
            failed_items=list(task.progress.failed_items),
        ),
    )


def task_event_response(record: object) -> TaskEventResponse:
    return TaskEventResponse(
        event_id=record.event_id,
        task_id=record.task_id,
        attempt=record.attempt,
        phase=record.phase,
        event_type=record.event_type,
        status=record.status,
        message=record.message,
        details=record.details,
        created_at=record.created_at,
    )


def analysis_run_response(record: object) -> AnalysisRunResponse:
    return AnalysisRunResponse(
        analysis_id=record.analysis_id,
        task_id=record.task_id,
        attempt=record.attempt,
        status=record.status,
        model=record.model,
        sample_version=record.sample_version,
        batch_count=record.batch_count,
        account_count=record.account_count,
        hit_count=record.hit_count,
        uncertain_count=record.uncertain_count,
        non_target_count=record.non_target_count,
        evidence_count=record.evidence_count,
        error_code=record.error_code,
        error_message=record.error_message,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


app = create_app()
