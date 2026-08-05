from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fix_agent.config import load_config
from fix_agent.contract import parse_review_event
from fix_agent.state import StateStore
from test_contract import event


class StateTest(unittest.TestCase):
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
