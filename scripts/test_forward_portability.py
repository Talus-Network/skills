"""Tests for pre-generation DAG/TAP intent and generated-artifact validation."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forward_portability
from forward_portability import (
    ForwardCleanupError,
    _assert_closed_rust_commands,
    _assert_closed_rust_proof,
    _assert_observed_toolchain_commands,
    _assert_sui_toolchain_version,
    _approved_public_interface_identity,
    _bind_public_interface_address,
    _assert_public_interface_lock_binding,
    _guarded_environment,
    _generate_dag_document,
    _generate_skill_document,
    _run_move_test,
    _run,
    _rust_tool,
    _semantic_intent,
    _source_acquisition_evidence,
    _copy_sui_framework_dependencies,
    _prepare_repository_fixture,
    _tracked_forward_workspace,
    _observed_forward_trace,
    _validate_generated_manifest_closure,
    _validate_generated_semantic_artifacts,
    _write_generated_semantic_artifacts,
)
from prepare_sources import SourcePreparationError


class ForwardIntentTests(unittest.TestCase):
    def test_public_interface_identity_comes_from_testnet_published_mapping(self) -> None:
        address = "0xd73f9252b5a1056b75f66c1ec9ddfb4b1f92b607d25d9297e7fc747644c7c762"
        with tempfile.TemporaryDirectory(prefix="forward-interface-identity-") as directory:
            root = Path(directory)
            package = root / "packages/interface"
            package.mkdir(parents=True)
            (package / "Move.toml").write_text(
                "[package]\nname = \"nexus_interface\"\nversion = \"2.0.0\"\n\n"
                "[addresses]\nnexus_interface = \"0x0\"\n",
                encoding="utf-8",
            )
            (package / "Published.toml").write_text(
                f"[published.testnet]\nchain-id = \"4c78adac\"\noriginal-id = \"{address}\"\npublished-at = \"{address}\"\n",
                encoding="utf-8",
            )
            identity = _approved_public_interface_identity(root)
            self.assertEqual(identity["package_address"], address)
            copied = root / "consumer"
            (copied / "deps/interface").mkdir(parents=True)
            (copied / "deps/interface/Move.toml").write_text(
                "[package]\nname = \"nexus_interface\"\n\n[addresses]\nnexus_interface = \"0x0\"\n",
                encoding="utf-8",
            )
            _bind_public_interface_address(copied, address)
            self.assertIn(f'nexus_interface = "{address}"', (copied / "deps/interface/Move.toml").read_text())
            (copied / "Move.lock").write_text(
                "[pinned.local.nexus_interface]\n"
                'source = { local = "deps/interface" }\n'
                'manifest_digest = "A"\n',
                encoding="utf-8",
            )
            _assert_public_interface_lock_binding(copied)
    def test_observed_sui_version_is_bound_to_exact_public_commit(self) -> None:
        _assert_sui_toolchain_version("sui 1.78.0-d8459684b41e")
        for version in ("sui 1.78.0-d8459684b41", "sui 1.78.0-unknown", "sui 1.79.0-d8459684b41e", "forged"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(SourcePreparationError, "does not match public source commit"):
                    _assert_sui_toolchain_version(version)

    def test_source_acquisition_is_pinned_and_separate_from_execution(self) -> None:
        from prepare_sources import DEFAULT_REPOSITORIES

        manifest = {
            "repositories": {
                name: {
                    "repository": spec.repository,
                    "ref": spec.ref,
                    "archive_url": spec.archive_url,
                    "archive_sha256": spec.archive_sha256,
                    "reviewed_archive_sha256": spec.archive_sha256,
                }
                for name, spec in DEFAULT_REPOSITORIES.items()
            }
        }
        evidence = _source_acquisition_evidence(manifest)
        self.assertEqual(evidence["phase"], "pre-execution-acquisition")
        self.assertEqual([record["logical_name"] for record in evidence["repositories"]], ["nexus-move-packages", "nexus-sdk", "sui"])
        altered = json.loads(json.dumps(manifest))
        altered["repositories"]["sui"]["ref"] = "main"
        with self.assertRaisesRegex(SourcePreparationError, "not pinned"):
            _source_acquisition_evidence(altered)

    def test_sui_framework_copy_uses_only_local_packages_and_strips_lockfiles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-sui-framework-copy-") as directory:
            root = Path(directory)
            source = root / "sui-source/crates/sui-framework/packages"
            for package_name, package_id in (("move-stdlib", "MoveStdlib"), ("sui-framework", "Sui")):
                package = source / package_name
                package.mkdir(parents=True)
                dependencies = "\n[dependencies]\nMoveStdlib = { local = \"../move-stdlib\" }\n" if package_name == "sui-framework" else ""
                (package / "Move.toml").write_text(
                    f'[package]\nname = "{package_id}"\nedition = "2024.beta"\n{dependencies}',
                    encoding="utf-8",
                )
                (package / "Move.lock").write_text("[pinned.local]\n", encoding="utf-8")
                (package / "sources").mkdir()
                (package / "sources/main.move").write_text("// source\n", encoding="utf-8")
            project = root / "consumer"
            evidence = _copy_sui_framework_dependencies(project, root / "sui-source")
            self.assertEqual(set(evidence["packages"]), {"move-stdlib", "sui-framework"})
            self.assertFalse((project / "deps/sui-framework/packages/move-stdlib/Move.lock").exists())
            self.assertFalse((project / "deps/sui-framework/packages/sui-framework/Move.lock").exists())
            self.assertTrue((project / "deps/sui-framework/packages/sui-framework/Move.toml").is_file())

    def test_fixture_template_rewrite_records_public_provenance(self) -> None:
        fixture_source = Path(__file__).resolve().parents[1] / "nexus-tap-development/fixtures/direct"
        with tempfile.TemporaryDirectory(prefix="forward-fixture-rewrite-") as directory:
            root = Path(directory)
            sui_source = root / "sui-source/crates/sui-framework/packages"
            for package_name, package_id in (("move-stdlib", "MoveStdlib"), ("sui-framework", "Sui")):
                package = sui_source / package_name
                package.mkdir(parents=True)
                (package / "Move.toml").write_text(
                    f'[package]\nname = "{package_id}"\nversion = "0.1.0"\nedition = "2024"\n',
                    encoding="utf-8",
                )
                (package / "sources").mkdir()
                (package / "sources/main.move").write_text("// authenticated public source fixture\n", encoding="utf-8")
            project = root / "fixture-direct"
            shutil.copytree(fixture_source, project)
            evidence = _prepare_repository_fixture(project, root / "sui-source")
            self.assertEqual(
                evidence["fixture_contract"]["dependency_template"]["source"]["repository"], "MystenLabs/sui"
            )
            self.assertEqual(
                evidence["rewrite"]["mode"], "replace-template-with-authenticated-public-sui-framework"
            )
            self.assertEqual(evidence["rewrite"]["template_sha256"], evidence["fixture_contract"]["dependency_template"]["sha256"])
            self.assertTrue((project / "deps/sui-framework/packages/move-stdlib/sources/main.move").is_file())

    def test_forward_runtime_boundary_uses_public_read_only_evidence(self) -> None:
        text = Path(__file__).with_name("forward_portability.py").read_text(encoding="utf-8")
        self.assertIn("SuiTestnetEvidenceClient", text)
        self.assertIn("graphql.testnet.sui.io/graphql", text)
        self.assertIn('"wallet_or_environment_config": "empty non-wallet build config only; no environment selected"', text)
        self.assertIn("empty-keystore.yaml", text)
        self.assertIn('"sui",\n        "move",', text)
        self.assertIn("verify_tap_artifacts.py", text)
        self.assertIn("validate_compiled_execute", text)
        self.assertIn("validate_artifact_consistency", text)
        self.assertIn("portable.dag.json", text)
        self.assertIn("move_test_counts", text)
        self.assertIn("_run_move_test", text)
        self.assertNotIn('"status": "not-run"', text)
        self.assertNotIn("sui client", text)
        self.assertNotIn("active_env", text)
        self.assertNotIn("ephemeral-keystore", text)

    def test_generators_capture_intent_before_writing_artifacts(self) -> None:
        dag = _generate_dag_document()
        skill = _generate_skill_document()
        intent = _semantic_intent(dag, skill)
        with tempfile.TemporaryDirectory(prefix="forward-intent-") as directory:
            project = Path(directory)
            _write_generated_semantic_artifacts(project, dag, skill)
            _validate_generated_semantic_artifacts(project, intent)
            self.assertEqual(json.loads((project / "portable.dag.json").read_text()), dag)
            self.assertEqual(json.loads((project / "portable.skill.tap.json").read_text()), skill)
            self.assertEqual(json.loads((project / "artifacts/portable.dag.json").read_text()), dag)
            self.assertEqual(json.loads((project / "artifacts/portable.skill.tap.json").read_text()), skill)
            self.assertEqual(skill["dag_path"], "portable.dag.json")

    def test_wrong_semantic_fields_are_rejected_before_post_generation_mutation(self) -> None:
        dag = _generate_dag_document()
        skill = _generate_skill_document()
        intent = _semantic_intent(dag, skill)
        mutations = {
            "payment": lambda document: document["requirements"].__setitem__("payment_policy", "AgentFunded"),
            "schedule": lambda document: document["requirements"].__setitem__("schedule_policy", "Recurring"),
            "fixed-tool": lambda document: document["requirements"]["fixed_tools"][0].__setitem__(
                "tool_fqn", {"bytes": list(b"other.taluslabs.tool@1")}
            ),
            "interface": lambda document: document.__setitem__("interface_revision", {"inner": 2}),
            "shared-object": lambda document: document["requirements"]["shared_objects"].append(
                {"id": "0x1", "mutable": True}
            ),
            "edge": lambda document: document.__setitem__(
                "edges", [{"from": "entry", "to": "other", "source_port": "state", "target_port": "state"}]
            ),
        }
        for name, mutate in mutations.items():
            altered_dag = copy.deepcopy(dag)
            altered_skill = copy.deepcopy(skill)
            target = altered_dag if name == "edge" else altered_skill
            mutate(target)
            with tempfile.TemporaryDirectory(prefix="forward-intent-mutation-") as directory:
                project = Path(directory)
                _write_generated_semantic_artifacts(project, altered_dag, altered_skill)
                with self.subTest(mutation=name):
                    with self.assertRaisesRegex(SourcePreparationError, "pre-generation caller intent"):
                        _validate_generated_semantic_artifacts(project, intent)

    def test_move_test_count_is_recorded_and_zero_tests_fail_closed(self) -> None:
        command_result = {
            "command": ["sui", "move", "test"],
            "returncode": 0,
            "stdout_tail": "Test result: OK. Total tests: 2; passed: 2; failed: 0;",
            "stderr_tail": "",
        }
        with mock.patch("forward_portability._run", return_value=command_result):
            result = _run_move_test(["sui", "move", "test"], Path("."), {})
        self.assertEqual(result["move_test_count"], 2)

        zero_result = {
            **command_result,
            "stdout_tail": "Test result: OK. Total tests: 0; passed: 0; failed: 0;",
        }
        with mock.patch("forward_portability._run", return_value=zero_result):
            with self.assertRaisesRegex(SourcePreparationError, "zero tests"):
                _run_move_test(["sui", "move", "test"], Path("."), {})

    def test_rust_forward_proof_is_std_only_and_has_no_acquisition_events(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-rust-proof-") as directory:
            root = Path(directory)
            sdk_root = root / "sdk-source"
            (sdk_root / "sdk/src").mkdir(parents=True)
            (sdk_root / "sdk/Cargo.toml").write_text("[package]\nname = \"public-sdk\"\n", encoding="utf-8")
            (sdk_root / "sdk/src/tool_fqn.rs").write_text("pub struct ToolFqn;\n", encoding="utf-8")
            project = root / "project"
            _rust_tool(project, sdk_root)
            workspace = root / "workspace"
            workspace.mkdir()
            trace = _assert_closed_rust_proof(project, sdk_root, workspace)
            self.assertEqual(trace["mode"], "std-only-rustc")
            self.assertEqual(trace["observation"], {"status": "pending"})
            rendered_trace = (project / "source-trace.json").read_text(encoding="utf-8").casefold()
            for marker in ("cargo", "crates.io", "registry", "git", "github.com", "http://", "https://"):
                self.assertNotIn(marker, rendered_trace)
            environment = _guarded_environment(workspace)
            self.assertNotIn("CARGO_HOME", environment)
            self.assertEqual(environment["CARGO_NET_OFFLINE"], "true")
            self.assertEqual(environment["NEXUS_PORTABILITY_ISOLATION_MODE"], "bwrap-unshare-net")
            _assert_closed_rust_commands(
                [{"command": ["rustc", "--edition=2021", "--test"]}], environment
            )
            with self.assertRaisesRegex(SourcePreparationError, "unapproved source acquisition"):
                _assert_closed_rust_commands([{"command": ["cargo", "build"]}], environment)

            (project / "unapproved-source.txt").write_text("https://" + "crates" + ".io/index", encoding="utf-8")
            with self.assertRaisesRegex(SourcePreparationError, "unapproved acquisition marker"):
                _assert_closed_rust_proof(project, sdk_root, workspace)

    def test_manifest_closure_rejects_hidden_transitive_sources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-manifest-closure-") as directory:
            workspace = Path(directory)
            consumers = workspace / "consumers"
            root = consumers / "tool"
            child = root / "deps/child"
            root.mkdir(parents=True)
            child.mkdir(parents=True, exist_ok=True)
            (root / "Move.toml").write_text(
                "[package]\nname = \"root\"\n[dependencies]\nchild = { local = \"deps/child\" }\n",
                encoding="utf-8",
            )
            child_manifest = "[package]\nname = \"child\"\n"
            (child / "Move.toml").write_text(child_manifest, encoding="utf-8")
            child_lock = (
                "[pinned.local.remote]\nsource = { git = \""
                + "https://"
                + "example.invalid/repo.git\" }\n"
            )
            (child / "Move.lock").write_text(child_lock, encoding="utf-8")
            with self.assertRaisesRegex(SourcePreparationError, "unapproved external dependency"):
                _validate_generated_manifest_closure(
                    consumers, workspace, roots=(root / "Move.toml",)
                )

    def test_manifest_closure_accepts_reachable_local_move_lock_sources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-manifest-local-") as directory:
            workspace = Path(directory)
            consumers = workspace / "consumers"
            root = consumers / "tool"
            child = root / "deps/child"
            child.mkdir(parents=True)
            (root / "Move.toml").write_text(
                "[package]\nname = \"root\"\n[dependencies]\nchild = { local = \"deps/child\" }\n",
                encoding="utf-8",
            )
            (child / "Move.toml").write_text("[package]\nname = \"child\"\n", encoding="utf-8")
            (root / "Move.lock").write_text(
                "[pinned.local.root]\nroot = true\n[pinned.local.child]\nsource = { local = \"deps/child\" }\n",
                encoding="utf-8",
            )
            closure = _validate_generated_manifest_closure(
                consumers, workspace, roots=(root / "Move.toml",)
            )
            self.assertEqual(closure["external_dependencies"], [])
            self.assertEqual(len(closure["manifests"]), 2)
            self.assertEqual(
                closure["dependency_counts"],
                {"dependencies": 1, "dev-dependencies": 0, "runtime": 1, "dev": 0, "total": 1},
            )

    def test_manifest_closure_rejects_direct_and_transitive_move_dev_sources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-move-dev-closure-") as directory:
            workspace = Path(directory)
            consumers = workspace / "consumers"
            root = consumers / "tool"
            child = root / "deps/child"
            root.mkdir(parents=True)
            child.mkdir(parents=True)
            (root / "Move.toml").write_text(
                "[package]\nname = \"root\"\n[dev-dependencies]\ndirect = { git = \""
                + "https://"
                + "example.invalid/direct.git\", rev = \"main\" }\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourcePreparationError, "unapproved external dependency"):
                _validate_generated_manifest_closure(consumers, workspace, roots=(root / "Move.toml",))

            (root / "Move.toml").write_text(
                "[package]\nname = \"root\"\n[dev-dependencies]\nchild = { local = \"deps/child\" }\n",
                encoding="utf-8",
            )
            (child / "Move.toml").write_text(
                "[package]\nname = \"child\"\n[dev-dependencies]\nremote = { registry = \""
                + "https://"
                + "example.invalid/index\", version = \"1\" }\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourcePreparationError, "unapproved external dependency"):
                _validate_generated_manifest_closure(consumers, workspace, roots=(root / "Move.toml",))

    def test_manifest_closure_accepts_transitive_local_move_dev_dependencies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-move-dev-local-") as directory:
            workspace = Path(directory)
            consumers = workspace / "consumers"
            root = consumers / "tool"
            child = root / "deps/child"
            grandchild = child / "deps/grandchild"
            grandchild.mkdir(parents=True)
            (root / "Move.toml").write_text(
                "[package]\nname = \"root\"\n[dev-dependencies]\nchild = { local = \"deps/child\" }\n",
                encoding="utf-8",
            )
            child.mkdir(parents=True, exist_ok=True)
            (child / "Move.toml").write_text(
                "[package]\nname = \"child\"\n[dev-dependencies]\ngrandchild = { local = \"deps/grandchild\" }\n",
                encoding="utf-8",
            )
            (grandchild / "Move.toml").write_text("[package]\nname = \"grandchild\"\n", encoding="utf-8")
            closure = _validate_generated_manifest_closure(consumers, workspace, roots=(root / "Move.toml",))
            self.assertEqual(
                closure["dependency_counts"],
                {"dependencies": 0, "dev-dependencies": 2, "runtime": 0, "dev": 2, "total": 2},
            )

    def test_manifest_closure_rejects_transitive_cargo_registry_dependency(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-cargo-closure-") as directory:
            workspace = Path(directory)
            consumers = workspace / "consumers"
            root = consumers / "rust"
            child = root / "child"
            child.mkdir(parents=True)
            (root / "Cargo.toml").write_text(
                "[package]\nname = \"root\"\nversion = \"0.1.0\"\n[dependencies]\nchild = { path = \"child\" }\n",
                encoding="utf-8",
            )
            (child / "Cargo.toml").write_text(
                "[package]\nname = \"child\"\nversion = \"0.1.0\"\n[dependencies]\nremote = \"1\"\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourcePreparationError, "unapproved external dependency"):
                _validate_generated_manifest_closure(
                    consumers, workspace, roots=(root / "Cargo.toml",)
                )

    def test_observed_forward_trace_derives_status_and_rejects_network_syscalls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-observed-trace-") as directory:
            workspace = Path(directory)
            observations = workspace / "observations"
            observations.mkdir()
            (observations / "events.jsonl").write_text(
                json.dumps(
                    {
                        "observed": True,
                        "mechanism": "strace -f -e trace=network",
                        "isolation": "bwrap --unshare-net",
                        "command": ["rustc", "--test"],
                        "network_event_count": 0,
                        "network_events": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            closure = {"external_dependencies": [], "manifests": []}
            trace = _observed_forward_trace(workspace, closure)
            self.assertEqual(trace["network_access"], "none")
            self.assertEqual(trace["acquisition_events"], [])
            (observations / "events.jsonl").write_text(
                json.dumps(
                    {
                        "observed": True,
                        "mechanism": "strace -f -e trace=network",
                        "isolation": "bwrap --unshare-net",
                        "command": ["rustc", "--test"],
                        "network_event_count": 1,
                        "network_events": ["socket(AF_INET, SOCK_STREAM, 0)"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            attempted = _observed_forward_trace(workspace, closure)
            self.assertEqual(attempted["network_access"], "attempted")
            self.assertEqual(len(attempted["acquisition_events"]), 1)

    def test_run_fails_when_syscall_trace_observes_network_access(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-network-attempt-") as directory:
            workspace = Path(directory)
            environment = _guarded_environment(workspace)

            def fake_run(command, **_kwargs):
                trace_path = Path(command[command.index("-o") + 1])
                trace_path.write_text("socket(AF_INET, SOCK_STREAM, 0) = 3\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch("forward_portability.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(SourcePreparationError, "attempted network access"):
                    _run(["rustc", "--version"], workspace, environment)

    def test_execution_runner_rejects_unobserved_subprocess_environment(self) -> None:
        with self.assertRaisesRegex(SourcePreparationError, "unobserved execution subprocess"):
            forward_portability._run(["rustc", "--version"], Path("."), {})

    def test_observed_trace_includes_runner_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-observed-command-list-") as directory:
            workspace = Path(directory)
            observations = workspace / "observations"
            observations.mkdir()
            (observations / "events.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "observed": True,
                            "mechanism": "strace -f -e trace=network",
                            "isolation": "bwrap --unshare-net",
                            "command": command,
                            "network_event_count": 0,
                            "network_events": [],
                        }
                    )
                    for command in (["sui", "move", "disassemble"], ["sui", "--version"])
                )
                + "\n",
                encoding="utf-8",
            )
            trace = _observed_forward_trace(workspace, {"external_dependencies": [], "dependency_counts": {}})
            self.assertEqual(trace["observed_commands"], 2)
            self.assertEqual(trace["commands"], [["sui", "move", "disassemble"], ["sui", "--version"]])
            _assert_observed_toolchain_commands(
                {
                    "commands": [
                        ["sui", "move", "--client.config", "config", "disassemble"],
                        ["sui", "--version"],
                    ]
                }
            )
            with self.assertRaisesRegex(SourcePreparationError, "omits"):
                _assert_observed_toolchain_commands({"commands": [["sui", "--version"]]})

    def test_forward_cleanup_preserves_primary_and_reports_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-cleanup-primary-") as directory:
            parent = Path(directory)
            workspace = parent / "nexus-forward-test"
            workspace.mkdir()
            original_cleanup = forward_portability._cleanup_forward_workspace
            calls: list[Path] = []

            def fixed_mkdtemp(*_args, **_kwargs):
                return str(workspace)

            def cleanup_then_fail(path: Path) -> None:
                calls.append(Path(path))
                original_cleanup(path)
                raise ForwardCleanupError(path, 23, [], "simultaneous cleanup failure")

            with mock.patch.object(forward_portability.tempfile, "mkdtemp", side_effect=fixed_mkdtemp), mock.patch.object(
                forward_portability, "_cleanup_forward_workspace", side_effect=cleanup_then_fail
            ):
                with self.assertRaises(ValueError) as raised:
                    with _tracked_forward_workspace():
                        raise ValueError("primary-forward")
            self.assertEqual(str(raised.exception), "primary-forward")
            cleanup_error = getattr(raised.exception, "_forward_cleanup_error", None)
            self.assertEqual(cleanup_error["returncode"], 23)
            self.assertEqual(cleanup_error["detail"], "simultaneous cleanup failure")
            self.assertEqual(len(calls), 1)
            self.assertFalse(workspace.exists())
            self.assertEqual(len(getattr(raised.exception, "__notes__", [])), 1)

    def test_forward_cleanup_only_failure_is_raised_after_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forward-cleanup-only-") as directory:
            parent = Path(directory)
            workspace = parent / "nexus-forward-test"
            workspace.mkdir()
            original_cleanup = forward_portability._cleanup_forward_workspace
            calls: list[Path] = []

            def cleanup_then_fail(path: Path) -> None:
                calls.append(Path(path))
                original_cleanup(path)
                raise ForwardCleanupError(path, 29, [], "cleanup-only failure")

            with mock.patch.object(
                forward_portability.tempfile,
                "mkdtemp",
                return_value=str(workspace),
            ), mock.patch.object(
                forward_portability,
                "_cleanup_forward_workspace",
                side_effect=cleanup_then_fail,
            ):
                with self.assertRaisesRegex(ForwardCleanupError, "cleanup-only failure"):
                    with _tracked_forward_workspace():
                        pass
            self.assertEqual(len(calls), 1)
            self.assertFalse(workspace.exists())

    def test_forward_workspace_signal_lifetime_cleans_once_and_stops_consumer(self) -> None:
        child_script = """
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, os.environ["HELPER_DIR"])
import forward_portability as forward

parent = Path(os.environ["TEST_PARENT"])
original_cleanup = forward._cleanup_forward_workspace
count_path = parent / "cleanup-count"

def counted_cleanup(workspace):
    current = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
    count_path.write_text(str(current + 1), encoding="utf-8")
    return original_cleanup(workspace)

forward._cleanup_forward_workspace = counted_cleanup
with forward._tracked_forward_workspace() as workspace:
    (parent / "workspace-path").write_text(str(workspace), encoding="utf-8")
    (parent / "consumer-started").write_text("started", encoding="utf-8")
    time.sleep(30)
    (parent / "post-signal").write_text("continued", encoding="utf-8")
"""
        for signum in (signal.SIGINT, signal.SIGTERM):
            with tempfile.TemporaryDirectory(prefix="forward-signal-lifetime-") as directory:
                parent = Path(directory)
                environment = dict(os.environ)
                environment["HELPER_DIR"] = str(Path(__file__).resolve().parent)
                environment["TEST_PARENT"] = str(parent)
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", child_script],
                    cwd=parent,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                started = parent / "consumer-started"
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists())
                workspace = Path((parent / "workspace-path").read_text(encoding="utf-8"))
                os.killpg(process.pid, signum)
                stdout, stderr = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual((parent / "cleanup-count").read_text(encoding="utf-8"), "1")
                self.assertFalse(workspace.exists())
                self.assertFalse((parent / "post-signal").exists())


if __name__ == "__main__":
    unittest.main()
