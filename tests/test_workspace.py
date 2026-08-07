from pathlib import Path
import os
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest

from fix_agent.command import CommandRunner
from fix_agent.config import load_config
from fix_agent.contract import parse_review_event
from fix_agent.errors import FixAgentError
from fix_agent.state import StateStore
from fix_agent.workspace import FixWorkspace, reconcile_recorded_worktree
from fakes import job


class WorkspaceTest(unittest.TestCase):
    def test_verifies_claim_and_enforces_diff_policy(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = self._repository(root)
            config = self._config(root, repository_path)
            work_job = job(
                baseline_commit=baseline,
                target_commit=target,
                introducing_commit=target,
                file="src/app.py",
            )
            with FixWorkspace(
                CommandRunner(), config.repositories[0], work_job, config.state_dir
            ) as workspace:
                self.assertIsNone(workspace.finding_mismatch_reason())
                (workspace.path / "src/app.py").write_text("fixed\n", encoding="utf-8")
                summary = workspace.validate_diff()
        self.assertEqual(summary.files, ("src/app.py",))
        self.assertEqual(summary.added_lines + summary.deleted_lines, 2)

    def test_rejects_new_file_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = self._repository(root)
            config = self._config(root, repository_path)
            work_job = job(
                baseline_commit=baseline,
                target_commit=target,
                introducing_commit=target,
                file="src/app.py",
            )
            with FixWorkspace(
                CommandRunner(), config.repositories[0], work_job, config.state_dir
            ) as workspace:
                (workspace.path / "src/app.py").write_text("fixed\n", encoding="utf-8")
                (workspace.path / "src/new.py").write_text("new\n", encoding="utf-8")
                with self.assertRaisesRegex(FixAgentError, "new files are not allowed"):
                    workspace.validate_diff()

    def test_zero_change_limits_allow_any_diff_size(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = self._repository(root)
            config = self._config(
                root,
                repository_path,
                policy="""
[repositories.policy]
allowed_paths = ["**"]
max_changed_files = 0
max_changed_lines = 0
allow_new_files = true
""",
            )
            work_job = job(
                baseline_commit=baseline,
                target_commit=target,
                introducing_commit=target,
                file="src/app.py",
            )
            with FixWorkspace(
                CommandRunner(), config.repositories[0], work_job, config.state_dir
            ) as workspace:
                (workspace.path / "src/app.py").write_text(
                    "fixed\n" * 600, encoding="utf-8"
                )
                for index in range(12):
                    (workspace.path / f"src/new-{index}.py").write_text(
                        "new\n", encoding="utf-8"
                    )
                summary = workspace.validate_diff()

        self.assertEqual(len(summary.files), 13)
        self.assertGreater(summary.added_lines + summary.deleted_lines, 500)

    def test_restores_setup_side_effects_without_losing_existing_fix(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = self._repository(root)
            config = self._config(root, repository_path)
            work_job = job(
                baseline_commit=baseline,
                target_commit=target,
                introducing_commit=target,
                file="src/app.py",
            )
            with FixWorkspace(
                CommandRunner(), config.repositories[0], work_job, config.state_dir
            ) as workspace:
                source = workspace.path / "src/app.py"
                generated = workspace.path / "setup-generated.txt"
                source.write_text("intended fix\n", encoding="utf-8")
                before = workspace.working_tree_fingerprint()
                snapshots = workspace.snapshot_working_changes()

                source.write_text("setup rewrite\n", encoding="utf-8")
                generated.write_text("setup output\n", encoding="utf-8")
                restored = workspace.restore_working_changes(snapshots)

                self.assertEqual(workspace.working_tree_fingerprint(), before)
                self.assertEqual(source.read_text(encoding="utf-8"), "intended fix\n")
                self.assertFalse(generated.exists())

        self.assertEqual(restored, ("setup-generated.txt", "src/app.py"))

    def test_repairs_worktree_permissions_and_uses_private_runtime_caches(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = self._repository(root)
            config = self._config(root, repository_path)
            work_job = job(
                baseline_commit=baseline,
                target_commit=target,
                introducing_commit=target,
                file="src/app.py",
            )
            with FixWorkspace(
                CommandRunner(), config.repositories[0], work_job, config.state_dir
            ) as workspace:
                source = workspace.path / "src/app.py"
                source.parent.chmod(0o500)
                source.chmod(0o400)

                permissions = workspace.ensure_writable()
                environment = workspace.safe_environment
                source.write_text("permission repaired\n", encoding="utf-8")

                self.assertTrue(
                    stat.S_IMODE(source.stat().st_mode) & stat.S_IWUSR
                )
                self.assertTrue(
                    stat.S_IMODE(source.parent.stat().st_mode) & stat.S_IWUSR
                )
                self.assertGreaterEqual(permissions.repaired_files, 1)
                self.assertGreaterEqual(permissions.repaired_directories, 1)
                self.assertEqual(environment.get("HOME"), os.environ.get("HOME"))
                for name in (
                    "NPM_CONFIG_CACHE",
                    "GRADLE_USER_HOME",
                    "PUB_CACHE",
                    "PLAYWRIGHT_BROWSERS_PATH",
                    "CP_HOME_DIR",
                    "TMPDIR",
                ):
                    cache = Path(environment[name])
                    self.assertTrue(cache.is_dir(), name)
                    self.assertTrue(os.access(cache, os.R_OK | os.W_OK | os.X_OK), name)
                self.assertTrue(
                    Path(environment["NPM_CONFIG_CACHE"]).is_relative_to(
                        config.state_dir / "runtime-cache"
                    )
                )
                self.assertTrue(
                    Path(environment["TMPDIR"]).is_relative_to(workspace.root)
                )

    def test_reconciles_recorded_worktree_after_push_cleanup_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = self._repository(root)
            config = self._config(root, repository_path, publish_mode="direct")
            event = parse_review_event(
                {
                    "version": 1,
                    "repository": "owner/repo",
                    "branch": "main",
                    "baseline": baseline,
                    "target": target,
                    "findings": [
                        {
                            "fingerprint": "sha256:" + "c" * 64,
                            "severity": "Major",
                            "commit": target,
                            "file": "src/app.py",
                            "line": 1,
                            "cause": "Failure is swallowed.",
                            "solution": "Return the failure.",
                        }
                    ],
                }
            )
            with StateStore(config.state_dir) as state:
                job_id = state.accept(config.repositories[0], event).job_ids[0]
            checkout = config.state_dir / "worktrees" / "fix-reconcile" / "checkout"
            checkout.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(checkout), target],
                cwd=repository_path,
                check=True,
                capture_output=True,
            )

            complete = reconcile_recorded_worktree(
                CommandRunner(),
                config.repositories[0],
                config.state_dir,
                job_id,
                str(checkout),
            )
            with StateStore(config.state_dir) as state:
                events = state.events(job_id)

        self.assertTrue(complete)
        self.assertFalse(checkout.parent.exists())
        self.assertEqual(events[-1].event_type, "worktree_removed")
        self.assertIn('"reconciliation": true', events[-1].details_json)

    @staticmethod
    def _repository(root: Path) -> tuple[Path, str, str]:
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
        (repository / "src").mkdir()
        (repository / "src/app.py").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repository / "src/app.py").write_text("defect\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "target"], cwd=repository, check=True, capture_output=True)
        target = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()
        remote = root / "remote.git"
        subprocess.run(["git", "clone", "--bare", str(repository), str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repository, check=True)
        return repository, baseline, target

    @staticmethod
    def _config(
        root: Path,
        repository: Path,
        publish_mode: str = "pull_request",
        policy: str = "",
    ):
        path = root / "fix.toml"
        path.write_text(
            f"""
version = 1
state_dir = ".state"
[server]
token_env = "FIX_TOKEN"
[[repositories]]
id = "repo"
github = "owner/repo"
target_branch = "main"
local_path = "{repository}"
publish_mode = "{publish_mode}"
github_token = "test-only-token"
test_commands = []
git_author_name = "broken-agent"
git_author_email = "g_uapm@inswave.com"
{policy}
""",
            encoding="utf-8",
        )
        return load_config(path)


if __name__ == "__main__":
    unittest.main()
