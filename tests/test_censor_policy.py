import unittest

from shared.censoring import (
    CensorPolicySnapshot,
    profile_censor_policy,
    source_censor_policy,
)


class CensorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "video_censor_enabled": "1",
            "video_censor_method": "beep",
            "video_censor_keep_original": "1",
            "video_censor_padding_ms": "275",
        }

    def test_profile_policy_is_validated_and_snapshotted(self):
        policy = profile_censor_policy(self.settings)

        self.assertEqual(
            policy,
            CensorPolicySnapshot(
                enabled=True, method="beep", keep_original=True, padding_ms=275
            ),
        )
        self.assertEqual(CensorPolicySnapshot.from_payload(policy.to_payload()), policy)

    def test_profile_padding_is_clamped(self):
        settings = dict(self.settings, video_censor_padding_ms="1201")
        self.assertEqual(profile_censor_policy(settings).padding_ms, 1000)
        settings["video_censor_padding_ms"] = "-20"
        self.assertEqual(profile_censor_policy(settings).padding_ms, 0)

    def test_source_inherit_uses_all_profile_values(self):
        policy = source_censor_policy(
            self.settings,
            policy="inherit",
            legacy_enabled=False,
            method="duck",
            keep_original=False,
        )

        self.assertEqual(policy, profile_censor_policy(self.settings))

    def test_legacy_enabled_and_explicit_source_overrides(self):
        legacy = source_censor_policy(
            self.settings,
            policy="inherit",
            legacy_enabled=True,
            method="duck",
            keep_original=False,
        )
        disabled = source_censor_policy(
            self.settings,
            policy="disabled",
            legacy_enabled=True,
            method="beep",
            keep_original=True,
        )

        self.assertTrue(legacy.enabled)
        self.assertEqual(legacy.method, "duck")
        self.assertFalse(legacy.keep_original)
        self.assertFalse(disabled.enabled)


if __name__ == "__main__":
    unittest.main()
