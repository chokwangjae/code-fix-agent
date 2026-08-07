from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading
from typing import Any
from urllib.parse import urlsplit

from .config import AppConfig
from .contract import parse_review_event
from .credentials import resolve_credential
from .crontrol import CrontrolReporter
from .errors import FixAgentError
from .notify import DiscordNotifier
from .state import StateStore
from .worker import FixWorker


class IntakeApplication:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def submit(self, payload: Any) -> dict[str, Any]:
        event = parse_review_event(payload)
        repository = self.config.repository(event.repository, event.branch)
        with StateStore(self.config.state_dir) as state:
            if repository.discord.enabled:
                state.initialize_discord_cursor(repository.id)
            result = state.accept(repository, event)
        return {
            "job_ids": list(result.job_ids),
            "created": result.created,
            "duplicate": result.duplicate,
            "skipped": result.skipped,
        }


def serve(config: AppConfig) -> None:
    token = resolve_credential(
        config.server.token, config.server.token_env, "server token"
    )
    application = IntakeApplication(config)
    with StateStore(config.state_dir) as state:
        recovered_jobs = state.recover_interrupted_jobs()
    if recovered_jobs:
        print(
            "scheduled interrupted job recovery: "
            + ", ".join(str(job_id) for job_id in recovered_jobs)
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "code-fix-agent/0.1"

        def do_GET(self) -> None:
            if urlsplit(self.path).path != "/health":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._json(HTTPStatus.OK, {"status": "ok", "version": 1})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/reviews":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(authorization, expected):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
                return
            if length < 1 or length > config.server.max_body_bytes:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                result = application.submit(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            except FixAgentError as exc:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                return
            self._json(HTTPStatus.ACCEPTED, result)

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}")

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((config.server.host, config.server.port), Handler)
    notifier = DiscordNotifier(config)
    crontrol = CrontrolReporter(config)
    workers = [
        FixWorker(config, notifier=notifier, crontrol=crontrol)
        for _ in range(config.server.max_concurrent_jobs)
    ]
    worker_threads = [
        threading.Thread(
            target=worker.run_forever,
            name=f"fix-agent-worker-{index}",
            daemon=True,
        )
        for index, worker in enumerate(workers, start=1)
    ]
    for worker_thread in worker_threads:
        worker_thread.start()
    print(
        f"fix agent listening on {config.server.host}:{config.server.port} "
        f"with {len(workers)} worker(s)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for worker in workers:
            worker.stop()
        server.server_close()
        for worker_thread in worker_threads:
            worker_thread.join(timeout=5)
