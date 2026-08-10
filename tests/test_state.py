from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fix_agent.config import load_config
from fix_agent.contract import parse_review_event
from fix_agent.state import StateStore
from test_contract import event


class StateTest(unittest.TestCase):
    def test_pause_blocks_claim_until_resume(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(self._config(), encoding="utf-8")
            config = load_config(config_path)
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parse_review_event(event()))
                state.set_worker_paused(True, "maintenance")
                paused = state.worker_control()
                blocked = state.claim_next(config.repositories)
                state.set_worker_paused(False)
                resumed = state.claim_next(config.repositories)

        self.assertTrue(paused.paused)
        self.assertEqual(paused.reason, "maintenance")
        self.assertIsNone(blocked)
        self.assertIsNotNone(resumed)

    def test_migrates_legacy_pause_trigger(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(self._config(), encoding="utf-8")
            config = load_config(config_path)
            with StateStore(config.state_dir) as state:
                state.connection.execute(
                    """
                    CREATE TRIGGER manual_pause_claims_20260810
                    BEFORE UPDATE OF status ON jobs
                    BEGIN
                        SELECT RAISE(ABORT, 'code-fix-agent manually paused');
                    END
                    """
                )
                state.connection.commit()
            with StateStore(config.state_dir) as state:
                control = state.worker_control()
                trigger = state.connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'trigger' AND name = 'manual_pause_claims_20260810'
                    """
                ).fetchone()

        self.assertTrue(control.paused)
        self.assertIsNone(trigger)

    def test_claims_review_event_as_one_batch_and_records_metrics(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(
                self._config().replace(
                    'github_token_env = "FIX_GITHUB_TOKEN"',
                    'github_token_env = "FIX_GITHUB_TOKEN"\n'
                    'publish_mode = "direct"\n'
                    'processing_mode = "review_batch"',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            value = event()
            value["findings"].append(
                {
                    **value["findings"][0],
                    "fingerprint": "sha256:" + "d" * 64,
                }
            )
            with StateStore(config.state_dir) as state:
                intake = state.accept(
                    config.repositories[0], parse_review_event(value)
                )
                batch = state.claim_next_batch(config.repositories)
                state.record_batch_metrics(
                    batch.id,
                    codex_calls=2,
                    input_tokens=30,
                    cached_input_tokens=10,
                    cache_write_input_tokens=0,
                    output_tokens=5,
                    reasoning_output_tokens=0,
                    total_tokens=35,
                    duration_ms=250,
                )
                run = state.batch_run(batch.id)
                finding_claim = state.claim_next(config.repositories)

        self.assertEqual(batch.id, intake.batch_id)
        self.assertEqual(len(batch.jobs), 2)
        self.assertEqual(run.status, "processing")
        self.assertEqual(run.codex_calls, 2)
        self.assertEqual(run.total_tokens, 35)
        self.assertIsNone(finding_claim)

    def test_routes_isolated_batch_finding_through_finding_claim(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(
                self._config().replace(
                    'github_token_env = "FIX_GITHUB_TOKEN"',
                    'github_token_env = "FIX_GITHUB_TOKEN"\n'
                    'publish_mode = "direct"\n'
                    'processing_mode = "review_batch"',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parse_review_event(event()))
                batch = state.claim_next_batch(config.repositories)
                shared_worktree = root / "shared-worktree"
                shared_worktree.mkdir()
                state.record_event(
                    batch.jobs[0].id,
                    "worktree_created",
                    "legacy batch worktree",
                    {
                        "path": str(shared_worktree),
                        "base_commit": batch.jobs[0].target_commit,
                    },
                )
                state.mark_finding_fallback_pending(
                    (batch.jobs[0].id,), "repeated harness failure"
                )
                pending_claim = state.claim_next(config.repositories)
                finding_resume = state.resumable_worktree(
                    batch.jobs[0].id, scope="finding"
                )
                batch_resume = state.resumable_worktree(
                    batch.jobs[0].id, scope="batch"
                )
                state.activate_finding_fallback(
                    (batch.jobs[0].id,), "repeated harness failure"
                )
                isolated = state.claim_next(config.repositories)

        self.assertIsNone(pending_claim)
        self.assertIsNone(finding_resume)
        self.assertEqual(batch_resume[0], str(shared_worktree))
        self.assertEqual(isolated.id, batch.jobs[0].id)
        self.assertEqual(isolated.fallback_finding, 1)
        self.assertEqual(isolated.attempts, 1)

    def test_restart_activates_pending_finding_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(
                self._config().replace(
                    'github_token_env = "FIX_GITHUB_TOKEN"',
                    'github_token_env = "FIX_GITHUB_TOKEN"\n'
                    'publish_mode = "direct"\n'
                    'processing_mode = "review_batch"',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parse_review_event(event()))
                batch = state.claim_next_batch(config.repositories)
                state.mark_finding_fallback_pending(
                    (batch.jobs[0].id,), "batch response failed"
                )
                recovered = state.recover_interrupted_jobs()
                resumed = state.claim_next(config.repositories)
                events = state.events(batch.jobs[0].id)

        self.assertEqual(recovered, (batch.jobs[0].id,))
        self.assertEqual(resumed.id, batch.jobs[0].id)
        self.assertEqual(resumed.fallback_finding, 1)
        self.assertIn(
            "fallback_recovery_scheduled",
            [item.event_type for item in events],
        )

    def test_worktree_removal_only_closes_the_matching_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(
                self._config().replace(
                    'github_token_env = "FIX_GITHUB_TOKEN"',
                    'github_token_env = "FIX_GITHUB_TOKEN"\n'
                    'publish_mode = "direct"\n'
                    'processing_mode = "review_batch"',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            shared = root / "shared"
            finding = root / "finding"
            shared.mkdir()
            finding.mkdir()
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parse_review_event(event()))
                batch = state.claim_next_batch(config.repositories)
                job = batch.jobs[0]
                state.record_event(
                    job.id,
                    "worktree_created",
                    "batch worktree created",
                    {
                        "path": str(shared),
                        "base_commit": job.target_commit,
                        "scope": "batch",
                    },
                )
                state.mark_finding_fallback_pending(
                    (job.id,), "batch response failed"
                )
                state.record_event(
                    job.id,
                    "worktree_created",
                    "finding worktree created",
                    {
                        "path": str(finding),
                        "base_commit": job.target_commit,
                        "scope": "finding",
                    },
                )
                state.record_event(
                    job.id,
                    "worktree_removed",
                    "batch worktree removed",
                    {"path": str(shared)},
                )
                finding_resume = state.resumable_worktree(
                    job.id, scope="finding"
                )
                batch_resume = state.resumable_worktree(job.id, scope="batch")

        self.assertEqual(finding_resume[0], str(finding))
        self.assertIsNone(batch_resume)

    def test_accepts_once_and_deduplicates_by_fingerprint(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(self._config(), encoding="utf-8")
            config = load_config(config_path)
            parsed = parse_review_event(event())
            with StateStore(config.state_dir) as state:
                first = state.accept(config.repositories[0], parsed)
                second = state.accept(config.repositories[0], parsed)
                claimed = state.claim_next(config.repositories)
                state.record_precheck(claimed.id, True, "A caller drops exit 1.")
                state.record_postcheck(claimed.id, True, "The failure now propagates.")
                jobs = state.jobs()
                events = state.events()
                after_first = state.events(after_id=events[0].id)
        self.assertEqual((first.created, first.duplicate), (1, 0))
        self.assertEqual((second.created, second.duplicate), (0, 1))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, "ready")
        self.assertEqual(jobs[0].precheck_reason, "A caller drops exit 1.")
        self.assertEqual(jobs[0].postcheck_reason, "The failure now propagates.")
        self.assertEqual(
            [item.id for item in after_first], [item.id for item in events[1:]]
        )
        self.assertIn("duplicate_received", [item.event_type for item in events])
        self.assertEqual(sorted(item.id for item in events), [item.id for item in events])

    def test_unlimited_job_retries_with_previous_failure_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(
                self._config().replace(
                    'test_commands = []',
                    'test_commands = []\n[repositories.execution]\nmax_attempts = 0',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            parsed = parse_review_event(event())
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parsed)
                first = state.claim_next(config.repositories)
                state.mark_failed(first.id, "harness failed", 0)
                second = state.claim_next(config.repositories)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.attempts, 2)
        self.assertEqual(second.last_error, "harness failed")
        self.assertIsNone(second.next_attempt_at)

    def test_retry_delay_allows_later_job_to_run(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(
                self._config().replace(
                    'test_commands = []',
                    'test_commands = []\n[repositories.execution]\nmax_attempts = 0',
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            parsed = parse_review_event(event())
            later_event = event()
            later_event["findings"][0]["fingerprint"] = "sha256:" + "d" * 64
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parsed)
                later = state.accept(
                    config.repositories[0], parse_review_event(later_event)
                )
                first = state.claim_next(config.repositories)
                state.mark_failed(first.id, "temporary failure", 3600)
                claimed = state.claim_next(config.repositories)
        self.assertEqual(claimed.id, later.job_ids[0])

    def test_three_workers_claim_distinct_jobs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(self._config(), encoding="utf-8")
            config = load_config(config_path)
            with StateStore(config.state_dir) as state:
                for character in "cde":
                    value = event()
                    value["findings"][0]["fingerprint"] = (
                        "sha256:" + character * 64
                    )
                    state.accept(
                        config.repositories[0], parse_review_event(value)
                    )

            def claim() -> int:
                with StateStore(config.state_dir) as state:
                    job = state.claim_next(config.repositories)
                return job.id

            with ThreadPoolExecutor(max_workers=3) as executor:
                claimed = list(executor.map(lambda _: claim(), range(3)))

        self.assertEqual(sorted(claimed), [1, 2, 3])

    def test_restart_recovers_in_progress_job_without_using_retry_budget(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "fix.toml"
            config_path.write_text(self._config(), encoding="utf-8")
            config = load_config(config_path)
            parsed = parse_review_event(event())
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parsed)
                first = state.claim_next(config.repositories)
                state.record_precheck(first.id, True, "The failure is valid.")
                state.mark_testing(first.id)
                recovered = state.recover_interrupted_jobs()
                interrupted = state.jobs(1)[0]
                resumed = state.claim_next(config.repositories)
                events = state.events(first.id)

        self.assertEqual(recovered, (first.id,))
        self.assertEqual(interrupted.status, "failed")
        self.assertEqual(interrupted.attempts, 0)
        self.assertEqual(resumed.id, first.id)
        self.assertEqual(resumed.attempts, first.attempts)
        self.assertIn("resume the recorded worktree", resumed.last_error)
        self.assertIn(
            "restart_recovery_scheduled",
            [item.event_type for item in events],
        )

    @staticmethod
    def _config() -> str:
        return """
version = 1
state_dir = ".state"
[server]
token_env = "FIX_TOKEN"
[[repositories]]
id = "repo"
github = "owner/repo"
branch = "main"
local_path = "repo"
github_token_env = "FIX_GITHUB_TOKEN"
test_commands = []
"""


if __name__ == "__main__":
    unittest.main()
