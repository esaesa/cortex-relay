import sys
import threading
import unittest
from pathlib import Path

from cortex_relay.runtime.process import ProcessCancelledError, ProcessRunner


class ProcessRunnerTests(unittest.TestCase):
    def test_process_can_be_cancelled(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.2, cancel_event.set)
        timer.start()
        try:
            with self.assertRaises(ProcessCancelledError):
                ProcessRunner().run(
                    [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(30)",
                    ],
                    cwd=Path.cwd(),
                    timeout_seconds=5,
                    cancel_event=cancel_event,
                )
        finally:
            timer.cancel()

    def test_process_returns_output(self):
        result = ProcessRunner().run(
            [sys.executable, "-c", "print('ok')"],
            cwd=Path.cwd(),
            timeout_seconds=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
