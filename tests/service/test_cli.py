import sys

from service import cli


def test_serve_defaults_to_the_frontend_build_output(monkeypatch) -> None:
    for name in (
        "BILIBILI_FILTER_HOST",
        "BILIBILI_FILTER_PORT",
        "BILIBILI_FILTER_DATABASE",
        "BILIBILI_FILTER_WEB_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    args = cli.build_parser().parse_args(["serve"])

    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.database == "data/bilibili-filter.sqlite3"
    assert args.web_root == "dist/web"


def test_serve_defaults_follow_environment(monkeypatch, tmp_path) -> None:
    database = tmp_path / "service.sqlite3"
    web_root = tmp_path / "web"
    monkeypatch.setenv("BILIBILI_FILTER_HOST", "0.0.0.0")
    monkeypatch.setenv("BILIBILI_FILTER_PORT", "9876")
    monkeypatch.setenv("BILIBILI_FILTER_DATABASE", str(database))
    monkeypatch.setenv("BILIBILI_FILTER_WEB_ROOT", str(web_root))

    args = cli.build_parser().parse_args(["serve"])

    assert args.host == "0.0.0.0"
    assert args.port == 9876
    assert args.database == str(database)
    assert args.web_root == str(web_root)


def test_main_passes_cli_configuration_to_application_and_uvicorn(monkeypatch, tmp_path) -> None:
    database = tmp_path / "service.sqlite3"
    web_root = tmp_path / "web"
    web_root.mkdir()
    application = object()
    captured: dict[str, object] = {}

    def fake_create_app(**kwargs):
        captured["create_app"] = kwargs
        return application

    def fake_run(app, **kwargs):
        captured["uvicorn_app"] = app
        captured["uvicorn_kwargs"] = kwargs

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setenv("BILIBILI_FILTER_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bilibili-filter",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "9876",
            "--database",
            str(database),
            "--web-root",
            str(web_root),
        ],
    )

    cli.main()

    assert captured["create_app"] == {
        "db_path": str(database),
        "web_root": web_root,
        "start_background_worker": True,
    }
    assert captured["uvicorn_app"] is application
    assert captured["uvicorn_kwargs"] == {
        "host": "0.0.0.0",
        "port": 9876,
        "log_level": "debug",
    }
