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
            "--repo nexus-sdk=c996eda4f125989cbec4f1cdc5d5e5235a558167 "
            "--checksum nexus-sdk=d273e64844b93efe5543200b2c0ae6fda3d49ee7e043a5046572ce74665d247f "
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
