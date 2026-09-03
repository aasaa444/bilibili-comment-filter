from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .app import create_app

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False

    raise ValueError(
        f"{name} must be one of 0, 1, false, true, no, yes, off, on; got {value!r}"
    )


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
        default=os.getenv("BILIBILI_FILTER_WEB_ROOT", "dist/web"),
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
        start_background_worker=_env_bool("BILIBILI_FILTER_WORKER_ENABLED", default=True),
    )
    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        log_level=os.getenv("BILIBILI_FILTER_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
