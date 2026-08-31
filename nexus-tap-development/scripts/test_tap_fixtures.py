"""Regression tests for repository-owned direct and delayed TAP fixtures."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_contract import validate_fixture_contract  # noqa: E402
from verify_tap_artifacts import verify_tap_artifacts  # noqa: E402


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


class TapFixtureTests(unittest.TestCase):
    def test_direct_and_delayed_fixtures_are_independently_verifiable(self) -> None:
        for name in ("direct", "delayed"):
            with self.subTest(fixture=name):
                root = FIXTURE_ROOT / name
                contract = validate_fixture_contract(root)
                report = verify_tap_artifacts(root, require_artifacts=True)
                self.assertEqual(report["status"], "pass")
                self.assertTrue((root / "Move.toml").is_file())
                self.assertTrue(list((root / "sources").glob("*.move")))
                self.assertTrue(list((root / "tests").glob("*.move")))
                move_check = next(check for check in report["checks"] if check["id"] == "move-manifest")
                self.assertEqual(move_check["fixture_contract"]["fixture"], name)
                self.assertEqual(
                    move_check["fixture_contract"]["test_contract"]["test_inventory"],
                    {"success": 2, "expected_failure": 2, "total": 4},
                )
                self.assertEqual(contract["dependency_template"]["source"]["repository"], "MystenLabs/sui")
                self.assertEqual(
                    contract["dependency_template"]["rewrite"]["mode"],
                    "replace-template-with-authenticated-public-sui-framework",
                )

    def test_fixture_contracts_cover_success_and_expected_failure_branches(self) -> None:
        expected = {
            "direct": {
                "success": {"direct_path_produces_and_completes_output", "direct_path_handles_zero_input"},
                "failure": {"direct_begin_rejects_input_above_limit", "direct_execute_rejects_input_above_limit"},
            },
            "delayed": {
                "success": {"delayed_path_keeps_scheduled_input_until_follow_up", "delayed_path_has_distinct_follow_up_result"},
                "failure": {"delayed_schedule_rejects_input_above_limit", "delayed_execute_rejects_invalid_scheduled_state"},
            },
        }
        for name, branches in expected.items():
            with self.subTest(fixture=name):
                contract = validate_fixture_contract(FIXTURE_ROOT / name)
                tests = contract["test_contract"]
                self.assertEqual(set(tests["success_tests"]), branches["success"])
                self.assertEqual(
                    {entry["name"] for entry in tests["expected_failure_tests"]}, branches["failure"]
                )
                self.assertEqual(tests["test_inventory"]["total"], 4)

    def test_fixture_paths_and_bindings_are_distinct_and_nontrivial(self) -> None:
        direct = json.loads((FIXTURE_ROOT / "direct/artifacts/direct.dag.json").read_text(encoding="utf-8"))
        delayed = json.loads((FIXTURE_ROOT / "delayed/artifacts/delayed.dag.json").read_text(encoding="utf-8"))
        self.assertEqual(len(direct["vertices"]), 1)
        self.assertEqual(len(delayed["vertices"]), 2)
        self.assertEqual(len(delayed["edges"]), 1)
        self.assertNotEqual(direct["outputs"], delayed["outputs"])
        self.assertNotEqual(
            json.loads((FIXTURE_ROOT / "direct/artifacts/direct.skill.tap.json").read_text(encoding="utf-8"))["name"],
            json.loads((FIXTURE_ROOT / "delayed/artifacts/delayed.skill.tap.json").read_text(encoding="utf-8"))["name"],
        )

    def test_fixture_documentation_links_are_repository_relative(self) -> None:
        skill = (FIXTURE_ROOT.parent / "SKILL.md").read_text(encoding="utf-8")
        patterns = ("fixtures/direct/README.md", "fixtures/delayed/README.md")
        for pattern in patterns:
            self.assertIn(pattern, skill)
        for name in ("direct", "delayed"):
            text = (FIXTURE_ROOT / name / "README.md").read_text(encoding="utf-8").casefold()
            for forbidden in (
                "cl" + "aude",
                "work" + "bench",
                "nexus-" + "docs",
                "nexus-" + "work" + "bench",
            ):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
