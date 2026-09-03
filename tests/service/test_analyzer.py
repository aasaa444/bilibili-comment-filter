import json

import httpx
import pytest

from service.analyzer import (
    AccountBundle,
    AnalysisDecision,
    AnalyzerContextLimitError,
    AnalyzerInvalidResponseError,
    AnalyzerTimeoutError,
    CommentForAnalysis,
    OpenAICompatibleBatchAnalyzer,
    OpenAICompatibleTransport,
    RuleCatalog,
    RuleEngine,
    SampleInjector,
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
        context_budget=1050,
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


def test_analyzer_caps_large_uid_batches_before_model_call() -> None:
    transport = RecordingModelTransport()
    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=transport,
        model="fixture-model",
        context_budget=100000,
        max_batch_accounts=2,
    )

    result = analyzer.analyze(
        [account(str(1000 + index), "ordinary discussion") for index in range(5)],
        SampleSet("v1", ()),
    )

    assert [len(request["accounts"]) for request in transport.requests] == [2, 2, 1]
    assert result.batch_count == 3
    assert len(result.results) == 5


def test_analyzer_splits_a_timed_out_large_batch_and_keeps_results() -> None:
    class TimeoutTransport:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def complete(self, payload: dict) -> str:
            self.calls.append(len(payload["accounts"]))
            if len(payload["accounts"]) > 1:
                raise AnalyzerTimeoutError("fixture read timeout")
            item = payload["accounts"][0]
            return json.dumps(
                {
                    "results": [
                        {
                            "uid": item["uid"],
                            "decision": "uncertain",
                            "evidence_comment_ids": [item["comments"][0]["comment_id"]],
                            "reason": "fixture review",
                            "confidence": 0.5,
                            "model_version": "fixture-model",
                        }
                    ]
                }
            )

    transport = TimeoutTransport()
    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=transport,
        model="fixture-model",
        context_budget=100000,
        max_batch_accounts=100,
    )

    result = analyzer.analyze(
        [account(str(1000 + index), "ordinary discussion") for index in range(4)],
        SampleSet("v1", ()),
    )

    assert transport.calls == [4, 2, 1, 1, 2, 1, 1]
    assert result.batch_count == 7
    assert {item.uid for item in result.results} == {"1000", "1001", "1002", "1003"}


def test_analyzer_splits_a_batch_after_model_validation_failure() -> None:
    class ValidationTransport:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def complete(self, payload: dict) -> str:
            self.calls.append(len(payload["accounts"]))
            item = payload["accounts"][0]
            result = {
                "uid": item["uid"],
                "decision": "uncertain",
                "evidence_comment_ids": [item["comments"][0]["comment_id"]],
                "reason": "fixture review",
                "confidence": 0.5,
                "model_version": "fixture-model",
            }
            if len(payload["accounts"]) > 1:
                result.pop("evidence_comment_ids")
            return json.dumps({"results": [result]})

    transport = ValidationTransport()
    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=transport,
        model="fixture-model",
        context_budget=100000,
        max_batch_accounts=100,
    )

    result = analyzer.analyze(
        [account(str(1000 + index), "ordinary discussion") for index in range(4)],
        SampleSet("v1", ()),
    )

    assert transport.calls == [4, 2, 1, 1, 2, 1, 1]
    assert result.batch_count == 7
    assert {item.uid for item in result.results} == {"1000", "1001", "1002", "1003"}


def test_rule_engine_prioritizes_friendly_exception_and_requires_context_for_terms() -> None:
    engine = RuleEngine(
        RuleCatalog(
            version="rules-v1",
            known_terms=("巴斯特", "䟋", "天龙八部", "粘慕斯"),
            friendly_exceptions=("曼巴斯特",),
            nickname_positive=("典型詹黑昵称",),
            standalone_terms=(),
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


@pytest.mark.parametrize("term", ["巴斯特", "䟋", "天龙八部", "粘慕斯"])
def test_default_catalog_treats_known_terms_as_standalone_high_confidence_hits(term: str) -> None:
    result = RuleEngine().evaluate(account("1005", term))

    assert result is not None
    assert result.decision is AnalysisDecision.HIT
    assert result.signals == (f"known_term_standalone:{term}",)
    assert result.confidence == 0.96


def test_friendly_exception_still_overrides_standalone_term() -> None:
    result = RuleEngine().evaluate(account("1006", "曼巴斯特"))

    assert result is not None
    assert result.decision is AnalysisDecision.NON_TARGET
    assert result.signals == ("friendly_exception:曼巴斯特",)


def test_published_nickname_sample_becomes_a_hard_rule_without_model_call() -> None:
    transport = RecordingModelTransport()
    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=transport,
        model="fixture-model",
        context_budget=1000,
    )
    target = AccountBundle(uid="1004", nickname="用户提供的詹黑型昵称", comments=())

    result = analyzer.analyze(
        (target,),
        SampleSet(
            "samples-v2",
            (SampleItem("n1", "nickname", "positive", "用户提供的詹黑型昵称"),),
        ),
    )

    assert result.batch_count == 0
    assert transport.requests == []
    assert result.results[0].decision is AnalysisDecision.HIT
    assert result.results[0].signals == ("nickname_hard_rule",)
    assert result.results[0].sample_version == "samples-v2"


def test_sample_injector_compresses_chinese_samples_within_budget() -> None:
    context = SampleInjector().prepare(
        SampleSet(
            "samples-v1",
            (SampleItem("s1", "comment", "positive", "詹黑样本" * 20),),
        ),
        (),
        20,
    )

    assert context.mode == "compressed"
    assert len(context.items) == 1
    assert len(context.items[0].content) <= 10


def test_analyzer_rejects_invalid_structured_model_response() -> None:
    class InvalidTransport:
        def complete(self, payload: dict) -> str:
            return "not-json"

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=InvalidTransport(), model="fixture-model", context_budget=1000
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
        transport=IncompleteTransport(), model="fixture-model", context_budget=1000
    )

    with pytest.raises(AnalyzerInvalidResponseError):
        analyzer.analyze([account("1001", "needs analysis")], SampleSet("v1", ()))


def test_analyzer_accepts_openai_text_content_segments() -> None:
    encoded = json.dumps(
        {
            "results": [
                {
                    "uid": "1001",
                    "decision": "uncertain",
                    "evidence_comment_ids": ["c1"],
                    "reason": "Needs review",
                    "confidence": 0.5,
                    "model_version": "fixture-model",
                }
            ]
        }
    )

    class SegmentedTransport:
        def complete(self, payload: dict) -> dict:
            midpoint = len(encoded) // 2
            return {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": encoded[:midpoint]},
                                {"type": "text", "text": encoded[midpoint:]},
                            ]
                        }
                    }
                ]
            }

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=SegmentedTransport(), model="fixture-model", context_budget=1000
    )

    result = analyzer.analyze([account("1001", "needs analysis")], SampleSet("v1", ()))

    assert result.results[0].uid == "1001"
    assert result.results[0].decision is AnalysisDecision.UNCERTAIN


def test_analyzer_recovers_when_output_limit_truncates_a_large_batch() -> None:
    class OutputLimitedTransport:
        max_output_tokens = 64

        def __init__(self) -> None:
            self.calls: list[int] = []

        def complete(self, payload: dict) -> str:
            self.calls.append(len(payload["accounts"]))
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
            encoded = json.dumps({"results": results})
            limit = self.max_output_tokens * 4
            return encoded if len(encoded) <= limit else encoded[:limit]

    transport = OutputLimitedTransport()
    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=transport,
        model="fixture-model",
        context_budget=100000,
    )

    result = analyzer.analyze(
        [account(str(1000 + index), "ordinary discussion") for index in range(24)],
        SampleSet("v1", ()),
    )

    assert len(result.results) == 24
    assert len(transport.calls) > 1


def test_analyzer_accepts_prefixed_fenced_json_model_content() -> None:
    encoded = json.dumps(
        {
            "results": [
                {
                    "uid": "1001",
                    "decision": "uncertain",
                    "evidence_comment_ids": ["c1"],
                    "reason": "Needs review",
                    "confidence": 0.5,
                    "model_version": "fixture-model",
                }
            ]
        }
    )

    class WrappedTransport:
        def complete(self, _payload: dict) -> str:
            return f"<think>analysis</think>\n```json\n{encoded}\n```"

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=WrappedTransport(), model="fixture-model", context_budget=1000
    )

    result = analyzer.analyze([account("1001", "needs analysis")], SampleSet("v1", ()))

    assert result.results[0].uid == "1001"
    assert result.results[0].decision is AnalysisDecision.UNCERTAIN


def test_analyzer_accepts_minimal_non_target_result() -> None:
    class MinimalTransport:
        def complete(self, _payload: dict) -> dict:
            return {"results": [{"uid": "1001", "decision": "non_target"}]}

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=MinimalTransport(), model="fixture-model", context_budget=1000
    )

    result = analyzer.analyze([account("1001", "ordinary discussion")], SampleSet("v1", ()))

    assert result.results[0].decision is AnalysisDecision.NON_TARGET
    assert result.results[0].evidence_comment_ids == ()
    assert result.results[0].confidence == 0.0
    assert result.results[0].model_version == "fixture-model"


def test_openai_transport_sends_batch_request_with_authentication() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-key"
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        assert body["max_tokens"] == 321
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
            max_output_tokens=321,
            client=client,
        )
        assert transport.complete({"accounts": []}) == {
            "choices": [{"message": {"content": "{}"}}]
        }
    finally:
        client.close()

    assert len(requests) == 1


def test_openai_transport_bounds_output_budget_to_batch_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 4096
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
            max_output_tokens=65535,
            client=client,
        )
        transport.complete(
            {
                "accounts": [{"uid": "1001"}, {"uid": "1002"}],
            }
        )
    finally:
        client.close()


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


def test_openai_transport_classifies_context_limit_without_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(413, json={"error": "request too large"})

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
            max_retries=3,
            retry_backoff=0,
        )
        with pytest.raises(AnalyzerContextLimitError):
            transport.complete({"accounts": []})
    finally:
        client.close()

    assert calls == 1


@pytest.mark.parametrize(
    "error_body",
    [
        {
            "error": {
                "code": "context_length_exceeded",
                "message": "The request exceeded the model context length",
            }
        },
        "maximum context length is 4096 tokens",
    ],
)
def test_openai_transport_classifies_nested_or_text_context_error_without_retry(
    error_body: dict | str,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if isinstance(error_body, str):
            return httpx.Response(400, content=error_body.encode())
        return httpx.Response(400, json=error_body)

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
            max_retries=3,
            retry_backoff=0,
        )
        with pytest.raises(AnalyzerContextLimitError):
            transport.complete({"accounts": []})
    finally:
        client.close()

    assert calls == 1


def test_openai_transport_retries_timeout_and_raises_unavailable() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture timeout")

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
        with pytest.raises(AnalyzerTimeoutError, match="fixture timeout"):
            transport.complete({"accounts": []})
    finally:
        client.close()

    assert calls == 2


def test_analyzer_routes_nested_context_error_to_batch_splitting() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        payload = json.loads(request_body["messages"][1]["content"])
        accounts = payload["accounts"]
        calls.append(len(accounts))
        if len(accounts) > 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "maximum context length exceeded",
                    }
                },
            )
        item = accounts[0]
        response = {
            "results": [
                {
                    "uid": item["uid"],
                    "decision": "uncertain",
                    "evidence_comment_ids": [item["comments"][0]["comment_id"]],
                    "reason": "fixture review",
                    "confidence": 0.5,
                    "model_version": "fixture-model",
                }
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(response)}}]},
        )

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
            max_output_tokens=64,
            max_retries=3,
            retry_backoff=0,
        )
        analyzer = OpenAICompatibleBatchAnalyzer(
            transport=transport,
            model="fixture-model",
            context_budget=1000,
        )
        result = analyzer.analyze(
            [account("1001", "ordinary one"), account("1002", "ordinary two")],
            SampleSet("v1", ()),
        )
    finally:
        client.close()

    assert calls == [2, 1, 1]
    assert result.batch_count == 3
    assert {item.uid for item in result.results} == {"1001", "1002"}


def test_analyzer_splits_a_server_rejected_batch_and_keeps_single_uid_fallback() -> None:
    class SplittingTransport:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def complete(self, payload: dict) -> dict:
            self.calls.append(len(payload["accounts"]))
            if len(payload["accounts"]) > 1:
                raise AnalyzerContextLimitError("fixture context limit")
            item = payload["accounts"][0]
            return {
                "results": [
                    {
                        "uid": item["uid"],
                        "decision": "uncertain",
                        "evidence_comment_ids": [item["comments"][0]["comment_id"]],
                        "reason": "fixture review",
                        "confidence": 0.5,
                        "model_version": "fixture-model",
                    }
                ]
            }

    transport = SplittingTransport()
    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=transport,
        model="fixture-model",
        context_budget=2000,
    )

    result = analyzer.analyze(
        [account("1001", "ordinary one"), account("1002", "ordinary two")],
        SampleSet("v1", ()),
    )

    assert transport.calls == [2, 1, 1]
    assert result.batch_count == 3
    assert {item.uid for item in result.results} == {"1001", "1002"}


def test_analyzer_keeps_a_single_uid_when_the_server_still_rejects_context() -> None:
    class RejectingTransport:
        def complete(self, _payload: dict) -> dict:
            raise AnalyzerContextLimitError("fixture context limit")

    analyzer = OpenAICompatibleBatchAnalyzer(
        transport=RejectingTransport(),
        model="fixture-model",
        context_budget=1000,
    )

    result = analyzer.analyze([account("1001", "ordinary")], SampleSet("v1", ()))

    assert result.batch_count == 1
    assert result.results[0].decision is AnalysisDecision.UNCERTAIN
    assert result.results[0].signals == ("context_overflow", "server_context_limit")


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
