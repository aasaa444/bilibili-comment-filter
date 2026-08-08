import json

import httpx
import pytest

from service.analyzer import (
    AccountBundle,
    AnalysisDecision,
    AnalyzerInvalidResponseError,
    CommentForAnalysis,
    OpenAICompatibleBatchAnalyzer,
    OpenAICompatibleTransport,
    RuleCatalog,
    RuleEngine,
    SampleItem,
    SampleSet,
)


def account(uid: str, text: str, comment_id: str = "c1") -> AccountBundle:
    return AccountBundle(
        uid=uid,
        nickname=f"user-{uid}",
        comments=(
            CommentForAnalysis(
                comment_id=comment_id,
                content=text,
                root_id=comment_id,
                parent_id=None,
                context=(),
                comment_url=f"https://example.test/{comment_id}",
            ),
        ),
    )


class RecordingModelTransport:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def complete(self, payload: dict) -> str:
        self.requests.append(payload)
        results = [
            {
                "uid": item["uid"],
                "decision": "uncertain",
                "evidence_comment_ids": [comment["comment_id"] for comment in item["comments"]],
                "signals": ["model-review"],
                "reason": "Needs human review",
                "confidence": 0.51,
                "model_version": "fixture-model",
            }
            for item in payload["accounts"]
        ]
        return json.dumps({"results": results})


def test_analyzer_batches_accounts_and_keeps_same_uid_together() -> None:
    transport = RecordingModelTransport()
    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=transport,
        model="fixture-model",
        context_budget=130,
    )
    accounts = [
        AccountBundle(
            uid="1001",
            nickname="alpha",
            comments=(
                CommentForAnalysis("a1", "ordinary discussion", "a1", None, (), "url-a1"),
                CommentForAnalysis("a2", "another ordinary discussion", "a1", "a1", (), "url-a2"),
            ),
        ),
        account("1002", "boundary statement", "b1"),
        account("1003", "another boundary statement", "c1"),
    ]
    samples = SampleSet(
        version="comments-v1",
        items=(SampleItem("s1", "comment", "positive", "example target expression"),),
    )

    result = analyzer.analyze(accounts, samples)

    assert result.batch_count == 2
    assert len(transport.requests) == 2
    assert all("samples" in request for request in transport.requests)
    assert all(
        len(item["comments"]) == 2
        for item in transport.requests[0]["accounts"]
        if item["uid"] == "1001"
    )
    assert {item.uid for item in result.results} == {"1001", "1002", "1003"}
    assert all(item.decision is AnalysisDecision.UNCERTAIN for item in result.results)
    assert result.sample_context.mode == "full"


def test_rule_engine_prioritizes_friendly_exception_and_requires_context_for_terms() -> None:
    engine = RuleEngine(
        RuleCatalog(
            version="rules-v1",
            known_terms=("巴斯特", "䟋", "天龙八部", "粘慕斯"),
            friendly_exceptions=("曼巴斯特",),
            nickname_positive=("典型詹黑昵称",),
            hostile_context=("垃圾", "恶心"),
        )
    )

    exception = engine.evaluate(account("1001", "曼巴斯特是友军表达"))
    bare_term = engine.evaluate(account("1002", "巴斯特"))
    contextual_term = engine.evaluate(account("1003", "巴斯特 垃圾詹"))
    nickname = engine.evaluate(AccountBundle(uid="1004", nickname="典型詹黑昵称", comments=()))

    assert exception is not None
    assert exception.decision is AnalysisDecision.NON_TARGET
    assert bare_term is None
    assert contextual_term is not None
    assert contextual_term.decision is AnalysisDecision.HIT
    assert nickname is not None
    assert nickname.decision is AnalysisDecision.HIT


def test_analyzer_rejects_invalid_structured_model_response() -> None:
    class InvalidTransport:
        def complete(self, payload: dict) -> str:
            return "not-json"

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=InvalidTransport(), model="fixture-model", context_budget=100
    )

    with pytest.raises(AnalyzerInvalidResponseError):
        analyzer.analyze([account("1001", "needs analysis")], SampleSet("v1", ()))


@pytest.mark.parametrize(
    "response",
    [
        {"results": []},
        {
            "results": [
                {
                    "uid": "1001",
                    "decision": "uncertain",
                    "evidence_comment_ids": ["not-part-of-account"],
                    "reason": "review",
                    "confidence": 0.5,
                    "model_version": "fixture-model",
                }
            ]
        },
    ],
)
def test_analyzer_rejects_incomplete_or_foreign_evidence(response: dict) -> None:
    class IncompleteTransport:
        def complete(self, payload: dict) -> dict:
            return response

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=IncompleteTransport(), model="fixture-model", context_budget=100
    )

    with pytest.raises(AnalyzerInvalidResponseError):
        analyzer.analyze([account("1001", "needs analysis")], SampleSet("v1", ()))


def test_openai_transport_sends_batch_request_with_authentication() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-key"
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.Client(
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = OpenAICompatibleTransport(
            base_url="https://model.example/v1",
            api_key="fixture-key",
            model="fixture-model",
            client=client,
        )
        assert transport.complete({"accounts": []}) == {
            "choices": [{"message": {"content": "{}"}}]
        }
    finally:
        client.close()

    assert len(requests) == 1


def test_openai_transport_retries_transient_http_status() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        transport = OpenAICompatibleTransport(
            base_url="https://model.example/v1",
            api_key=None,
            model="fixture-model",
            client=client,
            max_retries=1,
            retry_backoff=0,
        )
        assert transport.complete({"accounts": []}) == {"results": []}
    finally:
        client.close()

    assert calls == 2


def test_openai_transport_rejects_invalid_http_json() -> None:
    client = httpx.Client(
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"not-json")
        ),
    )
    try:
        transport = OpenAICompatibleTransport(
            base_url="https://model.example/v1",
            api_key=None,
            model="fixture-model",
            client=client,
        )
        with pytest.raises(AnalyzerInvalidResponseError):
            transport.complete({"accounts": []})
    finally:
        client.close()


def test_oversized_uid_context_becomes_explicit_uncertain_result() -> None:
    class FailingTransport:
        def complete(self, _payload: dict) -> str:
            raise AssertionError("oversized account must not reach the model")

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=FailingTransport(), model="fixture-model", context_budget=40
    )
    result = analyzer.analyze(
        [account("1001", "针对詹姆斯的长评论 " * 80)],
        SampleSet("v1", ()),
    )

    assert result.batch_count == 0
    assert len(result.results) == 1
    assert result.results[0].uid == "1001"
    assert result.results[0].decision is AnalysisDecision.UNCERTAIN
    assert "context_overflow" in result.results[0].signals
