#!/usr/bin/env python3
"""Tests for the repository-owned TAP artifact verifier."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_tap_artifacts import verify_tap_artifacts  # noqa: E402


def fixture(root: Path) -> None:
    (root / "tap" / "deps" / "interface").mkdir(parents=True)
    (root / "tap" / "sources").mkdir()
    (root / "tap" / "deps" / "interface" / "Move.toml").write_text(
        "[package]\nname = \"interface\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    (root / "tap" / "Move.toml").write_text(
        "[package]\nname = \"portable_tap\"\nversion = \"0.1.0\"\n\n"
        "[dependencies]\ninterface = { local = \"deps/interface\" }\n",
        encoding="utf-8",
    )
    (root / "artifacts").mkdir()
    (root / "artifacts" / "main.dag.json").write_text(
        json.dumps(
            {
                "vertices": [
                    {
                        "name": "entry",
                        "kind": {"variant": "on_chain", "tool_fqn": "abc.taluslabs.example@1"},
                        "entry_ports": [{"name": "state"}],
                        "output_ports": {"ok": [{"name": "result"}]},
                    }
                ],
                "edges": [],
                "outputs": [{"vertex": "entry", "output_variant": "ok", "output_port": "result"}],
                "shared_objects": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "artifacts" / "main.skill.tap.json").write_text(
        json.dumps(
            {
                "name": "portable",
                "dag_path": "main.dag.json",
                "requirements": {
                    "input_commitment": [1],
                    "payment_policy": "UserFunded",
                    "schedule_policy": "Once",
                    "fixed_tools": [
                        {
                            "tool_registry_id": {"bytes": "0x0"},
                            "tool_fqn": {"bytes": list(b"abc.taluslabs.example@1")},
                        }
                    ],
                    "shared_objects": [],
                },
                "interface_revision": {"inner": 1},
            }
        ),
        encoding="utf-8",
    )


class TapArtifactVerifierTests(unittest.TestCase):
    def test_repository_owned_fixture_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["runtime_proof"], "not-proven")
            self.assertEqual(report["checks"][0]["dependency_counts"]["total"], 1)
            self.assertEqual(report["checks"][2]["outputs"], 1)
            self.assertEqual(report["checks"][3]["fixed_tools"], 1)

    def test_dag_binding_mutations_fail_closed(self) -> None:
        mutations = {
            "duplicate vertex": lambda dag, _skill: dag["vertices"].append(dict(dag["vertices"][0])),
            "unknown kind": lambda dag, _skill: dag["vertices"][0]["kind"].__setitem__("variant", "unknown"),
            "dangling edge vertex": lambda dag, _skill: dag["edges"].append(
                {"from": "entry", "to": "missing", "source_port": "state", "target_port": "state"}
            ),
            "dangling edge port": lambda dag, _skill: dag["edges"].append(
                {"from": "entry", "to": "entry", "source_port": "missing", "target_port": "state"}
            ),
            "malformed edge direction": lambda dag, _skill: dag["edges"].append(
                {"from": "entry", "to": "entry", "source_port": "state"}
            ),
            "unknown output vertex": lambda dag, _skill: dag["outputs"][0].__setitem__("vertex", "missing"),
            "blank output port": lambda dag, _skill: dag["outputs"][0].__setitem__("output_port", ""),
            "malformed shared object": lambda dag, _skill: dag["shared_objects"].append({"id": "0x1"}),
            "unbound fixed Tool": lambda _dag, skill: skill["requirements"]["fixed_tools"][0].__setitem__(
                "tool_fqn", {"bytes": list(b"other.taluslabs.example@1")}
            ),
            "missing interface revision": lambda _dag, skill: skill.pop("interface_revision"),
            "missing requirements": lambda _dag, skill: skill.pop("requirements"),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture(root)
                dag_path = root / "artifacts" / "main.dag.json"
                skill_path = root / "artifacts" / "main.skill.tap.json"
                dag = json.loads(dag_path.read_text(encoding="utf-8"))
                skill = json.loads(skill_path.read_text(encoding="utf-8"))
                mutate(dag, skill)
                dag_path.write_text(json.dumps(dag), encoding="utf-8")
                skill_path.write_text(json.dumps(skill), encoding="utf-8")
                report = verify_tap_artifacts(root, require_artifacts=True)
                self.assertEqual(report["status"], "fail")

    def test_fixed_tool_bindings_are_an_exact_dag_bijection(self) -> None:
        fixture_root = Path(__file__).resolve().parents[1] / "fixtures"
        for fixture_name in ("direct", "delayed"):
            with self.subTest(fixture=fixture_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / fixture_name
                shutil.copytree(fixture_root / fixture_name, root)
                report = verify_tap_artifacts(root, require_artifacts=True)
                skill_check = next(check for check in report["checks"] if check["id"].startswith("skill:"))
                self.assertEqual(skill_check["dag_tool_fqns"], skill_check["fixed_tool_fqns"])

                skill_path = root / "artifacts" / f"{fixture_name}.skill.tap.json"
                skill = json.loads(skill_path.read_text(encoding="utf-8"))
                fixed_tools = skill["requirements"]["fixed_tools"]
                fixed_tools.pop()
                skill_path.write_text(json.dumps(skill), encoding="utf-8")
                report = verify_tap_artifacts(root, require_artifacts=True)
                self.assertEqual(report["status"], "fail")
                self.assertTrue(
                    any(
                        "bijection" in check.get("detail", "") or "non-empty list" in check.get("detail", "")
                        for check in report["checks"]
                    )
                )

                shutil.rmtree(root)
                shutil.copytree(fixture_root / fixture_name, root)
                skill_path = root / "artifacts" / f"{fixture_name}.skill.tap.json"
                skill = json.loads(skill_path.read_text(encoding="utf-8"))
                skill["requirements"]["fixed_tools"].append(dict(skill["requirements"]["fixed_tools"][0]))
                skill_path.write_text(json.dumps(skill), encoding="utf-8")
                report = verify_tap_artifacts(root, require_artifacts=True)
                self.assertEqual(report["status"], "fail")
                self.assertTrue(any("duplicates" in check.get("detail", "") for check in report["checks"]))

                shutil.rmtree(root)
                shutil.copytree(fixture_root / fixture_name, root)
                skill_path = root / "artifacts" / f"{fixture_name}.skill.tap.json"
                skill = json.loads(skill_path.read_text(encoding="utf-8"))
                extra = dict(skill["requirements"]["fixed_tools"][0])
                extra["tool_fqn"] = {"bytes": list(b"extra.taluslabs.tool@1")}
                skill["requirements"]["fixed_tools"].append(extra)
                skill_path.write_text(json.dumps(skill), encoding="utf-8")
                report = verify_tap_artifacts(root, require_artifacts=True)
                self.assertEqual(report["status"], "fail")
                self.assertTrue(any("bijection" in check.get("detail", "") for check in report["checks"]))

                if fixture_name == "delayed":
                    shutil.rmtree(root)
                    shutil.copytree(fixture_root / fixture_name, root)
                    dag_path = root / "artifacts" / "delayed.dag.json"
                    dag = json.loads(dag_path.read_text(encoding="utf-8"))
                    dag["vertices"][1]["kind"]["tool_fqn"] = dag["vertices"][0]["kind"]["tool_fqn"]
                    dag_path.write_text(json.dumps(dag), encoding="utf-8")
                    report = verify_tap_artifacts(root, require_artifacts=True)
                    self.assertEqual(report["status"], "fail")
                    self.assertTrue(any("repeated Tool FQN" in check.get("detail", "") for check in report["checks"]))

                    shutil.rmtree(root)
                    shutil.copytree(fixture_root / fixture_name, root)
                    dag_path = root / "artifacts" / "delayed.dag.json"
                    dag = json.loads(dag_path.read_text(encoding="utf-8"))
                    dag["edges"][0]["from"], dag["edges"][0]["to"] = dag["edges"][0]["to"], dag["edges"][0]["from"]
                    dag_path.write_text(json.dumps(dag), encoding="utf-8")
                    report = verify_tap_artifacts(root, require_artifacts=True)
                    self.assertEqual(report["status"], "fail")

    def test_declared_output_ports_bind_variants_and_ports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            dag_path = root / "artifacts" / "main.dag.json"
            dag = json.loads(dag_path.read_text(encoding="utf-8"))
            dag["vertices"][0]["output_ports"] = {"ok": [{"name": "result"}]}
            dag_path.write_text(json.dumps(dag), encoding="utf-8")
            self.assertEqual(verify_tap_artifacts(root, require_artifacts=True)["status"], "pass")
            dag["outputs"][0]["output_port"] = "missing"
            dag_path.write_text(json.dumps(dag), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(check["id"] == "dag:main.dag.json" and check["status"] == "fail" for check in report["checks"]))

    def test_dag_self_loop_and_multi_vertex_cycle_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            dag_path = root / "artifacts" / "main.dag.json"
            dag = json.loads(dag_path.read_text(encoding="utf-8"))
            dag["edges"] = [{"from": "entry", "to": "entry", "source_port": "result", "target_port": "state"}]
            dag_path.write_text(json.dumps(dag), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("self-loop", report["checks"][2]["detail"])

            dag["vertices"].append(
                {
                    "name": "step",
                    "kind": {"variant": "off_chain", "tool_fqn": "abc.taluslabs.step@1"},
                    "entry_ports": [{"name": "input"}],
                    "output_ports": {"ok": [{"name": "next"}]},
                }
            )
            dag["edges"] = [
                {"from": "entry", "to": "step", "source_port": "result", "target_port": "input"},
                {"from": "step", "to": "entry", "source_port": "next", "target_port": "state"},
            ]
            dag_path.write_text(json.dumps(dag), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("cycle involving vertices", report["checks"][2]["detail"])
            self.assertIn("entry", report["checks"][2]["detail"])
            self.assertIn("step", report["checks"][2]["detail"])

    def test_valid_branching_dag_has_stable_topological_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            dag_path = root / "artifacts" / "main.dag.json"
            dag = json.loads(dag_path.read_text(encoding="utf-8"))
            for name in ("left", "right"):
                dag["vertices"].append(
                    {
                        "name": name,
                        "kind": {"variant": "off_chain", "tool_fqn": f"abc.taluslabs.{name}@1"},
                        "entry_ports": [{"name": "input"}],
                        "output_ports": {"ok": [{"name": "next"}]},
                    }
                )
            dag["edges"] = [
                {"from": "entry", "to": "right", "source_port": "result", "target_port": "input"},
                {"from": "entry", "to": "left", "source_port": "result", "target_port": "input"},
            ]
            dag_path.write_text(json.dumps(dag), encoding="utf-8")
            skill_path = root / "artifacts" / "main.skill.tap.json"
            skill = json.loads(skill_path.read_text(encoding="utf-8"))
            skill["requirements"]["fixed_tools"].extend(
                {
                    "tool_registry_id": {"bytes": f"0x{index + 1}"},
                    "tool_fqn": {"bytes": list(f"abc.taluslabs.{name}@1".encode("ascii"))},
                }
                for index, name in enumerate(("left", "right"))
            )
            skill_path.write_text(json.dumps(skill), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["checks"][2]["topological_order"], ["entry", "left", "right"])

    def test_dependency_escape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            manifest = root / "tap" / "Move.toml"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace("deps/interface", "../../outside"), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["checks"][0]["id"], "move-manifest")

    def test_direct_runtime_remote_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            manifest = root / "tap" / "Move.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "[dependencies]\ninterface = { local = \"deps/interface\" }",
                    '[dependencies]\nremote = { git = "' + "https://" + 'example.invalid/repo.git", rev = "main" }',
                ),
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("remote or malformed", report["checks"][0]["detail"])

    def test_direct_dev_remote_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            manifest = root / "tap" / "Move.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                + '\n[dev-dependencies]\nremote = { registry = "' + "https://" + 'example.invalid/index", version = "1" }\n',
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("remote or malformed", report["checks"][0]["detail"])

    def test_transitive_runtime_and_dev_remote_dependencies_fail_closed(self) -> None:
        cases = {
            "runtime": (
                "[dependencies]\nremote = { git = \"" + "https://" + "example.invalid/runtime.git\", rev = \"main\" }\n",
                "[dependencies]\nchild = { local = \"deps/child\" }\n",
            ),
            "dev": (
                "[dev-dependencies]\nremote = { registry = \"" + "https://" + "example.invalid/index\", version = \"1\" }\n",
                "[dev-dependencies]\nchild = { local = \"deps/child\" }\n",
            ),
        }
        for kind, (child_table, root_table) in cases.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture(root)
                child = root / "tap" / "deps" / "child"
                child.mkdir()
                (child / "Move.toml").write_text(
                    "[package]\nname = \"child\"\nversion = \"0.1.0\"\n\n" + child_table,
                    encoding="utf-8",
                )
                manifest = root / "tap" / "Move.toml"
                original = manifest.read_text(encoding="utf-8")
                dependency_start = original.index("[dependencies]")
                original = original[:dependency_start] + root_table
                manifest.write_text(original, encoding="utf-8")
                report = verify_tap_artifacts(root, require_artifacts=True)
                self.assertEqual(report["status"], "fail")
                self.assertIn("remote or malformed", report["checks"][0]["detail"])

    def test_transitive_missing_manifest_and_cycle_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            manifest = root / "tap" / "Move.toml"
            original = manifest.read_text(encoding="utf-8")
            manifest.write_text(
                original.replace(
                    'interface = { local = "deps/interface" }',
                    'child = { local = "deps/missing" }',
                ),
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("directory is missing", report["checks"][0]["detail"])

            missing = root / "tap" / "deps" / "missing"
            missing.mkdir()
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("has no Move.toml", report["checks"][0]["detail"])

            child = root / "tap" / "deps" / "child"
            child.mkdir()
            (child / "Move.toml").write_text(
                "[package]\nname = \"child\"\nversion = \"0.1.0\"\n\n"
                "[dependencies]\nroot = { local = \"../..\" }\n",
                encoding="utf-8",
            )
            manifest.write_text(
                original.replace(
                    'interface = { local = "deps/interface" }',
                    'child = { local = "deps/child" }',
                ),
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("dependency cycle", report["checks"][0]["detail"])

    def test_remote_lock_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            (root / "tap" / "Move.lock").write_text(
                "[pinned.testnet.remote]\nsource = { git = \"" + "https://" + "example.invalid/repo.git\" }\n",
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertIn("remote or malformed", report["checks"][0]["detail"])

    def test_mixed_runtime_dev_local_closure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            child = root / "tap" / "deps" / "child"
            grandchild = child / "deps" / "grandchild"
            grandchild.mkdir(parents=True)
            (grandchild / "Move.toml").write_text(
                "[package]\nname = \"grandchild\"\nversion = \"0.1.0\"\n", encoding="utf-8"
            )
            child.mkdir(parents=True, exist_ok=True)
            (child / "Move.toml").write_text(
                "[package]\nname = \"child\"\nversion = \"0.1.0\"\n\n"
                "[dev-dependencies]\ngrandchild = { local = \"deps/grandchild\" }\n",
                encoding="utf-8",
            )
            manifest = root / "tap" / "Move.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                + '\n[dev-dependencies]\nchild = { local = "deps/child" }\n',
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "pass")
            closure = report["checks"][0]["dependency_counts"]
            self.assertEqual(
                closure,
                {"dependencies": 1, "dev-dependencies": 2, "runtime": 1, "dev": 2, "total": 3},
            )

    def test_local_package_alias_is_checked_against_target_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            dependency = root / "tap/deps/interface/Move.toml"
            dependency.write_text(
                "[package]\nname = \"MoveStdlib\"\nversion = \"0.1.0\"\n",
                encoding="utf-8",
            )
            manifest = root / "tap/Move.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'interface = { local = "deps/interface" }',
                    'std = { local = "deps/interface", package = "MoveStdlib" }',
                ),
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "pass")
            dependency.write_text(
                dependency.read_text(encoding="utf-8").replace("MoveStdlib", "Other"),
                encoding="utf-8",
            )
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")

    def test_missing_required_artifacts_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            (root / "artifacts" / "main.dag.json").unlink()
            (root / "artifacts" / "main.skill.tap.json").unlink()
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(check["id"] == "artifacts-present" and check["status"] == "fail" for check in report["checks"]))

    def test_skill_dag_escape_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            skill = root / "artifacts" / "main.skill.tap.json"
            skill.write_text(json.dumps({"name": "portable", "dag_path": "../outside.json"}), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(check["id"].startswith("skill:") and check["status"] == "fail" for check in report["checks"]))

    def test_skill_dag_pointer_must_name_a_validated_dag_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            skill = root / "artifacts" / "main.skill.tap.json"
            (root / "artifacts" / "other.json").write_text(
                json.dumps({"vertices": [{"name": "unvalidated"}], "edges": []}), encoding="utf-8"
            )
            skill.write_text(json.dumps({"name": "portable", "dag_path": "other.json"}), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(check["id"].startswith("skill:") and check["status"] == "fail" for check in report["checks"]))

            (root / "artifacts" / "other.dag.json").write_text(
                json.dumps({"vertices": [{"name": "unrelated"}], "edges": []}), encoding="utf-8"
            )
            skill.write_text(json.dumps({"name": "portable", "dag_path": "other.dag.json"}), encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(check["id"].startswith("skill:") and check["status"] == "fail" for check in report["checks"]))

            (root / "artifacts" / "other.dag.json").write_text("{", encoding="utf-8")
            report = verify_tap_artifacts(root, require_artifacts=True)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(check["id"] == "dag:other.dag.json" and check["status"] == "fail" for check in report["checks"]))


if __name__ == "__main__":
    unittest.main()
