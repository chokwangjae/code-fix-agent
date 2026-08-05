from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .config import load_config
from .errors import FixAgentError
from .notify import DiscordNotifier
from .server import IntakeApplication, serve
from .state import StateStore
from .worker import FixWorker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fix-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="receive validated review events")
    serve_parser.add_argument("--config", required=True, type=Path)

    submit_parser = subparsers.add_parser("submit", help="import one review event file")
    submit_parser.add_argument("--config", required=True, type=Path)
    submit_parser.add_argument("--file", required=True, type=Path)
    submit_parser.add_argument(
        "--run-now", action="store_true", help="process one queued job after import"
    )

    jobs_parser = subparsers.add_parser("jobs", help="list recent fix jobs")
    jobs_parser.add_argument("--config", required=True, type=Path)
    jobs_parser.add_argument("--limit", type=int, default=20)
    jobs_parser.add_argument("--json", action="store_true")

    events_parser = subparsers.add_parser(
        "events", help="list append-only job events from a global cursor"
    )
    events_parser.add_argument("--config", required=True, type=Path)
    events_parser.add_argument("--job-id", type=int)
    events_parser.add_argument("--after-id", type=int, default=0)
    events_parser.add_argument("--limit", type=int, default=500)
    events_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run-once", help="process one queued fix job")
    run_parser.add_argument("--config", required=True, type=Path)

    notify_parser = subparsers.add_parser(
        "notify-once", help="deliver pending Discord job events"
    )
    notify_parser.add_argument("--config", required=True, type=Path)
    notify_parser.add_argument("--max-events", type=int, default=100)
    notify_parser.add_argument(
        "--force", action="store_true", help="ignore the stored retry delay"
    )
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
            if args.run_now:
                FixWorker(config).run_once()
            return 0
        if args.command == "run-once":
            processed = FixWorker(config).run_once()
            print("processed one job" if processed else "no queued jobs")
            return 0
        if args.command == "notify-once":
            if args.max_events < 1:
                raise FixAgentError("--max-events must be a positive integer")
            notifier = DiscordNotifier(config)
            notifier.initialize_cursors()
            result = notifier.dispatch_pending(
                force=args.force, max_events=args.max_events
            )
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 1 if result.failed else 0
        if args.command == "events":
            if args.job_id is not None and args.job_id < 1:
                raise FixAgentError("--job-id must be a positive integer")
            if args.after_id < 0 or args.limit < 1:
                raise FixAgentError("--after-id and --limit are invalid")
            with StateStore(config.state_dir) as state:
                events = state.events(args.job_id, args.after_id, args.limit)
            if args.json:
                payload = []
                for event in events:
                    value = asdict(event)
                    value["details"] = json.loads(value.pop("details_json"))
                    payload.append(value)
                print(json.dumps(payload, ensure_ascii=False))
                return 0
            for event in events:
                print(
                    f"{event.id}\t{event.created_at}\t{event.status}\t"
                    f"{event.event_type}\t{event.message}"
                )
            return 0
        if args.limit < 1:
            raise FixAgentError("--limit must be a positive integer")
        with StateStore(config.state_dir) as state:
            jobs = state.jobs(args.limit)
        if args.json:
            print(json.dumps([asdict(job) for job in jobs], ensure_ascii=False))
            return 0
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
