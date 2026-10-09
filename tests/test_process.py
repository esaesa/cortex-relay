import gc
import os
import sys
import tempfile
import threading
import unittest
import warnings
from unittest.mock import patch

from pathlib import Path

from cortex_relay.runtime.process import (
    ProcessCancelledError,
    ProcessIdleTimeoutError,
    ProcessRunner,
)


class ProcessRunnerTests(unittest.TestCase):
    def test_process_idle_timeout_is_distinct_from_absolute_timeout(self):
        with self.assertRaises(ProcessIdleTimeoutError) as caught:
            ProcessRunner().run(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(5)",
                ],
                cwd=Path.cwd(),
                timeout_seconds=4,
                idle_timeout_seconds=0.2,
            )
        self.assertAlmostEqual(caught.exception.idle_timeout_seconds, 0.2)

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

    def test_invalid_idle_timeout_never_spawns_process(self):
        with patch("cortex_relay.runtime.process.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "idle_timeout_seconds"):
                ProcessRunner().run(
                    [sys.executable, "-c", "pass"],
                    cwd=Path.cwd(), timeout_seconds=5, idle_timeout_seconds=0,
                )
            popen.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process-group cancellation")
    def test_cancel_terminates_process_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-finished.txt"
            child = (
                "import sys,time;time.sleep(1.1);"
                "open(sys.argv[1],'w').write('child-finished')"
            )
            parent = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);"
                "print('started',flush=True);time.sleep(30)"
            )
            cancel = threading.Event()
            timer = threading.Timer(0.3, cancel.set)
            timer.start()
            try:
                with self.assertRaises(ProcessCancelledError):
                    ProcessRunner().run(
                        [sys.executable, "-c", parent, child, str(marker)],
                        cwd=Path.cwd(), timeout_seconds=5, cancel_event=cancel,
                    )
            finally:
                timer.cancel()
            import time
            time.sleep(1.3)
            self.assertFalse(marker.exists(), "provider descendant outlived cancellation")

    def test_process_closes_pipe_wrappers(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            result = ProcessRunner().run(
                [sys.executable, "-c", "print('closed')"],
                cwd=Path.cwd(),
                timeout_seconds=5,
            )
            self.assertEqual(result.stdout.strip(), "closed")
            gc.collect()

        leaked = [
            warning
            for warning in caught
            if issubclass(warning.category, ResourceWarning)
            and "unclosed file" in str(warning.message).lower()
        ]
        self.assertEqual(leaked, [])

    def test_process_returns_output(self):
        result = ProcessRunner().run(
            [sys.executable, "-c", "print('ok')"],
            cwd=Path.cwd(),
            timeout_seconds=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_process_stdin_is_devnull(self):
        result = ProcessRunner().run(
            [sys.executable, "-c", "import sys; print('eof:' + repr(sys.stdin.read()))"],
            cwd=Path.cwd(),
            timeout_seconds=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "eof:''")

    def test_process_decodes_utf8_bytes_safely(self):
        lines = []
        result = ProcessRunner().run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write('route → Î\\n'.encode('utf-8'))",
            ],
            cwd=Path.cwd(),
            timeout_seconds=5,
            on_stdout_line=lines.append,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("→", result.stdout)
        self.assertEqual(lines, ["route → Î"])

    def test_process_does_not_hang_when_descendant_holds_pipe_open(self):
        result = ProcessRunner().run(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); print('done', flush=True)",
            ],
            cwd=Path.cwd(),
            timeout_seconds=3,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "done")




    @unittest.skipUnless(os.name == "nt", "Windows npm shim regression")
    def test_windows_cmd_shim_uses_sibling_powershell_without_shell_interpolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = root / "fake-provider.cmd"
            ps1 = root / "fake-provider.ps1"

            cmd.write_text("@echo off\r\necho should-not-run\r\n", encoding="utf-8")
            ps1.write_text(
                'Write-Output ("ok:" + $args[0])\nexit 0\n',
                encoding="utf-8",
            )

            result = ProcessRunner().run(
                [str(cmd), "hello & goodbye"],
                cwd=root,
                timeout_seconds=30,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok:hello & goodbye")
        self.assertEqual(result.argv, (str(cmd), "hello & goodbye"))


if __name__ == "__main__":
    unittest.main()
