#!/usr/bin/env python3
"""Behavioral contract tests for the embedded development requirements phase."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_SKILLS = (
    "nexus-offchain-tool-development",
    "nexus-onchain-tool-development",
    "nexus-tap-development",
)
EVAL_PREFIXES = {
    "nexus-offchain-tool-development": "offchain",
    "nexus-onchain-tool-development": "onchain",
    "nexus-tap-development": "tap",
}
GATE_HEADING = "## Embedded `$grill-me` requirements/design phase"
REQUIRED_MARKERS = (
    "goal/outcome",
    "requirements and observable behavior",
    "inputs and integration boundary",
    "non-goals",
    "authorization",
    "acceptance evidence/tests",
    "compact implementation design",
    "exactly one question at a time",
    "recommended answer",
    "approved public docs",
    "shared understanding complete",
    "do not install or invoke another skill",
    "do not run development commands",
)
FIRST_DEVELOPMENT_MARKERS = (
    "SKILLS_BUNDLE_ROOT=",
    "SOURCE_HELPER=",
    "source_prepare_status=",
    "scripts/prepare_sources.py",
    "scripts/testnet_evidence.py",
)


def skill_text(skill: str) -> str:
    return (ROOT / skill / "SKILL.md").read_text(encoding="utf-8")


class DevelopmentGrillTests(unittest.TestCase):
    def test_each_development_skill_has_a_self_contained_gate_before_actions(self) -> None:
        for skill in DEVELOPMENT_SKILLS:
            text = skill_text(skill)
            lowered = text.casefold()
            with self.subTest(skill=skill):
                self.assertIn(GATE_HEADING.casefold(), lowered)
                gate_start = lowered.index(GATE_HEADING.casefold())
                later = lowered[gate_start:]
                setup_index = lowered.index("for version-sensitive setup")
                self.assertLess(gate_start, setup_index)
                for marker in REQUIRED_MARKERS:
                    self.assertIn(marker.casefold(), later, marker)
                action_positions = [lowered.index(marker.casefold()) for marker in FIRST_DEVELOPMENT_MARKERS if marker.casefold() in lowered]
                self.assertTrue(action_positions)
                self.assertLess(gate_start, min(action_positions))
                prefix = lowered[:gate_start]
                self.assertFalse(any(marker.casefold() in prefix for marker in FIRST_DEVELOPMENT_MARKERS))
                self.assertNotIn("pip install", later)
                self.assertNotIn("grill-me/skill", later)
                self.assertNotIn("skill://", later)

    def test_gate_requires_one_question_and_recommends_without_batching(self) -> None:
        for skill in DEVELOPMENT_SKILLS:
            lowered = skill_text(skill).casefold()
            with self.subTest(skill=skill):
                self.assertRegex(lowered, r"ask exactly one question at a time")
                self.assertIn("include a recommended answer and why", lowered)
                self.assertIn("wait for the answer", lowered)
                self.assertIn("update the contract", lowered)

    def test_ambiguous_and_fully_specified_eval_cases_cover_gate_behavior(self) -> None:
        for skill in DEVELOPMENT_SKILLS:
            document = json.loads((ROOT / skill / "evals/evals.json").read_text(encoding="utf-8"))
            cases = {entry["id"]: entry for entry in document["evals"]}
            prefix = EVAL_PREFIXES[skill]
            ambiguous = cases[f"{prefix}-grill-ambiguous"]
            specified = cases[f"{prefix}-grill-fully-specified"]
            with self.subTest(skill=skill, case="ambiguous"):
                ambiguous_text = json.dumps(ambiguous).casefold()
                self.assertIn("exactly one", ambiguous_text)
                self.assertIn("recommended", ambiguous_text)
                self.assertIn("no-action", ambiguous_text)
                self.assertIn("before", ambiguous_text)
                self.assertTrue(any("public" in item.casefold() and "resolv" in item.casefold() for item in ambiguous["expectations"]))
            with self.subTest(skill=skill, case="fully-specified"):
                specified_text = json.dumps(specified).casefold()
                self.assertIn("complete contract", specified_text)
                self.assertIn("compact design", specified_text)
                self.assertIn("no material clarification question", specified_text)
                self.assertTrue(any("before development actions" in item.casefold() for item in specified["expectations"]))
                self.assertTrue(any("public-source facts" in item.casefold() for item in specified["expectations"]))

    def test_eval_cases_do_not_add_an_external_grill_dependency(self) -> None:
        for skill in DEVELOPMENT_SKILLS:
            text = (ROOT / skill / "evals/evals.json").read_text(encoding="utf-8").casefold()
            with self.subTest(skill=skill):
                self.assertNotIn("skill://", text)
                self.assertNotIn("install a grill", text)
                self.assertNotIn("grill-me/", text)


if __name__ == "__main__":
    unittest.main()
