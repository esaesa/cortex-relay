"""Version consistency between code, packaging metadata, and the doctor."""

from __future__ import annotations

import io
import re
import tomllib
import unittest

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cortex_relay import __version__
from cortex_relay.cli import _doctor
from cortex_relay.diagnostics import (
    installed_distribution_version,
    version_check,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class PyprojectVersionTests(unittest.TestCase):
    def _project_config(self) -> dict:
        raw = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        return tomllib.loads(raw)

    def test_project_declares_a_dynamic_version(self):
        config = self._project_config()
        project = config["project"]
        self.assertNotIn("version", project, "pyproject must not pin a static version")
        self.assertIn("version", project.get("dynamic", []))
        dynamic = config["tool"]["setuptools"]["dynamic"]
        self.assertEqual(
            dynamic["version"],
            {"attr": "cortex_relay.__version__"},
        )

    def test_module_version_is_a_release_version(self):
        self.assertRegex(
            __version__,
            r"^\d+\.\d+\.\d+(?:[a-z]+\d*)?(?:\.\d+)*$",
        )


class InstalledMetadataTests(unittest.TestCase):
    def test_installed_distribution_matches_the_module_version(self):
        installed = installed_distribution_version()
        self.assertIsNotNone(
            installed,
            "cortex-relay is not installed; run `pip install -e .`",
        )
        self.assertEqual(
            installed,
            __version__,
            "installed metadata is stale; run `pip install -e .`",
        )


class VersionCheckTests(unittest.TestCase):
    def test_matching_versions_are_ok(self):
        check = version_check(module_version="1.2.3", installed_version="1.2.3")
        self.assertTrue(check.ok)
        self.assertEqual(check.name, "version:consistency")
        self.assertIn("1.2.3", check.detail)

    def test_mismatched_versions_are_reported(self):
        check = version_check(module_version="1.2.3", installed_version="1.2.2")
        self.assertFalse(check.ok)
        self.assertIn("module=1.2.3", check.detail)
        self.assertIn("distribution=1.2.2", check.detail)
        self.assertIn("pip install -e .", check.detail)

    def test_missing_metadata_is_reported(self):
        check = version_check(module_version="1.2.3", installed_version=None)
        self.assertFalse(check.ok)
        self.assertIn("distribution=missing", check.detail)


class DoctorVersionTests(unittest.TestCase):
    def _run_doctor(self, installed: str | None) -> tuple[int, str]:
        buffer = io.StringIO()
        with (
            patch("cortex_relay.cli.configuration_checks", return_value=[]),
            patch("cortex_relay.cli.runtime_checks", return_value=[]),
            patch("cortex_relay.cli.installed_distribution_version", return_value=installed),
        ):
            with redirect_stdout(buffer):
                code = _doctor(
                    provider="codex",
                    scope="user",
                    project_dir=REPO_ROOT,
                    runtime_only=False,
                )
        return code, buffer.getvalue()

    def test_matching_version_is_printed_as_ok(self):
        code, output = self._run_doctor(__version__)
        self.assertEqual(code, 0)
        self.assertIn("version:consistency", output)
        self.assertNotIn("WARN", output)
        self.assertRegex(output, r"OK\s+version:consistency")

    def test_stale_install_warns_without_failing_the_doctor(self):
        code, output = self._run_doctor("0.0.1")
        self.assertEqual(code, 0)
        self.assertRegex(output, r"WARN\s+version:consistency")
        self.assertIn("pip install -e .", output)

    def test_missing_metadata_warns_without_failing_the_doctor(self):
        code, output = self._run_doctor(None)
        self.assertEqual(code, 0)
        self.assertRegex(output, r"WARN\s+version:consistency")
        self.assertIn("distribution=missing", output)


class VersionFlagTests(unittest.TestCase):
    def test_version_flag_prints_the_module_version(self):
        from cortex_relay.cli import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(
            re.search(rf"\b{re.escape(__version__)}\b", buffer.getvalue()),
            buffer.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
