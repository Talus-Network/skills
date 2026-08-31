#!/usr/bin/env python3
"""Regression checks for the payment skill's source bootstrap boundary."""

from __future__ import annotations

import re
from pathlib import Path
import unittest


SKILL_PATH = Path(__file__).resolve().parents[1] / "nexus-cli-payment-tracking" / "SKILL.md"


class PaymentBootstrapTests(unittest.TestCase):
    def test_normal_bootstrap_uses_only_public_authorities(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        normal_commands = re.findall(
            r"prepare --only nexus-sdk --only nexus-move-packages --only sui --print-manifest-path",
            text,
        )
        self.assertEqual(len(normal_commands), 1)
        self.assertNotIn("prepare --print-manifest-path", text)

    def test_payment_guidance_uses_explicit_read_only_testnet_evidence(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("testnet_evidence.py", text)
        self.assertIn("--graphql-url", text)
        self.assertIn("read-only", text.lower())
        self.assertNotIn("optional_manifest", text)


if __name__ == "__main__":
    unittest.main()
