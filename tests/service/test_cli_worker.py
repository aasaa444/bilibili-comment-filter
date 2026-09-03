import sys

import pytest

from service import cli


@pytest.mark.parametrize(
    ("configured_value", "expected"),
    [
        (None, True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_main_passes_worker_setting_to_create_app(
    monkeypatch, configured_value: str | None, expected: bool
) -> None:
    captured: dict[str, object] = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    def fake_run(*_args, **_kwargs):
        return None

    monkeypatch.delenv("BILIBILI_FILTER_WORKER_ENABLED", raising=False)
    if configured_value is not None:
        monkeypatch.setenv("BILIBILI_FILTER_WORKER_ENABLED", configured_value)
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["bilibili-filter", "serve"])

    cli.main()

    assert captured["start_background_worker"] is expected


def test_main_rejects_unknown_worker_setting(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_FILTER_WORKER_ENABLED", "maybe")
    monkeypatch.setattr(sys, "argv", ["bilibili-filter", "serve"])

    with pytest.raises(ValueError, match="BILIBILI_FILTER_WORKER_ENABLED"):
        cli.main()
