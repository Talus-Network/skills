"""Unit tests for generated Tool artifact identity and provenance consistency."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_artifacts import (
    ArtifactConsistencyError,
    _intent_projection,
    _summary_identity,
    canonical_schema_digest,
    canonical_semantic_digest,
    normalize_abi_type,
    validate_artifact_consistency,
)
import validate_artifacts
from validate_compiled_move import make_build_receipt, receipt_acceptance_digest, sha256_bytes


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def artifact_fixture(
    root: Path,
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    parameters = [
        {
            "name": "authorization",
            "type": {
                "kind": "datatype",
                "module": {"address": "nexus_primitives", "name": "authorization"},
                "name": "ProvenValue",
                "type_arguments": [
                    {
                        "phantom": False,
                        "argument": {
                            "kind": "datatype",
                            "module": {"address": "nexus_interface", "name": "authorization"},
                            "name": "AgentVertexAuthorization",
                            "type_arguments": [],
                        },
                    }
                ],
            },
        },
        {
            "name": "requirements",
            "type": {
                "kind": "datatype",
                "module": {"address": "nexus_primitives", "name": "proof_of_uid"},
                "name": "UIDRequirements",
                "type_arguments": [],
            },
        },
        {
            "name": "result",
            "type": {
                "kind": "datatype",
                "module": {"address": "nexus_interface", "name": "onchain_tool_result"},
                "name": "OnchainToolResult",
                "type_arguments": [],
            },
        },
        {
            "name": "state",
            "type": {
                "kind": "reference",
                "mutable": True,
                "to": {
                    "kind": "datatype",
                    "module": {"address": "example", "name": "example"},
                    "name": "ExampleState",
                    "type_arguments": [],
                },
            },
        },
        {"name": "amount", "type": "u64"},
        {
            "name": "ctx",
            "type": {
                "kind": "reference",
                "mutable": True,
                "to": {
                    "kind": "datatype",
                    "module": {"address": "sui", "name": "tx_context"},
                    "name": "TxContext",
                    "type_arguments": [],
                },
            },
        },
    ]
    output_variants = [
        {"name": "Ok", "fields": [{"name": "value", "type": "u64"}]},
        {"name": "Error", "fields": [{"name": "message", "type": {"kind": "vector", "element": "u8"}}]},
    ]
    metadata = {
        "schema_version": 1,
        "tool": {
            "fqn": "abc.taluslabs.example@7",
            "module": "example",
            "function": "run",
            "output_enum": "Result",
            "input_ports": ["state", "amount"],
            "output_ports": {"ok": ["value"], "error": ["message"]},
            "parameters": parameters,
            "output_variants": output_variants,
        },
    }
    summary = {
        "id": {"name": "example"},
        "functions": {
            "run": {
                "visibility": "Public",
                "parameters": [
                    {
                        "name": "authorization",
                        "type_": {
                            "Datatype": {
                                "module": {"address": "nexus_primitives", "name": "authorization"},
                                "name": "ProvenValue",
                                "type_arguments": [
                                    {
                                        "phantom": False,
                                        "argument": {
                                            "Datatype": {
                                                "module": {"address": "nexus_interface", "name": "authorization"},
                                                "name": "AgentVertexAuthorization",
                                                "type_arguments": [],
                                            }
                                        },
                                    }
                                ],
                            }
                        },
                    },
                    {
                        "name": "requirements",
                        "type_": {
                            "Datatype": {
                                "module": {"address": "nexus_primitives", "name": "proof_of_uid"},
                                "name": "UIDRequirements",
                                "type_arguments": [],
                            }
                        },
                    },
                    {
                        "name": "result",
                        "type_": {
                            "Datatype": {
                                "module": {"address": "nexus_interface", "name": "onchain_tool_result"},
                                "name": "OnchainToolResult",
                                "type_arguments": [],
                            }
                        },
                    },
                    {
                        "name": "state",
                        "type_": {
                            "Reference": [
                                True,
                                {
                                    "Datatype": {
                                        "module": {"address": "example", "name": "example"},
                                        "name": "ExampleState",
                                        "type_arguments": [],
                                    }
                                },
                            ]
                        },
                    },
                    {"name": "amount", "type_": "u64"},
                    {
                        "name": "ctx",
                        "type_": {
                            "Reference": [
                                True,
                                {
                                    "Datatype": {
                                        "module": {"address": "sui", "name": "tx_context"},
                                        "name": "TxContext",
                                        "type_arguments": [],
                                    }
                                },
                            ]
                        },
                    },
                ],
            }
        },
        "enums": {
            "Result": {
                "variants": {
                    "Ok": {"fields": {"fields": {"value": {"type_": "u64"}}}},
                    "Error": {"fields": {"fields": {"message": {"type_": {"vector": "u8"}}}}},
                }
            }
        },
    }
    dag = {
        "vertices": [
            {
                "name": "example_vertex",
                "kind": {"tool_fqn": "abc.taluslabs.example@7"},
                "entry_ports": [{"name": "state"}, {"name": "amount"}],
            }
        ],
        "outputs": [
            {"vertex": "example_vertex", "output_variant": "ok", "output_port": "value"},
            {"vertex": "example_vertex", "output_variant": "error", "output_port": "message"},
        ],
        "edges": [
            {
                "from": "example_vertex",
                "to": "example_vertex",
                "source_port": "state",
                "target_port": "state",
            }
        ],
    }
    skill = {
        "name": "example-skill",
        "dag_path": "dag.json",
        "entry_values": {"state": {"bytes": [1, 2, 3]}, "amount": 7},
        "requirements": {
            "input_commitment": [1, 2, 3],
            "payment_policy": "UserFunded",
            "schedule_policy": "Once",
            "shared_objects": [{"id": "0x1", "mutable": False}],
            "fixed_tools": [
                {
                    "tool_registry_id": {"bytes": "0x0"},
                    "tool_fqn": {"bytes": list(b"abc.taluslabs.example@7")},
                }
            ],
        },
        "interface_revision": {"inner": 1},
    }
    package_root = root / "package"
    build_root = package_root / "build" / "example"
    compiled_module = build_root / "bytecode_modules" / "example.mv"
    disassembly = build_root / "disassembly" / "example.mvb"
    build_info = build_root / "BuildInfo.yaml"
    compiled_module.parent.mkdir(parents=True)
    disassembly.parent.mkdir(parents=True)
    compiled_module.write_bytes(b"compiled example module")
    disassembly.write_text("trusted disassembly", encoding="utf-8")
    build_info.write_text("compiled_package_info:\n  package_name: example\n", encoding="utf-8")
    metadata_path = package_root / "tool-registration.json"
    summary_path = package_root / "summary.json"
    dag_path = package_root / "dag.json"
    skill_path = package_root / "skill.tap.json"
    write_json(metadata_path, metadata)
    write_json(summary_path, summary)
    write_json(dag_path, dag)
    write_json(skill_path, skill)
    schema_digest = canonical_schema_digest(
        module_address="0",
        module="example",
        function="run",
        output_enum="Result",
        input_ports=["state", "amount"],
        output_ports={"ok": ["value"], "error": ["message"]},
        parameters=parameters,
        output_variants=output_variants,
    )
    receipt = make_build_receipt(
        package_root=package_root,
        build_root=build_root,
        compiled_module=compiled_module,
        module_address="0",
        module_name="example",
        function_name="run",
        trusted_disassembly_sha256=sha256_bytes(disassembly.read_bytes()),
        toolchain={"command": ["sui", "move", "disassemble"], "version": "test"},
        disassembly_path=disassembly,
        schema_summary=summary_path,
        schema_digest=schema_digest,
        tree_manifest_root=root / "provenance",
    )
    receipt_path = root / "build-receipt.json"
    write_json(receipt_path, receipt)
    intent = {
        "fqn": "abc.taluslabs.example@7",
        "module_id": "0.example",
        "module_address": "0",
        "module": "example",
        "function": "run",
        "output_enum": "Result",
        "input_ports": ["state", "amount"],
        "output_ports": {"ok": ["value"], "error": ["message"]},
        "parameters": parameters,
        "output_variants": output_variants,
        "compiled_sha256": receipt["compiled_sha256"],
        "schema_digest": schema_digest,
        "dag_sha256": canonical_semantic_digest(dag),
        "skill_sha256": canonical_semantic_digest(skill),
        "receipt_acceptance_sha256": receipt_acceptance_digest(receipt),
    }
    manifest_path = package_root / "artifact-consistency.json"
    write_json(
        manifest_path,
        {
            "schema_version": 2,
            "tool_metadata": metadata_path.name,
            "abi_summary": summary_path.name,
            "dag": dag_path.name,
            "skill": skill_path.name,
            "build_receipt": "../build-receipt.json",
            "intent": _intent_projection(intent),
        },
    )
    # Refresh after the artifact manifest exists so the package tree snapshot
    # covers every package entry while the receipt remains outside the root.
    receipt = make_build_receipt(
        package_root=package_root,
        build_root=build_root,
        compiled_module=compiled_module,
        module_address="0",
        module_name="example",
        function_name="run",
        trusted_disassembly_sha256=sha256_bytes(disassembly.read_bytes()),
        toolchain={"command": ["sui", "move", "disassemble"], "version": "test"},
        disassembly_path=disassembly,
        schema_summary=summary_path,
        schema_digest=schema_digest,
        tree_manifest_root=root / "provenance",
    )
    write_json(receipt_path, receipt)
    intent["compiled_sha256"] = receipt["compiled_sha256"]
    intent["receipt_acceptance_sha256"] = receipt_acceptance_digest(receipt)
    write_json(
        manifest_path,
        {
            "schema_version": 2,
            "tool_metadata": metadata_path.name,
            "abi_summary": summary_path.name,
            "dag": dag_path.name,
            "skill": skill_path.name,
            "build_receipt": "../build-receipt.json",
            "intent": _intent_projection(intent),
        },
    )
    return manifest_path, intent, metadata, summary, dag, skill


class ValidateArtifactsTests(unittest.TestCase):
    def test_matching_tool_dag_summary_and_skill_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            manifest_path, intent, *_ = artifact_fixture(Path(directory))
            evidence = validate_artifact_consistency(
                manifest_path,
                expected_intent=intent,
                expected_receipt_sha256=str(intent["receipt_acceptance_sha256"]),
            )
            self.assertEqual(evidence["fqn"], "abc.taluslabs.example@7")
            self.assertEqual(evidence["declared_outputs"], 2)
            self.assertEqual(set(evidence["digests"]), {"manifest", "tool_metadata", "abi_summary", "dag", "skill", "dag_semantics", "skill_semantics", "build_receipt", "build_receipt_self", "compiled_module", "package_tree", "build_tree", "schema", "trusted_disassembly"})

    def test_complete_abi_types_reject_same_name_shape_mutations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-abi-") as directory:
            root = Path(directory)
            _manifest_path, intent, _metadata, summary, _dag, _skill = artifact_fixture(root)
            expected = {"module": "example", "function": "run", "output_enum": "Result", "parameters": intent["parameters"], "output_variants": intent["output_variants"]}
            mutations = {
                "reference-mutability": lambda document: document["functions"]["run"]["parameters"][3]["type_"]["Reference"].__setitem__(0, False),
                "different-reference-type": lambda document: document["functions"]["run"]["parameters"][3]["type_"]["Reference"][1]["Datatype"].__setitem__("name", "OtherState"),
                "generic-argument": lambda document: document["functions"]["run"]["parameters"][0]["type_"]["Datatype"]["type_arguments"].append({"phantom": False, "argument": "u64"}),
                "output-field-type": lambda document: document["enums"]["Result"]["variants"]["Ok"]["fields"]["fields"]["value"].__setitem__("type_", "u128"),
                "output-field-order": lambda document: document["enums"]["Result"]["variants"]["Error"]["fields"]["fields"].update({"extra": {"type_": "u8"}}),
            }
            for name, mutate in mutations.items():
                altered = copy.deepcopy(summary)
                mutate(altered)
                with self.subTest(mutation=name):
                    with self.assertRaisesRegex(ArtifactConsistencyError, "ABI summary"):
                        _summary_identity(altered, expected)

    def test_abi_normalization_retains_package_generic_and_reference_identity(self) -> None:
        datatype = {
            "Datatype": {
                "module": {"address": "pkg", "name": "mod"},
                "name": "Wrapper",
                "type_arguments": [{"phantom": True, "argument": {"vector": "u8"}}],
            }
        }
        first = normalize_abi_type({"Reference": [True, datatype]})
        second = normalize_abi_type({"Reference": [False, datatype]})
        self.assertNotEqual(first, second)
        self.assertEqual(first["to"]["module"], {"address": "pkg", "name": "mod"})
        self.assertEqual(first["to"]["type_arguments"][0]["argument"], {"kind": "vector", "element": "u8"})

    def test_identity_mutations_are_rejected(self) -> None:
        mutations = {
            "valid wrong FQN": ("dag.json", lambda document: document["vertices"][0]["kind"].__setitem__("tool_fqn", "other.taluslabs.example@9"), "(?:FQN does not match|package tree)"),
            "wrong module": ("tool-registration.json", lambda document: document["tool"].__setitem__("module", "other_module"), "(?:Tool metadata module|package tree)"),
            "wrong function": ("tool-registration.json", lambda document: document["tool"].__setitem__("function", "other_function"), "(?:Tool metadata function|package tree)"),
            "wrong output port": ("tool-registration.json", lambda document: document["tool"]["output_ports"]["ok"].__setitem__(0, "other_value"), "(?:Tool metadata output_ports|package tree)"),
            "mismatched fixed Tool": ("skill.tap.json", lambda document: document["requirements"]["fixed_tools"][0].__setitem__("tool_fqn", {"bytes": list(b"other.taluslabs.example@9")}), "(?:TAP fixed Tool FQN|package tree)"),
        }
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            root = Path(directory)
            manifest_path, intent, metadata, summary, dag, skill = artifact_fixture(root)
            originals = {
                "tool-registration.json": copy.deepcopy(metadata),
                "summary.json": copy.deepcopy(summary),
                "dag.json": copy.deepcopy(dag),
                "skill.tap.json": copy.deepcopy(skill),
            }
            for label, (filename, mutate, message) in mutations.items():
                document = copy.deepcopy(originals[filename])
                mutate(document)
                write_json(root / "package" / filename, document)
                with self.subTest(mutation=label):
                    with self.assertRaisesRegex(ArtifactConsistencyError, message):
                        validate_artifact_consistency(
                            manifest_path,
                            expected_intent=intent,
                            expected_receipt_sha256=str(intent["receipt_acceptance_sha256"]),
                        )
                write_json(root / "package" / filename, originals[filename])

    def test_fixed_tool_multiplicity_matches_dag_vertices(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            root = Path(directory)
            manifest_path, intent, _metadata, _summary, _dag, skill = artifact_fixture(root)
            skill["requirements"]["fixed_tools"].append(copy.deepcopy(skill["requirements"]["fixed_tools"][0]))
            write_json(root / "package/skill.tap.json", skill)
            intent["skill_sha256"] = canonical_semantic_digest(skill)
            receipt = json.loads((root / "build-receipt.json").read_text(encoding="utf-8"))
            receipt_evidence = {
                "receipt_sha256": "a" * 64,
                "compiled_sha256": str(intent["compiled_sha256"]),
                "package_tree_sha256": str(receipt["package_tree_sha256"]),
                "build_tree_sha256": str(receipt["build_tree_sha256"]),
            }
            with mock.patch.object(validate_artifacts, "validate_build_receipt", return_value=receipt_evidence):
                with self.assertRaisesRegex(ArtifactConsistencyError, "bijection"):
                    validate_artifact_consistency(
                        manifest_path,
                        expected_intent=intent,
                        expected_receipt_sha256=str(intent["receipt_acceptance_sha256"]),
                    )

    def test_coherent_json_rewrite_cannot_replace_build_anchored_intent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            root = Path(directory)
            manifest_path, intent, metadata, summary, dag, skill = artifact_fixture(root)
            stale_fqn = "stale.taluslabs.stale_tool@9"
            metadata["tool"].update({"fqn": stale_fqn, "module": "stale_tool", "function": "stale_execute", "output_enum": "StaleOutput"})
            summary["id"]["name"] = "stale_tool"
            summary["functions"]["stale_execute"] = summary["functions"].pop("run")
            summary["enums"]["StaleOutput"] = summary["enums"].pop("Result")
            dag["vertices"][0]["kind"]["tool_fqn"] = stale_fqn
            skill["requirements"]["fixed_tools"][0]["tool_fqn"] = {"bytes": list(stale_fqn.encode("ascii"))}
            write_json(root / "package/tool-registration.json", metadata)
            write_json(root / "package/summary.json", summary)
            write_json(root / "package/dag.json", dag)
            write_json(root / "package/skill.tap.json", skill)
            with self.assertRaisesRegex(ArtifactConsistencyError, "(?:schema summary SHA-256|package tree)"):
                validate_artifact_consistency(
                    manifest_path,
                    expected_intent=intent,
                    expected_receipt_sha256=str(intent["receipt_acceptance_sha256"]),
                )

    def test_wrong_explicit_identity_and_schema_anchors_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            manifest_path, intent, *_ = artifact_fixture(Path(directory))
            for field, value, message in (
                ("fqn", "other.taluslabs.other@1", "manifest intent"),
                ("module", "other_module", "caller acceptance contract"),
                ("function", "other_function", "caller acceptance contract"),
                ("schema_digest", "0" * 64, "caller acceptance contract"),
                ("compiled_sha256", "0" * 64, "caller acceptance contract"),
                ("dag_sha256", "0" * 64, "canonical DAG semantics"),
                ("skill_sha256", "0" * 64, "canonical TAP skill semantics"),
            ):
                altered = dict(intent)
                altered[field] = value
                with self.subTest(anchor=field):
                    with self.assertRaisesRegex(ArtifactConsistencyError, message):
                        validate_artifact_consistency(
                            manifest_path,
                            expected_intent=altered,
                            expected_receipt_sha256=str(intent["receipt_acceptance_sha256"]),
                        )

    def test_missing_external_intent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            manifest_path, *_ = artifact_fixture(Path(directory))
            with self.assertRaisesRegex(ArtifactConsistencyError, "explicit caller intent"):
                validate_artifact_consistency(manifest_path)

    def test_external_receipt_digest_is_required_not_persisted_or_manifest_derived(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            manifest_path, intent, *_ = artifact_fixture(Path(directory))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads((Path(directory) / "build-receipt.json").read_text(encoding="utf-8"))
            self.assertNotIn("build_receipt_sha256", manifest["intent"])
            self.assertNotIn("receipt_acceptance_sha256", manifest["intent"])
            self.assertNotIn("receipt_acceptance_sha256", receipt)
            with self.assertRaisesRegex(ArtifactConsistencyError, "external expected build receipt digest"):
                validate_artifact_consistency(manifest_path, expected_intent=intent)
            with self.assertRaisesRegex(ArtifactConsistencyError, "complete build receipt digest"):
                validate_artifact_consistency(
                    manifest_path,
                    expected_intent=intent,
                    expected_receipt_sha256="0" * 64,
                )

    def test_complete_dag_and_skill_semantics_are_caller_anchored(self) -> None:
        mutations = {
            "edge-reroute": ("dag.json", lambda document: document["edges"][0].__setitem__("to", "other_vertex")),
            "edge-removal": ("dag.json", lambda document: document["edges"].pop()),
            "edge-addition": (
                "dag.json",
                lambda document: document["edges"].append({"from": "example_vertex", "to": "other_vertex"}),
            ),
            "output": ("dag.json", lambda document: document["outputs"].pop()),
            "default-value": (
                "dag.json",
                lambda document: document.__setitem__(
                    "default_values", [{"vertex": "example_vertex", "input_port": "amount", "value": {"one": 7}}]
                ),
            ),
            "dag-path": ("skill.tap.json", lambda document: document.__setitem__("dag_path", "other.json")),
            "entry-value": (
                "skill.tap.json",
                lambda document: document["entry_values"].__setitem__("amount", 99),
            ),
            "input-commitment": (
                "skill.tap.json",
                lambda document: document["requirements"].__setitem__("input_commitment", [9]),
            ),
            "payment-policy": (
                "skill.tap.json",
                lambda document: document["requirements"].__setitem__("payment_policy", "Free"),
            ),
            "schedule-policy": (
                "skill.tap.json",
                lambda document: document["requirements"].__setitem__("schedule_policy", "Recurring"),
            ),
            "interface-revision": (
                "skill.tap.json",
                lambda document: document["interface_revision"].__setitem__("inner", 2),
            ),
            "shared-object": (
                "skill.tap.json",
                lambda document: document["requirements"]["shared_objects"].append({"id": "0x2"}),
            ),
            "fixed-tool-id": (
                "skill.tap.json",
                lambda document: document["requirements"]["fixed_tools"][0].__setitem__(
                    "tool_registry_id", {"bytes": "0x9"}
                ),
            ),
            "fixed-tool-fqn": (
                "skill.tap.json",
                lambda document: document["requirements"]["fixed_tools"][0].__setitem__(
                    "tool_fqn", {"bytes": list(b"other.taluslabs.example@9")}
                ),
            ),
        }
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            root = Path(directory)
            manifest_path, intent, metadata, summary, dag, skill = artifact_fixture(root)
            originals = {
                "dag.json": copy.deepcopy(dag),
                "skill.tap.json": copy.deepcopy(skill),
            }
            for label, (filename, mutate) in mutations.items():
                altered = copy.deepcopy(originals[filename])
                mutate(altered)
                self.assertNotEqual(
                    canonical_semantic_digest(altered), intent["dag_sha256" if filename == "dag.json" else "skill_sha256"]
                )
                write_json(root / "package" / filename, altered)
                with self.subTest(mutation=label):
                    with self.assertRaisesRegex(ArtifactConsistencyError, "(?:canonical|package tree)"):
                        validate_artifact_consistency(
                            manifest_path,
                            expected_intent=intent,
                            expected_receipt_sha256=str(intent["receipt_acceptance_sha256"]),
                        )
                write_json(root / "package" / filename, originals[filename])

    def test_tampered_persisted_build_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact-validator-") as directory:
            root = Path(directory)
            manifest_path, intent, *_ = artifact_fixture(root)
            receipt_path = root / "build-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["module_name"] = "tampered"
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(ArtifactConsistencyError, "receipt self-digest"):
                validate_artifact_consistency(
                    manifest_path,
                    expected_intent=intent,
                    expected_receipt_sha256=str(intent["receipt_acceptance_sha256"]),
                )


if __name__ == "__main__":
    unittest.main()
