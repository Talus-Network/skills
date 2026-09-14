#!/usr/bin/env python3
"""Regression tests for the offline Skill evaluation runner."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import unittest

import run_skill_evals as runner


class SkillEvalRunnerTests(unittest.TestCase):
    def test_catalog_has_trigger_matrix_for_every_skill(self) -> None:
        cases = runner.load_cases()
        self.assertEqual({case.skill for case in cases}, set(runner.EXPECTED_SKILLS))
        for skill in runner.EXPECTED_SKILLS:
            selected = [case for case in cases if case.skill == skill]
            self.assertEqual({case.trigger for case in selected}, set(runner.TRIGGERS))
            self.assertEqual(
                {case.case_id for case in selected if not case.should_trigger},
                {
                    case.case_id
                    for case in selected
                    if case.trigger == "negative" or case.expected_skill is not None
                },
            )

    def test_expected_sibling_route_requires_nontriggering_owner(self) -> None:
        raw = {
            "id": "route-case",
            "prompt": "prompt",
            "sources": ["https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool"],
            "expected_output": "output",
            "expectations": ["observe", "retain evidence"],
            "trigger": "implicit",
            "should_trigger": True,
            "expected_skill": "nexus-onchain-tool-development",
        }
        with self.assertRaises(runner.CatalogError):
            runner._case(runner.ROOT, "nexus-offchain-tool-development", raw, 0)

        raw["should_trigger"] = False
        case = runner._case(runner.ROOT, "nexus-offchain-tool-development", raw, 0)
        self.assertEqual(case.expected_skill, "nexus-onchain-tool-development")

    def test_cross_routing_cases_grade_expected_sibling_and_reject_owner(self) -> None:
        expected_routes = {
            "payment-negative-offchain-build": "nexus-offchain-tool-development",
            "offchain-negative-move-tool": "nexus-onchain-tool-development",
            "offchain-negative-tap-app": "nexus-tap-development",
            "onchain-negative-offchain-provider": "nexus-offchain-tool-development",
            "onchain-negative-payment-read": "nexus-cli-payment-tracking",
            "task-negative-move-authoring": "nexus-onchain-tool-development",
            "task-negative-tool-authoring": "nexus-offchain-tool-development",
            "tap-negative-payment-reconciliation": "nexus-cli-payment-tracking",
            "tap-negative-offchain-tool": "nexus-offchain-tool-development",
        }
        cases = {case.case_id: case for case in runner.load_cases()}
        self.assertEqual(
            {case_id: cases[case_id].expected_skill for case_id in expected_routes},
            expected_routes,
        )
        for case_id, expected_skill in expected_routes.items():
            case = cases[case_id]
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory(prefix="skill-eval-route-") as directory:
                terminal = "print(json.dumps({'type': 'turn.completed'}))\n"
                owner_body = (
                    "import json\n"
                    f"print(json.dumps({{'type': 'skill_invocation', 'skill': {case.skill!r}}}))\n"
                    + terminal
                )
                sibling_body = (
                    "import json\n"
                    f"print(json.dumps({{'type': 'skill_invocation', 'skill': {expected_skill!r}}}))\n"
                    + terminal
                )
                owner_result = runner._run_case(
                    case,
                    self._fake_command(directory, owner_body),
                    Path(directory) / "owner-out",
                    10,
                )
                sibling_result = runner._run_case(
                    case,
                    self._fake_command(directory, sibling_body),
                    Path(directory) / "sibling-out",
                    10,
                )
                self.assertEqual(owner_result["status"], "fail")
                self.assertEqual(
                    next(check for check in owner_result["checks"] if check["name"] == "skill_invocation")["status"],
                    "fail",
                )
                self.assertEqual(sibling_result["status"], "unknown")
                self.assertEqual(
                    next(check for check in sibling_result["checks"] if check["name"] == "skill_invocation")["status"],
                    "pass",
                )

    def test_catalog_and_replay_case_ids_are_safe_path_components(self) -> None:
        invalid_ids = ("../../escaped", "/absolute", ".", "..", "nested/id", r"nested\id")
        raw = {
            "id": "safe-case",
            "prompt": "prompt",
            "sources": ["https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool"],
            "expected_output": "output",
            "expectations": ["observe", "retain evidence"],
            "trigger": "explicit",
            "should_trigger": True,
        }
        for case_id in invalid_ids:
            with self.subTest(source="catalog", case_id=case_id):
                with self.assertRaises(runner.CatalogError):
                    runner._case(
                        runner.ROOT,
                        "nexus-cli-payment-tracking",
                        {**raw, "id": case_id},
                        0,
                    )

        with tempfile.TemporaryDirectory(prefix="skill-eval-case-id-") as directory:
            for index, case_id in enumerate(invalid_ids):
                with self.subTest(source="replay", case_id=case_id):
                    metadata_path = Path(directory) / f"metadata-{index}.json"
                    metadata_path.write_text(
                        json.dumps(
                            {
                                "skill": "nexus-cli-payment-tracking",
                                "case_id": case_id,
                                "prompt": "prompt",
                                "trigger": "explicit",
                                "should_trigger": True,
                                "sources": [],
                                "expectations": [],
                                "artifacts": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(runner.CatalogError):
                        runner._metadata_case(metadata_path)

        valid = {**raw, "id": "safe-case_1.2"}
        self.assertEqual(
            runner._case(runner.ROOT, "nexus-cli-payment-tracking", valid, 0).case_id,
            "safe-case_1.2",
        )

    def test_catalog_non_object_roots_fail_through_cli_validation(self) -> None:
        invalid_roots = {
            "array": "[]",
            "null": "null",
            "string": json.dumps("catalog is not an object"),
            "number": "7",
        }
        with tempfile.TemporaryDirectory(prefix="skill-eval-catalog-root-") as directory:
            directory_path = Path(directory)
            for label, content in invalid_roots.items():
                with self.subTest(root_type=label):
                    bundle_root = directory_path / f"bundle-{label}"
                    shutil.copytree(runner.ROOT, bundle_root)
                    catalog_path = (
                        bundle_root
                        / "nexus-cli-payment-tracking"
                        / "evals"
                        / "evals.json"
                    )
                    catalog_path.write_text(content, encoding="utf-8")
                    output = directory_path / f"output-{label}"
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        status = runner.main(
                            [
                                "--plan",
                                "--root",
                                str(bundle_root),
                                "--skill",
                                "nexus-cli-payment-tracking",
                                "--output-dir",
                                str(output),
                            ]
                        )
                    self.assertEqual(status, 2)
                    self.assertIn("catalog root must be a JSON object", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertFalse(output.exists())

    def test_plan_writes_only_inspectable_artifact_and_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-plan-") as directory:
            output = Path(directory) / "out"
            status = runner.main(
                [
                    "--plan",
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--case",
                    "payment-read-trace",
                    "--output-dir",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            plan_path = output / "nexus-cli-payment-tracking/payment-read-trace/plan.json"
            self.assertTrue(plan_path.is_file())
            self.assertFalse((output / "summary.json").exists())
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["execution"], "no command executed")
            self.assertTrue(plan["source_digests"])

    def _fake_command(self, directory: str, body: str) -> list[str]:
        script = Path(directory) / "fake_runner.py"
        script.write_text(body, encoding="utf-8")
        return [sys.executable, str(script)]

    def _case(self, case_id: str = "artifact-case", artifacts: tuple[dict[str, str], ...] = ()) -> runner.Case:
        return runner.Case(
            "nexus-cli-payment-tracking",
            case_id,
            "prompt",
            "explicit",
            True,
            ("bundled:nexus-cli-payment-tracking/references/payment-ledger.md",),
            "output",
            ("manual behavior",),
            artifacts,
        )

    def test_structured_invocation_is_required_and_rubrics_remain_pending(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-run-") as directory:
            command = self._fake_command(
                directory,
                "import json, sys\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
                "print('diagnostic', file=sys.stderr)\n",
            )
            output = Path(directory) / "out"
            result = runner._run_case(runner.load_cases()[0], command, output, 10)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["deterministic_status"], "pass")
            self.assertEqual(result["manual_status"], "pending")
            case_output = output / "nexus-cli-payment-tracking/payment-read-trace"
            self.assertIn("diagnostic", (case_output / "stderr.log").read_text(encoding="utf-8"))
            metadata = json.loads((case_output / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["effective_argv"], command + [runner.load_cases()[0].prompt])
            self.assertTrue((case_output / "snapshot/nexus-cli-payment-tracking/SKILL.md").is_file())
            self.assertFalse((case_output / "snapshot/nexus-cli-payment-tracking/evals").exists())
            self.assertTrue(metadata["source_digests"])

    def test_prose_claim_and_negative_absence_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-prose-") as directory:
            command = self._fake_command(
                directory,
                "import json\n"
                "print(json.dumps({'type': 'assistant_message', 'text': 'I invoked nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            output = Path(directory) / "out"
            result = runner._run_case(runner.load_cases()[0], command, output, 10)
            self.assertEqual(result["status"], "unknown")
            invocation = next(check for check in result["checks"] if check["name"] == "skill_invocation")
            self.assertEqual(invocation["status"], "unknown")
            negative = next(case for case in runner.load_cases() if case.trigger == "negative")
            negative_output = Path(directory) / "negative-out"
            negative_result = runner._run_case(negative, command, negative_output, 10)
            self.assertEqual(negative_result["status"], "unknown")
            negative_invocation = next(check for check in negative_result["checks"] if check["name"] == "skill_invocation")
            self.assertEqual(negative_invocation["status"], "unknown")

    def test_generic_and_failed_terminal_events_fail_closed(self) -> None:
        terminal_cases = (
            ("generic-type", ({"type": "completed"},)),
            ("generic-status", ({"status": "completed"},)),
            ("failed-turn", ({"type": "turn.completed", "status": "failed"},)),
            (
                "success-then-failure",
                (
                    {"type": "turn.completed"},
                    {"type": "task.completed", "status": "failed"},
                ),
            ),
            (
                "failure-then-success",
                (
                    {"type": "turn.completed", "status": "failed"},
                    {"type": "task.completed"},
                ),
            ),
        )
        for suffix, terminals in terminal_cases:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory(prefix=f"skill-eval-{suffix}-") as directory:
                body = (
                    "import json\n"
                    "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                    + "\n".join(f"print(json.dumps({terminal!r}))" for terminal in terminals)
                    + "\n"
                )
                result = runner._run_case(
                    self._case(case_id=suffix),
                    self._fake_command(directory, body),
                    Path(directory) / "out",
                    10,
                )
                self.assertEqual(result["status"], "fail")
                completion = next(check for check in result["checks"] if check["name"] == "completion")
                self.assertEqual(completion["status"], "fail")
                self.assertTrue(any(check["name"] == "skill_invocation" and check["status"] == "pass" for check in result["checks"]))

    def test_timeout_preserves_partial_bytes_and_reaps_child_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-timeout-") as directory:
            marker = Path(directory) / "child-survived"
            body = (
                "import pathlib, sys, time\n"
                "print('partial', flush=True)\n"
                "print('err', file=sys.stderr, flush=True)\n"
                f"time.sleep(0.2); pathlib.Path({str(marker)!r}).write_text('survived')\n"
            )
            output = Path(directory) / "out"
            result = runner._run_case(self._case(case_id="timeout"), self._fake_command(directory, body), output, 0.05)
            case_output = output / "nexus-cli-payment-tracking/timeout"
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["returncode"], 124)
            self.assertTrue(result["timed_out"])
            self.assertIn("partial", (case_output / "trace.jsonl").read_text(encoding="utf-8"))
            self.assertIn("err", (case_output / "stderr.log").read_text(encoding="utf-8"))
            self.assertIn("command timed out", (case_output / "stderr.log").read_text(encoding="utf-8"))
            time.sleep(0.3)
            self.assertFalse(marker.exists())

    def test_launch_and_invalid_timeout_failures_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-launch-") as directory:
            case = self._case(case_id="launch")
            output = Path(directory) / "launch-out"
            result = runner._run_case(case, [str(Path(directory) / "missing-command")], output, 10)
            self.assertEqual(result["status"], "fail")
            case_output = output / "nexus-cli-payment-tracking/launch"
            self.assertIn("launch failed", (case_output / "stderr.log").read_text(encoding="utf-8"))
            self.assertTrue((case_output / "result.json").is_file())

            invalid_output = Path(directory) / "invalid-out"
            status = runner.main(
                [
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--case",
                    "payment-read-trace",
                    "--output-dir",
                    str(invalid_output),
                    "--timeout",
                    "not-a-number",
                    "--command",
                    "true",
                ]
            )
            self.assertEqual(status, 1)
            invalid_case = invalid_output / "nexus-cli-payment-tracking/payment-read-trace"
            metadata = json.loads((invalid_case / "metadata.json").read_text(encoding="utf-8"))
            self.assertIn("invalid timeout", metadata["launch_error"])
            self.assertTrue((invalid_case / "result.json").is_file())

    def test_artifact_file_and_parent_symlink_are_rejected(self) -> None:
        for case_id, artifact_path, body in (
            (
                "artifact-file-symlink",
                "report.json",
                "import json, pathlib, sys\npathlib.Path('report.json').symlink_to(sys.argv[1])\nprint(json.dumps({'type':'skill_invocation','skill':'nexus-cli-payment-tracking'}))\nprint(json.dumps({'type':'turn.completed'}))\n",
            ),
            (
                "artifact-parent-symlink",
                "nested/report.json",
                "import json, pathlib, sys\npathlib.Path('nested').symlink_to(pathlib.Path(sys.argv[1]).parent, target_is_directory=True)\nprint(json.dumps({'type':'skill_invocation','skill':'nexus-cli-payment-tracking'}))\nprint(json.dumps({'type':'turn.completed'}))\n",
            ),
        ):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory(prefix=f"skill-eval-{case_id}-") as directory:
                outside = Path(directory) / "outside.txt"
                outside.write_text("outside-secret\n", encoding="utf-8")
                command = self._fake_command(directory, body) + [str(outside)]
                result = runner._run_case(
                    self._case(case_id=case_id, artifacts=({"path": artifact_path},)),
                    command,
                    Path(directory) / "out",
                    10,
                )
                self.assertEqual(result["status"], "fail")
                artifact = Path(directory) / "out/nexus-cli-payment-tracking" / case_id / "artifacts" / artifact_path
                self.assertFalse(artifact.exists())
                self.assertTrue(any(check["name"] == f"artifact:{artifact_path}" and check["status"] == "fail" for check in result["checks"]))

    def test_stale_and_source_overlapping_output_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-output-") as directory:
            stale = Path(directory) / "stale"
            stale.mkdir()
            marker = stale / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            status = runner.main(["--plan", "--skill", "nexus-cli-payment-tracking", "--output-dir", str(stale)])
            self.assertEqual(status, 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            overlap = runner.ROOT / "eval-output-overlap-probe"
            self.assertEqual(runner.main(["--plan", "--skill", "nexus-cli-payment-tracking", "--output-dir", str(overlap)]), 2)

    def test_replay_uses_saved_trace_without_launching_a_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-") as directory:
            command = self._fake_command(
                directory,
                "import json\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            run_output = Path(directory) / "run"
            result = runner._run_case(runner.load_cases()[0], command, run_output, 10)
            self.assertEqual(result["deterministic_status"], "pass")
            replay_output = Path(directory) / "replay"
            status = runner.main(["--replay", str(run_output), "--output-dir", str(replay_output)])
            self.assertEqual(status, 0)
            replay_case = replay_output / "nexus-cli-payment-tracking/payment-read-trace"
            replay_result = json.loads((replay_case / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(replay_result["mode"], "replay")
            self.assertEqual(replay_result["deterministic_status"], "pass")
            self.assertEqual(replay_result["status"], "unknown")
            self.assertTrue((replay_case / "trace.jsonl").read_text(encoding="utf-8"))
            self.assertEqual((replay_case / "stderr.log").read_text(encoding="utf-8"), "")
            self.assertTrue((replay_case / "snapshot/nexus-cli-payment-tracking/SKILL.md").is_file())

    def test_replay_honors_skill_case_filters_and_rejects_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-filters-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            command = self._fake_command(
                directory,
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0\n"
                "counter.write_text(str(count + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            command_line = shlex.join(command)

            def invoke(arguments: list[str]) -> tuple[int, str]:
                stderr = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    status = runner.main(arguments)
                return status, stderr.getvalue()

            run_output = directory_path / "run"
            run_status, run_stderr = invoke(
                [
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-tap-development",
                    "--case",
                    "payment-read-trace",
                    "--case",
                    "tap-local-package",
                    "--output-dir",
                    str(run_output),
                    "--command",
                    command_line,
                ]
            )
            self.assertEqual(run_status, 0, run_stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            tap_output = directory_path / "replay-tap"
            tap_status, tap_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-tap-development",
                    "--output-dir",
                    str(tap_output),
                ]
            )
            self.assertEqual(tap_status, 0, tap_stderr)
            self.assertTrue(
                (tap_output / "nexus-tap-development/tap-local-package").is_dir()
            )
            self.assertFalse(
                (tap_output / "nexus-cli-payment-tracking/payment-read-trace").exists()
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            union_output = directory_path / "replay-union"
            union_status, union_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-tap-development",
                    "--output-dir",
                    str(union_output),
                ]
            )
            self.assertEqual(union_status, 0, union_stderr)
            self.assertTrue(
                (union_output / "nexus-cli-payment-tracking/payment-read-trace").is_dir()
            )
            self.assertTrue(
                (union_output / "nexus-tap-development/tap-local-package").is_dir()
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            intersection_output = directory_path / "replay-intersection"
            intersection_status, intersection_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-tap-development",
                    "--case",
                    "payment-read-trace",
                    "--output-dir",
                    str(intersection_output),
                ]
            )
            self.assertEqual(intersection_status, 0, intersection_stderr)
            self.assertTrue(
                (
                    intersection_output
                    / "nexus-cli-payment-tracking/payment-read-trace"
                ).is_dir()
            )
            self.assertFalse(
                (intersection_output / "nexus-tap-development/tap-local-package").exists()
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            no_match_output = directory_path / "replay-no-match"
            no_match_status, no_match_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-onchain-tool-development",
                    "--output-dir",
                    str(no_match_output),
                ]
            )
            self.assertEqual(no_match_status, 2)
            self.assertIn("no saved cases match skill(s)", no_match_stderr)
            self.assertFalse(no_match_output.exists())

            unknown_skill_output = directory_path / "replay-unknown-skill"
            unknown_skill_status, unknown_skill_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "missing-skill",
                    "--output-dir",
                    str(unknown_skill_output),
                ]
            )
            self.assertEqual(unknown_skill_status, 2)
            self.assertIn("unknown skill(s)", unknown_skill_stderr)
            self.assertFalse(unknown_skill_output.exists())

            no_match_case_output = directory_path / "replay-no-match-case"
            no_match_case_status, no_match_case_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-tap-development",
                    "--case",
                    "payment-read-trace",
                    "--output-dir",
                    str(no_match_case_output),
                ]
            )
            self.assertEqual(no_match_case_status, 2)
            self.assertIn("unknown saved case(s)", no_match_case_stderr)
            self.assertFalse(no_match_case_output.exists())

            unknown_case_output = directory_path / "replay-unknown-case"
            unknown_case_status, unknown_case_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--case",
                    "missing-case",
                    "--output-dir",
                    str(unknown_case_output),
                ]
            )
            self.assertEqual(unknown_case_status, 2)
            self.assertIn("unknown saved case(s)", unknown_case_stderr)
            self.assertFalse(unknown_case_output.exists())

            command_output = directory_path / "replay-command"
            command_status, command_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--command",
                    command_line,
                    "--output-dir",
                    str(command_output),
                ]
            )
            self.assertEqual(command_status, 2)
            self.assertIn("--command cannot be combined with --replay", command_stderr)
            self.assertFalse(command_output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            empty_command_output = directory_path / "replay-empty-command"
            empty_command_status, empty_command_stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--command",
                    "",
                    "--output-dir",
                    str(empty_command_output),
                ]
            )
            self.assertEqual(empty_command_status, 2)
            self.assertIn(
                "--command cannot be combined with --replay", empty_command_stderr
            )
            self.assertFalse(empty_command_output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            list_command_output = directory_path / "list-command"
            list_command_status, list_command_stderr = invoke(
                [
                    "--list",
                    "--command",
                    command_line,
                    "--output-dir",
                    str(list_command_output),
                ]
            )
            self.assertEqual(list_command_status, 2)
            self.assertIn("--command cannot be combined with --list", list_command_stderr)
            self.assertFalse(list_command_output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

    def test_replay_recomputes_status_after_artifact_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-artifact-") as directory:
            case = self._case(
                case_id="replay-artifact",
                artifacts=({"path": "missing.txt"},),
            )
            command = self._fake_command(
                directory,
                "import json\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            run_output = Path(directory) / "run"
            run_result = runner._run_case(case, command, run_output, 10)
            self.assertEqual(run_result["status"], "fail")
            self.assertEqual(run_result["deterministic_status"], "fail")
            self.assertTrue(
                any(
                    check["name"] == "artifact:missing.txt" and check["status"] == "fail"
                    for check in run_result["checks"]
                )
            )

            replay_output = Path(directory) / "replay"
            replay_status = runner.main(
                ["--replay", str(run_output), "--output-dir", str(replay_output)]
            )
            self.assertEqual(replay_status, 1)
            replay_result = json.loads(
                (replay_output / "nexus-cli-payment-tracking/replay-artifact/result.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(replay_result["status"], "fail")
            self.assertEqual(replay_result["deterministic_status"], "fail")
            self.assertTrue(
                any(
                    check["name"] == "artifact:missing.txt" and check["status"] == "fail"
                    for check in replay_result["checks"]
                )
            )

    def test_replay_requires_catalog_evidence_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-schema-") as directory:
            base_case = runner.load_cases()[0]
            run_output = Path(directory) / "run"
            command = self._fake_command(
                directory,
                "import json\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            runner._run_case(base_case, command, run_output, 10)
            saved_case = run_output / base_case.skill / base_case.case_id
            metadata_path = saved_case / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

            invalid_fields = {
                "prompt": "",
                "expected_output": " \t",
                "sources": [],
                "expectations": ["  "],
                "artifacts": [{"path": "  "}],
            }
            for field, value in invalid_fields.items():
                with self.subTest(field=field):
                    invalid_metadata = dict(metadata)
                    invalid_metadata["case_id"] = f"replay-invalid-{field}"
                    invalid_metadata[field] = value
                    invalid_path = Path(directory) / f"{field}.json"
                    invalid_path.write_text(json.dumps(invalid_metadata), encoding="utf-8")
                    with self.assertRaises(runner.CatalogError):
                        runner._metadata_case(invalid_path)

            metadata["expectations"] = []
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            replay_output = Path(directory) / "replay"
            self.assertEqual(
                runner.main(
                    ["--replay", str(run_output), "--output-dir", str(replay_output)]
                ),
                2,
            )
            self.assertFalse((replay_output / base_case.skill / base_case.case_id).exists())

    def test_non_utf8_stdout_and_stderr_are_captured_with_replacement(self) -> None:
        body = (
            "import sys\n"
            "sys.stdout.buffer.write(b'{\"type\":\"skill_invocation\",\"skill\":\"nexus-cli-payment-tracking\"}\\n\\xff\\n{\"type\":\"turn.completed\"}\\n')\n"
            "sys.stderr.buffer.write(b'\\xfe diagnostic\\n')\n"
        )
        with tempfile.TemporaryDirectory(prefix="skill-eval-binary-") as directory:
            command = self._fake_command(directory, body)
            output = Path(directory) / "out"
            result = runner._run_case(self._case(case_id="binary"), command, output, 10)
            case_output = output / "nexus-cli-payment-tracking/binary"
            self.assertEqual(result["status"], "fail")
            self.assertIn("\ufffd", (case_output / "trace.jsonl").read_text(encoding="utf-8"))
            self.assertIn("\ufffd", (case_output / "stderr.log").read_text(encoding="utf-8"))
            trace_check = next(check for check in result["checks"] if check["name"] == "trace_jsonl")
            self.assertEqual(trace_check["status"], "fail")

    def test_truncated_json_trace_is_recorded_as_a_failure(self) -> None:
        trace = (
            '{"type":"skill_invocation","skill":"nexus-cli-payment-tracking"}\n'
            '{"type":"turn.completed"'
        )
        body = f"import sys\nsys.stdout.write({trace!r})\n"
        with tempfile.TemporaryDirectory(prefix="skill-eval-truncated-") as directory:
            command = self._fake_command(directory, body)
            output = Path(directory) / "out"
            result = runner._run_case(self._case(case_id="truncated"), command, output, 10)
            case_output = output / "nexus-cli-payment-tracking/truncated"
            self.assertEqual(result["status"], "fail")
            trace_check = next(check for check in result["checks"] if check["name"] == "trace_jsonl")
            self.assertEqual(trace_check["status"], "fail")
            self.assertIn("trace line 2 is not JSON", trace_check["detail"])
            self.assertEqual(
                (case_output / "trace.jsonl").read_text(encoding="utf-8"),
                trace,
            )



    def test_command_sees_discoverable_bundle_and_explicit_sibling_resources(self) -> None:
        case = runner.Case(
            "nexus-cli-payment-tracking",
            "discovery-bundle",
            "prompt",
            "explicit",
            True,
            ("bundled:nexus-offchain-tool-development/references/scaffolding.md",),
            "output",
            ("bundle is discoverable",),
            (),
        )
        with tempfile.TemporaryDirectory(prefix="skill-eval-discovery-") as directory:
            body = (
                "import json, os, pathlib\n"
                "root = pathlib.Path(os.environ['SKILLS_BUNDLE_ROOT'])\n"
                "json.dump({\n"
                "  'root': str(root),\n"
                "  'selected': (root / 'nexus-cli-payment-tracking/SKILL.md').is_file(),\n"
                "  'sibling': (root / 'nexus-offchain-tool-development/references/scaffolding.md').is_file(),\n"
                "  'helper': (root / 'scripts/prepare_sources.py').is_file(),\n"
                "  'evals': (root / 'nexus-cli-payment-tracking/evals').exists(),\n"
                "  'tests': (root / 'scripts/test_run_skill_evals.py').exists(),\n"
                "  'git': (root / '.git').exists(),\n"
                "}, open('bundle-inspection.json', 'w'))\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            output = Path(directory) / "out"
            result = runner._run_case(
                case, self._fake_command(directory, body), output, 10
            )
            self.assertEqual(result["deterministic_status"], "pass")
            case_output = output / case.skill / case.case_id
            inspection = json.loads(
                (case_output / "workspace/bundle-inspection.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(inspection["selected"])
            self.assertTrue(inspection["sibling"])
            self.assertTrue(inspection["helper"])
            self.assertFalse(inspection["evals"])
            self.assertFalse(inspection["tests"])
            self.assertFalse(inspection["git"])
            metadata = json.loads(
                (case_output / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["bundle_root"], "workspace/.agents/skills")
            self.assertIn("nexus-offchain-tool-development", metadata["bundle_components"])
            self.assertTrue(
                (case_output / "snapshot/scripts/prepare_sources.py").is_file()
            )

    def test_routing_fails_for_every_wrong_or_mixed_recognized_selection(self) -> None:
        cases = {
            case.case_id: case
            for case in runner.load_cases()
            if not case.should_trigger and case.expected_skill is not None
        }
        self.assertEqual(len(cases), 9)
        for case_id, case in cases.items():
            unrelated = next(
                skill
                for skill in runner.EXPECTED_SKILLS
                if skill not in {case.skill, case.expected_skill}
            )
            with self.subTest(case_id=case_id, selection="single"):
                for selected in runner.EXPECTED_SKILLS:
                    body = (
                        "import json\n"
                        f"print(json.dumps({{'type': 'skill_invocation', 'skill': {selected!r}}}))\n"
                        "print(json.dumps({'type': 'turn.completed'}))\n"
                    )
                    with tempfile.TemporaryDirectory(
                        prefix=f"skill-eval-route-{case_id}-"
                    ) as directory:
                        result = runner._run_case(
                            case,
                            self._fake_command(directory, body),
                            Path(directory) / "out",
                            10,
                        )
                    activation = next(
                        check
                        for check in result["checks"]
                        if check["name"] == "skill_invocation"
                    )
                    expected_status = "pass" if selected == case.expected_skill else "fail"
                    self.assertEqual(activation["status"], expected_status)
                    self.assertEqual(
                        result["deterministic_status"],
                        "pass" if expected_status == "pass" else "fail",
                    )

            for selection, selected_skills in {
                "absent": (),
                "owner-plus-expected": (case.skill, case.expected_skill),
                "unrelated-plus-expected": (unrelated, case.expected_skill),
            }.items():
                with self.subTest(case_id=case_id, selection=selection):
                    body = "import json\n"
                    body += "\n".join(
                        f"print(json.dumps({{'type': 'skill_invocation', 'skill': {skill!r}}}))"
                        for skill in selected_skills
                    )
                    if selected_skills:
                        body += "\n"
                    body += "print(json.dumps({'type': 'turn.completed'}))\n"
                    with tempfile.TemporaryDirectory(
                        prefix=f"skill-eval-route-{case_id}-"
                    ) as directory:
                        result = runner._run_case(
                            case,
                            self._fake_command(directory, body),
                            Path(directory) / "out",
                            10,
                        )
                    activation = next(
                        check
                        for check in result["checks"]
                        if check["name"] == "skill_invocation"
                    )
                    self.assertEqual(
                        activation["status"],
                        "unknown" if selection == "absent" else "fail",
                    )
                    self.assertEqual(
                        result["deterministic_status"],
                        "unknown" if selection == "absent" else "fail",
                    )

            with self.subTest(case_id=case_id, selection="expected-plus-failure"):
                body = (
                    "import json\n"
                    f"print(json.dumps({{'type': 'skill_invocation', 'skill': {case.expected_skill!r}}}))\n"
                    "print(json.dumps({'type': 'turn.completed', 'status': 'failed'}))\n"
                )
                with tempfile.TemporaryDirectory(
                    prefix=f"skill-eval-route-{case_id}-"
                ) as directory:
                    result = runner._run_case(
                        case,
                        self._fake_command(directory, body),
                        Path(directory) / "out",
                        10,
                    )
                activation = next(
                    check
                    for check in result["checks"]
                    if check["name"] == "skill_invocation"
                )
                self.assertEqual(activation["status"], "pass")
                self.assertEqual(result["status"], "fail")

        positive = next(
            case
            for case in runner.load_cases()
            if case.should_trigger and case.expected_skill is None
        )
        wrong_skill = next(skill for skill in runner.EXPECTED_SKILLS if skill != positive.skill)
        body = (
            "import json\n"
            f"print(json.dumps({{'type': 'skill_invocation', 'skill': {wrong_skill!r}}}))\n"
            "print(json.dumps({'type': 'turn.completed'}))\n"
        )
        with tempfile.TemporaryDirectory(prefix="skill-eval-route-positive-") as directory:
            result = runner._run_case(
                positive,
                self._fake_command(directory, body),
                Path(directory) / "out",
                10,
            )
        self.assertEqual(
            next(check for check in result["checks"] if check["name"] == "skill_invocation")[
                "status"
            ],
            "fail",
        )
        self.assertEqual(result["deterministic_status"], "fail")

    def test_mixed_routing_and_terminal_failure_are_both_reported(self) -> None:
        cases = [
            case
            for case in runner.load_cases()
            if not case.should_trigger and case.expected_skill is not None
        ]
        for case in cases:
            unrelated = next(
                skill
                for skill in runner.EXPECTED_SKILLS
                if skill not in {case.skill, case.expected_skill}
            )
            body = (
                "import json\n"
                f"print(json.dumps({{'type': 'skill_invocation', 'skill': {case.expected_skill!r}}}))\n"
                f"print(json.dumps({{'type': 'skill_invocation', 'skill': {unrelated!r}}}))\n"
                "print(json.dumps({'type': 'turn.completed', 'status': 'failed'}))\n"
            )
            with self.subTest(case_id=case.case_id):
                with tempfile.TemporaryDirectory(
                    prefix=f"skill-eval-route-failure-{case.case_id}-"
                ) as directory:
                    result = runner._run_case(
                        case,
                        self._fake_command(directory, body),
                        Path(directory) / "out",
                        10,
                    )
                activation = next(
                    check
                    for check in result["checks"]
                    if check["name"] == "skill_invocation"
                )
                completion = next(
                    check
                    for check in result["checks"]
                    if check["name"] == "completion"
                )
                self.assertEqual(activation["status"], "fail")
                self.assertEqual(completion["status"], "fail")
                self.assertEqual(result["status"], "fail")

    def test_replay_validates_saved_sources_snapshot_and_digests(self) -> None:
        case = runner.Case(
            skill="nexus-cli-payment-tracking",
            case_id="replay-source-valid",
            prompt="prompt",
            trigger="explicit",
            should_trigger=True,
            sources=(
                "bundled:nexus-cli-payment-tracking/references/payment-ledger.md",
                "https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool",
            ),
            expected_output="output",
            expectations=("manual behavior",),
            artifacts=(),
        )
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-source-") as directory:
            root = Path(directory)
            command_body = (
                "import json\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            saved_root = root / "saved"
            run_result = runner._run_case(
                case,
                self._fake_command(directory, command_body),
                saved_root,
                10,
            )
            self.assertEqual(run_result["deterministic_status"], "pass")
            saved_case = saved_root / case.skill / case.case_id
            metadata_path = saved_case / "metadata.json"
            baseline = json.loads(metadata_path.read_text(encoding="utf-8"))

            mutations = {
                "invalid-scheme": lambda metadata: metadata.update(
                    {"sources": ["invalid-scheme:source"]}
                ),
                "traversal": lambda metadata: metadata.update(
                    {"sources": ["bundled:../outside.txt"]}
                ),
                "absolute": lambda metadata: metadata.update(
                    {"sources": ["bundled:/tmp/outside.txt"]}
                ),
                "missing-bundled": lambda metadata: metadata.update(
                    {"sources": ["bundled:nexus-cli-payment-tracking/references/missing.md"]}
                ),
                "snapshot-traversal": lambda metadata: metadata.update(
                    {"snapshot_root": "../snapshot"}
                ),
                "snapshot-wrong-type": lambda metadata: metadata.update(
                    {"snapshot_root": []}
                ),
                "skill-provenance": lambda metadata: metadata.update(
                    {"skill_snapshot": "snapshot/nexus-onchain-tool-development"}
                ),
                "skill-provenance-wrong-type": lambda metadata: metadata.update(
                    {"skill_snapshot": {}}
                ),
                "component-provenance": lambda metadata: metadata.update(
                    {"bundle_components": []}
                ),
                "component-traversal": lambda metadata: metadata.update(
                    {"bundle_components": ["../snapshot"]}
                ),
                "digest-wrong-type": lambda metadata: metadata.update(
                    {"source_digests": []}
                ),
                "digest-mismatch": lambda metadata: metadata["source_digests"].update(
                    {"bundled:nexus-cli-payment-tracking/SKILL.md": "0" * 64}
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(mutation=name):
                    mutated = json.loads(json.dumps(baseline))
                    mutate(mutated)
                    metadata_path.write_text(
                        json.dumps(mutated), encoding="utf-8"
                    )
                    with self.assertRaises(runner.CatalogError):
                        runner._replay_case(
                            saved_case,
                            root / f"invalid-replay-{name}",
                        )
            metadata_path.write_text(json.dumps(baseline), encoding="utf-8")

            replay_output = root / "replay"
            relocated_saved = root / "relocated-saved"
            saved_root.rename(relocated_saved)
            status = runner.main(
                [
                    "--replay",
                    str(relocated_saved),
                    "--root",
                    str(root / "missing-current-checkout"),
                    "--output-dir",
                    str(replay_output),
                ]
            )
            self.assertEqual(status, 0)
            replay_result = json.loads(
                (
                    replay_output / case.skill / case.case_id / "result.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(replay_result["deterministic_status"], "pass")
            self.assertEqual(replay_result["status"], "unknown")
            self.assertEqual(replay_result["manual_status"], "pending")


    def test_terminal_status_matrix_fails_closed_for_explicit_unknowns(self) -> None:
        absent = object()
        cases = [
            ("status-absent", absent, "pass"),
            ("status-complete", "complete", "pass"),
            ("status-completed", "completed", "pass"),
            ("status-success", "success", "pass"),
            ("status-succeeded", "succeeded", "pass"),
            ("status-aborted", "aborted", "fail"),
            ("status-failure", "failure", "fail"),
            ("status-errored", "errored", "fail"),
            ("status-timeout", "timeout", "fail"),
            ("status-incomplete", "incomplete", "fail"),
            ("status-null", None, "fail"),
            ("status-number", 1, "fail"),
            ("status-object", {"ok": True}, "fail"),
        ]
        with tempfile.TemporaryDirectory(prefix="skill-eval-terminal-status-") as directory:
            for case_id, status, expected in cases:
                event = {"type": "turn.completed"}
                if status is not absent:
                    event["status"] = status
                body = (
                    "import json\n"
                    f"print(json.dumps({event!r}))\n"
                    "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                )
                with self.subTest(case_id=case_id):
                    result = runner._run_case(
                        self._case(case_id=case_id),
                        self._fake_command(directory, body),
                        Path(directory) / "out",
                        10,
                    )
                    completion = next(
                        check for check in result["checks"] if check["name"] == "completion"
                    )
                    self.assertEqual(completion["status"], expected)
                    self.assertEqual(result["deterministic_status"], expected)

            mixed_body = (
                "import json\n"
                "print(json.dumps({'type': 'turn.completed', 'status': 'completed'}))\n"
                "print(json.dumps({'type': 'task.completed', 'status': 'aborted'}))\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
            )
            mixed = runner._run_case(
                self._case(case_id="status-mixed-unsupported"),
                self._fake_command(directory, mixed_body),
                Path(directory) / "mixed-out",
                10,
            )
            self.assertEqual(
                next(check for check in mixed["checks"] if check["name"] == "completion")[
                    "status"
                ],
                "fail",
            )
            self.assertEqual(mixed["deterministic_status"], "fail")

            success_flag_body = (
                "import json\n"
                "print(json.dumps({'type': 'turn.completed', 'status': 'completed', 'success': False}))\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
            )
            success_flag = runner._run_case(
                self._case(case_id="status-success-false"),
                self._fake_command(directory, success_flag_body),
                Path(directory) / "success-flag-out",
                10,
            )
            self.assertEqual(success_flag["deterministic_status"], "fail")

            for terminal_type in sorted(runner.TERMINAL_TYPES):
                body = (
                    "import json\n"
                    f"print(json.dumps({{'type': {terminal_type!r}}}))\n"
                    "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                )
                result = runner._run_case(
                    self._case(case_id=f"native-{terminal_type.split('.')[0]}"),
                    self._fake_command(directory, body),
                    Path(directory) / f"native-{terminal_type.split('.')[0]}-out",
                    10,
                )
                self.assertEqual(result["deterministic_status"], "pass")

    def test_replay_validates_complete_bundle_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-manifest-") as directory:
            directory_path = Path(directory)
            body = (
                "import json\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            case = self._case(case_id="manifest-valid")
            run_output = directory_path / "run"
            result = runner._run_case(
                case,
                self._fake_command(directory, body),
                run_output,
                10,
            )
            self.assertEqual(result["deterministic_status"], "pass")
            saved_case = run_output / case.skill / case.case_id
            metadata = json.loads((saved_case / "metadata.json").read_text(encoding="utf-8"))
            manifest = metadata["bundle_manifest"]
            self.assertTrue(manifest)
            self.assertIn("nexus-cli-payment-tracking/SKILL.md", manifest)
            self.assertIn("scripts/prepare_sources.py", manifest)
            target = "nexus-cli-payment-tracking/SKILL.md"
            outside = directory_path / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("outside\n", encoding="utf-8")

            def altered(root: Path) -> None:
                (root / "snapshot" / target).write_text("altered\n", encoding="utf-8")

            def added(root: Path) -> None:
                (root / "snapshot" / "nexus-cli-payment-tracking" / "added.md").write_text(
                    "added\n", encoding="utf-8"
                )

            def removed(root: Path) -> None:
                (root / "snapshot" / target).unlink()

            def symlinked(root: Path) -> None:
                path = root / "snapshot" / target
                path.unlink()
                path.symlink_to(outside / "secret.txt")

            def escaped(root: Path) -> None:
                (root / "snapshot" / "nexus-cli-payment-tracking" / "escaped").symlink_to(
                    outside, target_is_directory=True
                )

            def workspace_altered(root: Path) -> None:
                (root / "workspace" / ".agents" / "skills" / target).write_text(
                    "workspace altered\n", encoding="utf-8"
                )

            mutations = {
                "altered": altered,
                "added": added,
                "removed": removed,
                "symlinked": symlinked,
                "escaped": escaped,
                "workspace-altered": workspace_altered,
            }
            for name, mutate in mutations.items():
                with self.subTest(mutation=name):
                    mutated_case = directory_path / f"saved-{name}"
                    shutil.copytree(saved_case, mutated_case, symlinks=True)
                    mutate(mutated_case)
                    replay_output = directory_path / f"replay-{name}"
                    with self.assertRaises(runner.CatalogError):
                        runner._replay_case(mutated_case, replay_output)
                    self.assertFalse(replay_output.exists())

    def test_nested_artifact_is_valid_and_replay_artifact_root_symlink_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-artifact-boundary-") as directory:
            directory_path = Path(directory)
            case = self._case(
                case_id="nested-artifact-valid",
                artifacts=({"path": "nested/proof.txt"},),
            )
            body = (
                "import json, pathlib\n"
                "pathlib.Path('nested').mkdir()\n"
                "pathlib.Path('nested/proof.txt').write_text('inside\\n')\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            run_output = directory_path / "run"
            result = runner._run_case(
                case,
                self._fake_command(directory, body),
                run_output,
                10,
            )
            self.assertEqual(result["deterministic_status"], "pass")
            saved_case = run_output / case.skill / case.case_id
            self.assertEqual(
                (saved_case / "artifacts/nested/proof.txt").read_text(encoding="utf-8"),
                "inside\n",
            )

            outside = directory_path / "outside"
            outside.mkdir()
            (outside / "proof.txt").write_text("outside-secret\n", encoding="utf-8")
            shutil.rmtree(saved_case / "artifacts")
            (saved_case / "artifacts").symlink_to(outside, target_is_directory=True)
            replay_output = directory_path / "replay"
            replay_result = runner._replay_case(saved_case, replay_output)
            self.assertEqual(replay_result["deterministic_status"], "fail")
            self.assertTrue(
                any(
                    check["name"] == "artifact_root" and check["status"] == "fail"
                    for check in replay_result["checks"]
                )
            )
            self.assertFalse(
                (replay_output / case.skill / case.case_id / "artifacts/nested/proof.txt").exists()
            )

    def test_live_artifact_destination_root_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-live-artifact-root-") as directory:
            case = self._case(
                case_id="live-artifact-root-symlink",
                artifacts=({"path": "proof.txt"},),
            )
            body = (
                "import json, os, pathlib\n"
                "case_root = pathlib.Path(os.environ['SKILL_EVAL_CASE_ROOT'])\n"
                "outside = case_root.parent / 'outside'\n"
                "outside.mkdir()\n"
                "pathlib.Path('proof.txt').write_text('inside\\n')\n"
                "(outside / 'proof.txt').write_text('outside-secret\\n')\n"
                "(case_root / 'artifacts').symlink_to(outside, target_is_directory=True)\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            result = runner._run_case(
                case,
                self._fake_command(directory, body),
                Path(directory) / "out",
                10,
            )
            self.assertEqual(result["deterministic_status"], "fail")
            self.assertTrue(
                any(
                    check["name"] == "artifact_destination_root"
                    and check["status"] == "fail"
                    for check in result["checks"]
                )
            )
            self.assertEqual(
                (
                    Path(directory)
                    / "out"
                    / case.skill
                    / "outside/proof.txt"
                ).read_text(encoding="utf-8"),
                "outside-secret\n",
            )

    def test_replay_rejects_symlinked_saved_evidence_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-evidence-") as directory:
            directory_path = Path(directory)
            case = self._case(case_id="evidence-boundary")
            body = (
                "import json\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            run_output = directory_path / "run"
            runner._run_case(case, self._fake_command(directory, body), run_output, 10)
            saved_case = run_output / case.skill / case.case_id
            outside = directory_path / "outside"
            outside.mkdir()
            evidence = {
                "metadata.json": json.loads(
                    (saved_case / "metadata.json").read_text(encoding="utf-8")
                ),
                "trace.jsonl": '{"type":"turn.completed"}\n',
                "stderr.log": "outside stderr\n",
            }
            for filename, value in evidence.items():
                with self.subTest(filename=filename):
                    mutated_case = directory_path / f"saved-{filename.replace('.', '-') }"
                    shutil.copytree(saved_case, mutated_case, symlinks=True)
                    target = outside / filename
                    if isinstance(value, dict):
                        target.write_text(json.dumps(value), encoding="utf-8")
                    else:
                        target.write_text(value, encoding="utf-8")
                    (mutated_case / filename).unlink()
                    (mutated_case / filename).symlink_to(target)
                    with self.assertRaises(runner.CatalogError):
                        runner._replay_case(mutated_case, directory_path / f"replay-{filename}")

    def test_catalog_and_replay_https_sources_require_valid_authority(self) -> None:
            base = {
                "id": "https-grammar",
                "prompt": "prompt",
                "sources": ["https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool"],
                "expected_output": "output",
                "expectations": ["observe"],
                "trigger": "explicit",
                "should_trigger": True,
            }
            https = "https" + "://"
            invalid_sources = (
                https,
                https + "/missing-host",
                https + "bad " + "host/path",
                https + "user:pass@example.com/path",
                https + "[::1/path",
                https + "example" + ".com:bad/path",
                "http" + "://example.com/path",
            )
            for source in invalid_sources:
                with self.subTest(source=source):
                    raw = dict(base)
                    raw["id"] = "https-invalid"
                    raw["sources"] = [source]
                    with self.assertRaises(runner.CatalogError):
                        runner._case(runner.ROOT, "nexus-cli-payment-tracking", raw, 0)
            runner._validate_https_source("https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool", "test")
            runner._validate_https_source(https + "127.0.0.1:443/path", "test")

            with tempfile.TemporaryDirectory(prefix="skill-eval-replay-https-") as directory:
                directory_path = Path(directory)
                source_case = runner.Case(
                    "nexus-cli-payment-tracking",
                    "https-replay",
                    "prompt",
                    "explicit",
                    True,
                    (
                        "bundled:nexus-cli-payment-tracking/references/payment-ledger.md",
                        "https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool",
                    ),
                    "output",
                    ("manual",),
                    (),
                )
                body = (
                    "import json\n"
                    "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                    "print(json.dumps({'type': 'turn.completed'}))\n"
                )
                run_output = directory_path / "run"
                runner._run_case(
                    source_case,
                    self._fake_command(directory, body),
                    run_output,
                    10,
                )
                saved_case = run_output / source_case.skill / source_case.case_id
                for index, source in enumerate(invalid_sources):
                    with self.subTest(replay_source=source):
                        mutated_case = directory_path / f"saved-https-{index}"
                        shutil.copytree(saved_case, mutated_case, symlinks=True)
                        metadata_path = mutated_case / "metadata.json"
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        metadata["sources"] = [source]
                        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                        with self.assertRaises(runner.CatalogError):
                            runner._replay_case(mutated_case, directory_path / f"replay-https-{index}")


    def test_replay_metadata_envelope_is_strict_and_preserves_producer_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-envelope-") as directory:
            directory_path = Path(directory)
            case = runner.load_cases()[0]
            body = (
                "import json\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            run_output = directory_path / "run"
            runner._run_case(case, self._fake_command(directory, body), run_output, 10)
            saved_case = run_output / case.skill / case.case_id

            mutations = {
                "schema-missing": lambda metadata: metadata.pop("schema_version"),
                "schema-wrong-value": lambda metadata: metadata.update(schema_version=3),
                "schema-bool": lambda metadata: metadata.update(schema_version=True),
                "mode-missing": lambda metadata: metadata.pop("mode"),
                "mode-wrong-type": lambda metadata: metadata.update(mode={"run": True}),
                "mode-unsupported": lambda metadata: metadata.update(mode="plan"),
                "returncode-missing": lambda metadata: metadata.pop("command_returncode"),
                "returncode-bool": lambda metadata: metadata.update(command_returncode=False),
                "returncode-float": lambda metadata: metadata.update(command_returncode=0.0),
                "returncode-string": lambda metadata: metadata.update(command_returncode="0"),
                "returncode-object": lambda metadata: metadata.update(command_returncode={}),
                "timed-out-wrong-type": lambda metadata: metadata.update(timed_out=0),
                "launch-error-wrong-type": lambda metadata: metadata.update(launch_error=[]),
                "effective-argv-wrong-type": lambda metadata: metadata.update(effective_argv={}),
                "snapshot-error-wrong-type": lambda metadata: metadata.update(snapshot_error=[]),
            }
            for name, mutate in mutations.items():
                with self.subTest(mutation=name):
                    mutated_case = directory_path / f"saved-{name}"
                    shutil.copytree(saved_case, mutated_case)
                    metadata_path = mutated_case / "metadata.json"
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    mutate(metadata)
                    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                    replay_output = directory_path / f"replay-{name}"
                    with self.assertRaises(runner.CatalogError):
                        runner._replay_case(mutated_case, replay_output)
                    self.assertFalse(replay_output.exists())

            outside = directory_path / "workspace-outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
            for name in ("workspace-missing", "workspace-symlink"):
                with self.subTest(workspace_mutation=name):
                    mutated_case = directory_path / name
                    shutil.copytree(saved_case, mutated_case, symlinks=True)
                    workspace = mutated_case / "workspace"
                    if name == "workspace-missing":
                        shutil.rmtree(workspace)
                    else:
                        shutil.rmtree(workspace)
                        workspace.symlink_to(outside, target_is_directory=True)
                    with self.assertRaises(runner.CatalogError):
                        runner._replay_case(
                            mutated_case, directory_path / f"replay-{name}"
                        )

            failed_output = directory_path / "failed"
            runner._run_case(
                case,
                self._fake_command(directory, "import sys\nsys.exit(7)\n"),
                failed_output,
                10,
            )
            self.assertEqual(
                runner._metadata_case(
                    failed_output / case.skill / case.case_id / "metadata.json"
                ).case_id,
                case.case_id,
            )

            timeout_output = directory_path / "timeout"
            runner._run_case(
                case,
                self._fake_command(directory, "import time\ntime.sleep(1)\n"),
                timeout_output,
                0.01,
            )
            self.assertEqual(
                runner._metadata_case(
                    timeout_output / case.skill / case.case_id / "metadata.json"
                ).case_id,
                case.case_id,
            )

            launch_output = directory_path / "launch"
            runner._run_case(case, ["command-does-not-exist"], launch_output, 10)
            self.assertEqual(
                runner._metadata_case(
                    launch_output / case.skill / case.case_id / "metadata.json"
                ).case_id,
                case.case_id,
            )

            invalid_timeout_output = directory_path / "invalid-timeout"
            runner._run_case(
                case,
                self._fake_command(directory, body),
                invalid_timeout_output,
                None,
            )
            self.assertEqual(
                runner._metadata_case(
                    invalid_timeout_output / case.skill / case.case_id / "metadata.json"
                ).case_id,
                case.case_id,
            )

            empty_root = directory_path / "empty-skills"
            empty_root.mkdir()
            snapshot_error_output = directory_path / "snapshot-error"
            runner._run_case(
                case,
                self._fake_command(directory, body),
                snapshot_error_output,
                10,
                root=empty_root,
            )
            snapshot_error_metadata = (
                snapshot_error_output / case.skill / case.case_id / "metadata.json"
            )
            self.assertEqual(
                runner._metadata_case(snapshot_error_metadata).case_id,
                case.case_id,
            )

    def test_replay_outputs_are_self_contained_across_relocations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-cycles-") as directory:
            directory_path = Path(directory)
            case = runner.load_cases()[0]
            body = (
                "import json\n"
                "from pathlib import Path\n"
                "Path('workspace-observation.txt').write_text('saved workspace\\n')\n"
                "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            command = shlex.join(self._fake_command(directory, body))
            run_output = directory_path / "run"
            self.assertEqual(
                runner.main(
                    [
                        "--skill",
                        case.skill,
                        "--case",
                        case.case_id,
                        "--output-dir",
                        str(run_output),
                        "--command",
                        command,
                    ]
                ),
                0,
            )

            def case_root(output: Path) -> Path:
                return output / case.skill / case.case_id

            def metadata_at(output: Path) -> dict[str, object]:
                return json.loads((case_root(output) / "metadata.json").read_text(encoding="utf-8"))

            def result_at(output: Path) -> dict[str, object]:
                return json.loads((case_root(output) / "result.json").read_text(encoding="utf-8"))

            original_metadata = metadata_at(run_output)
            original_result = result_at(run_output)
            original_workspace_files = sorted(
                path.relative_to(case_root(run_output) / "workspace").as_posix()
                for path in (case_root(run_output) / "workspace").rglob("*")
                if path.is_file()
            )
            outputs = [run_output]
            for index in range(1, 4):
                replay_output = directory_path / f"replay-{index}"
                self.assertEqual(
                    runner.main(
                        [
                            "--replay",
                            str(outputs[-1]),
                            "--root",
                            str(directory_path / "missing-current-skills"),
                            "--output-dir",
                            str(replay_output),
                        ]
                    ),
                    0,
                )
                outputs.append(replay_output)
                replay_case_root = case_root(replay_output)
                replay_metadata = metadata_at(replay_output)
                replay_result = result_at(replay_output)
                self.assertEqual(replay_metadata["mode"], "replay")
                self.assertTrue(replay_metadata["replayed_from"])
                self.assertEqual(
                    replay_metadata["bundle_manifest"], original_metadata["bundle_manifest"]
                )
                self.assertEqual(
                    replay_metadata["source_digests"], original_metadata["source_digests"]
                )
                self.assertEqual(replay_result["checks"], original_result["checks"])
                self.assertEqual(replay_result["deterministic_status"], "pass")
                self.assertEqual(replay_result["status"], "unknown")
                self.assertEqual(replay_result["manual_status"], "pending")
                self.assertTrue(
                    (replay_case_root / "snapshot/nexus-cli-payment-tracking/SKILL.md").is_file()
                )
                self.assertTrue(
                    (replay_case_root / "workspace/.agents/skills/nexus-cli-payment-tracking/SKILL.md").is_file()
                )
                self.assertTrue(
                    (replay_case_root / "workspace/workspace-observation.txt").is_file()
                )
                self.assertEqual(
                    sorted(
                        path.relative_to(replay_case_root / "workspace").as_posix()
                        for path in (replay_case_root / "workspace").rglob("*")
                        if path.is_file()
                    ),
                    original_workspace_files,
                )

            relocated = directory_path / "relocated-latest"
            shutil.rmtree(run_output)
            shutil.rmtree(outputs[1])
            shutil.rmtree(outputs[2])
            outputs[3].rename(relocated)
            final_output = directory_path / "replay-after-relocation"
            self.assertEqual(
                runner.main(
                    [
                        "--replay",
                        str(relocated),
                        "--root",
                        str(directory_path / "missing-current-skills"),
                        "--output-dir",
                        str(final_output),
                    ]
                ),
                0,
            )
            final_metadata = metadata_at(final_output)
            final_result = result_at(final_output)
            self.assertEqual(final_metadata["mode"], "replay")
            self.assertEqual(
                final_metadata["bundle_manifest"], original_metadata["bundle_manifest"]
            )
            self.assertEqual(final_result["checks"], original_result["checks"])
            self.assertEqual(final_result["deterministic_status"], "pass")
            self.assertEqual(final_result["status"], "unknown")
            self.assertEqual(final_result["manual_status"], "pending")
            self.assertTrue(
                (case_root(final_output) / "workspace/.agents/skills/nexus-cli-payment-tracking/SKILL.md").is_file()
            )
            self.assertTrue(
                (case_root(final_output) / "workspace/workspace-observation.txt").is_file()
            )


    def test_catalog_selectors_are_idempotent_across_run_plan_and_list(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-selectors-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            command = self._fake_command(
                directory,
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0\n"
                "counter.write_text(str(count + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            command_line = shlex.join(command)

            def invoke(arguments: list[str]) -> tuple[int, str, str]:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = runner.main(arguments)
                return status, stdout.getvalue(), stderr.getvalue()

            duplicate_selector = [
                "--skill",
                "nexus-cli-payment-tracking",
                "--skill",
                "nexus-cli-payment-tracking",
                "--case",
                "payment-read-trace",
                "--case",
                "payment-read-trace",
            ]
            union_selector = [
                "--skill",
                "nexus-cli-payment-tracking",
                "--skill",
                "nexus-cli-payment-tracking",
                "--skill",
                "nexus-tap-development",
                "--skill",
                "nexus-tap-development",
                "--case",
                "payment-read-trace",
                "--case",
                "payment-read-trace",
                "--case",
                "tap-local-package",
                "--case",
                "tap-local-package",
            ]

            duplicate_run_output = directory_path / "run-duplicate"
            status, stdout, stderr = invoke(
                [
                    *duplicate_selector,
                    "--output-dir",
                    str(duplicate_run_output),
                    "--command",
                    command_line,
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(len(stdout.splitlines()), 1)
            duplicate_summary = json.loads(
                (duplicate_run_output / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(duplicate_summary["cases"], 1)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            union_run_output = directory_path / "run-union"
            status, stdout, stderr = invoke(
                [
                    *union_selector,
                    "--output-dir",
                    str(union_run_output),
                    "--command",
                    command_line,
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(len(stdout.splitlines()), 2)
            union_summary = json.loads(
                (union_run_output / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(union_summary["cases"], 2)
            self.assertEqual(
                [(item["skill"], item["case_id"]) for item in union_summary["results"]],
                [
                    ("nexus-cli-payment-tracking", "payment-read-trace"),
                    ("nexus-tap-development", "tap-local-package"),
                ],
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "3")

            duplicate_plan_output = directory_path / "plan-duplicate"
            status, stdout, stderr = invoke(
                [
                    "--plan",
                    *duplicate_selector,
                    "--output-dir",
                    str(duplicate_plan_output),
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(len(stdout.splitlines()), 1)
            self.assertEqual(len(list(duplicate_plan_output.rglob("plan.json"))), 1)
            self.assertIn("planned 1 case(s)", stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "3")

            union_plan_output = directory_path / "plan-union"
            status, stdout, stderr = invoke(
                [
                    "--plan",
                    *union_selector,
                    "--output-dir",
                    str(union_plan_output),
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(len(stdout.splitlines()), 2)
            self.assertEqual(len(list(union_plan_output.rglob("plan.json"))), 2)
            self.assertIn("planned 2 case(s)", stderr)

            status, stdout, stderr = invoke(
                [
                    "--list",
                    *duplicate_selector,
                    "--output-dir",
                    str(directory_path / "list-duplicate"),
                ]
            )
            self.assertEqual(status, 0, stderr)
            duplicate_rows = [
                json.loads(line) for line in stdout.splitlines() if line
            ]
            self.assertEqual(len(duplicate_rows), 1)
            self.assertEqual(duplicate_rows[0]["case_id"], "payment-read-trace")

            status, stdout, stderr = invoke(
                [
                    "--list",
                    *union_selector,
                    "--output-dir",
                    str(directory_path / "list-union"),
                ]
            )
            self.assertEqual(status, 0, stderr)
            union_rows = [json.loads(line) for line in stdout.splitlines() if line]
            self.assertEqual(
                [(row["skill"], row["case_id"]) for row in union_rows],
                [
                    ("nexus-cli-payment-tracking", "payment-read-trace"),
                    ("nexus-tap-development", "tap-local-package"),
                ],
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "3")

    def test_replay_selectors_are_idempotent_and_use_metadata_case_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-case-id-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            command = self._fake_command(
                directory,
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0\n"
                "counter.write_text(str(count + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            command_line = shlex.join(command)
            run_output = directory_path / "run"
            self.assertEqual(
                runner.main(
                    [
                        "--skill",
                        "nexus-cli-payment-tracking",
                        "--skill",
                        "nexus-tap-development",
                        "--case",
                        "payment-read-trace",
                        "--case",
                        "tap-local-package",
                        "--output-dir",
                        str(run_output),
                        "--command",
                        command_line,
                    ]
                ),
                0,
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            saved_case = (
                run_output / "nexus-cli-payment-tracking" / "payment-read-trace"
            )
            renamed_case = saved_case.with_name("renamed-case-root")
            saved_case.rename(renamed_case)

            def invoke(arguments: list[str]) -> tuple[int, str]:
                stderr = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    status = runner.main(arguments)
                return status, stderr.getvalue()

            repeated_output = directory_path / "replay-repeated"
            status, stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--case",
                    "payment-read-trace",
                    "--case",
                    "payment-read-trace",
                    "--output-dir",
                    str(repeated_output),
                ]
            )
            self.assertEqual(status, 0, stderr)
            repeated_case = (
                repeated_output / "nexus-cli-payment-tracking" / "payment-read-trace"
            )
            self.assertTrue(repeated_case.is_dir())
            self.assertFalse(
                (repeated_output / "nexus-cli-payment-tracking" / renamed_case.name).exists()
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            union_output = directory_path / "replay-union"
            status, stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-tap-development",
                    "--skill",
                    "nexus-tap-development",
                    "--output-dir",
                    str(union_output),
                ]
            )
            self.assertEqual(status, 0, stderr)
            union_summary = json.loads(
                (union_output / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(union_summary["cases"], 2)
            self.assertTrue(
                (union_output / "nexus-cli-payment-tracking" / "payment-read-trace").is_dir()
            )
            self.assertTrue(
                (union_output / "nexus-tap-development" / "tap-local-package").is_dir()
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            intersection_output = directory_path / "replay-intersection"
            status, stderr = invoke(
                [
                    "--replay",
                    str(run_output),
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-tap-development",
                    "--skill",
                    "nexus-tap-development",
                    "--case",
                    "payment-read-trace",
                    "--case",
                    "payment-read-trace",
                    "--output-dir",
                    str(intersection_output),
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertTrue(
                (
                    intersection_output
                    / "nexus-cli-payment-tracking"
                    / "payment-read-trace"
                ).is_dir()
            )
            self.assertFalse(
                (intersection_output / "nexus-tap-development" / "tap-local-package").exists()
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            metadata = json.loads(
                (
                    intersection_output
                    / "nexus-cli-payment-tracking"
                    / "payment-read-trace"
                    / "metadata.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["case_id"], "payment-read-trace")

    def test_live_bundle_manifest_rejects_producer_changes(self) -> None:
        mutations = {
            "modified": (
                "target = bundle / 'nexus-cli-payment-tracking' / 'SKILL.md'\n"
                "target.write_text(target.read_text(encoding='utf-8') + 'producer mutation', encoding='utf-8')\n"
            ),
            "added": (
                "(bundle / 'nexus-cli-payment-tracking' / 'producer-added.txt').write_text('added', encoding='utf-8')\n"
            ),
            "deleted": (
                "(bundle / 'nexus-cli-payment-tracking' / 'SKILL.md').unlink()\n"
            ),
        }
        with tempfile.TemporaryDirectory(prefix="skill-eval-live-bundle-") as directory:
            directory_path = Path(directory)

            def invoke(arguments: list[str]) -> tuple[int, str, str]:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = runner.main(arguments)
                return status, stdout.getvalue(), stderr.getvalue()

            for operation, mutation in mutations.items():
                with self.subTest(operation=operation):
                    body = (
                        "import json, os\n"
                        "from pathlib import Path\n"
                        "bundle = Path(os.environ['SKILLS_BUNDLE_ROOT'])\n"
                        + mutation
                        + "print(json.dumps({'type': 'skill_invocation', 'skill': 'nexus-cli-payment-tracking'}))\n"
                        + "print(json.dumps({'type': 'turn.completed'}))\n"
                    )
                    command_line = shlex.join(self._fake_command(directory, body))
                    output = directory_path / f"run-{operation}"
                    status, stdout, stderr = invoke(
                        [
                            "--skill",
                            "nexus-cli-payment-tracking",
                            "--case",
                            "payment-read-trace",
                            "--output-dir",
                            str(output),
                            "--command",
                            command_line,
                        ]
                    )
                    self.assertEqual(status, 1, stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual(len(stdout.splitlines()), 1)
                    case_root = output / "nexus-cli-payment-tracking" / "payment-read-trace"
                    result = json.loads((case_root / "result.json").read_text(encoding="utf-8"))
                    metadata = json.loads((case_root / "metadata.json").read_text(encoding="utf-8"))
                    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
                    integrity = next(
                        check for check in result["checks"] if check["name"] == "bundle_integrity"
                    )
                    self.assertEqual(integrity["status"], "fail")
                    self.assertIn("command-visible bundle", integrity["detail"])
                    self.assertEqual(result["deterministic_status"], "fail")
                    self.assertEqual(result["status"], "fail")
                    self.assertEqual(summary["cases"], 1)
                    self.assertEqual(summary["fail"], 1)
                    self.assertEqual(summary["results"][0]["status"], "fail")
                    self.assertTrue(metadata["bundle_manifest"])
                    self.assertTrue((case_root / "trace.jsonl").is_file())
                    self.assertTrue((case_root / "workspace" / ".agents" / "skills").is_dir())

    def test_replay_preflight_rejects_duplicate_metadata_identities_before_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-duplicate-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            counter.write_text("0", encoding="utf-8")
            body = (
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "counter.write_text(str(int(counter.read_text(encoding='utf-8')) + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            command_line = shlex.join(self._fake_command(directory, body))
            run_output = directory_path / "run"
            self.assertEqual(
                runner.main(
                    [
                        "--skill",
                        "nexus-cli-payment-tracking",
                        "--case",
                        "payment-read-trace",
                        "--output-dir",
                        str(run_output),
                        "--command",
                        command_line,
                    ]
                ),
                0,
            )
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")
            saved_case = run_output / "nexus-cli-payment-tracking" / "payment-read-trace"
            replay_root = directory_path / "saved-duplicate"
            replay_root.mkdir()
            shutil.copytree(saved_case, replay_root / "a-original")
            shutil.copytree(saved_case, replay_root / "z-renamed")
            replay_output = directory_path / "replay"
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                status = runner.main(
                    [
                        "--replay",
                        str(replay_root),
                        "--output-dir",
                        str(replay_output),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertIn("duplicate saved case identity", stderr.getvalue())
            self.assertFalse(replay_output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

    def test_replay_selectors_ignore_malformed_nonselected_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-selector-identity-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            counter.write_text("0", encoding="utf-8")
            body = (
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0\n"
                "counter.write_text(str(count + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            command_line = shlex.join(self._fake_command(directory, body))
            saved_output = directory_path / "saved"
            run_status = runner.main(
                [
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--skill",
                    "nexus-tap-development",
                    "--case",
                    "payment-read-trace",
                    "--case",
                    "tap-local-package",
                    "--output-dir",
                    str(saved_output),
                    "--command",
                    command_line,
                ]
            )
            self.assertEqual(run_status, 0)
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            nonselected_metadata_path = (
                saved_output
                / "nexus-tap-development"
                / "tap-local-package"
                / "metadata.json"
            )
            nonselected_metadata = json.loads(
                nonselected_metadata_path.read_text(encoding="utf-8")
            )
            nonselected_metadata["expectations"] = []
            nonselected_metadata_path.write_text(
                json.dumps(nonselected_metadata), encoding="utf-8"
            )

            def invoke(selectors: list[str], name: str) -> tuple[int, str, str, Path]:
                output = directory_path / name
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = runner.main(
                        [
                            "--replay",
                            str(saved_output),
                            *selectors,
                            "--output-dir",
                            str(output),
                        ]
                    )
                return status, stdout.getvalue(), stderr.getvalue(), output

            selector_cases = (
                (
                    "skill-only",
                    ["--skill", "nexus-cli-payment-tracking"],
                ),
                ("case-only", ["--case", "payment-read-trace"]),
                (
                    "intersection",
                    [
                        "--skill",
                        "nexus-cli-payment-tracking",
                        "--skill",
                        "nexus-tap-development",
                        "--case",
                        "payment-read-trace",
                    ],
                ),
            )
            for name, selectors in selector_cases:
                with self.subTest(selector=name):
                    status, stdout, stderr, output = invoke(selectors, f"replay-{name}")
                    self.assertEqual(status, 0, stderr)
                    self.assertEqual(len(stdout.splitlines()), 1)
                    self.assertTrue(
                        (
                            output
                            / "nexus-cli-payment-tracking"
                            / "payment-read-trace"
                        ).is_dir()
                    )
                    self.assertFalse(
                        (output / "nexus-tap-development" / "tap-local-package").exists()
                    )
                    summary = json.loads(
                        (output / "summary.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(summary["cases"], 1)
                    self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            selected_metadata_path = (
                saved_output
                / "nexus-cli-payment-tracking"
                / "payment-read-trace"
                / "metadata.json"
            )
            selected_metadata = json.loads(
                selected_metadata_path.read_text(encoding="utf-8")
            )
            selected_metadata["expectations"] = []
            selected_metadata_path.write_text(
                json.dumps(selected_metadata), encoding="utf-8"
            )
            status, _, stderr, output = invoke(
                ["--skill", "nexus-cli-payment-tracking"], "replay-selected-invalid"
            )
            self.assertEqual(status, 2)
            self.assertIn("saved expectations are invalid", stderr)
            self.assertFalse(output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            selected_metadata["expectations"] = ["observe"]
            selected_metadata_path.write_text(
                json.dumps(selected_metadata), encoding="utf-8"
            )
            nonselected_metadata["case_id"] = "nested/case"
            nonselected_metadata_path.write_text(
                json.dumps(nonselected_metadata), encoding="utf-8"
            )
            status, _, stderr, output = invoke(
                ["--skill", "nexus-cli-payment-tracking"], "replay-identity-invalid"
            )
            self.assertEqual(status, 2)
            self.assertIn("case id must be a single safe path component", stderr)
            self.assertFalse(output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

    def test_replay_rejects_special_workspace_entries_before_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-special-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            counter.write_text("0", encoding="utf-8")
            body = (
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0\n"
                "counter.write_text(str(count + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            saved_output = directory_path / "saved"
            run_status = runner.main(
                [
                    "--skill",
                    "nexus-cli-payment-tracking",
                    "--case",
                    "payment-read-trace",
                    "--output-dir",
                    str(saved_output),
                    "--command",
                    shlex.join(self._fake_command(directory, body)),
                ]
            )
            self.assertEqual(run_status, 0)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            workspace = (
                saved_output
                / "nexus-cli-payment-tracking"
                / "payment-read-trace"
                / "workspace"
            )
            fifo = workspace / "cache.fifo"
            try:
                os.mkfifo(fifo)
            except (AttributeError, NotImplementedError, OSError) as exc:
                self.skipTest(f"special filesystem entries unsupported: {exc}")

            replay_output = directory_path / "replay"
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                replay_status = runner.main(
                    [
                        "--replay",
                        str(saved_output),
                        "--output-dir",
                        str(replay_output),
                    ]
                )
            self.assertEqual(replay_status, 2)
            self.assertIn("unsupported special entry", stderr.getvalue())
            self.assertFalse(replay_output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

    def test_replay_preflight_validates_later_records_before_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-replay-later-invalid-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            counter.write_text("0", encoding="utf-8")
            body = (
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "counter.write_text(str(int(counter.read_text(encoding='utf-8')) + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            command_line = shlex.join(self._fake_command(directory, body))
            run_output = directory_path / "run"
            run_stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(run_stderr):
                run_status = runner.main(
                    [
                        "--skill",
                        "nexus-cli-payment-tracking",
                        "--skill",
                        "nexus-tap-development",
                        "--case",
                        "payment-read-trace",
                        "--case",
                        "tap-local-package",
                        "--output-dir",
                        str(run_output),
                        "--command",
                        command_line,
                    ]
                )
            self.assertEqual(run_status, 0, run_stderr.getvalue())
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            first_case = run_output / "nexus-cli-payment-tracking" / "payment-read-trace"
            later_case = run_output / "nexus-tap-development" / "tap-local-package"
            replay_root = directory_path / "saved-later-invalid"
            replay_root.mkdir()
            shutil.copytree(first_case, replay_root / "a-valid")
            shutil.copytree(later_case, replay_root / "z-invalid")
            metadata_path = replay_root / "z-invalid" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["expectations"] = []
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            replay_output = directory_path / "replay"
            replay_stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(replay_stderr):
                replay_status = runner.main(
                    [
                        "--replay",
                        str(replay_root),
                        "--output-dir",
                        str(replay_output),
                    ]
                )
            self.assertEqual(replay_status, 2)
            self.assertIn("saved expectations are invalid", replay_stderr.getvalue())
            self.assertFalse(replay_output.exists())
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

    def test_terminal_actions_are_mutually_exclusive_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-terminal-conflicts-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            counter.write_text("0", encoding="utf-8")
            command = self._fake_command(
                directory,
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "counter.write_text(str(int(counter.read_text(encoding='utf-8')) + 1), encoding='utf-8')\n"
                "print('producer launched')\n",
            )
            command_line = shlex.join(command)
            replay_root = directory_path / "replay-input"

            def invoke(arguments: list[str]) -> tuple[int, str, str]:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    try:
                        status = runner.main(arguments)
                    except SystemExit as exc:
                        status = exc.code
                return status, stdout.getvalue(), stderr.getvalue()

            conflicts = (
                ("plan-list", ("--plan", "--list")),
                ("list-plan", ("--list", "--plan")),
                ("plan-replay", ("--plan", "--replay", str(replay_root))),
                ("replay-plan", ("--replay", str(replay_root), "--plan")),
                ("list-replay", ("--list", "--replay", str(replay_root))),
                ("replay-list", ("--replay", str(replay_root), "--list")),
                ("plan-list-replay", ("--plan", "--list", "--replay", str(replay_root))),
            )
            for label, mode_args in conflicts:
                with self.subTest(conflict=label):
                    output = directory_path / f"output-{label}"
                    status, stdout, stderr = invoke(
                        [*mode_args, "--output-dir", str(output), "--command", command_line]
                    )
                    self.assertEqual(status, 2)
                    self.assertIn("not allowed with argument", stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual(stdout, "")
                    self.assertFalse(output.exists())
                    self.assertEqual(counter.read_text(encoding="utf-8"), "0")
    def test_command_presence_is_mode_specific_and_suppresses_producer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-eval-command-matrix-") as directory:
            directory_path = Path(directory)
            counter = directory_path / "producer-count.txt"
            command = self._fake_command(
                directory,
                "import json\n"
                "from pathlib import Path\n"
                f"counter = Path({str(counter)!r})\n"
                "count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0\n"
                "counter.write_text(str(count + 1), encoding='utf-8')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
            command_line = shlex.join(command)
            case_args = [
                "--skill",
                "nexus-cli-payment-tracking",
                "--case",
                "payment-read-trace",
            ]

            def invoke(arguments: list[str]) -> tuple[int, str, str]:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = runner.main(arguments)
                return status, stdout.getvalue(), stderr.getvalue()

            status, _, stderr = invoke(
                [*case_args, "--output-dir", str(directory_path / "run-absent")]
            )
            self.assertEqual(status, 2)
            self.assertIn("--command is required", stderr)
            self.assertFalse(counter.exists())

            status, _, stderr = invoke(
                [
                    *case_args,
                    "--output-dir",
                    str(directory_path / "run-empty"),
                    "--command",
                    "",
                ]
            )
            self.assertEqual(status, 2)
            self.assertIn("--command is required", stderr)
            self.assertFalse(counter.exists())

            run_valid_output = directory_path / "run-valid"
            status, _, stderr = invoke(
                [
                    *case_args,
                    "--output-dir",
                    str(run_valid_output),
                    "--command",
                    command_line,
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            status, _, stderr = invoke(
                [
                    "--plan",
                    *case_args,
                    "--output-dir",
                    str(directory_path / "plan-absent"),
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            for label, command_value in (("valid", command_line), ("empty", "")):
                plan_output = directory_path / f"plan-{label}-command"
                status, _, stderr = invoke(
                    [
                        "--plan",
                        *case_args,
                        "--output-dir",
                        str(plan_output),
                        "--command",
                        command_value,
                    ]
                )
                self.assertEqual(status, 2)
                self.assertIn("--command cannot be combined with --plan", stderr)
                self.assertFalse(any(plan_output.rglob("plan.json")))
                self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            status, stdout, stderr = invoke(
                [
                    "--list",
                    *case_args,
                    "--output-dir",
                    str(directory_path / "list-absent"),
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(len(stdout.splitlines()), 1)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            for label, command_value in (("valid", command_line), ("empty", "")):
                status, _, stderr = invoke(
                    [
                        "--list",
                        *case_args,
                        "--output-dir",
                        str(directory_path / f"list-{label}-command"),
                        "--command",
                        command_value,
                    ]
                )
                self.assertEqual(status, 2)
                self.assertIn("--command cannot be combined with --list", stderr)
                self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            status, _, stderr = invoke(
                [
                    "--replay",
                    str(run_valid_output),
                    "--output-dir",
                    str(directory_path / "replay-absent"),
                ]
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            for label, command_value in (("valid", command_line), ("empty", "")):
                replay_output = directory_path / f"replay-{label}-command"
                status, _, stderr = invoke(
                    [
                        "--replay",
                        str(run_valid_output),
                        "--output-dir",
                        str(replay_output),
                        "--command",
                        command_value,
                    ]
                )
                self.assertEqual(status, 2)
                self.assertIn("--command cannot be combined with --replay", stderr)
                self.assertFalse(replay_output.exists())
                self.assertEqual(counter.read_text(encoding="utf-8"), "1")


if __name__ == "__main__":
    unittest.main()