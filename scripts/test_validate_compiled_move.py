"""Unit tests for compiled Move semantic and provenance validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_compiled_move import (
    CompiledMoveInvariantError,
    ObservedCommandResult,
    _trusted_disassemble,
    _validate_parsed_execute,
    make_build_receipt,
    parse_disassembly_text,
    receipt_acceptance_digest,
    seal_build_receipt,
    sha256_bytes,
    sha256_file,
    validate_build_receipt,
)


INVARIANT = {
    "parameters": [
        {"name": "requirements", "type": "UIDRequirements"},
        {"name": "result", "type": "OnchainToolResult"},
        {"name": "state", "type": "&mut ExampleState"},
        {"name": "ctx", "type": "&mut TxContext"},
    ],
    "state_parameter": "state",
    "state_type": "ExampleState",
    "witness_type": "ExampleWitness",
    "witness_uid_field": "id",
    "requirements_parameter": "requirements",
    "satisfy_call": "proof_of_uid::satisfy",
    "finalize_call": "onchain_tool_result::finalize_and_share",
    "finalize_types": "OnchainToolResult, UIDRequirements, TaggedOutput, &mut TxContext",
    "output_call": "tagged_output::new",
}


APPROVED_INTERFACE_PACKAGE_ADDRESS = "0xd73f9252b5a1056b75f66c1ec9ddfb4b1f92b607d25d9297e7fc747644c7c762"


def compiled_fixture(
    *,
    branch_bypass: bool = False,
    missing_satisfy: bool = False,
    wrong_witness: bool = False,
    finalize_first: bool = False,
) -> str:
    """Return a small Sui disassembly fixture with selectable invalid paths."""

    instructions = [
        "MoveLoc[1](requirements#0#0: UIDRequirements)",
        "StLoc[6](requirements#1#0: UIDRequirements)",
        "MutBorrowLoc[6](requirements#1#0: UIDRequirements)",
    ]
    if branch_bypass:
        state_offset = len(instructions) + 3
        instructions.extend(["LdU64(1)", f"BrFalse({state_offset})", "Abort"])
    witness_field_owner = "ExampleState" if wrong_witness else "ExampleWitness"
    witness_sequence = [
        "MoveLoc[3](state#0#0: &mut ExampleState)",
        "FreezeRef",
        "Call witness(&ExampleState): &ExampleWitness",
        f"ImmBorrowField[0]({witness_field_owner}.id: UID)",
    ]
    satisfy = "Call proof_of_uid::satisfy(&mut UIDRequirements, &UID)"
    output_sequence = [
        "LdConst[0](vector<u8>: \"ok\")",
        "Call tagged_output::new(vector<u8>): TaggedOutput",
        "StLoc[7](output#0#0: TaggedOutput)",
    ]
    finalize_sequence = [
        "MoveLoc[2](result#0#0: OnchainToolResult)",
        "MoveLoc[6](requirements#1#0: UIDRequirements)",
        "MoveLoc[7](output#0#0: TaggedOutput)",
        "MoveLoc[5](ctx#0#0: &mut TxContext)",
        "Call onchain_tool_result::finalize_and_share(OnchainToolResult, UIDRequirements, TaggedOutput, &mut TxContext)",
    ]
    if finalize_first:
        instructions.extend(output_sequence)
        instructions.extend(finalize_sequence)
        instructions.extend(witness_sequence)
        if not missing_satisfy:
            instructions.append(satisfy)
    else:
        instructions.extend(witness_sequence)
        if not missing_satisfy:
            instructions.append(satisfy)
        instructions.extend(output_sequence)
        instructions.extend(finalize_sequence)
    body = "\n".join(f"  {offset}: {instruction}" for offset, instruction in enumerate(instructions))
    return f'''// Move bytecode v7
module 0.example_tool {{
struct ExampleWitness has key {{
  id: UID
}}
struct ExampleState has key {{
  id: UID,
  witness: ExampleWitness
}}

public execute(authorization#0#0: ProvenValue<AgentVertexAuthorization>, requirements#0#0: UIDRequirements, result#0#0: OnchainToolResult, state#0#0: &mut ExampleState, input#0#0: u64, ctx#0#0: &mut TxContext) {{
B0:
{body}
  {len(instructions)}: Ret
}}

witness(self#0#0: &ExampleState): &ExampleWitness {{
B0:
  0: MoveLoc[0](self#0#0: &ExampleState)
  1: Ret
}}
}}
'''


AUTH_INVARIANT = {
    **INVARIANT,
    "authorization_parameter": "authorization",
    "authorization_call": "authorization::consume_verified_for_worksheet_as_recipient",
    "authorization_package_address": APPROVED_INTERFACE_PACKAGE_ADDRESS,
    "authorization_types": "",
    "authorization_proof_call": "proof_of_uid::proof",
    "authorization_commitment_call": "onchain_tool_result::input_commitment",
    "authorization_recipient_owner": "ExampleState",
    "authorization_recipient_field": "id",
}


def authorization_compiled_fixture(
    *,
    ignored_result: bool = False,
    failure_branch: int | None = None,
    overwrite_result: bool = False,
    stale_load: bool = False,
    abort_only: bool = False,
    branch_opcode: str = "BrFalse",
    decoy_uid: bool = False,
    decoy_commitment: bool = False,
    decoy_proof: bool = False,
    decoy_recipient: bool = False,
    authorization_address: str = APPROVED_INTERFACE_PACKAGE_ADDRESS,
    authorization_module: str = "authorization",
    authorization_function: str = "consume_verified_for_worksheet_as_recipient",
) -> str:
    instructions = [
        "ImmBorrowLoc[2](result#0#0: OnchainToolResult)",
        "Call onchain_tool_result::input_commitment(OnchainToolResult): vector<u8>",
        "StLoc[8](input_commitment#0#0: vector<u8>)",
        "MoveLoc[0](authorization#0#0: ProvenValue<AgentVertexAuthorization>)",
        "ImmBorrowLoc[1](requirements#0#0: UIDRequirements)",
        "Call proof_of_uid::proof(&UIDRequirements): &ProofOfUID",
    ]
    if decoy_proof:
        instructions.extend(
            [
                "StLoc[9](expected_proof#0#0: &ProofOfUID)",
                "CopyLoc[3](state#0#0: &mut ExampleState)",
                "ImmBorrowField[1](ExampleState.id: UID)",
                "Call proof_of_uid::new(&UID): ProofOfUID",
                "StLoc[9](decoy_proof#0#0: ProofOfUID)",
                "ImmBorrowLoc[9](decoy_proof#1#0: ProofOfUID)",
            ]
        )
    if decoy_uid or decoy_recipient:
        instructions.extend(
            [
                "CopyLoc[3](state#0#0: &mut ExampleState)",
                "ImmBorrowField[1](ExampleState.id: UID)",
            ]
        )
        if decoy_uid:
            instructions.append("StLoc[10](expected_uid#0#0: &UID)")
            instructions.extend(
                [
                    "CopyLoc[3](state#0#0: &mut ExampleState)",
                    "ImmBorrowField[1](ExampleState.other_id: UID)",
                ]
            )
        else:
            instructions.append("StLoc[10](expected_uid#0#0: &UID)")
            instructions.extend(
                [
                    "CopyLoc[3](state#0#0: &mut ExampleState)",
                    "ImmBorrowField[1](ExampleState.witness_id: UID)",
                ]
            )
    else:
        instructions.extend(
            [
                "CopyLoc[3](state#0#0: &mut ExampleState)",
                "ImmBorrowField[1](ExampleState.id: UID)",
            ]
        )
    if decoy_commitment:
        instructions.append('LdConst[0](vector<u8>: "decoy")')
    else:
        instructions.append("MoveLoc[8](input_commitment#1#0: vector<u8>)")
    instructions.append(
        f"Call authorization::{authorization_function}(ProvenValue<AgentVertexAuthorization>, &ProofOfUID, &UID, vector<u8>): bool"
    )
    if overwrite_result or stale_load:
        instructions.extend(
            [
                "StLoc[8](authorization_ok#0#0: bool)",
                "LdTrue",
                "StLoc[8](authorization_ok#1#0: bool)",
                "MoveLoc[8](authorization_ok#0#0: bool)" if stale_load else "MoveLoc[8](authorization_ok#2#0: bool)",
            ]
        )
    gate_index = len(instructions)
    if ignored_result:
        success_start = gate_index + 2
        instructions.extend(["Pop", f"Branch({success_start})"])
    elif branch_opcode == "BrFalse":
        success_start = gate_index + 4
        failure_start = gate_index + 3
        instructions.extend(
            [
                f"BrFalse({failure_start if failure_branch is None else failure_branch})",
                f"Branch({success_start})",
                "LdU64(0)",
                "Abort",
            ]
        )
    elif branch_opcode == "BrTrue":
        success_start = gate_index + 3
        failure_start = gate_index + 2
        instructions.extend(
            [
                f"BrTrue({success_start if failure_branch is None else failure_branch})",
                "LdU64(0)",
                "Abort",
            ]
        )
    else:
        raise ValueError(f"unsupported branch opcode: {branch_opcode}")
    if abort_only:
        instructions.append("Abort")
    instructions.extend(
        [
            "MutBorrowLoc[1](requirements#1#0: UIDRequirements)",
        "MoveLoc[3](state#0#0: &mut ExampleState)",
        "FreezeRef",
        "Call witness(&ExampleState): &ExampleWitness",
        "ImmBorrowField[0](ExampleWitness.id: UID)",
            "Call proof_of_uid::satisfy(&mut UIDRequirements, &UID)",
            "LdConst[0](vector<u8>: \"ok\")",
            "Call tagged_output::new(vector<u8>): TaggedOutput",
            "StLoc[7](output#0#0: TaggedOutput)",
            "MoveLoc[2](result#1#0: OnchainToolResult)",
            "MoveLoc[1](requirements#2#0: UIDRequirements)",
            "MoveLoc[7](output#1#0: TaggedOutput)",
            "MoveLoc[5](ctx#0#0: &mut TxContext)",
            "Call onchain_tool_result::finalize_and_share(OnchainToolResult, UIDRequirements, TaggedOutput, &mut TxContext)",
            "Ret",
        ]
    )
    body = "\n".join(f"  {offset}: {instruction}" for offset, instruction in enumerate(instructions))
    return f'''// Move bytecode v7
module 0.example_tool {{
use {authorization_address.removeprefix("0x")}::{authorization_module} as authorization;
struct ExampleWitness has key {{
  id: UID
}}
struct ExampleState has key {{
  id: UID,
  witness: ExampleWitness
}}

public execute(authorization#0#0: ProvenValue<AgentVertexAuthorization>, requirements#0#0: UIDRequirements, result#0#0: OnchainToolResult, state#0#0: &mut ExampleState, input_value#0#0: u64, ctx#0#0: &mut TxContext) {{
B0:
{body}
}}

witness(self#0#0: &ExampleState): &ExampleWitness {{
B0:
  0: MoveLoc[0](self#0#0: &ExampleState)
  1: Ret
}}
}}
'''


class CompiledSemanticTests(unittest.TestCase):
    def validate(self, content: str) -> None:
        evidence = _validate_parsed_execute(
            parse_disassembly_text(content),
            module_name="example_tool",
            function_name="execute",
            invariant=INVARIANT,
            expected_module_address="0",
        )
        self.assertEqual(evidence["module"], "example_tool")

    def assert_rejected(self, content: str, message: str) -> None:
        with self.assertRaisesRegex(CompiledMoveInvariantError, message):
            _validate_parsed_execute(
                parse_disassembly_text(content),
                module_name="example_tool",
                function_name="execute",
                invariant=INVARIANT,
                expected_module_address="0",
            )

    def test_valid_execute_reaches_finalization(self) -> None:
        self.validate(compiled_fixture())

    def test_missing_witness_satisfaction_is_rejected(self) -> None:
        self.assert_rejected(compiled_fixture(missing_satisfy=True), "must contain exactly one proof_of_uid::satisfy")

    def test_wrong_witness_uid_field_is_rejected(self) -> None:
        self.assert_rejected(compiled_fixture(wrong_witness=True), "witness UID field is not")

    def test_finalize_before_satisfaction_is_rejected(self) -> None:
        self.assert_rejected(compiled_fixture(finalize_first=True), "must precede finalization")

    def test_every_reachable_branch_must_finalize(self) -> None:
        self.assert_rejected(compiled_fixture(branch_bypass=True), "return path that bypasses")

    def test_verifier_result_must_reach_a_false_abort_gate(self) -> None:
        evidence = _validate_parsed_execute(
            parse_disassembly_text(authorization_compiled_fixture()),
            module_name="example_tool",
            function_name="execute",
            invariant=AUTH_INVARIANT,
            expected_module_address="0",
        )
        self.assertEqual(evidence["authorization_call"], AUTH_INVARIANT["authorization_call"])
        for content, message in (
            (authorization_compiled_fixture(ignored_result=True), "discarded"),
            (authorization_compiled_fixture(failure_branch=14), "does not control an abort"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(CompiledMoveInvariantError, message):
                _validate_parsed_execute(
                    parse_disassembly_text(content),
                    module_name="example_tool",
                    function_name="execute",
                    invariant=AUTH_INVARIANT,
                    expected_module_address="0",
                )

    def test_authorization_callee_requires_exact_public_package_identity(self) -> None:
        evidence = _validate_parsed_execute(
            parse_disassembly_text(authorization_compiled_fixture()),
            module_name="example_tool",
            function_name="execute",
            invariant=AUTH_INVARIANT,
            expected_module_address="0",
        )
        self.assertEqual(
            evidence["authorization_identity"],
            f"{APPROVED_INTERFACE_PACKAGE_ADDRESS}::authorization::consume_verified_for_worksheet_as_recipient",
        )
        decoy_addresses = (
            "0x" + "1" * 64,
            "0x" + "2" * 64,
            "nexus_interface",
        )
        for address in decoy_addresses:
            with self.subTest(address=address), self.assertRaisesRegex(
                CompiledMoveInvariantError, "exact.*authorization.*call"
            ):
                _validate_parsed_execute(
                    parse_disassembly_text(authorization_compiled_fixture(authorization_address=address)),
                    module_name="example_tool",
                    function_name="execute",
                    invariant=AUTH_INVARIANT,
                    expected_module_address="0",
                )
        for options in (
            {"authorization_module": "decoy_authorization"},
            {"authorization_function": "consume_verified_for_worksheet_as_recipient_decoy"},
        ):
            with self.subTest(options=options), self.assertRaisesRegex(
                CompiledMoveInvariantError, "exact.*authorization.*call"
            ):
                _validate_parsed_execute(
                    parse_disassembly_text(authorization_compiled_fixture(**options)),
                    module_name="example_tool",
                    function_name="execute",
                    invariant=AUTH_INVARIANT,
                    expected_module_address="0",
                )
        zero_expected = {**AUTH_INVARIANT, "authorization_package_address": "0x0"}
        with self.assertRaisesRegex(CompiledMoveInvariantError, "non-zero"):
            _validate_parsed_execute(
                parse_disassembly_text(authorization_compiled_fixture()),
                module_name="example_tool",
                function_name="execute",
                invariant=zero_expected,
                expected_module_address="0",
            )

    def test_local_overwrite_clears_verifier_taint(self) -> None:
        for fixture in (
            authorization_compiled_fixture(overwrite_result=True),
            authorization_compiled_fixture(stale_load=True),
        ):
            with self.subTest(fixture=fixture), self.assertRaisesRegex(
                CompiledMoveInvariantError, "does not control an abort"
            ):
                _validate_parsed_execute(
                    parse_disassembly_text(fixture),
                    module_name="example_tool",
                    function_name="execute",
                    invariant=AUTH_INVARIANT,
                    expected_module_address="0",
                )

    def test_true_branch_target_and_false_fallthrough_are_path_sensitive(self) -> None:
        evidence = _validate_parsed_execute(
            parse_disassembly_text(authorization_compiled_fixture(branch_opcode="BrTrue")),
            module_name="example_tool",
            function_name="execute",
            invariant=AUTH_INVARIANT,
            expected_module_address="0",
        )
        self.assertEqual(evidence["authorization_call"], AUTH_INVARIANT["authorization_call"])

    def test_abort_only_authorization_graph_is_rejected(self) -> None:
        with self.assertRaisesRegex(CompiledMoveInvariantError, "no authorized true path"):
            _validate_parsed_execute(
                parse_disassembly_text(authorization_compiled_fixture(abort_only=True)),
                module_name="example_tool",
                function_name="execute",
                invariant=AUTH_INVARIANT,
                expected_module_address="0",
            )

    def test_authorization_call_operand_origins_are_bound(self) -> None:
        decoys = (
            {"decoy_uid": True},
            {"decoy_recipient": True},
            {"decoy_commitment": True},
            {"decoy_proof": True},
        )
        for options in decoys:
            with self.subTest(options=options), self.assertRaisesRegex(
                CompiledMoveInvariantError, "unbound.*origins"
            ):
                _validate_parsed_execute(
                    parse_disassembly_text(authorization_compiled_fixture(**options)),
                    module_name="example_tool",
                    function_name="execute",
                    invariant=AUTH_INVARIANT,
                    expected_module_address="0",
                )


class TrustedCommandRunnerTests(unittest.TestCase):
    def test_trusted_disassembly_uses_observed_runner_for_disassembly_and_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compiled-move-observed-runner-") as directory:
            package_root = Path(directory)
            compiled_module = package_root / "example.mv"
            compiled_module.write_bytes(b"compiled")
            commands: list[list[str]] = []

            def runner(command, _cwd, _environment):
                commands.append(list(command))
                observation = {
                    "observed": True,
                    "mechanism": "strace -f -e trace=network",
                    "isolation": "bwrap --unshare-net",
                    "command": list(command),
                    "network_event_count": 0,
                    "network_events": [],
                }
                if command[-1] == "--version":
                    return ObservedCommandResult(0, b"sui 1.78.0-test\n", b"", observation)
                return ObservedCommandResult(0, compiled_fixture().encode(), b"", observation)

            modules, _bytes, disassemble_command, version = _trusted_disassemble(
                compiled_module,
                package_root,
                command_runner=runner,
            )
            self.assertEqual(len(modules), 1)
            self.assertEqual(disassemble_command[-4:], ["--path", str(package_root), "--build-env", "local"])
            self.assertEqual(version, "sui 1.78.0-test")
            self.assertEqual(commands[1][-1], "--version")

    def test_trusted_disassembly_rejects_unobserved_runner_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compiled-move-unobserved-runner-") as directory:
            package_root = Path(directory)
            compiled_module = package_root / "example.mv"
            compiled_module.write_bytes(b"compiled")

            def runner(command, _cwd, _environment):
                return ObservedCommandResult(
                    0,
                    compiled_fixture().encode(),
                    b"",
                    {
                        "observed": False,
                        "mechanism": "direct",
                        "isolation": "none",
                        "command": list(command),
                        "network_event_count": 0,
                        "network_events": [],
                    },
                )

            with self.assertRaisesRegex(CompiledMoveInvariantError, "unobserved"):
                _trusted_disassemble(compiled_module, package_root, command_runner=runner)


class BuildProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="compiled-move-provenance-")
        root = Path(self.directory.name)
        self.package_root = root / "package"
        self.build_root = self.package_root / "build" / "example"
        self.provenance_root = root / "provenance"
        self.compiled_module = self.build_root / "bytecode_modules" / "example.mv"
        self.disassembly = self.build_root / "disassembly" / "example.mvb"
        self.build_info = self.build_root / "BuildInfo.yaml"
        self.package_file = self.package_root / "Move.toml"
        self.package_unrelated = self.package_root / "tests" / "unrelated.move"
        self.build_unrelated = self.build_root / "bytecode_modules" / "unrelated.mv"
        self.link_target = self.package_root / "link-target.txt"
        self.safe_link = self.package_root / "safe-link.txt"
        self.compiled_module.parent.mkdir(parents=True)
        self.disassembly.parent.mkdir(parents=True)
        self.package_file.write_text("[package]\nname = \"example\"\n", encoding="utf-8")
        self.package_unrelated.parent.mkdir(parents=True)
        self.package_unrelated.write_text("#[test]\nfun unrelated() {}\n", encoding="utf-8")
        self.build_unrelated.write_bytes(b"unrelated compiled artifact")
        self.link_target.write_text("stable target\n", encoding="utf-8")
        self.safe_link.symlink_to(self.link_target.name)
        self.build_info.write_text("compiled_package_info:\n  package_name: example\n", encoding="utf-8")
        self.compiled_module.write_bytes(b"trusted compiled module bytes")
        self.disassembly.write_text(compiled_fixture(), encoding="utf-8")
        self.receipt = make_build_receipt(
            package_root=self.package_root,
            build_root=self.build_root,
            compiled_module=self.compiled_module,
            module_address="0",
            module_name="example_tool",
            function_name="execute",
            trusted_disassembly_sha256=sha256_bytes(self.disassembly.read_bytes()),
            toolchain={"command": ["sui", "move", "disassemble"], "version": "test"},
            disassembly_path=self.disassembly,
            tree_manifest_root=self.provenance_root,
        )
        self.receipt_acceptance_sha256 = receipt_acceptance_digest(self.receipt)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def validate(self, receipt: dict[str, object] | None = None, **kwargs: object) -> dict[str, object]:
        build_root = kwargs.pop("build_root", self.build_root)
        expected_compiled_sha256 = kwargs.pop("expected_compiled_sha256", self.receipt["compiled_sha256"])
        expected_receipt_sha256 = kwargs.pop("expected_receipt_sha256", self.receipt_acceptance_sha256)
        return validate_build_receipt(
            receipt or self.receipt,
            package_root=self.package_root,
            build_root=build_root,
            expected_compiled_sha256=expected_compiled_sha256,
            expected_module_address="0",
            expected_module_name="example_tool",
            expected_function_name="execute",
            expected_receipt_sha256=expected_receipt_sha256,
            **kwargs,
        )

    def test_receipt_binds_actual_compiled_module_bytes(self) -> None:
        self.validate()
        self.compiled_module.write_bytes(b"swapped module bytes")
        with self.assertRaisesRegex(CompiledMoveInvariantError, "compiled module SHA-256"):
            self.validate()

    def test_changed_module_address_requires_original_intent(self) -> None:
        altered = seal_build_receipt(
            {**self.receipt, "module_address": "deadbeef", "module_id": "deadbeef.example_tool"}
        )
        with self.assertRaisesRegex(CompiledMoveInvariantError, "complete build receipt digest"):
            self.validate(altered)

    def test_stale_or_fake_saved_disassembly_is_rejected(self) -> None:
        self.validate()
        self.disassembly.write_text(compiled_fixture().replace("module 0.", "module deadbeef."), encoding="utf-8")
        with self.assertRaisesRegex(CompiledMoveInvariantError, "saved disassembly SHA-256"):
            self.validate()

    def test_mismatched_build_info_and_build_root_are_rejected(self) -> None:
        original = self.build_info.read_bytes()
        self.build_info.write_bytes(original + b"tampered")
        with self.assertRaisesRegex(CompiledMoveInvariantError, "BuildInfo SHA-256"):
            self.validate()
        self.build_info.write_bytes(original)
        with self.assertRaisesRegex(CompiledMoveInvariantError, "build root"):
            self.validate(build_root=self.package_root / "build" / "other")

    def test_wrong_expected_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(CompiledMoveInvariantError, "compiled module SHA-256.*caller acceptance"):
            self.validate(expected_compiled_sha256="0" * 64)

    def test_tampered_receipt_is_rejected(self) -> None:
        altered = dict(self.receipt)
        altered["module_name"] = "tampered"
        with self.assertRaisesRegex(CompiledMoveInvariantError, "receipt self-digest"):
            self.validate(altered)

    def test_external_receipt_digest_is_required_and_must_match(self) -> None:
        with self.assertRaisesRegex(CompiledMoveInvariantError, "external expected build receipt digest"):
            self.validate(expected_receipt_sha256=None)
        with self.assertRaisesRegex(CompiledMoveInvariantError, "complete build receipt digest"):
            self.validate(expected_receipt_sha256="0" * 64)

    def test_self_resealed_toolchain_path_and_version_forgery_is_rejected(self) -> None:
        mutations = {
            "toolchain": {"command": [str(self.package_root.parent / "forged-sui"), "move", "disassemble"], "version": "forged"},
            "package_root": str(self.package_root.parent / "forged-package"),
            "toolchain_version": {"command": ["sui", "move", "disassemble"], "version": "forged"},
        }
        for name, value in mutations.items():
            with self.subTest(mutation=name):
                altered = dict(self.receipt)
                if name == "toolchain_version":
                    altered["toolchain"] = value
                else:
                    altered[name] = value
                altered = seal_build_receipt(altered)
                with self.assertRaisesRegex(CompiledMoveInvariantError, "complete build receipt digest"):
                    self.validate(altered)

    def test_receipt_can_bind_a_schema_digest(self) -> None:
        schema_digest = "a" * 64
        receipt = seal_build_receipt({**self.receipt, "schema_digest": schema_digest})
        evidence = self.validate(
            receipt,
            expected_schema_digest=schema_digest,
            expected_receipt_sha256=receipt_acceptance_digest(receipt),
        )
        self.assertEqual(evidence["schema_digest"], schema_digest)

    def test_saved_receipt_digest_is_canonical(self) -> None:
        self.assertEqual(self.receipt["compiled_sha256"], sha256_file(self.compiled_module))
        self.assertEqual(
            self.receipt["receipt_sha256"],
            seal_build_receipt(self.receipt)["receipt_sha256"],
        )
        self.assertEqual(self.receipt_acceptance_sha256, receipt_acceptance_digest(self.receipt))
        for field in ("package_tree_manifest", "build_tree_manifest"):
            self.assertFalse(Path(str(self.receipt[field])).is_relative_to(self.package_root))
            self.assertFalse(Path(str(self.receipt[field])).is_relative_to(self.build_root))

    def test_stable_boundary_tree_validates_repeatedly(self) -> None:
        first = self.validate()
        second = self.validate()
        self.assertEqual(first["package_tree_sha256"], second["package_tree_sha256"])
        self.assertEqual(first["build_tree_sha256"], second["build_tree_sha256"])

    def test_unrelated_package_and_build_mutations_are_rejected(self) -> None:
        package_original = self.package_unrelated.read_bytes()
        build_original = self.build_unrelated.read_bytes()
        package_added = self.package_root / "new-unrelated.move"
        build_added = self.build_root / "bytecode_modules" / "new-unrelated.mv"
        try:
            self.package_unrelated.write_bytes(package_original + b"changed")
            with self.assertRaisesRegex(CompiledMoveInvariantError, "package tree"):
                self.validate()
            self.package_unrelated.write_bytes(package_original)
            self.package_unrelated.unlink()
            with self.assertRaisesRegex(CompiledMoveInvariantError, "package tree"):
                self.validate()
            self.package_unrelated.write_bytes(package_original)
            package_added.write_bytes(b"added")
            with self.assertRaisesRegex(CompiledMoveInvariantError, "package tree"):
                self.validate()
            package_added.unlink()

            self.build_unrelated.write_bytes(build_original + b"changed")
            with self.assertRaisesRegex(CompiledMoveInvariantError, "(?:package tree|build tree)"):
                self.validate()
            self.build_unrelated.write_bytes(build_original)
            self.build_unrelated.unlink()
            with self.assertRaisesRegex(CompiledMoveInvariantError, "(?:package tree|build tree)"):
                self.validate()
            self.build_unrelated.write_bytes(build_original)
            build_added.write_bytes(b"added")
            with self.assertRaisesRegex(CompiledMoveInvariantError, "build tree"):
                self.validate()
        finally:
            package_added.unlink(missing_ok=True)
            build_added.unlink(missing_ok=True)
            self.package_unrelated.parent.mkdir(parents=True, exist_ok=True)
            self.package_unrelated.write_bytes(package_original)
            self.build_unrelated.parent.mkdir(parents=True, exist_ok=True)
            self.build_unrelated.write_bytes(build_original)

    def test_mode_changes_are_rejected(self) -> None:
        original_package_mode = self.package_file.stat().st_mode & 0o7777
        original_build_mode = self.build_unrelated.stat().st_mode & 0o7777
        try:
            os.chmod(self.package_file, original_package_mode ^ 0o100)
            with self.assertRaisesRegex(CompiledMoveInvariantError, "package tree"):
                self.validate()
            os.chmod(self.package_file, original_package_mode)
            os.chmod(self.build_unrelated, original_build_mode ^ 0o100)
            with self.assertRaisesRegex(CompiledMoveInvariantError, "build tree"):
                self.validate()
        finally:
            os.chmod(self.package_file, original_package_mode)
            os.chmod(self.build_unrelated, original_build_mode)

    def test_symlink_target_swap_and_escape_are_rejected(self) -> None:
        swapped_target = self.package_root / "swapped-target.txt"
        escape_target = self.package_root.parent / "escape-target.txt"
        swapped_target.write_text("swapped\n", encoding="utf-8")
        escape_target.write_text("escape\n", encoding="utf-8")
        try:
            self.safe_link.unlink()
            self.safe_link.symlink_to(swapped_target.name)
            with self.assertRaisesRegex(CompiledMoveInvariantError, "package tree"):
                self.validate()
            self.safe_link.unlink()
            self.safe_link.symlink_to(self.link_target.name)
            self.safe_link.unlink()
            self.safe_link.symlink_to("../escape-target.txt")
            with self.assertRaisesRegex(CompiledMoveInvariantError, "symlink target"):
                self.validate()
        finally:
            self.safe_link.unlink(missing_ok=True)
            self.safe_link.symlink_to(self.link_target.name)
            swapped_target.unlink(missing_ok=True)
            escape_target.unlink(missing_ok=True)

    def test_path_normalization_collisions_and_special_files_are_rejected(self) -> None:
        upper = self.package_root / "Collision"
        lower = self.package_root / "collision"
        fifo = self.package_root / "named-pipe"
        upper.write_text("upper\n", encoding="utf-8")
        lower.write_text("lower\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(CompiledMoveInvariantError, "normalization collision"):
                self.validate()
        finally:
            upper.unlink(missing_ok=True)
            lower.unlink(missing_ok=True)
        try:
            os.mkfifo(fifo)
            with self.assertRaisesRegex(CompiledMoveInvariantError, "special file"):
                self.validate()
        finally:
            fifo.unlink(missing_ok=True)

    def test_tree_manifest_tampering_is_rejected(self) -> None:
        manifest_path = Path(str(self.receipt["package_tree_manifest"]))
        original = manifest_path.read_bytes()
        try:
            document = json.loads(original.decode("utf-8"))
            document["tree_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(CompiledMoveInvariantError, "package manifest"):
                self.validate()
        finally:
            manifest_path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
