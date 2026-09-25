import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from cortex_relay.core.models import TaskSpec
from cortex_relay.core.profiles import (
    ProfileResolver,
    runtime_config_from_mapping,
)


class ProfileConfigTests(unittest.TestCase):
    def test_preset_overlays_role_assignments(self):
        config = runtime_config_from_mapping(
            {
                "profiles": {
                    "muse": {
                        "provider": "opencode",
                        "model": "opencode/muse",
                        "reasoning": "xhigh",
                        "access": "read_only",
                    },
                    "luna": {
                        "provider": "opencode",
                        "model": "opencode/gpt-6-luna",
                        "reasoning": "max",
                    },
                },
                "roles": {
                    "orchestrator": "muse",
                    "implementer": "luna",
                },
                "presets": {
                    "quality": {
                        "orchestrator": "luna",
                        "roles": {"reviewer": "luna"},
                    }
                },
                "active_preset": "quality",
            }
        )

        roles = config.effective_roles()
        self.assertEqual(roles["orchestrator"], "luna")
        self.assertEqual(roles["implementer"], "luna")
        self.assertEqual(roles["reviewer"], "luna")

    def test_explicit_profile_wins_over_role_mapping(self):
        config = runtime_config_from_mapping(
            {
                "profiles": {
                    "muse": {"provider": "opencode"},
                    "luna": {"provider": "opencode"},
                },
                "roles": {"reviewer": "muse"},
            }
        )
        profile = config.profile_for_task(
            TaskSpec(objective="review", role="reviewer", profile="luna")
        )
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "luna")

    def test_explicit_legacy_provider_bypasses_role_mapping(self):
        config = runtime_config_from_mapping(
            {
                "profiles": {"muse": {"provider": "opencode"}},
                "roles": {"reviewer": "muse"},
            }
        )
        profile = config.profile_for_task(
            TaskSpec(
                objective="review",
                role="reviewer",
                provider="codex",
                model="gpt-6-sol",
            )
        )
        self.assertIsNone(profile)

    def test_unknown_profile_reference_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown profile"):
            runtime_config_from_mapping(
                {
                    "profiles": {"muse": {"provider": "opencode"}},
                    "roles": {"reviewer": "missing"},
                }
            )

    def test_fallback_chain_and_cycle_detection(self):
        config = runtime_config_from_mapping(
            {
                "profiles": {
                    "primary": {
                        "provider": "opencode",
                        "fallbacks": ["backup"],
                    },
                    "backup": {"provider": "codex"},
                }
            }
        )
        chain = config.fallback_chain(config.profiles["primary"])
        self.assertEqual([item.name for item in chain], ["primary", "backup"])

        cyclic = runtime_config_from_mapping(
            {
                "profiles": {
                    "a": {"provider": "opencode", "fallbacks": ["b"]},
                    "b": {"provider": "codex", "fallbacks": ["a"]},
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            cyclic.fallback_chain(cyclic.profiles["a"])

    def test_scheduler_budget_and_state_policy_parse(self):
        config = runtime_config_from_mapping(
            {
                "scheduler": {
                    "max_workers": 6,
                    "providers": {"opencode": 3, "codex": 1},
                    "profiles": {"premium": 1},
                },
                "budgets": {
                    "max_session_tokens": 500000,
                    "max_group_tokens": 150000,
                    "max_premium_tasks": 3,
                },
                "state": {
                    "retention_days": 14,
                    "max_completed_tasks": 250,
                    "max_event_log_mb": 8,
                    "cleanup_on_start": False,
                },
            }
        )

        self.assertEqual(config.scheduler.max_workers, 6)
        self.assertEqual(config.scheduler.provider_limits["opencode"], 3)
        self.assertEqual(config.scheduler.profile_limits["premium"], 1)
        self.assertEqual(config.budgets.max_group_tokens, 150000)
        self.assertEqual(config.state.retention_days, 14)
        self.assertFalse(config.state.cleanup_on_start)

    def test_invalid_scheduler_limit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            runtime_config_from_mapping(
                {"scheduler": {"providers": {"codex": 0}}}
            )

    def test_project_overrides_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            project = root / "repo"
            (fake_home / ".cortex-relay").mkdir(parents=True)
            (project / ".cortex-relay").mkdir(parents=True)
            (project / ".git").mkdir()

            (fake_home / ".cortex-relay" / "config.toml").write_text(
                """
[profiles.worker]
provider = "codex"
model = "gpt-6-luna"
reasoning = "high"

[roles]
reviewer = "worker"
""".strip(),
                encoding="utf-8",
            )
            (project / ".cortex-relay" / "config.toml").write_text(
                """
[profiles.worker]
provider = "opencode"
model = "opencode/gpt-6-luna"
reasoning = "max"
""".strip(),
                encoding="utf-8",
            )

            with patch("pathlib.Path.home", return_value=fake_home):
                config = ProfileResolver().load(project)

        self.assertEqual(config.profiles["worker"].provider, "opencode")
        self.assertEqual(config.profiles["worker"].reasoning, "max")
        self.assertEqual(config.roles["reviewer"], "worker")
        self.assertEqual(len(config.sources), 2)


if __name__ == "__main__":
    unittest.main()
