from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

_DEFAULT_MAX_OUTPUT_TOKENS = 512
_REQUEST_JSON_OVERHEAD_TOKENS = 128
_SYSTEM_PROMPT = (
    "You classify Bilibili comment authors at UID level for a local "
    "high-recall filter. Judge all comments belonging to one UID together, "
    "using nickname, wording, thread context, and provided samples. "
    "Mark hit for explicit insults, malicious denigration, sustained "
    "mockery, obvious hostility toward James, or a clearly hostile "
    "nickname. Mark non_target only when the account is clearly ordinary, "
    "friendly, or neutral. Use uncertain when evidence is ambiguous, but "
    "always cite at least one supplied comment for hit or uncertain. "
    "A configured friendly exception such as Mamba Buster must not be "
    "treated as a hit. Return exactly one result for every input UID, "
    "with only the values hit, non_target, or uncertain in decision. "
    "Return one JSON object with a results array and no markdown."
)


class AnalysisDecision(StrEnum):
    HIT = "hit"
    NON_TARGET = "non_target"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class CommentForAnalysis:
    comment_id: str
    content: str
    root_id: str
    parent_id: str | None
    context: tuple[str, ...]
    comment_url: str
    video_id: str = ""
    level: str = "root"
    created_at: int | None = None
    is_pinned: bool = False


@dataclass(frozen=True)
class AccountBundle:
    uid: str
    nickname: str | None
    comments: tuple[CommentForAnalysis, ...]


@dataclass(frozen=True)
class SampleItem:
    sample_id: str
    kind: str
    label: str
    content: str
    source: str = "manual"


@dataclass(frozen=True)
class SampleSet:
    version: str
    items: tuple[SampleItem, ...]


@dataclass(frozen=True)
class SampleContext:
    version: str
    items: tuple[SampleItem, ...]
    mode: str
    degradation: str | None = None


@dataclass(frozen=True)
class AnalysisResult:
    uid: str
    decision: AnalysisDecision
    evidence_comment_ids: tuple[str, ...]
    signals: tuple[str, ...]
    reason: str
    confidence: float
    model_version: str
    sample_version: str
    rule_version: str


@dataclass(frozen=True)
class AnalysisBatchResult:
    results: tuple[AnalysisResult, ...]
    batch_count: int
    sample_context: SampleContext


class BatchAnalyzer(Protocol):
    def analyze(
        self, accounts: tuple[AccountBundle, ...], samples: SampleSet
    ) -> AnalysisBatchResult:
        """Analyze UID account packages at the model protocol boundary."""


class AnalyzerTransport(Protocol):
    def complete(self, payload: dict[str, Any]) -> str | dict[str, Any]:
        """Send one structured batch to an OpenAI-compatible endpoint."""


class AnalyzerUnavailableError(RuntimeError):
    def __init__(self, message: str, *, partial_results: tuple[AnalysisResult, ...] = ()) -> None:
        super().__init__(message)
        self.partial_results = tuple(partial_results)


class AnalyzerContextLimitError(AnalyzerUnavailableError):
    """The model endpoint rejected a request because its context is too large."""


class AnalyzerInvalidResponseError(ValueError):
    def __init__(self, message: str, *, partial_results: tuple[AnalysisResult, ...] = ()) -> None:
        super().__init__(message)
        self.partial_results = tuple(partial_results)


@dataclass(frozen=True)
class RuleCatalog:
    version: str = "rules-v1"
    known_terms: tuple[str, ...] = ("巴斯特", "䟋", "天龙八部", "粘慕斯")
    friendly_exceptions: tuple[str, ...] = ("曼巴斯特",)
    nickname_positive: tuple[str, ...] = ()
    hostile_context: tuple[str, ...] = ("垃圾", "恶心", "废物", "滚", "詹黑")


class RuleEngine:
    def __init__(self, catalog: RuleCatalog | None = None) -> None:
        self.catalog = catalog or RuleCatalog()

    def evaluate(self, account: AccountBundle) -> AnalysisResult | None:
        nickname = account.nickname or ""
        text = "\n".join([nickname, *(comment.content for comment in account.comments)])
        for exception in self.catalog.friendly_exceptions:
            if exception in text:
                return self._result(
                    account,
                    AnalysisDecision.NON_TARGET,
                    (f"friendly_exception:{exception}",),
                    "Configured friendly exception takes priority",
                    0.99,
                )
        if self._normalize(nickname) in {
            self._normalize(value) for value in self.catalog.nickname_positive
        }:
            return self._result(
                account,
                AnalysisDecision.HIT,
                ("nickname_hard_rule",),
                "Nickname matches a configured high-confidence sample",
                0.98,
            )
        for term in self.catalog.known_terms:
            if term in text and any(marker in text for marker in self.catalog.hostile_context):
                return self._result(
                    account,
                    AnalysisDecision.HIT,
                    (f"known_term:{term}",),
                    "Known term appears with hostile context",
                    0.95,
                )
        return None

    def _result(
        self,
        account: AccountBundle,
        decision: AnalysisDecision,
        signals: tuple[str, ...],
        reason: str,
        confidence: float,
    ) -> AnalysisResult:
        return AnalysisResult(
            uid=account.uid,
            decision=decision,
            evidence_comment_ids=tuple(comment.comment_id for comment in account.comments),
            signals=signals,
            reason=reason,
            confidence=confidence,
            model_version="rules",
            sample_version="",
            rule_version=self.catalog.version,
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()


class SampleInjector:
    def prepare(
        self, samples: SampleSet, accounts: tuple[AccountBundle, ...], budget: int
    ) -> SampleContext:
        if not samples.items:
            return SampleContext(samples.version, (), "full")
        full_tokens = sum(_estimate_text(item.content) for item in samples.items)
        if full_tokens <= max(1, budget // 2):
            return SampleContext(samples.version, samples.items, "full")
        corpus = " ".join(comment.content for account in accounts for comment in account.comments)
        scored = sorted(
            samples.items,
            key=lambda item: sum(token in corpus for token in _keywords(item.content)),
            reverse=True,
        )
        selected: list[SampleItem] = []
        selected_tokens = 0
        for item in scored:
            item_tokens = _estimate_text(item.content)
            if selected and selected_tokens + item_tokens > max(1, budget // 2):
                break
            selected.append(item)
            selected_tokens += item_tokens
        if selected and selected_tokens < full_tokens:
            return SampleContext(
                samples.version,
                tuple(selected),
                "relevant",
                "selected samples by lexical relevance to the current accounts",
            )
        summary = " | ".join(item.content for item in samples.items)
        compressed = SampleItem(
            "compressed",
            "summary",
            "summary",
            _truncate_text(summary, max(1, budget // 2)),
        )
        return SampleContext(
            samples.version,
            (compressed,),
            "compressed",
            "sample content exceeded the configured context budget",
        )


class OpenAICompatibleTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float = 30.0,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
        client: httpx.Client | None = None,
    ) -> None:
        self.client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max(1, int(max_output_tokens))
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        request = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT,
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(
                    "/chat/completions", headers=headers, json=request
                )
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as exc:
                    raise AnalyzerInvalidResponseError(
                        "Model HTTP response was not valid JSON"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if _is_context_limit_response(exc.response):
                    raise AnalyzerContextLimitError(
                        "Model endpoint rejected the batch because its context is too large"
                    ) from exc
                retryable = exc.response.status_code in {408, 409, 429} or (
                    exc.response.status_code >= 500
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                retryable = True
            except AnalyzerInvalidResponseError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                retryable = False
            if not retryable or attempt >= self.max_retries:
                break
            if self.retry_backoff:
                time.sleep(self.retry_backoff * (attempt + 1))
        raise AnalyzerUnavailableError(
            f"Model service is unavailable: {last_error}"
        ) from last_error


class _ResultPayload(BaseModel):
    uid: str = Field(min_length=1)
    decision: AnalysisDecision
    evidence_comment_ids: list[str] = []
    signals: list[str] = []
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    model_version: str = Field(min_length=1)
    sample_version: str | None = None
    rule_version: str | None = None


class OpenAICompatibleBatchAnalyzer:
    def __init__(
        self,
        *,
        transport: AnalyzerTransport,
        model: str,
        context_budget: int = 4096,
        rule_engine: RuleEngine | None = None,
        sample_injector: SampleInjector | None = None,
    ) -> None:
        self.transport = transport
        self.model = model
        self.context_budget = context_budget
        self.max_output_tokens = max(
            1,
            int(getattr(transport, "max_output_tokens", _DEFAULT_MAX_OUTPUT_TOKENS)),
        )
        self.rule_engine = rule_engine or RuleEngine()
        self.sample_injector = sample_injector or SampleInjector()

    def analyze(
        self, accounts: tuple[AccountBundle, ...], samples: SampleSet
    ) -> AnalysisBatchResult:
        sample_context = self.sample_injector.prepare(samples, accounts, self._input_budget())
        rule_results: list[AnalysisResult] = []
        unresolved: list[AccountBundle] = []
        available_tokens = self._available_tokens(sample_context)
        for account in accounts:
            rule_result = self.rule_engine.evaluate(account)
            if rule_result is None:
                account_tokens = _estimate_text(
                    json.dumps(self._account_payload(account), ensure_ascii=False)
                )
                if account_tokens > available_tokens:
                    rule_results.append(
                        self._context_overflow_result(account, sample_context, account_tokens)
                    )
                else:
                    unresolved.append(account)
            else:
                rule_results.append(
                    AnalysisResult(
                        **{
                            **rule_result.__dict__,
                            "sample_version": sample_context.version,
                        }
                    )
                )

        batches = self._split(unresolved, sample_context)
        model_results: list[AnalysisResult] = []
        pending_batches = list(batches)
        batch_count = 0
        while pending_batches:
            batch = pending_batches.pop(0)
            payload = {
                "model": self.model,
                "rule_version": rule_engine.catalog.version,
                "sample_version": sample_context.version,
                "sample_mode": sample_context.mode,
                "sample_degradation": sample_context.degradation,
                "samples": [_sample_payload(item) for item in sample_context.items],
                "accounts": [self._account_payload(account) for account in batch],
            }
            completed_results = tuple(rule_results + model_results)
            try:
                batch_count += 1
                raw = self.transport.complete(payload)
                parsed = self._parse_response(raw, batch, sample_context)
            except AnalyzerContextLimitError:
                if len(batch) > 1:
                    midpoint = max(1, len(batch) // 2)
                    pending_batches[0:0] = [batch[:midpoint], batch[midpoint:]]
                    continue
                model_results.append(
                    self._context_overflow_result(
                        batch[0],
                        sample_context,
                        _estimate_text(
                            json.dumps(self._account_payload(batch[0]), ensure_ascii=False)
                        ),
                        server_rejected=True,
                    )
                )
                continue
            except AnalyzerUnavailableError as exc:
                raise AnalyzerUnavailableError(
                    str(exc), partial_results=completed_results
                ) from exc
            except AnalyzerInvalidResponseError as exc:
                raise AnalyzerInvalidResponseError(
                    str(exc), partial_results=completed_results
                ) from exc
            model_results.extend(parsed)
        return AnalysisBatchResult(
            results=tuple(rule_results + model_results),
            batch_count=batch_count,
            sample_context=sample_context,
        )

    def _split(
        self, accounts: list[AccountBundle], sample_context: SampleContext
    ) -> list[tuple[AccountBundle, ...]]:
        available = self._available_tokens(sample_context)
        batches: list[list[AccountBundle]] = []
        current: list[AccountBundle] = []
        current_tokens = 0
        for account in accounts:
            account_tokens = _estimate_text(
                json.dumps(self._account_payload(account), ensure_ascii=False)
            )
            if current and current_tokens + account_tokens > available:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(account)
            current_tokens += account_tokens
        if current:
            batches.append(current)
        return [tuple(batch) for batch in batches]

    def _available_tokens(self, sample_context: SampleContext) -> int:
        sample_tokens = _estimate_text(
            json.dumps([_sample_payload(item) for item in sample_context.items], ensure_ascii=False)
        )
        return max(1, self._input_budget() - sample_tokens)

    def _input_budget(self) -> int:
        return max(
            1,
            self.context_budget
            - _estimate_text(_SYSTEM_PROMPT)
            - _REQUEST_JSON_OVERHEAD_TOKENS
            - self.max_output_tokens,
        )

    def _context_overflow_result(
        self,
        account: AccountBundle,
        sample_context: SampleContext,
        account_tokens: int,
        *,
        server_rejected: bool = False,
    ) -> AnalysisResult:
        signal = "server_context_limit" if server_rejected else "context_overflow"
        reason = (
            "Model endpoint rejected this UID context after batch splitting; retained for "
            "high-recall review"
            if server_rejected
            else (
                f"UID context is larger than the configured model budget "
                f"({account_tokens} estimated tokens); retained for high-recall review"
            )
        )
        return AnalysisResult(
            uid=account.uid,
            decision=AnalysisDecision.UNCERTAIN,
            evidence_comment_ids=tuple(comment.comment_id for comment in account.comments),
            signals=("context_overflow", signal) if server_rejected else (signal,),
            reason=reason,
            confidence=0.5,
            model_version="context-guard",
            sample_version=sample_context.version,
            rule_version=self.rule_engine.catalog.version,
        )

    def _parse_response(
        self,
        raw: str | dict[str, Any],
        accounts: tuple[AccountBundle, ...],
        sample_context: SampleContext,
    ) -> list[AnalysisResult]:
        payload = raw
        if isinstance(payload, dict) and payload.get("choices"):
            choice = payload["choices"][0]
            if not isinstance(choice, dict):
                raise AnalyzerInvalidResponseError("Model response choice must be an object")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise AnalyzerInvalidResponseError("Model response message must be an object")
            payload = _message_content_text(message.get("content"))
        if isinstance(payload, str):
            payload = _parse_json_text(payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise AnalyzerInvalidResponseError("Model response must contain a results array")
        account_map = {account.uid: account for account in accounts}
        results: list[AnalysisResult] = []
        returned_uids: set[str] = set()
        try:
            for item in payload["results"]:
                parsed = _ResultPayload.model_validate(item)
                account = account_map.get(parsed.uid)
                if account is None:
                    raise AnalyzerInvalidResponseError(
                        f"Model returned an unknown UID: {parsed.uid}"
                    )
                if parsed.uid in returned_uids:
                    raise AnalyzerInvalidResponseError(
                        f"Model returned duplicate results for UID: {parsed.uid}"
                    )
                allowed_comment_ids = {comment.comment_id for comment in account.comments}
                invalid_comment_ids = set(parsed.evidence_comment_ids) - allowed_comment_ids
                if invalid_comment_ids:
                    raise AnalyzerInvalidResponseError(
                        f"Model returned unknown evidence for UID {parsed.uid}: "
                        f"{sorted(invalid_comment_ids)}"
                    )
                if (
                    parsed.decision in {AnalysisDecision.HIT, AnalysisDecision.UNCERTAIN}
                    and not parsed.evidence_comment_ids
                ):
                    raise AnalyzerInvalidResponseError(
                        f"Model returned no evidence for UID: {parsed.uid}"
                    )
                returned_uids.add(parsed.uid)
                results.append(
                    AnalysisResult(
                        uid=parsed.uid,
                        decision=parsed.decision,
                        evidence_comment_ids=tuple(parsed.evidence_comment_ids),
                        signals=tuple(parsed.signals),
                        reason=parsed.reason,
                        confidence=parsed.confidence,
                        model_version=parsed.model_version,
                        sample_version=parsed.sample_version or sample_context.version,
                        rule_version=parsed.rule_version or self.rule_engine.catalog.version,
                    )
                )
        except ValidationError as exc:
            raise AnalyzerInvalidResponseError(f"Invalid model result: {exc}") from exc
        missing_uids = set(account_map) - returned_uids
        if missing_uids:
            raise AnalyzerInvalidResponseError(
                f"Model omitted results for UID(s): {sorted(missing_uids)}"
            )
        return results

    @staticmethod
    def _account_payload(account: AccountBundle) -> dict[str, Any]:
        return {
            "uid": account.uid,
            "nickname": account.nickname,
            "comments": [
                {
                    "comment_id": comment.comment_id,
                    "content": comment.content,
                    "root_id": comment.root_id,
                    "parent_id": comment.parent_id,
                    "context": list(comment.context),
                    "comment_url": comment.comment_url,
                    "video_id": comment.video_id,
                    "level": comment.level,
                    "created_at": comment.created_at,
                    "is_pinned": comment.is_pinned,
                }
                for comment in account.comments
            ],
        }


def _parse_json_text(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AnalyzerInvalidResponseError("Model response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise AnalyzerInvalidResponseError("Model response must be a JSON object")
    return parsed


def _estimate_text(value: str) -> int:
    ascii_characters = sum(character.isascii() for character in value)
    non_ascii_characters = len(value) - ascii_characters
    return max(1, non_ascii_characters + (ascii_characters + 3) // 4)


def _sample_payload(item: SampleItem) -> dict[str, str]:
    """Keep management-only sample metadata out of the model context."""
    return {
        "sample_id": item.sample_id,
        "kind": item.kind,
        "label": item.label,
        "content": item.content,
    }


def _truncate_text(value: str, token_budget: int) -> str:
    ascii_characters = 0
    non_ascii_characters = 0
    end = 0
    for index, character in enumerate(value, start=1):
        if character.isascii():
            ascii_characters += 1
        else:
            non_ascii_characters += 1
        if non_ascii_characters + (ascii_characters + 3) // 4 > token_budget:
            break
        end = index
    return value[:end]


def _is_context_limit_response(response: httpx.Response) -> bool:
    if response.status_code == 413:
        return True
    if response.status_code != 400:
        return False
    fragments = [response.text]
    try:
        fragments.extend(_text_fragments(response.json()))
    except ValueError:
        pass
    body = re.sub(r"[_-]+", " ", "\n".join(fragments).casefold())
    return any(
        marker in body
        for marker in (
            "context length exceeded",
            "maximum context length",
            "context length",
            "context window",
            "too many tokens",
            "max tokens",
        )
    )


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        segments: list[str] = []
        for segment in content:
            if isinstance(segment, str):
                segments.append(segment)
            elif isinstance(segment, dict) and isinstance(segment.get("text"), str):
                segments.append(segment["text"])
            else:
                raise AnalyzerInvalidResponseError(
                    "Model message content must contain text segments"
                )
        return "".join(segments)
    raise AnalyzerInvalidResponseError("Model message content must be a string or text segments")


def _text_fragments(value: Any) -> list[str]:
    if isinstance(value, dict):
        fragments: list[str] = []
        for key, item in value.items():
            fragments.append(str(key))
            fragments.extend(_text_fragments(item))
        return fragments
    if isinstance(value, list):
        fragments = []
        for item in value:
            fragments.extend(_text_fragments(item))
        return fragments
    if isinstance(value, str):
        return [value]
    return []


def _keywords(value: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"\W+", value.casefold()) if len(token) > 1)
