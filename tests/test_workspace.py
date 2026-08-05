from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from fix_agent.command import CommandRunner
from fix_agent.config import load_config
from fix_agent.errors import FixAgentError
from fix_agent.workspace import FixWorkspace
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
    def _config(root: Path, repository: Path):
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
branch = "main"
local_path = "{repository}"
github_token_env = "FIX_GITHUB_TOKEN"
test_commands = []
""",
            encoding="utf-8",
        )
        return load_config(path)


if __name__ == "__main__":
    unittest.main()
