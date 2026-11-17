#!/usr/bin/env python3
"""Regression checks for the payment skill's source bootstrap boundary."""

from __future__ import annotations

import re
from pathlib import Path
import unittest


SKILL_PATH = Path(__file__).resolve().parents[1] / "nexus-cli-payment-tracking" / "SKILL.md"
SOURCE_REFERENCE_PATH = SKILL_PATH.parent / "references" / "source-preparation.md"


class PaymentBootstrapTests(unittest.TestCase):
    def test_normal_bootstrap_uses_only_public_authorities(self) -> None:
        text = SOURCE_REFERENCE_PATH.read_text(encoding="utf-8")
        blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
        bootstrap = next(
            (block for block in blocks if "trap cleanup_sources EXIT" in block),
            None,
        )
        self.assertIsNotNone(bootstrap)
        expected_prepare = (
            "prepare --only nexus-sdk "
            "--repo nexus-sdk=v2.2.0 "
            "--checksum nexus-sdk=a213e13580e060754acc3bdfe57ed6bde6f85ea5727b43a47cf2bd0e722d091f "
            "--only nexus-move-packages --only sui --print-manifest-path"
        )
        self.assertEqual(bootstrap.count(expected_prepare), 1)
        self.assertNotIn("prepare --print-manifest-path", bootstrap)
        self.assertIn("testnet_evidence.py", text)
        self.assertIn("--graphql-url https://graphql.testnet.sui.io/graphql", text)
        self.assertIn("read-only", text.lower())

    def test_payment_guidance_uses_explicit_read_only_testnet_evidence(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("testnet_evidence.py", text)
        self.assertIn("--graphql-url", text)
        self.assertIn("read-only", text.lower())
        self.assertNotIn("optional_manifest", text)


if __name__ == "__main__":
    unittest.main()
