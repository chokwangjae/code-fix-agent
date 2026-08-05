import unittest

from fix_agent.contract import parse_review_event
from fix_agent.errors import FixAgentError


def event() -> dict:
    return {
        "version": 1,
        "repository": "owner/repo",
        "branch": "main",
        "baseline": "a" * 40,
        "target": "b" * 40,
        "findings": [
            {
                "fingerprint": "sha256:" + "c" * 64,
                "severity": "Major",
                "commit": "b" * 40,
                "file": "src/app.py",
                "line": 12,
                "cause": "Failure is swallowed.",
                "solution": "Return the failure.",
            }
        ],
    }


class ContractTest(unittest.TestCase):
    def test_parses_version_one_event(self) -> None:
        parsed = parse_review_event(event())
        self.assertEqual(parsed.target, "b" * 40)
        self.assertEqual(parsed.findings[0].file, "src/app.py")

    def test_rejects_parent_path(self) -> None:
        value = event()
        value["findings"][0]["file"] = "../secret"
        with self.assertRaisesRegex(FixAgentError, "repository-relative"):
            parse_review_event(value)

    def test_rejects_duplicate_fingerprint(self) -> None:
        value = event()
        value["findings"].append(dict(value["findings"][0]))
        with self.assertRaisesRegex(FixAgentError, "duplicate"):
            parse_review_event(value)


if __name__ == "__main__":
    unittest.main()
