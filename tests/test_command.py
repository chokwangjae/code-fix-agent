from pathlib import Path
import os
import signal
from tempfile import TemporaryDirectory
import time
import unittest

from fix_agent.command import CommandRunner
from fix_agent.errors import FixAgentError


class CommandRunnerTest(unittest.TestCase):
    def test_writes_configured_input_to_stdin(self) -> None:
        result = CommandRunner().run(
            ["sh", "-c", "IFS= read -r value; printf 'received:%s' \"$value\""],
            input_text="review prompt\n",
        )

        self.assertEqual(result.stdout, "received:review prompt")

    def test_timeout_terminates_descendant_process_group(self) -> None:
        with TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            script = f"sleep 30 & echo $! > {pid_file}; wait"

            with self.assertRaisesRegex(FixAgentError, "timed out"):
                CommandRunner().run(
                    ["sh", "-c", script],
                    timeout_seconds=1,
                )

            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self._is_running(child_pid):
                time.sleep(0.05)

        self.assertFalse(self._is_running(child_pid))

    @staticmethod
    def _is_running(pid: int) -> bool:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            return False
        return True


if __name__ == "__main__":
    unittest.main()
