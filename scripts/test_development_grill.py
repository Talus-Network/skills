#!/usr/bin/env python3
"""Regression tests for the development-skill task-contract guidance."""

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
CONTRACT_HEADING = "## Task contract before setup"
CONTRACT_MARKERS = (
    "goal and outcome",
    "observable requirements",
    "integration boundary",
    "non-goals",
    "authorization",
    "acceptance evidence",
    "compact implementation design",
    "reversible assumptions",
    "only when an unresolved material",
    "pause dependent work",
    "useful authorized independent",
    "no default grilling ritual",
)


class DevelopmentContractTests(unittest.TestCase):
    def test_each_development_skill_contract_routes_before_setup_without_ceremony(self) -> None:
        for skill in DEVELOPMENT_SKILLS:
            text = (ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
            lowered = text.casefold()
            with self.subTest(skill=skill):
                self.assertIn(CONTRACT_HEADING.casefold(), lowered)
                contract_start = lowered.index(CONTRACT_HEADING.casefold())
                route_index = lowered.index("## route before setup", contract_start)
                source_index = lowered.index("## public source", route_index)
                self.assertLess(contract_start, route_index)
                self.assertLess(route_index, source_index)
                contract = lowered[contract_start:route_index]
                for marker in CONTRACT_MARKERS:
                    self.assertIn(marker.casefold(), contract, marker)
                self.assertNotIn("shared understanding complete", lowered)
                self.assertNotIn("ask exactly one question", lowered)
                self.assertNotIn("do not run development commands", lowered)
                self.assertNotIn("grill-me/skill", lowered)
                self.assertNotIn("skill://", lowered)

    def test_ambiguous_and_fully_specified_cases_describe_material_questions(self) -> None:
        for skill in DEVELOPMENT_SKILLS:
            document = json.loads((ROOT / skill / "evals/evals.json").read_text(encoding="utf-8"))
            cases = {entry["id"]: entry for entry in document["evals"]}
            prefix = EVAL_PREFIXES[skill]
            ambiguous = cases[f"{prefix}-grill-ambiguous"]
            specified = cases[f"{prefix}-grill-fully-specified"]
            routine = cases[f"{prefix}-routine-" + ("output-review" if prefix == "offchain" else "witness-decoder-review" if prefix == "onchain" else "artifact-review")]
            with self.subTest(skill=skill, case="ambiguous"):
                ambiguous_text = json.dumps(ambiguous).casefold()
                self.assertIn("material", ambiguous_text)
                self.assertIn("reversible", ambiguous_text)
                self.assertNotIn("exactly one question", ambiguous_text)
                self.assertTrue(any("public" in item.casefold() and "resolv" in item.casefold() for item in ambiguous["expectations"]))
            with self.subTest(skill=skill, case="fully-specified"):
                specified_text = json.dumps(specified).casefold()
                self.assertIn("compact contract", specified_text)
                self.assertIn("no clarification question", specified_text)
                self.assertTrue(any("before development actions" in item.casefold() for item in specified["expectations"]))
            with self.subTest(skill=skill, case="routine"):
                routine_text = json.dumps(routine).casefold()
                self.assertIn("without a ceremonial", routine_text)
                self.assertNotIn("shared understanding complete", routine_text)

    def test_observed_domain_repair_requirements_remain_in_catalogs(self) -> None:
        offchain = json.loads((ROOT / "nexus-offchain-tool-development/evals/evals.json").read_text(encoding="utf-8"))
        offchain_text = json.dumps(offchain).casefold()
        self.assertIn("every reachable output variant", offchain_text)
        self.assertIn("canonical invocation decoding", offchain_text)
        onchain = json.loads((ROOT / "nexus-onchain-tool-development/evals/evals.json").read_text(encoding="utf-8"))
        onchain_text = json.dumps(onchain).casefold()
        self.assertIn("nested bag witness decoder fixture", onchain_text)
        self.assertIn("actual nested witness uid", onchain_text)

    def test_eval_cases_do_not_add_an_external_grill_dependency(self) -> None:
        for skill in DEVELOPMENT_SKILLS:
            text = (ROOT / skill / "evals/evals.json").read_text(encoding="utf-8").casefold()
            with self.subTest(skill=skill):
                self.assertNotIn("skill://", text)
                self.assertNotIn("install a grill", text)
                self.assertNotIn("grill-me/", text)


if __name__ == "__main__":
    unittest.main()
