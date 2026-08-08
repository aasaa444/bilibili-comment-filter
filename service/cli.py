from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bilibili-filter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="start the local API and background worker")
    serve.add_argument("--host", default=os.getenv("BILIBILI_FILTER_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("BILIBILI_FILTER_PORT", "8765")))
    serve.add_argument(
        "--database",
        default=os.getenv("BILIBILI_FILTER_DATABASE", "data/bilibili-filter.sqlite3"),
    )
    serve.add_argument(
        "--web-root",
        default=os.getenv("BILIBILI_FILTER_WEB_ROOT", "web/dist"),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command != "serve":
        raise SystemExit(2)
    web_root = Path(args.web_root)
    application = create_app(
        db_path=args.database,
        web_root=web_root if web_root.exists() else None,
        start_background_worker=True,
    )
    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        log_level=os.getenv("BILIBILI_FILTER_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
