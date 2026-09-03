from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .analyzer import AnalysisDecision


class AuthStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    MISSING = "missing"
    VERIFICATION_FAILED = "verification_failed"


class UidState(StrEnum):
    HIDDEN = "hidden"
    REVIEW = "review"
    QUEUED = "queued"
    BLOCKED = "blocked"
    EXCEPTION = "exception"
    FAILED = "failed"
    PAUSED = "paused"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    PAUSED = "paused"


class EvidenceReviewStatus(StrEnum):
    PENDING = "pending"
    HISTORY = "history"
    ALL = "all"


class ComponentHealth(BaseModel):
    status: str
    detail: str


class ModelHealth(ComponentHealth):
    """Safe diagnostics for the remote OpenAI-compatible model configuration."""

    base_url_configured: bool
    model_configured: bool
    api_key_configured: bool


class HealthResponse(BaseModel):
    status: str
    database: ComponentHealth
    worker: ComponentHealth
    auth: ComponentHealth
    model: ModelHealth


class AuthSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cookies: dict[str, str] | list[AuthCookie] = Field(default_factory=dict)
    source: str = Field(default="extension", min_length=1, max_length=64)
    origin: str | None = Field(default=None, max_length=2048)


class AuthCookie(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=256)
    value: str
    domain: str | None = None
    path: str = "/"
    expiration_date: float | None = None
    secure: bool | None = None
    http_only: bool | None = None
    same_site: str | None = None


class AuthSessionResponse(BaseModel):
    status: AuthStatus
    detail: str
    checked_at: datetime | None = None
    cookie_present: bool = False


class UidCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str = Field(pattern=r"^[1-9][0-9]*$", min_length=1, max_length=32)
    nickname: str | None = Field(default=None, max_length=256)
    state: UidState = UidState.HIDDEN


class UidPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, max_length=256)
    state: UidState | None = None


class UidRecordResponse(BaseModel):
    uid: str
    nickname: str | None
    state: UidState
    version: int
    created_at: datetime
    updated_at: datetime


class UidListResponse(BaseModel):
    items: list[UidRecordResponse]
    version: int


class UidSyncResponse(BaseModel):
    mode: str
    version: int
    items: list[UidRecordResponse]
    removed: list[str]


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=512)


class FilterProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)
    known_terms: list[str] = Field(default_factory=list, max_length=200)
    standalone_terms: list[str] = Field(default_factory=list, max_length=200)
    friendly_exceptions: list[str] = Field(default_factory=list, max_length=200)
    hostile_context: list[str] = Field(default_factory=list, max_length=200)
    nickname_positive: list[str] = Field(default_factory=list, max_length=200)


class FilterProfileResponse(BaseModel):
    profile_id: str
    name: str
    description: str
    status: str
    known_terms: list[str]
    standalone_terms: list[str]
    friendly_exceptions: list[str]
    hostile_context: list[str]
    nickname_positive: list[str]
    created_at: datetime
    updated_at: datetime
    is_current: bool


class FilterProfileListResponse(BaseModel):
    items: list[FilterProfileResponse]


class TaskProgressResponse(BaseModel):
    requested_pages: int
    saved_comments: int
    saved_replies: int
    pinned_comments: int
    declared_comments: int
    declared_replies: int
    declared_total: int | None
    coverage: float
    failed_items: list[str]


class TaskResponse(BaseModel):
    task_id: str
    video_id: str
    video_url: str
    title: str | None
    status: TaskStatus
    submitted_at: datetime
    updated_at: datetime
    attempt: int
    error_code: str | None
    error_message: str | None
    profile_id: str
    progress: TaskProgressResponse


class TaskListResponse(BaseModel):
    items: list[TaskResponse]


class CommentResponse(BaseModel):
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
    context: list[str]


class CommentListResponse(BaseModel):
    items: list[CommentResponse]


class TaskEventResponse(BaseModel):
    event_id: int
    task_id: str
    attempt: int
    phase: str
    event_type: str
    status: str
    message: str
    details: dict[str, object]
    created_at: datetime


class TaskEventListResponse(BaseModel):
    items: list[TaskEventResponse]


class AnalysisRunResponse(BaseModel):
    analysis_id: str
    task_id: str
    attempt: int
    status: str
    model: str | None
    sample_version: str | None
    batch_count: int
    account_count: int
    hit_count: int
    uncertain_count: int
    non_target_count: int
    evidence_count: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class TaskAnalysisResponse(BaseModel):
    latest: AnalysisRunResponse | None
    attempts: list[AnalysisRunResponse]


class EvidenceResponse(BaseModel):
    evidence_id: str
    task_id: str
    uid: str
    decision: AnalysisDecision
    nickname: str | None
    video_id: str
    comment_ids: list[str]
    comments: list[dict[str, object]]
    signals: list[str]
    reason: str
    confidence: float
    model_version: str
    sample_version: str
    rule_version: str
    created_at: datetime
    profile_id: str


class EvidenceListResponse(BaseModel):
    items: list[EvidenceResponse]


class ReviewActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str | None = None
    action: str = Field(pattern=r"^(revoke|hide_only|exception|confirm|highlight)$")
    actor: str = Field(default="local-user", min_length=1, max_length=128)


class ReviewActionResponse(BaseModel):
    action_id: str
    evidence_id: str
    uid: str
    action: str
    before_state: UidState | None
    after_state: UidState | None
    actor: str
    created_at: datetime


class SampleItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=10000)
    label: str | None = None
    kind: str | None = Field(
        default=None,
        pattern=r"^(comment|nickname|comment-positive|comment-negative|nickname-positive)$",
    )
    source: str | None = Field(default=None, pattern=r"^(manual|file|review)$")


class SampleItemResponse(BaseModel):
    text: str
    content: str
    kind: str
    label: str
    source: str


class SampleImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(comment|nickname)$")
    label: str = Field(default="positive", min_length=1, max_length=64)
    text: str | None = Field(default=None, max_length=100000)
    items: list[SampleItemInput] = Field(default_factory=list)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)


class SampleResponse(BaseModel):
    sample_id: str
    kind: str
    version: str
    status: str
    label: str
    items: list[SampleItemResponse]
    duplicate_count: int = 0
    created_at: datetime
    published_at: datetime | None = None
    is_current: bool = False
    profile_id: str = "default-james-haters"


class SampleListResponse(BaseModel):
    items: list[SampleResponse]


class BlacklistResponse(BaseModel):
    item_id: str
    uid: str
    evidence_id: str | None
    status: str
    attempts: int
    last_error: str | None
    error_category: str | None
    failure_type: str | None
    user_message: str | None
    recovery_action: str | None
    error_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class BlacklistListResponse(BaseModel):
    items: list[BlacklistResponse]


class BlacklistSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class BlacklistSettingsResponse(BaseModel):
    enabled: bool
    mode: Literal["local_only", "local_and_official_queue"]
    updated_at: datetime
