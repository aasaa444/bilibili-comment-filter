from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError


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
    pass


class AnalyzerInvalidResponseError(ValueError):
    pass


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
        compressed = SampleItem("compressed", "summary", "summary", summary[: max(16, budget * 4)])
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
    ) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.api_key = api_key
        self.model = model

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = self.client.post(
                "/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Return JSON with a results array and no markdown.",
                        },
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AnalyzerUnavailableError(f"Model service is unavailable: {exc}") from exc


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
        self.rule_engine = rule_engine or RuleEngine()
        self.sample_injector = sample_injector or SampleInjector()

    def analyze(
        self, accounts: tuple[AccountBundle, ...], samples: SampleSet
    ) -> AnalysisBatchResult:
        sample_context = self.sample_injector.prepare(samples, accounts, self.context_budget)
        rule_results: list[AnalysisResult] = []
        unresolved: list[AccountBundle] = []
        for account in accounts:
            rule_result = self.rule_engine.evaluate(account)
            if rule_result is None:
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
        for batch in batches:
            payload = {
                "model": self.model,
                "rule_version": self.rule_engine.catalog.version,
                "sample_version": sample_context.version,
                "sample_mode": sample_context.mode,
                "sample_degradation": sample_context.degradation,
                "samples": [item.__dict__ for item in sample_context.items],
                "accounts": [self._account_payload(account) for account in batch],
            }
            model_results.extend(
                self._parse_response(self.transport.complete(payload), batch, sample_context)
            )
        return AnalysisBatchResult(
            results=tuple(rule_results + model_results),
            batch_count=len(batches),
            sample_context=sample_context,
        )

    def _split(
        self, accounts: list[AccountBundle], sample_context: SampleContext
    ) -> list[tuple[AccountBundle, ...]]:
        sample_tokens = sum(_estimate_text(item.content) for item in sample_context.items)
        available = max(1, self.context_budget - sample_tokens - 16)
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

    def _parse_response(
        self,
        raw: str | dict[str, Any],
        accounts: tuple[AccountBundle, ...],
        sample_context: SampleContext,
    ) -> list[AnalysisResult]:
        payload = raw
        if isinstance(payload, dict) and payload.get("choices"):
            payload = payload["choices"][0].get("message", {}).get("content", "")
        if isinstance(payload, str):
            payload = _parse_json_text(payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise AnalyzerInvalidResponseError("Model response must contain a results array")
        account_map = {account.uid: account for account in accounts}
        results: list[AnalysisResult] = []
        try:
            for item in payload["results"]:
                parsed = _ResultPayload.model_validate(item)
                account = account_map.get(parsed.uid)
                if account is None:
                    raise AnalyzerInvalidResponseError(
                        f"Model returned an unknown UID: {parsed.uid}"
                    )
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
        return results

    @staticmethod
    def _account_payload(account: AccountBundle) -> dict[str, Any]:
        return {
            "uid": account.uid,
            "nickname": account.nickname,
            "comments": [comment.__dict__ for comment in account.comments],
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
    return max(1, (len(value) + 3) // 4)


def _keywords(value: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"\W+", value.casefold()) if len(token) > 1)
