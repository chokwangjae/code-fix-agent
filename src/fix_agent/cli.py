from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from .config import load_config
from .errors import FixAgentError
from .server import IntakeApplication, serve
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fix-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="receive validated review events")
    serve_parser.add_argument("--config", required=True, type=Path)

    submit_parser = subparsers.add_parser("submit", help="import one review event file")
    submit_parser.add_argument("--config", required=True, type=Path)
    submit_parser.add_argument("--file", required=True, type=Path)

    jobs_parser = subparsers.add_parser("jobs", help="list recent fix jobs")
    jobs_parser.add_argument("--config", required=True, type=Path)
    jobs_parser.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "serve":
            serve(config)
            return 0
        if args.command == "submit":
            try:
                payload = json.loads(args.file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FixAgentError(f"cannot load review event {args.file}: {exc}") from exc
            result = IntakeApplication(config).submit(payload)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.limit < 1:
            raise FixAgentError("--limit must be a positive integer")
        with StateStore(config.state_dir) as state:
            jobs = state.jobs(args.limit)
        for job in jobs:
            print(
                f"{job.id}\t{job.status}\t{job.repository}@{job.branch}\t"
                f"{job.fingerprint}\t{job.file}:{job.line}"
            )
        return 0
    except (FixAgentError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
