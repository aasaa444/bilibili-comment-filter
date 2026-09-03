from fastapi.testclient import TestClient

from service.analyzer import (
    AccountBundle,
    AnalysisDecision,
    CommentForAnalysis,
    FilterProfileContext,
    SampleSet,
    evaluate_rule_results,
)
from service.app import create_app
from service.db import Database
from service.profiles import DEFAULT_FILTER_PROFILE_ID, FilterProfileStore
from service.samples import NewSampleItem, SampleStore
from service.tasks import TaskStore


def test_default_profile_migrates_legacy_records_and_new_task_keeps_its_profile() -> None:
    database = Database(":memory:")
    database.initialize()
    profiles = FilterProfileStore(database)
    default = profiles.current()

    assert default.profile_id == DEFAULT_FILTER_PROFILE_ID
    assert default.name == "詹黑过滤"
    assert "巴斯特" in default.catalog.known_terms

    custom = profiles.create(
        name="广告评论过滤",
        description="识别重复广告和导流评论",
        standalone_terms=("加微信",),
    )
    profiles.activate(custom.profile_id)
    task, created = TaskStore(database).create(
        video_url="https://www.bilibili.com/video/BV1profile99",
        profile_id=profiles.current().profile_id,
    )

    assert created is True
    assert task.profile_id == custom.profile_id
    assert profiles.get(DEFAULT_FILTER_PROFILE_ID).name == "詹黑过滤"


def test_samples_are_scoped_to_their_profile() -> None:
    database = Database(":memory:")
    database.initialize()
    profiles = FilterProfileStore(database)
    samples = SampleStore(database, profiles)
    custom = profiles.create(name="剧透过滤", description="识别剧情剧透")
    draft = samples.create(
        kind="comment",
        label="positive",
        items=[NewSampleItem("大结局主角离开", "positive", "comment")],
        profile_id=custom.profile_id,
    )
    samples.publish(draft.sample_id)

    assert [item.content for item in samples.current(custom.profile_id).items] == ["大结局主角离开"]
    assert samples.current(DEFAULT_FILTER_PROFILE_ID).items == ()


def test_profile_api_creates_and_activates_a_custom_profile() -> None:
    app = create_app(db_path=":memory:")
    client = TestClient(app)

    initial = client.get("/api/profiles")
    created = client.post(
        "/api/profiles",
        json={
            "name": "广告评论过滤",
            "description": "识别广告与导流评论",
            "standalone_terms": ["加微信"],
        },
    )
    profile_id = created.json()["profile_id"]
    activated = client.post(f"/api/profiles/{profile_id}/activate")

    assert initial.status_code == 200
    assert initial.json()["items"][0]["profile_id"] == DEFAULT_FILTER_PROFILE_ID
    assert created.status_code == 201
    assert activated.status_code == 200
    assert activated.json()["is_current"] is True


def test_custom_profile_rules_are_used_by_the_analyzer() -> None:
    database = Database(":memory:")
    database.initialize()
    profiles = FilterProfileStore(database)
    profile = profiles.create(
        name="广告评论过滤",
        description="识别广告与导流评论",
        standalone_terms=("加微信",),
    )
    account = AccountBundle(
        uid="1001",
        nickname="广告账号",
        comments=(
            CommentForAnalysis("c1", "加微信领取资料", "c1", None, (), "https://example.test"),
        ),
    )
    results = evaluate_rule_results(
        (account,),
        SampleSet(
            "samples-empty",
            (),
            profile=FilterProfileContext(
                profile.profile_id,
                profile.name,
                profile.description,
                profile.catalog,
            ),
        ),
    )

    assert results[0].decision is AnalysisDecision.HIT
    assert results[0].signals == ("known_term_standalone:加微信",)
