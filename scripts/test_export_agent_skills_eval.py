#!/usr/bin/env python3
"""Regression tests for the agent-skills-eval catalog exporter."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "scripts" / "export_agent_skills_eval.py"


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_agent_skills_eval", EXPORTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {EXPORTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORTER = _load_exporter()


class AgentSkillsEvalExporterTests(unittest.TestCase):
    def test_response_catalogs_are_separate_and_have_expected_case_count(self) -> None:
        cases = []
        for path in sorted(ROOT.glob("nexus-*/evals/response-evals.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            cases.extend(
                (document["skill_name"], case)
                for case in document["evals"]
            )
        self.assertEqual(len(cases), 7)
        self.assertEqual(
            sum(len(case["expectations"]) + 1 for _, case in cases),
            34,
        )
        self.assertTrue(all("assertions" not in case for _, case in cases))

    def test_response_export_maps_expectations_and_keeps_expected_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-skills-export-") as directory:
            output = Path(directory) / "bundle"
            manifest = EXPORTER.export_bundle(
                root=ROOT,
                output_dir=output,
                skills=["nexus-cli-payment-tracking", "nexus-tap-development"],
                catalog="response",
            )
            payment = json.loads(
                (output / "nexus-cli-payment-tracking/evals/evals.json").read_text(encoding="utf-8")
            )
            tap = json.loads(
                (output / "nexus-tap-development/evals/evals.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(payment["evals"]), 1)
            self.assertEqual(len(tap["evals"]), 2)
            for document in (payment, tap):
                for case in document["evals"]:
                    self.assertEqual(
                        len(case["assertions"]),
                        len(case["expectations"]) + 1,
                    )
                    self.assertIn(
                        f"The output satisfies this expected output: {case['expected_output']}",
                        case["assertions"],
                    )
            self.assertEqual(manifest["catalog"], "response")
            self.assertEqual(len(manifest["cases"]), 3)
            self.assertTrue((output / "agent-skills-eval-manifest.json").is_file())
            self.assertTrue((output / "nexus-tap-development/SKILL.md").is_file())

    def test_all_catalog_export_preserves_criteria_and_source_digests(self) -> None:
        skill = "nexus-cli-payment-tracking"
        with tempfile.TemporaryDirectory(prefix="agent-skills-export-") as directory:
            output = Path(directory) / "bundle"
            manifest = EXPORTER.export_bundle(
                root=ROOT,
                output_dir=output,
                skills=[skill],
                catalog="all",
            )
            execution = json.loads((ROOT / skill / "evals/evals.json").read_text(encoding="utf-8"))
            response = json.loads(
                (ROOT / skill / "evals/response-evals.json").read_text(encoding="utf-8")
            )
            combined = json.loads(
                (output / skill / "evals/evals.json").read_text(encoding="utf-8")
            )
            source_cases = execution["evals"] + response["evals"]
            exported_by_id = {case["id"]: case for case in combined["evals"]}
            self.assertEqual(set(exported_by_id), {case["id"] for case in source_cases})
            self.assertEqual(len(combined["evals"]), len(source_cases))
            for source_case in source_cases:
                exported_case = exported_by_id[source_case["id"]]
                self.assertEqual(exported_case["expectations"], source_case["expectations"])
                self.assertEqual(exported_case["expected_output"], source_case["expected_output"])
                self.assertTrue(
                    set(source_case["expectations"]).issubset(exported_case["assertions"])
                )
                self.assertIn(
                    f"The output satisfies this expected output: {source_case['expected_output']}",
                    exported_case["assertions"],
                )

            provenance_path = output / skill / "agent-skills-eval-source-catalogs.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {record["catalog"] for record in provenance},
                {"execution", "response"},
            )
            self.assertNotIn("source_sha256", manifest)
            for record in provenance:
                source_path = ROOT / record["source"]
                self.assertEqual(record["source_sha256"], EXPORTER._sha256(source_path))
                source_document = json.loads(source_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    record["selected_cases"],
                    [case["id"] for case in source_document["evals"]],
                )

    def test_all_catalog_rejects_duplicate_case_ids(self) -> None:
        skill = "nexus-cli-payment-tracking"
        with tempfile.TemporaryDirectory(prefix="agent-skills-export-") as directory:
            source_root = Path(directory) / "source"
            shutil.copytree(ROOT / skill, source_root / skill)
            response_path = source_root / skill / "evals/response-evals.json"
            response = json.loads(response_path.read_text(encoding="utf-8"))
            response["evals"][0]["id"] = "payment-read-trace"
            response_path.write_text(json.dumps(response), encoding="utf-8")
            with self.assertRaisesRegex(
                EXPORTER.ExportError,
                "duplicate selected case id across catalogs",
            ):
                EXPORTER.export_bundle(
                    root=source_root,
                    output_dir=Path(directory) / "bundle",
                    skills=[skill],
                    catalog="all",
                )

    def test_execution_export_is_explicit_and_case_filter_is_scoped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-skills-export-") as directory:
            output = Path(directory) / "bundle"
            EXPORTER.export_bundle(
                root=ROOT,
                output_dir=output,
                skills=["nexus-cli-payment-tracking"],
                catalog="execution",
                cases=["payment-read-trace"],
            )
            document = json.loads(
                (output / "nexus-cli-payment-tracking/evals/evals.json").read_text(encoding="utf-8")
            )
            self.assertEqual([case["id"] for case in document["evals"]], ["payment-read-trace"])
            self.assertTrue(document["evals"][0]["expected_output"])
            self.assertTrue(document["evals"][0]["assertions"])

    def test_case_filter_narrows_the_default_skill_set(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-skills-export-") as directory:
            output = Path(directory) / "bundle"
            manifest = EXPORTER.export_bundle(
                root=ROOT,
                output_dir=output,
                catalog="response",
                cases=["nexus-tap-development/tap-supplied-replay-response"],
            )
            self.assertEqual(manifest["skills"], ["nexus-tap-development"])
            self.assertTrue((output / "nexus-tap-development/evals/evals.json").is_file())
            self.assertFalse((output / "nexus-cli-payment-tracking").exists())

    def test_export_refuses_unknown_case_and_nonempty_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-skills-export-") as directory:
            output = Path(directory) / "bundle"
            with self.assertRaisesRegex(EXPORTER.ExportError, "unknown selected case"):
                EXPORTER.export_bundle(
                    root=ROOT,
                    output_dir=output,
                    skills=["nexus-cli-payment-tracking"],
                    catalog="response",
                    cases=["missing-case"],
                )
            self.assertFalse(output.exists())
            output.mkdir()
            (output / "keep").write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(EXPORTER.ExportError, "non-empty"):
                EXPORTER.export_bundle(
                    root=ROOT,
                    output_dir=output,
                    skills=["nexus-cli-payment-tracking"],
                    catalog="response",
                )

    def test_canonical_catalog_with_assertions_is_rejected(self) -> None:
        invalid = {
            "skill_name": "nexus-cli-payment-tracking",
            "evals": [
                {
                    "id": "case",
                    "prompt": "prompt",
                    "expected_output": "output",
                    "expectations": ["one"],
                    "assertions": ["duplicate"],
                }
            ],
        }
        with self.assertRaisesRegex(EXPORTER.ExportError, "must use expectations"):
            EXPORTER._case_list(invalid, Path("evals.json"))


if __name__ == "__main__":
    unittest.main()
