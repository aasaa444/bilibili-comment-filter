from service.cli import build_parser


def test_serve_defaults_to_the_frontend_build_output(monkeypatch) -> None:
    monkeypatch.delenv("BILIBILI_FILTER_WEB_ROOT", raising=False)

    args = build_parser().parse_args(["serve"])

    assert args.web_root == "dist/web"
