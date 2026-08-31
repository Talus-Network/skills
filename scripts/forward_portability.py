#!/usr/bin/env python3
"""Hermetic forward check for generated Rust Tool and Move TAP consumers.

The check intentionally prepares only the three approved public source inputs
through ``prepare_sources.py``. It writes generated consumers below its own
disposable workspace, uses downloaded SDK/Move-package roots plus the pinned Sui
framework package roots as dependencies, and removes the workspace before
returning. It never searches the surrounding workspace.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import re
from typing import Any, Iterator, Mapping

_TAP_SCRIPTS = Path(__file__).resolve().parents[1] / "nexus-tap-development/scripts"
if _TAP_SCRIPTS.is_dir():
    sys.path.insert(0, str(_TAP_SCRIPTS))

from fixture_contract import (  # noqa: E402
    FixtureContractError,
    validate_fixture_contract,
    validate_fixture_rewrite,
)
from validate_artifacts import (
    ArtifactConsistencyError,
    _intent_projection,
    canonical_schema_digest,
    canonical_semantic_digest,
    normalize_abi_type,
    validate_artifact_consistency,
)
from testnet_evidence import SuiTestnetEvidenceClient, TestnetEvidenceError
from validate_compiled_move import (
    CommandRunner,
    CompiledMoveInvariantError,
    ObservedCommandResult,
    capture_build_receipt,
    receipt_acceptance_digest,
    seal_build_receipt,
    sha256_file,
    validate_build_receipt,
    validate_compiled_execute,
)

from prepare_sources import (
    DEFAULT_REPOSITORIES,
    PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES,
    PUBLIC_MOVE_PACKAGE_NAMES,
    SUI_FRAMEWORK_PACKAGE_NAMES,
    SUI_FRAMEWORK_PACKAGE_PATHS,
    SUI_REVISION,
    SUI_TOOLCHAIN_VERSION,
    SourcePreparationError,
    _ignore_source_preparation_signals,
    _source_preparation_signal_guard,
    load_manifest,
    prepared_sources,
)
from tree_integrity import TreeIntegrityError, copy_verified_tree, tree_digest, tree_entries, validate_root
from move_manifest_closure import MoveManifestClosureError, validate_move_manifest_closure

ONCHAIN_TOOL_FQN = "xyz.taluslabs.portable_tool@1"
ONCHAIN_TOOL_MODULE = "portable_tool"
ONCHAIN_TOOL_FUNCTION = "execute"
ONCHAIN_TOOL_OUTPUT_ENUM = "Output"
ONCHAIN_TOOL_INPUT_PORTS = ["state", "input_value", "clock"]
ONCHAIN_TOOL_OUTPUT_PORTS = {"ok": ["result"], "err": ["reason"]}
MOVE_TEST_COUNT_RE = re.compile(r"Test result: OK\. Total tests:\s*(?P<count>\d+);")
SUI_FRAMEWORK_DEPENDENCIES = {
    "std": {"package": "MoveStdlib", "local": "deps/sui-framework/packages/move-stdlib"},
    "sui": {"package": "Sui", "local": "deps/sui-framework/packages/sui-framework"},
}


def _abi_datatype(package: str, module: str, name: str, *arguments: object) -> dict[str, object]:
    return {
        "kind": "datatype",
        "module": {"address": package, "name": module},
        "name": name,
        "type_arguments": list(arguments),
    }


ONCHAIN_TOOL_PARAMETERS = [
    {
        "name": "authorization",
        "type": _abi_datatype(
            "nexus_primitives",
            "authorization",
            "ProvenValue",
            {"phantom": False, "argument": _abi_datatype("nexus_interface", "authorization", "AgentVertexAuthorization")},
        ),
    },
    {"name": "requirements", "type": _abi_datatype("nexus_primitives", "proof_of_uid", "UIDRequirements")},
    {"name": "result", "type": _abi_datatype("nexus_interface", "onchain_tool_result", "OnchainToolResult")},
    {
        "name": "state",
        "type": {
            "kind": "reference",
            "mutable": True,
            "to": _abi_datatype("portable_tool", "portable_tool", "PortableToolState"),
        },
    },
    {"name": "input_value", "type": "u64"},
    {
        "name": "clock",
        "type": {
            "kind": "reference",
            "mutable": False,
            "to": _abi_datatype("sui", "clock", "Clock"),
        },
    },
    {
        "name": "ctx",
        "type": {
            "kind": "reference",
            "mutable": True,
            "to": _abi_datatype("sui", "tx_context", "TxContext"),
        },
    },
]
ONCHAIN_TOOL_OUTPUT_VARIANTS = [
    {"name": "Ok", "fields": [{"name": "result", "type": "u64"}]},
    {"name": "Err", "fields": [{"name": "reason", "type": {"kind": "vector", "element": "u8"}}]},
]
ONCHAIN_TOOL_OUTPUT_PAYLOAD_CONTRACT = {
    "err": {"reason": "vector<u8>"},
    "ok": {"result": "u64"},
}
ONCHAIN_TOOL_SEMANTIC_INVARIANT = {
    "parameters": [
        {
            "name": "authorization",
            "type": "ProvenValue<AgentVertexAuthorization>",
        },
        {"name": "requirements", "type": "UIDRequirements"},
        {"name": "result", "type": "OnchainToolResult"},
        {"name": "state", "type": "&mut PortableToolState"},
        {"name": "ctx", "type": "&mut TxContext"},
    ],
    "state_parameter": "state",
    "state_type": "PortableToolState",
    "witness_type": "PortableToolWitness",
    "witness_uid_field": "id",
    "requirements_parameter": "requirements",
    "satisfy_call": "proof_of_uid::satisfy",
    "finalize_call": "onchain_tool_result::finalize_and_share",
    "finalize_types": "OnchainToolResult, UIDRequirements, TaggedOutput, &mut TxContext",
    "output_call": "tagged_output::new",
    "authorization_parameter": "authorization",
    "authorization_call": "authorization::consume_verified_for_worksheet_as_recipient",
    "authorization_types": "ProvenValue<AgentVertexAuthorization>, &ProofOfUID, &UID, vector<u8>",
    "authorization_proof_call": "proof_of_uid::proof",
    "authorization_commitment_call": "onchain_tool_result::input_commitment",
    "authorization_recipient_owner": "PortableToolState",
    "authorization_recipient_field": "id",
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _rust_tool(project: Path, sdk_root: Path) -> None:
    sdk = sdk_root / "sdk"
    for required in (sdk / "Cargo.toml", sdk / "src/tool_fqn.rs"):
        if not required.is_file():
            raise SourcePreparationError(f"downloaded SDK source is missing {required.name}: {required}")
    if "ToolFqn" not in (sdk / "src/tool_fqn.rs").read_text(encoding="utf-8"):
        raise SourcePreparationError("downloaded public SDK tool FQN source is missing its expected API marker")
    _write(
        project / "rust-proof-manifest.json",
        json.dumps(
            {
                "schema_version": 1,
                "language": "rust",
                "edition": "2021",
                "source": "src/lib.rs",
                "dependencies": [],
                "build": "rustc --edition=2021 --test",
            },
            indent=2,
        )
        + "\n",
    )
    _write(
        project / "src/lib.rs",
        '''#[derive(Debug, PartialEq)]
pub struct Input {
    pub choice: u8,
}

#[derive(Debug, PartialEq)]
pub enum Output {
    Ok { choice: u8 },
    ErrInvalid { reason: String },
}

pub struct PortableTool;

impl PortableTool {
    pub fn fqn() -> &'static str {
        "demo.taluslabs.portable-tool@1"
    }

    pub fn invoke(&self, input: Input) -> Output {
        if input.choice <= 2 {
            Output::Ok { choice: input.choice }
        } else {
            Output::ErrInvalid { reason: "choice must be 0, 1, or 2".to_owned() }
        }
    }

    pub fn healthy(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_tool_covers_legal_and_invalid_choices() {
        let tool = PortableTool;
        assert_eq!(PortableTool::fqn(), "demo.taluslabs.portable-tool@1");
        assert!(tool.healthy());
        let legal = tool.invoke(Input { choice: 2 });
        assert_eq!(legal, Output::Ok { choice: 2 });
        let invalid = tool.invoke(Input { choice: 3 });
        assert!(matches!(invalid, Output::ErrInvalid { .. }));
    }
}
''',
    )


def _assert_closed_rust_proof(project: Path, sdk_root: Path, workspace: Path) -> dict[str, Any]:
    """Prove the generated Rust check has no package or remote-source acquisition."""

    try:
        manifest = json.loads((project / "rust-proof-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(f"generated Rust proof manifest is unavailable or malformed: {project}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("language") != "rust"
        or manifest.get("edition") != "2021"
        or manifest.get("source") != "src/lib.rs"
        or manifest.get("dependencies") != []
        or manifest.get("build") != "rustc --edition=2021 --test"
    ):
        raise SourcePreparationError("generated Rust proof must have no external dependencies")
    forbidden_tokens = ("crates.io", "registry", "git", "move-binding", "github.com", "http://", "https://")
    for path in project.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeDecodeError) as exc:
            raise SourcePreparationError(f"generated Rust proof input cannot be inspected: {path}") from exc
        if any(token in text for token in forbidden_tokens):
            raise SourcePreparationError(f"generated Rust proof contains an unapproved acquisition marker: {path}")
    trace = {
        "mode": "std-only-rustc",
        "allowed_inputs": [
            "repository-owned generated Rust source",
            "public SDK source/API marker: sdk/src/tool_fqn.rs",
            "public Move Packages source for the coupled Move consumers",
            "public Sui framework source for the coupled Move consumers",
        ],
        "workspace": str(workspace.resolve()),
        "sdk_source": str((sdk_root / "sdk/src/tool_fqn.rs").resolve()),
        "observation": {"status": "pending"},
    }
    _write(project / "source-trace.json", json.dumps(trace, indent=2, sort_keys=True) + "\n")
    return trace


def _assert_closed_rust_commands(
    command_results: Sequence[Mapping[str, object]], environment: Mapping[str, str]
) -> None:
    """Reject a forward run that attempts package, VCS, or remote acquisition."""

    forbidden = ("cargo", "crates.io", "registry", "git", "github.com", "http://", "https://")
    for result in command_results:
        command = result.get("command")
        if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
            raise SourcePreparationError("forward command trace is malformed")
        rendered = " ".join(command).casefold()
        if any(marker in rendered for marker in forbidden):
            raise SourcePreparationError(f"forward command attempted an unapproved source acquisition: {rendered}")
    if any(key.casefold() in {"cargo_home", "cargo_target_dir"} for key in environment):
        raise SourcePreparationError("forward environment exposes Cargo acquisition state")
    if environment.get("CARGO_NET_OFFLINE") != "true":
        raise SourcePreparationError("forward environment does not enforce Cargo offline mode")


def _observed_forward_trace(
    workspace: Path, manifest_closure: Mapping[str, object]
) -> dict[str, object]:
    trace_log = workspace / "observations/events.jsonl"
    try:
        records = [
            json.loads(line)
            for line in trace_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(f"forward syscall observation log is missing or malformed: {trace_log}") from exc
    if not records or any(
        not isinstance(record, Mapping)
        or record.get("observed") is not True
        or record.get("mechanism") != "strace -f -e trace=network"
        or record.get("isolation") != "bwrap --unshare-net"
        or not isinstance(record.get("command"), list)
        or any(not isinstance(item, str) or not item for item in record.get("command", []))
        or not isinstance(record.get("network_events"), list)
        or not isinstance(record.get("network_event_count"), int)
        or record.get("network_event_count") != len(record.get("network_events", []))
        for record in records
    ):
        raise SourcePreparationError("forward syscall observation output is incomplete")
    acquisition_events = [
        {"command": list(record["command"]), "syscall": event}
        for record in records
        for event in record["network_events"]
        if isinstance(event, str)
    ]
    external_dependencies = manifest_closure.get("external_dependencies")
    if not isinstance(external_dependencies, list):
        raise SourcePreparationError("forward manifest closure output is incomplete")
    return {
        "mechanism": "strace -f -e trace=network",
        "isolation": "bwrap --unshare-net",
        "protocol_denial": {
            "network_policy": "deny",
            "cargo_offline": True,
            "git_prompt": False,
        },
        "observed_commands": len(records),
        "commands": [list(record["command"]) for record in records],
        "network_access": "none" if not acquisition_events else "attempted",
        "external_dependencies": list(external_dependencies),
        "acquisition_events": acquisition_events,
    }


def _assert_observed_toolchain_commands(observation: Mapping[str, object]) -> None:
    """Require validator-internal Sui version/disassembly commands in the trace."""

    commands = observation.get("commands")
    if not isinstance(commands, list):
        raise SourcePreparationError("forward observation command inventory is missing")
    valid_commands = [
        command for command in commands if isinstance(command, list) and all(isinstance(item, str) for item in command)
    ]
    has_disassembly = any(
        "move" in command
        and "disassemble" in command
        and command.index("move") < command.index("disassemble")
        for command in valid_commands
    )
    has_version = any(command[-1:] == ["--version"] for command in valid_commands)
    if not has_disassembly or not has_version:
        raise SourcePreparationError(
            "forward observation command inventory omits the isolated Sui disassembly or version command"
        )


def _assert_sui_toolchain_version(version: str) -> None:
    """Bind the observed compiler version to the authenticated public source commit."""

    expected = SUI_TOOLCHAIN_VERSION
    if expected != f"sui 1.78.0-{SUI_REVISION[:12]}" or version != expected:
        raise SourcePreparationError(
            f"observed Sui toolchain {version!r} does not match public source commit {SUI_REVISION}"
        )


def _validate_generated_manifest_closure(
    consumers: Path,
    workspace: Path,
    *,
    roots: Sequence[Path],
) -> dict[str, Any]:
    """Validate every reachable generated Move/Rust manifest has only local dependencies."""

    workspace = workspace.resolve()
    consumer_root = consumers.resolve()
    visited: set[Path] = set()
    manifest_paths: set[Path] = set()
    external_dependencies: list[dict[str, str]] = []
    dependency_counts = {"dependencies": 0, "dev-dependencies": 0}

    def contained(path: Path, label: str) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise SourcePreparationError(f"generated manifest dependency escapes its workspace: {label}") from exc
        return resolved

    def record_external(
        manifest_path: Path,
        dependency_name: str,
        detail: str,
        dependency_kind: str = "unknown",
    ) -> None:
        external_dependencies.append(
            {
                "manifest": str(manifest_path),
                "dependency": dependency_name,
                "detail": detail,
                "kind": dependency_kind,
            }
        )
        raise SourcePreparationError(
            f"generated manifest has an unapproved external dependency {dependency_name}: {detail}"
        )

    def visit(manifest_path: Path) -> None:
        manifest_path = contained(manifest_path, str(manifest_path))
        if manifest_path in visited:
            return
        visited.add(manifest_path)
        manifest_paths.add(manifest_path)
        if manifest_path.name == "Move.toml":
            try:
                closure = validate_move_manifest_closure(manifest_path.parent, manifest_path)
            except MoveManifestClosureError as exc:
                raise SourcePreparationError(str(exc)) from exc
            manifest_paths.update(Path(path) for path in closure["manifests"])
            counts = closure["dependency_counts"]
            for dependency_kind in ("dependencies", "dev-dependencies"):
                dependency_counts[dependency_kind] += int(counts[dependency_kind])
            return
        if manifest_path.name == "Cargo.toml":
            try:
                document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise SourcePreparationError(f"generated Cargo manifest cannot be parsed: {manifest_path}") from exc
            for dependency_kind in ("dependencies", "dev-dependencies"):
                dependencies = document.get(dependency_kind, {})
                if dependencies is None:
                    dependencies = {}
                if not isinstance(dependencies, Mapping):
                    raise SourcePreparationError(
                        f"generated Cargo manifest {dependency_kind} are malformed: {manifest_path}"
                    )
                for dependency_name, specification in dependencies.items():
                    dependency_counts[dependency_kind] += 1
                    if not isinstance(specification, Mapping) or "path" not in specification:
                        record_external(
                            manifest_path,
                            str(dependency_name),
                            "registry/Git dependency",
                            dependency_kind,
                        )
                    extra_sources = set(specification) - {
                        "path",
                        "package",
                        "optional",
                        "default-features",
                        "features",
                    }
                    if extra_sources:
                        record_external(
                            manifest_path,
                            str(dependency_name),
                            ", ".join(sorted(extra_sources)),
                            dependency_kind,
                        )
                    target = contained(manifest_path.parent / str(specification["path"]), str(dependency_name))
                    candidate = target / "Cargo.toml"
                    if not candidate.is_file() or candidate.is_symlink():
                        raise SourcePreparationError(
                            f"generated Cargo local dependency has no Cargo.toml: {dependency_name}"
                        )
                    visit(candidate)
            return
        if manifest_path.name == "rust-proof-manifest.json":
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SourcePreparationError(f"generated Rust proof manifest cannot be parsed: {manifest_path}") from exc
            if not isinstance(document, Mapping) or document.get("dependencies") != []:
                record_external(manifest_path, "rust-proof", "non-empty dependency list")
            return
        raise SourcePreparationError(f"generated manifest kind is unsupported: {manifest_path}")

    for root in roots:
        root = Path(root)
        try:
            root.resolve().relative_to(consumer_root)
        except ValueError as exc:
            raise SourcePreparationError(f"generated manifest root escapes the consumer workspace: {root}") from exc
        if root.name == "Move.toml":
            visit(root)
        elif root.name == "rust-proof-manifest.json":
            visit(root)
        elif root.name == "Cargo.toml":
            visit(root)
        else:
            raise SourcePreparationError(f"generated manifest root is not a manifest: {root}")
    return {
        "roots": [str(Path(root).resolve()) for root in roots],
        "manifests": sorted(str(path) for path in manifest_paths),
        "external_dependencies": external_dependencies,
        "dependency_counts": {
            **dependency_counts,
            "runtime": dependency_counts["dependencies"],
            "dev": dependency_counts["dev-dependencies"],
            "total": sum(dependency_counts.values()),
        },
    }


def _public_move_package_roots(move_root: Path) -> dict[str, Path]:
    roots = {name: move_root / "packages" / name for name in PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES}
    for name, root in roots.items():
        for required in (root / "Move.toml", root / "Published.toml"):
            if not required.is_file():
                raise SourcePreparationError(f"downloaded Move package source is missing {name} metadata: {required}")
    return roots


def _copy_public_move_dependencies(project: Path, move_root: Path) -> dict[str, Path]:
    roots = _public_move_package_roots(move_root)
    deps = project / "deps"
    # Keep the direct six-package consumer surface plus the kernel package
    # required transitively by registry/workflow/scheduler manifests.  Kernel
    # is support closure only; generated application manifests never declare it
    # as a direct dependency.
    for name in PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES:
        shutil.copytree(roots[name], deps / name)
    return roots


def _framework_lockfile_filter(_directory: str, names: list[str]) -> set[str]:
    """Exclude source-owned lockfiles so the generated root owns lock provenance."""

    return {name for name in names if name == "Move.lock"}


def _sui_framework_package_roots(sui_root: Path) -> dict[str, Path]:
    """Resolve the two framework packages from the authenticated public Sui tree."""

    sui_root = validate_root(Path(sui_root), "authenticated Sui source root")
    roots: dict[str, Path] = {}
    for name, relative in zip(SUI_FRAMEWORK_PACKAGE_NAMES, SUI_FRAMEWORK_PACKAGE_PATHS):
        package_root = sui_root / relative
        try:
            package_root = validate_root(package_root, f"public Sui framework package {name}")
        except TreeIntegrityError as exc:
            raise SourcePreparationError(f"public Sui framework package is unavailable: {name}") from exc
        manifest = package_root / "Move.toml"
        if not manifest.is_file() or manifest.is_symlink():
            raise SourcePreparationError(f"public Sui framework package has no Move.toml: {name}")
        try:
            document = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SourcePreparationError(f"public Sui framework manifest is malformed: {name}") from exc
        package = document.get("package")
        expected_package_name = "MoveStdlib" if name == "move-stdlib" else "Sui"
        if not isinstance(package, Mapping) or package.get("name") != expected_package_name:
            raise SourcePreparationError(f"public Sui framework package identity is not canonical: {name}")
        roots[name] = package_root
    return roots


def _copy_sui_framework_dependencies(
    project: Path,
    sui_root: Path,
    *,
    replace_template: bool = False,
) -> dict[str, object]:
    """Copy only authenticated Sui framework packages into a generated consumer."""

    source_roots = _sui_framework_package_roots(sui_root)
    destination_root = project / "deps/sui-framework/packages"
    copies: dict[str, object] = {}
    for name, source_root in source_roots.items():
        destination = destination_root / name
        if destination.exists() or destination.is_symlink():
            if not replace_template or destination.is_symlink() or not destination.is_dir():
                raise SourcePreparationError(f"generated Sui framework destination already exists: {destination}")
            try:
                destination.resolve().relative_to(project.resolve())
            except ValueError as exc:
                raise SourcePreparationError(f"generated Sui framework template escapes its project: {destination}") from exc
            shutil.rmtree(destination)
        try:
            shutil.copytree(source_root, destination, ignore=_framework_lockfile_filter)
            validate_root(destination, f"copied Sui framework package {name}")
        except (OSError, TreeIntegrityError) as exc:
            raise SourcePreparationError(f"copying public Sui framework package failed: {name}") from exc
        if any(path.name == "Move.lock" for path in destination.rglob("Move.lock")):
            raise SourcePreparationError(f"copied Sui framework package retained a source lockfile: {name}")
        copies[name] = {
            "source_root": str(source_root.resolve()),
            "source_tree_sha256": tree_digest(tree_entries(source_root)),
            "copied_root": str(destination.resolve()),
            "copied_tree_sha256": tree_digest(tree_entries(destination)),
            "excluded_files": ["Move.lock"],
        }
    return {"packages": copies, "dependencies": json.loads(json.dumps(SUI_FRAMEWORK_DEPENDENCIES, sort_keys=True))}


def _bind_local_framework_to_public_dependencies(project: Path) -> None:
    """Make copied public Move packages use the workspace-local Sui framework."""

    framework_root = (project / "deps/sui-framework/packages").resolve()
    if not framework_root.is_dir():
        raise SourcePreparationError(f"generated public dependency framework root is missing: {project}")
    for name in PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES:
        manifest_path = project / "deps" / name / "Move.toml"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise SourcePreparationError(f"generated public dependency manifest is missing: {name}")
        try:
            original = manifest_path.read_text(encoding="utf-8")
            document = tomllib.loads(original)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SourcePreparationError(f"generated public dependency manifest is malformed: {name}") from exc
        package = document.get("package")
        if not isinstance(package, Mapping):
            raise SourcePreparationError(f"generated public dependency package section is malformed: {name}")
        dependencies = document.get("dependencies", {})
        if dependencies is None:
            dependencies = {}
        if not isinstance(dependencies, Mapping):
            raise SourcePreparationError(f"generated public dependency table is malformed: {name}")
        package_root = manifest_path.parent.resolve()
        std_path = os.path.relpath(framework_root / "move-stdlib", package_root)
        sui_path = os.path.relpath(framework_root / "sui-framework", package_root)
        expected = {
            "std": {"local": std_path, "package": "MoveStdlib"},
            "sui": {"local": sui_path, "package": "Sui"},
        }
        for alias, specification in expected.items():
            existing = dependencies.get(alias)
            if existing is not None and (not isinstance(existing, Mapping) or dict(existing) != specification):
                raise SourcePreparationError(f"public dependency {name} has a conflicting framework alias: {alias}")
        if all(
            isinstance(dependencies.get(alias), Mapping) and dict(dependencies[alias]) == specification
            for alias, specification in expected.items()
        ) and package.get("implicit-dependencies") is False:
            continue
        updated = original
        package_match = re.search(r"(?m)^\[package\]\n", updated)
        if package_match is None:
            raise SourcePreparationError(f"generated public dependency has no package table: {name}")
        if not re.search(r"(?m)^implicit-dependencies\s*=", updated[package_match.end() : updated.find("\n[", package_match.end()) if "\n[" in updated[package_match.end() :] else len(updated)]):
            edition_match = re.search(r"(?m)^edition\s*=.*$", updated[package_match.end() :])
            insertion = package_match.end() + (edition_match.end() if edition_match else 0)
            updated = updated[:insertion] + "\nimplicit-dependencies = false" + updated[insertion:]
        missing_dependencies = {
            alias: specification for alias, specification in expected.items() if alias not in dependencies
        }
        if missing_dependencies:
            dependency_lines = "\n".join(
                f'{alias} = {{ local = "{specification["local"]}", package = "{specification["package"]}" }}'
                for alias, specification in missing_dependencies.items()
            )
            dependency_match = re.search(r"(?m)^\[dependencies\]\n", updated)
            if dependency_match is None:
                next_table = re.search(r"(?m)^\[[^\n]+\]\n", updated[package_match.end() :])
                insertion = package_match.end() + (next_table.start() if next_table else len(updated[package_match.end() :]))
                prefix = "" if insertion == 0 or updated[:insertion].endswith("\n\n") else "\n"
                updated = updated[:insertion] + prefix + "[dependencies]\n" + dependency_lines + "\n" + updated[insertion:]
            else:
                after_dependencies = dependency_match.end()
                next_table = re.search(r"(?m)^\[[^\n]+\]\n", updated[after_dependencies:])
                insertion = after_dependencies + (next_table.start() if next_table else len(updated[after_dependencies:]))
                prefix = "" if updated[:insertion].endswith("\n") else "\n"
                updated = updated[:insertion] + prefix + dependency_lines + "\n" + updated[insertion:]
        try:
            parsed = tomllib.loads(updated)
        except tomllib.TOMLDecodeError as exc:
            raise SourcePreparationError(f"generated public dependency manifest rewrite is malformed: {name}") from exc
        parsed_dependencies = parsed.get("dependencies")
        if parsed.get("package", {}).get("implicit-dependencies") is not False or not isinstance(parsed_dependencies, Mapping):
            raise SourcePreparationError(f"generated public dependency manifest rewrite failed: {name}")
        if any(
            not isinstance(parsed_dependencies.get(alias), Mapping)
            or dict(parsed_dependencies[alias]) != specification
            for alias, specification in expected.items()
        ):
            raise SourcePreparationError(f"generated public dependency framework aliases are not local: {name}")
        manifest_path.write_text(updated, encoding="utf-8")


def _assert_framework_dependencies(project: Path, *, required: bool) -> None:
    """Require local lowercase aliases for the copied Sui framework packages."""

    try:
        document = tomllib.loads((project / "Move.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SourcePreparationError(f"generated Move.toml cannot be parsed: {project}") from exc
    package = document.get("package")
    dependencies = document.get("dependencies", {})
    if not isinstance(package, Mapping) or not isinstance(dependencies, Mapping):
        raise SourcePreparationError(f"generated Move.toml package/dependencies are malformed: {project}")
    present = {name for name in SUI_FRAMEWORK_DEPENDENCIES if name in dependencies}
    if required and present != set(SUI_FRAMEWORK_DEPENDENCIES):
        raise SourcePreparationError(f"generated Move.toml is missing local Sui framework dependencies: {project}")
    if not required and present:
        required = True
    if not required:
        return
    if package.get("implicit-dependencies") is not False:
        raise SourcePreparationError(f"generated Move.toml must disable implicit Sui dependencies: {project}")
    for alias, expected in SUI_FRAMEWORK_DEPENDENCIES.items():
        specification = dependencies.get(alias)
        if not isinstance(specification, Mapping) or dict(specification) != expected:
            raise SourcePreparationError(f"generated Move.toml Sui dependency {alias} is not local and explicit")
        resolved = (project / str(expected["local"])).resolve()
        if not resolved.is_relative_to((project / "deps/sui-framework").resolve()):
            raise SourcePreparationError(f"generated Move.toml Sui dependency escapes its local framework copy: {alias}")
        if not (resolved / "Move.toml").is_file() or (resolved / "Move.toml").is_symlink():
            raise SourcePreparationError(f"generated Move.toml Sui dependency has no local package: {alias}")


def _canonical_public_address(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value) is None:
        raise SourcePreparationError(f"{label} is not a canonical public Sui address")
    digits = value[2:].lower().zfill(64)
    address = f"0x{digits}"
    if address == "0x" + "0" * 64:
        raise SourcePreparationError(f"{label} must not be the zero address")
    return address


def _approved_public_interface_identity(move_root: Path) -> dict[str, str]:
    """Derive the interface callee identity from the authenticated public package."""

    package_root = move_root / "packages/interface"
    manifest_path = package_root / "Move.toml"
    published_path = package_root / "Published.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        published = tomllib.loads(published_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SourcePreparationError("public interface manifest/published mapping is unavailable") from exc
    package = manifest.get("package")
    addresses = manifest.get("addresses")
    if not isinstance(package, Mapping) or package.get("name") != "nexus_interface":
        raise SourcePreparationError("public interface package manifest identity is not canonical")
    if not isinstance(addresses, Mapping) or addresses.get("nexus_interface") not in {"0x0", "0x" + "0" * 64}:
        raise SourcePreparationError("public interface package manifest has no named address")
    published_environments = published.get("published")
    testnet = published_environments.get("testnet") if isinstance(published_environments, Mapping) else None
    if not isinstance(testnet, Mapping):
        raise SourcePreparationError("public interface package has no testnet published mapping")
    chain_id = testnet.get("chain-id")
    if not isinstance(chain_id, str) or not chain_id.strip():
        raise SourcePreparationError("public interface testnet mapping has no chain identifier")
    original_id = _canonical_public_address(testnet.get("original-id"), "public interface original-id")
    published_at = _canonical_public_address(testnet.get("published-at"), "public interface published-at")
    if original_id != published_at:
        raise SourcePreparationError("public interface testnet published mapping has divergent package IDs")
    module = "authorization"
    function = "consume_verified_for_worksheet_as_recipient"
    return {
        "package_address": original_id,
        "module": module,
        "function": function,
        "call": f"{module}::{function}",
        "module_id": f"{original_id}::{module}",
        "published_chain_id": chain_id,
    }


def _bind_public_interface_address(project: Path, address: str) -> None:
    """Bind the copied public interface package to its reviewed published ID."""

    manifest_path = project / "deps/interface/Move.toml"
    try:
        source = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourcePreparationError("copied public interface manifest is unavailable") from exc
    pattern = re.compile(r"(?m)^(nexus_interface\s*=\s*\")[^\"]*(\"\s*)$")
    updated, replacements = pattern.subn(rf"\g<1>{address}\g<2>", source, count=1)
    if replacements != 1:
        raise SourcePreparationError("copied public interface manifest has no unique named address")
    try:
        document = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise SourcePreparationError("copied public interface address rewrite is malformed") from exc
    addresses = document.get("addresses")
    if not isinstance(addresses, Mapping) or addresses.get("nexus_interface") != address:
        raise SourcePreparationError("copied public interface address rewrite was not retained")
    manifest_path.write_text(updated, encoding="utf-8")


def _assert_public_move_manifest(
    project: Path,
    *,
    require_interface_dependencies: bool = True,
    require_framework_dependencies: bool = False,
    expected_interface_address: str | None = None,
) -> None:
    try:
        document = tomllib.loads((project / "Move.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SourcePreparationError(f"generated Move.toml cannot be parsed: {project}") from exc
    dependencies = document.get("dependencies")
    if require_interface_dependencies:
        if not isinstance(dependencies, dict):
            raise SourcePreparationError(f"generated Move.toml has no public interface dependencies: {project}")
        expected = {"nexus_primitives": "deps/primitives", "nexus_interface": "deps/interface"}
        for package, local_path in expected.items():
            entry = dependencies.get(package)
            if not isinstance(entry, dict) or entry.get("local") != local_path:
                raise SourcePreparationError(
                    f"generated Move.toml dependency {package} must originate from the public nexus-move-packages root"
                )
            resolved = (project / local_path).resolve()
            if not resolved.is_relative_to((project / "deps").resolve()) or not (resolved / "Move.toml").is_file():
                raise SourcePreparationError(f"generated Move.toml dependency escapes its disposable public package copy: {package}")
        if expected_interface_address is not None:
            interface_manifest = project / "deps/interface/Move.toml"
            try:
                interface_document = tomllib.loads(interface_manifest.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise SourcePreparationError("generated public interface manifest is malformed") from exc
            addresses = interface_document.get("addresses")
            if not isinstance(addresses, Mapping) or addresses.get("nexus_interface") != expected_interface_address:
                raise SourcePreparationError("generated public interface address is not the approved published identity")
    elif dependencies not in (None, {}):
        allowed = set(SUI_FRAMEWORK_DEPENDENCIES)
        if set(dependencies) - allowed:
            raise SourcePreparationError(f"logic-only TAP Move.toml must not load native public interface dependencies: {project}")
    _assert_framework_dependencies(project, required=require_framework_dependencies)
    for name in PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES:
        package_root = project / "deps" / name
        if not (package_root / "Move.toml").is_file() or not (package_root / "Published.toml").is_file():
            raise SourcePreparationError(
                f"generated Move project is missing public nexus-move-packages closure package: {name}"
            )


def _assert_public_interface_lock_binding(project: Path) -> None:
    """Ensure the generated lock binds the interface package to its copied public source."""

    lock_path = project / "Move.lock"
    try:
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SourcePreparationError("generated public interface Move.lock is unavailable or malformed") from exc
    pinned = lock.get("pinned")
    local = pinned.get("local") if isinstance(pinned, Mapping) else None
    interface = local.get("nexus_interface") if isinstance(local, Mapping) else None
    source = interface.get("source") if isinstance(interface, Mapping) else None
    if (
        not isinstance(interface, Mapping)
        or not isinstance(interface.get("manifest_digest"), str)
        or not interface["manifest_digest"]
        or not isinstance(source, Mapping)
        or set(source) != {"local"}
        or source.get("local") != "deps/interface"
    ):
        raise SourcePreparationError("generated public interface Move.lock is not bound to deps/interface")


def _assert_onchain_tool_source(project: Path) -> None:
    """Check the source-level obligations the live SDK schema cannot fetch offline."""

    source = project / "sources/tool.move"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourcePreparationError(f"generated on-chain Tool source is unavailable: {source}") from exc
    execute_start = text.find("public fun execute(")
    if execute_start < 0:
        raise SourcePreparationError("generated on-chain Tool is missing required ABI/finalization behavior: public fun execute(")
    execute_end = text.find(") {", execute_start)
    if execute_end < 0:
        raise SourcePreparationError("generated on-chain Tool execute declaration is incomplete")
    execute_header = text[execute_start:execute_end]
    execute_body = text[execute_end:]
    header_fragments = (
        "authorization: ProvenValue<AgentVertexAuthorization>",
        "requirements: UIDRequirements",
        "result: OnchainToolResult",
        "ctx: &mut TxContext",
    )
    body_fragments = (
        "onchain_tool_result::input_commitment(&result)",
        "interface_authorization::consume_verified_for_worksheet_as_recipient(",
        "requirements.proof()",
        "&state.id",
        "requirements.satisfy(",
        "tool_logic::classify_input(",
        "tagged_output::new(",
        "onchain_tool_result::finalize_and_share(result, requirements, output, ctx);",
    )
    missing = [fragment for fragment in header_fragments if fragment not in execute_header]
    missing.extend(fragment for fragment in body_fragments if fragment not in execute_body)
    if missing:
        raise SourcePreparationError(
            "generated on-chain Tool is missing required ABI/finalization behavior: " + ", ".join(missing)
        )
    if execute_body.count("tagged_output::new(") < 3:
        raise SourcePreparationError("generated on-chain Tool must construct TaggedOutput on every output branch")
    if "assert!(" not in execute_body:
        raise SourcePreparationError("generated workflow authorization result must be checked before mutation")
    output_matches = list(
        re.finditer(
            r'tagged_output::new\(b"(?P<tag>[^"]+)"\)(?P<chain>.*?)(?=\n\s*tagged_output::new|\n\s*onchain_tool_result::finalize_and_share|\Z)',
            execute_body,
            re.DOTALL,
        )
    )
    if not output_matches:
        raise SourcePreparationError("generated on-chain Tool has no inspectable TaggedOutput branches")
    seen_tags: dict[str, int] = {}
    for output_match in output_matches:
        tag = output_match.group("tag")
        expected_fields = ONCHAIN_TOOL_OUTPUT_PAYLOAD_CONTRACT.get(tag)
        if expected_fields is None:
            raise SourcePreparationError(f"generated on-chain Tool emits an undeclared output tag: {tag}")
        seen_tags[tag] = seen_tags.get(tag, 0) + 1
        payload_matches = list(
            re.finditer(
                r'\.with_named_payload\(b"(?P<field>[^"]+)",\s*data::inline_data_value\((?P<expr>b"(?:[^"\\]|\\.)*"|(?:computed|input_value)\.to_string\(\)\.into_bytes\(\))\)',
                output_match.group("chain"),
                re.DOTALL,
            )
        )
        if len(payload_matches) != len(expected_fields):
            raise SourcePreparationError(f"output tag {tag!r} does not provide exactly its declared payload fields")
        actual_fields = {match.group("field") for match in payload_matches}
        if actual_fields != set(expected_fields):
            raise SourcePreparationError(f"output tag {tag!r} payload fields do not match its declared schema")
        for payload_match in payload_matches:
            field = payload_match.group("field")
            expression = payload_match.group("expr").strip()
            move_type = expected_fields[field]
            if move_type == "u64":
                if expression.startswith('b"'):
                    try:
                        value = json.loads(json.loads(expression[1:]))
                    except (json.JSONDecodeError, TypeError):
                        value = None
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise SourcePreparationError(f"output {tag}.{field} must contain a JSON number")
                elif not re.fullmatch(r"(?:computed|input_value)\.to_string\(\)\.into_bytes\(\)", expression):
                    raise SourcePreparationError(f"output {tag}.{field} must encode a numeric JSON value")
            elif move_type == "vector<u8>":
                if not expression.startswith('b"'):
                    raise SourcePreparationError(f"output {tag}.{field} must encode a JSON byte array")
                try:
                    value = json.loads(json.loads(expression[1:]))
                except (json.JSONDecodeError, TypeError) as exc:
                    raise SourcePreparationError(f"output {tag}.{field} is not valid JSON") from exc
                if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255 for item in value):
                    raise SourcePreparationError(f"output {tag}.{field} must encode a JSON byte array")
    if "abort " in execute_body:
        raise SourcePreparationError("generated on-chain Tool execute must not use an aborting stub")
    if set(seen_tags) != set(ONCHAIN_TOOL_OUTPUT_PAYLOAD_CONTRACT) or seen_tags.get("ok", 0) < 2:
        raise SourcePreparationError("generated on-chain Tool does not cover every declared output branch")


def _assert_dag_document(path: Path, *, expected_fqn: str | None = None) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(f"generated DAG is unavailable or malformed: {path}") from exc
    vertices = document.get("vertices") if isinstance(document, dict) else None
    if not isinstance(vertices, list) or len(vertices) != 1:
        raise SourcePreparationError("generated DAG must contain exactly one entry vertex")
    vertex = vertices[0]
    if not isinstance(vertex, dict):
        raise SourcePreparationError("generated DAG vertex is malformed")
    kind = vertex.get("kind")
    if not isinstance(kind, dict) or kind.get("variant") != "on_chain":
        raise SourcePreparationError("generated DAG must use an on_chain entry vertex")
    fqn = kind.get("tool_fqn")
    if not isinstance(fqn, str) or (expected_fqn is not None and fqn != expected_fqn):
        raise SourcePreparationError("generated DAG Tool FQN is not aligned with the generated Tool")
    ports = vertex.get("entry_ports")
    if not isinstance(ports, list) or not ports or any(
        not isinstance(port, dict) or not isinstance(port.get("name"), str) or not port["name"].strip()
        for port in ports
    ):
        raise SourcePreparationError("generated DAG entry ports are malformed")
    output_ports = vertex.get("output_ports")
    if (
        not isinstance(output_ports, dict)
        or not output_ports
        or any(
            not isinstance(variant, str)
            or not isinstance(variant_ports, list)
            or not variant_ports
            or any(
                not isinstance(port, dict)
                or set(port) != {"name"}
                or not isinstance(port.get("name"), str)
                or not port["name"].strip()
                for port in variant_ports
            )
            for variant, variant_ports in output_ports.items()
        )
    ):
        raise SourcePreparationError("generated DAG output ports are malformed")


def _assert_skill_document(path: Path, *, expected_name: str | None = None) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(f"generated skill config is unavailable or malformed: {path}") from exc
    if not isinstance(document, dict) or (expected_name is not None and document.get("name") != expected_name):
        raise SourcePreparationError("generated skill config has the wrong skill name")
    if document.get("dag_path") != "portable.dag.json":
        raise SourcePreparationError("generated skill config must point to portable.dag.json")
    requirements = document.get("requirements")
    if not isinstance(requirements, dict) or not requirements.get("input_commitment"):
        raise SourcePreparationError("generated skill config has no input commitment")


def _generate_dag_document() -> dict[str, object]:
    """Return the caller-authored DAG contract before any artifact is written."""

    return {
        "vertices": [
            {
                "kind": {"variant": "on_chain", "tool_fqn": ONCHAIN_TOOL_FQN},
                "name": "entry",
                "entry_ports": [{"name": name} for name in ONCHAIN_TOOL_INPUT_PORTS],
                "output_ports": {
                    variant: [{"name": port} for port in ports]
                    for variant, ports in ONCHAIN_TOOL_OUTPUT_PORTS.items()
                },
            }
        ],
        "edges": [],
        "outputs": [
            {"vertex": "entry", "output_variant": variant, "output_port": port}
            for variant, ports in ONCHAIN_TOOL_OUTPUT_PORTS.items()
            for port in ports
        ],
        "shared_objects": [],
    }


def _generate_skill_document() -> dict[str, object]:
    """Return the caller-authored TAP skill contract before any artifact is written."""

    return {
        "name": "portable-skill",
        "dag_path": "portable.dag.json",
        "requirements": {
            "input_commitment": [1],
            "payment_policy": "UserFunded",
            "schedule_policy": "Once",
            "fixed_tools": [
                {
                    "tool_registry_id": {"bytes": "0x0"},
                    "tool_fqn": {"bytes": list(ONCHAIN_TOOL_FQN.encode("ascii"))},
                },
            ],
            "shared_objects": [],
        },
        "interface_revision": {"inner": 1},
    }


def _semantic_intent(dag: Mapping[str, object], skill: Mapping[str, object]) -> dict[str, object]:
    """Capture complete caller intent without reading generated artifacts."""

    return {
        "dag_sha256": canonical_semantic_digest(dag),
        "skill_sha256": canonical_semantic_digest(skill),
        "dag_projection": json.loads(json.dumps(dag, sort_keys=True)),
        "skill_projection": json.loads(json.dumps(skill, sort_keys=True)),
    }


def _write_generated_semantic_artifacts(
    project: Path,
    dag: Mapping[str, object],
    skill: Mapping[str, object],
) -> None:
    dag_text = json.dumps(dict(dag), indent=2) + "\n"
    skill_text = json.dumps(dict(skill), indent=2) + "\n"
    _write(project / "portable.dag.json", dag_text)
    _write(project / "portable.skill.tap.json", skill_text)
    artifact_root = project / "artifacts"
    _write(artifact_root / "portable.dag.json", dag_text)
    _write(artifact_root / "portable.skill.tap.json", skill_text)


def _validate_generated_semantic_artifacts(
    project: Path,
    intent: Mapping[str, object],
) -> None:
    """Validate actual generated files against pre-existing caller intent."""

    try:
        dag = json.loads((project / "portable.dag.json").read_text(encoding="utf-8"))
        skill = json.loads((project / "portable.skill.tap.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourcePreparationError("generated DAG/TAP artifacts cannot be validated against caller intent") from exc
    if not isinstance(dag, dict) or not isinstance(skill, dict):
        raise SourcePreparationError("generated DAG/TAP artifacts must be JSON objects")
    if canonical_semantic_digest(dag) != intent["dag_sha256"]:
        raise SourcePreparationError("generated DAG semantics do not match pre-generation caller intent")
    if canonical_semantic_digest(skill) != intent["skill_sha256"]:
        raise SourcePreparationError("generated TAP skill semantics do not match pre-generation caller intent")


def _move_manifest_text(name: str, *, framework: bool, public_interfaces: bool = False) -> str:
    """Render a modern local-only Move manifest for a generated consumer."""

    lines = [
        "[package]",
        f'name = "{name}"',
        'version = "0.1.0"',
        'edition = "2024"',
    ]
    if framework:
        lines.append("implicit-dependencies = false")
    if public_interfaces or framework:
        lines.extend(["", "[dependencies]"])
    if public_interfaces:
        lines.extend(
            [
                'nexus_primitives = { local = "deps/primitives" }',
                'nexus_interface = { local = "deps/interface" }',
            ]
        )
    if framework:
        lines.extend(
            [
                'std = { local = "deps/sui-framework/packages/move-stdlib", package = "MoveStdlib" }',
                'sui = { local = "deps/sui-framework/packages/sui-framework", package = "Sui" }',
            ]
        )
    lines.extend(["", "[environments]", 'local = "00000000"', ""])
    return "\n".join(lines)


def _prepare_repository_fixture(project: Path, sui_root: Path) -> dict[str, object]:
    """Rewrite a checked-in public Sui template to authenticated framework packages."""

    manifest_path = project / "Move.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SourcePreparationError(f"repository-owned TAP fixture manifest is malformed: {project}") from exc
    package = manifest.get("package")
    if not isinstance(package, Mapping) or not isinstance(package.get("name"), str) or not package["name"]:
        raise SourcePreparationError(f"repository-owned TAP fixture package identity is missing: {project}")
    try:
        fixture_contract = validate_fixture_contract(project)
    except FixtureContractError as exc:
        raise SourcePreparationError(f"repository-owned TAP fixture contract is invalid: {project}") from exc
    framework_evidence = _copy_sui_framework_dependencies(project, sui_root, replace_template=True)
    _write(manifest_path, _move_manifest_text(package["name"], framework=True))
    _assert_framework_dependencies(project, required=True)
    try:
        rewrite_evidence = validate_fixture_rewrite(project, fixture_contract)
    except FixtureContractError as exc:
        raise SourcePreparationError(f"repository-owned TAP fixture rewrite is invalid: {project}") from exc
    framework_evidence["fixture_contract"] = fixture_contract
    framework_evidence["rewrite"] = rewrite_evidence
    return framework_evidence


def _move_tap(
    project: Path,
    move_root: Path,
    *,
    dag_document: Mapping[str, object],
    skill_document: Mapping[str, object],
    sui_root: Path | None = None,
) -> dict[str, object] | None:
    tap_package = project / "tap"
    _copy_public_move_dependencies(tap_package, move_root)
    framework_evidence = _copy_sui_framework_dependencies(tap_package, sui_root) if sui_root is not None else None
    if framework_evidence is not None:
        _bind_local_framework_to_public_dependencies(tap_package)
    _write(
        tap_package / "Move.toml",
        _move_manifest_text("portable_tap", framework=framework_evidence is not None),
    )
    _assert_public_move_manifest(
        tap_package,
        require_interface_dependencies=False,
        require_framework_dependencies=framework_evidence is not None,
    )
    _write(
        tap_package / "sources/game.move",
        '''module portable_tap::game;

public struct GameState has key {
    id: object::UID,
    rounds: u64,
    last_offchain: u8,
    last_onchain: u8,
    last_winner: u8,
}

fun init(ctx: &mut tx_context::TxContext) {
    transfer::share_object(GameState {
        id: object::new(ctx),
        rounds: 0,
        last_offchain: 0,
        last_onchain: 0,
        last_winner: 0,
    });
}

public fun resolve(offchain: u8, onchain: u8): u8 {
    assert!(offchain <= 2, 0);
    assert!(onchain <= 2, 1);
    if (offchain == onchain) {
        0
    } else if ((offchain + 3 - onchain) % 3 == 1) {
        1
    } else {
        2
    }
}

public fun execute(state: &mut GameState, offchain: u8, onchain: u8) {
    state.rounds = state.rounds + 1;
    state.last_offchain = offchain;
    state.last_onchain = onchain;
    state.last_winner = resolve(offchain, onchain);
}
''',
    )
    _write(
        tap_package / "tests/game.test.move",
        '''#[test_only]
module portable_tap::game_tests;

use portable_tap::game;

#[test]
fun resolver_covers_all_pairs() {
    assert!(game::resolve(0, 0) == 0, 0);
    assert!(game::resolve(0, 1) == 2, 1);
    assert!(game::resolve(0, 2) == 1, 2);
    assert!(game::resolve(1, 0) == 1, 3);
    assert!(game::resolve(1, 1) == 0, 4);
    assert!(game::resolve(1, 2) == 2, 5);
    assert!(game::resolve(2, 0) == 2, 6);
    assert!(game::resolve(2, 1) == 1, 7);
    assert!(game::resolve(2, 2) == 0, 8);
}
''',
    )
    _write_generated_semantic_artifacts(project, dag_document, skill_document)
    _assert_dag_document(project / "portable.dag.json", expected_fqn=ONCHAIN_TOOL_FQN)
    _assert_skill_document(project / "portable.skill.tap.json", expected_name="portable-skill")
    return framework_evidence


def _move_tap_logic_test(
    project: Path,
    source_project: Path,
    sui_root: Path | None = None,
) -> dict[str, object] | None:
    """Run pure TAP logic without loading native interface declarations in the VM."""

    framework_evidence = _copy_sui_framework_dependencies(project, sui_root) if sui_root is not None else None
    _write(
        project / "Move.toml",
        _move_manifest_text("portable_tap", framework=framework_evidence is not None),
    )
    shutil.copytree(source_project / "tap/sources", project / "sources")
    shutil.copytree(source_project / "tap/tests", project / "tests")
    return framework_evidence


def _onchain_tool(
    project: Path,
    move_root: Path,
    sui_root: Path | None = None,
    interface_address: str | None = None,
) -> dict[str, object] | None:
    """Generate a satisfiable public-ABI Move Tool against downloaded interfaces."""

    _copy_public_move_dependencies(project, move_root)
    framework_evidence = _copy_sui_framework_dependencies(project, sui_root) if sui_root is not None else None
    if framework_evidence is not None:
        _bind_local_framework_to_public_dependencies(project)
    if interface_address is not None:
        _bind_public_interface_address(project, interface_address)
    _write(
        project / "Move.toml",
        _move_manifest_text(
            "portable_tool",
            framework=framework_evidence is not None,
            public_interfaces=True,
        ),
    )
    _assert_public_move_manifest(
        project,
        require_framework_dependencies=framework_evidence is not None,
        expected_interface_address=interface_address,
    )
    _write(
        project / "sources/tool_logic.move",
        '''module portable_tool::tool_logic;

public fun classify_input(input_value: u64): u8 {
    if (input_value == 0) {
        0
    } else if (input_value > 1000) {
        1
    } else {
        2
    }
}
''',
    )
    _write(
        project / "sources/tool.move",
        '''module portable_tool::portable_tool;

use nexus_interface::authorization::{Self as interface_authorization, AgentVertexAuthorization};
use nexus_interface::onchain_tool_result::{Self as onchain_tool_result, OnchainToolResult};
use nexus_primitives::authorization::ProvenValue;
use nexus_primitives::data;
use nexus_primitives::proof_of_uid::UIDRequirements;
use nexus_primitives::tagged_output;
use sui::bag::{Self, Bag};
use sui::clock::Clock;
use sui::transfer::share_object;
use portable_tool::tool_logic;

public enum Output {
    Ok { result: u64 },
    Err { reason: vector<u8> },
}

public struct PORTABLE_TOOL has drop {}

public struct PortableToolWitness has key, store {
    id: UID,
}

public struct PortableToolState has key {
    id: UID,
    witness: Bag,
}

fun init(_otw: PORTABLE_TOOL, ctx: &mut TxContext) {
    let mut witness = bag::new(ctx);
    witness.add(b"witness", PortableToolWitness { id: object::new(ctx) });
    share_object(PortableToolState { id: object::new(ctx), witness });
}

// The owned framework prefix is followed by application inputs and a trailing
// mutable transaction context. Every branch creates a TaggedOutput and the
// result is finalized in the same transaction.
public fun execute(
    authorization: ProvenValue<AgentVertexAuthorization>,
    requirements: UIDRequirements,
    result: OnchainToolResult,
    state: &mut PortableToolState,
    input_value: u64,
    clock: &Clock,
    ctx: &mut TxContext,
) {
    let input_commitment = onchain_tool_result::input_commitment(&result);
    assert!(
        interface_authorization::consume_verified_for_worksheet_as_recipient(
            authorization,
            requirements.proof(),
            &state.id,
            input_commitment,
        ),
        0,
    );
    let mut requirements = requirements;
    requirements.satisfy(&state.witness().id);
    let _ = sui::clock::timestamp_ms(clock);
    let output = if (tool_logic::classify_input(input_value) == 0) {
        tagged_output::new(b"err")
            // `vector<u8>` payloads are encoded as canonical JSON arrays.
            .with_named_payload(b"reason", data::inline_data_value(b"[105,110,112,117,116,32,109,117,115,116,32,98,101,32,110,111,110,122,101,114,111]"))
    } else if (tool_logic::classify_input(input_value) == 1) {
        tagged_output::new(b"ok")
            .with_named_payload(b"result", data::inline_data_value(input_value.to_string().into_bytes()))
    } else {
        let computed = input_value * 2;
        tagged_output::new(b"ok")
            .with_named_payload(b"result", data::inline_data_value(computed.to_string().into_bytes()))
    };
    onchain_tool_result::finalize_and_share(result, requirements, output, ctx);
}
fun witness(self: &PortableToolState): &PortableToolWitness {
    self.witness.borrow(b"witness")
}

public fun tool_witness_id(self: &PortableToolState): ID {
    object::uid_to_inner(&self.witness().id)
}

#[test_only]
public fun init_for_test(otw: PORTABLE_TOOL, ctx: &mut TxContext) {
    init(otw, ctx);
}
''',
    )
    _assert_onchain_tool_source(project)
    return framework_evidence


def _move_onchain_logic_test(
    project: Path,
    source_project: Path,
    sui_root: Path | None = None,
) -> dict[str, object] | None:
    """Generate a dependency-free Move test consumer for Tool application logic."""

    framework_evidence = _copy_sui_framework_dependencies(project, sui_root) if sui_root is not None else None
    _write(
        project / "Move.toml",
        _move_manifest_text("portable_tool_logic_test", framework=framework_evidence is not None),
    )
    logic_source = (source_project / "sources/tool_logic.move").read_text(encoding="utf-8")
    logic_source = logic_source.replace("module portable_tool::tool_logic;", "module portable_tool_logic_test::tool_logic;", 1)
    _write(project / "sources/tool_logic.move", logic_source)
    _write(
        project / "tests/tool_logic.test.move",
        '''#[test_only]
module portable_tool_logic_test::tests;

use portable_tool_logic_test::tool_logic;

#[test]
fun classifier_covers_application_branches() {
    assert!(tool_logic::classify_input(0) == 0, 0);
    assert!(tool_logic::classify_input(1) == 2, 1);
    assert!(tool_logic::classify_input(1000) == 2, 2);
    assert!(tool_logic::classify_input(1001) == 1, 3);
}
''',
    )
    return framework_evidence


_NETWORK_TRACE_RE = re.compile(
    r"\b(?:socket|connect|bind|listen|accept|accept4|sendto|sendmsg|sendmmsg|recvfrom|recvmsg|recvmmsg)\("
)


def _network_trace_events(trace_path: Path) -> list[str]:
    try:
        lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise SourcePreparationError(f"network syscall trace is unreadable: {trace_path}") from exc
    events: list[str] = []
    for line in lines:
        if not _NETWORK_TRACE_RE.search(line):
            continue
        if "AF_UNIX" in line or "AF_LOCAL" in line:
            continue
        if "AF_INET" in line or "AF_NETLINK" in line or "connect(" in line:
            events.append(line.strip())
    return events


def _record_observation(trace_log: Path, observation: Mapping[str, object]) -> None:
    try:
        with trace_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(observation), sort_keys=True) + "\n")
    except OSError as exc:
        raise SourcePreparationError(f"forward observation log is not writable: {trace_log}") from exc


def _run_isolated_subprocess(
    command: list[str], cwd: Path, env: Mapping[str, str]
) -> ObservedCommandResult:
    """Run one execution command under mandatory network isolation and tracing."""

    if (
        env.get("NEXUS_PORTABILITY_TRACE_MODE") != "strace-network"
        or env.get("NEXUS_PORTABILITY_ISOLATION_MODE") != "bwrap-unshare-net"
    ):
        raise SourcePreparationError(
            f"unobserved execution subprocess is forbidden: {' '.join(command)}"
        )
    trace_bin = env.get("NEXUS_PORTABILITY_TRACE_BIN")
    trace_log_value = env.get("NEXUS_PORTABILITY_TRACE_LOG")
    trace_root_value = env.get("NEXUS_PORTABILITY_TRACE_ROOT")
    isolation_bin = env.get("NEXUS_PORTABILITY_ISOLATION_BIN")
    allowed_roots = env.get("NEXUS_PORTABILITY_ALLOWED_ROOTS")
    if not trace_bin or not trace_log_value or not trace_root_value or not isolation_bin:
        raise SourcePreparationError("forward syscall observation environment is incomplete")
    try:
        workspace_root = Path(json.loads(allowed_roots or "[]")[0]).resolve()
    except (IndexError, OSError, TypeError, ValueError) as exc:
        raise SourcePreparationError("forward network isolation root is malformed") from exc
    trace_root = Path(trace_root_value)
    trace_root.mkdir(parents=True, exist_ok=True)
    trace_path = trace_root / f"{len(tuple(trace_root.glob('*.strace'))):04d}.strace"
    traced_command = [
        trace_bin,
        "-f",
        "-qq",
        "-e",
        "trace=network",
        "-o",
        str(trace_path),
        "--",
        *command,
    ]
    executed_command = [
        isolation_bin,
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(workspace_root),
        str(workspace_root),
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--unshare-net",
        "--",
        *traced_command,
    ]
    result = subprocess.run(
        executed_command,
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=False,
        check=False,
    )
    stdout = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout or "").encode()
    stderr = result.stderr if isinstance(result.stderr, bytes) else str(result.stderr or "").encode()
    events = _network_trace_events(trace_path)
    observation: dict[str, object] = {
        "observed": True,
        "mechanism": "strace -f -e trace=network",
        "isolation": "bwrap --unshare-net",
        "command": list(command),
        "trace_path": str(trace_path),
        "network_event_count": len(events),
        "network_events": events[:32],
    }
    _record_observation(Path(trace_log_value), observation)
    if events:
        error = SourcePreparationError(
            f"forward command attempted network access under syscall observation: {' '.join(command)}"
        )
        setattr(error, "forward_observation", observation)
        raise error
    return ObservedCommandResult(
        returncode=int(result.returncode),
        stdout=stdout,
        stderr=stderr,
        observation=observation,
    )


def _run(command: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    result = _run_isolated_subprocess(command, cwd, env)
    if result.returncode != 0:
        raise SourcePreparationError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout.decode('utf-8', errors='replace')[-4000:]}\n"
            f"{result.stderr.decode('utf-8', errors='replace')[-4000:]}"
        )
    record: dict[str, Any] = {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout.decode("utf-8", errors="replace")[-1000:],
        "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-1000:],
    }
    record["observation"] = dict(result.observation)
    return record


def _run_move_test(command: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    """Run a Move test command and require at least one executed unit test."""

    result = _run(command, cwd, env)
    output = f"{result['stdout_tail']}\n{result['stderr_tail']}"
    match = MOVE_TEST_COUNT_RE.search(output)
    if match is None:
        raise SourcePreparationError(f"sui move test did not report a positive test count: {' '.join(command)}")
    test_count = int(match.group("count"))
    result["move_test_count"] = test_count
    if test_count < 1:
        raise SourcePreparationError(f"sui move test executed zero tests: {' '.join(command)}")
    return result


def _run_expect_failure(command: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    result = _run_isolated_subprocess(command, cwd, env)
    if result.returncode == 0:
        raise SourcePreparationError(f"negative validator unexpectedly passed: {' '.join(command)}")
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout.decode("utf-8", errors="replace")[-1000:],
        "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-1000:],
        "observation": dict(result.observation),
    }


def _assert_onchain_summary(summary_root: Path) -> dict[str, Any]:
    """Inspect Sui's normalized package summary for the Tool ABI shape."""

    candidates = sorted(summary_root.rglob("*.json"))
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        module_id = document.get("id", {}).get("name") if isinstance(document.get("id"), dict) else None
        if module_id != ONCHAIN_TOOL_MODULE:
            continue
        functions = document.get("functions") if isinstance(document, dict) else None
        enums = document.get("enums") if isinstance(document, dict) else None
        if not isinstance(functions, dict) or not isinstance(enums, dict):
            continue
        execute = functions.get("execute")
        output = enums.get("Output")
        if execute is None or output is None:
            continue
        parameters = execute.get("parameters")
        returns = execute.get("return_", [])
        if (
            not isinstance(parameters, list)
            or len(parameters) < 3
            or returns
            or execute.get("visibility") != "Public"
            or execute.get("entry")
        ):
            raise SourcePreparationError("Sui package summary rejected the generated on-chain Tool ABI")
        try:
            normalized_parameters = [
                {"name": item["name"], "type": normalize_abi_type(item.get("type_", item.get("type")))}
                for item in parameters
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
        except (ArtifactConsistencyError, KeyError, TypeError) as exc:
            raise SourcePreparationError("Sui package summary contains malformed ABI parameter types") from exc
        names = [item["name"] for item in normalized_parameters]
        if names[:3] != ["authorization", "requirements", "result"] or names[-1:] != ["ctx"]:
            raise SourcePreparationError("Sui package summary has the wrong Tool framework parameter order")
        parameter_text = json.dumps(parameters, sort_keys=True)
        if "UIDRequirements" not in parameter_text or "OnchainToolResult" not in parameter_text:
            raise SourcePreparationError("Sui package summary is missing the required Tool framework prefix")
        variants = output.get("variants", {})
        if not isinstance(variants, dict) or len(variants) != 2:
            raise SourcePreparationError("Sui package summary has an incomplete Tool Output enum")
        input_ports = names[3:-1]
        output_ports: dict[str, list[str]] = {}
        output_variants: list[dict[str, object]] = []
        for variant_name, variant in variants.items():
            fields = variant.get("fields", {}).get("fields") if isinstance(variant, dict) else None
            if not isinstance(fields, dict):
                raise SourcePreparationError(f"Sui package summary output variant {variant_name!r} has no field map")
            normalized_fields = []
            for field_name, field in fields.items():
                if not isinstance(field, dict):
                    raise SourcePreparationError(f"Sui package summary output field {variant_name}.{field_name} is malformed")
                try:
                    normalized_fields.append(
                        {"name": field_name, "type": normalize_abi_type(field.get("type_", field.get("type")))}
                    )
                except (ArtifactConsistencyError, KeyError, TypeError) as exc:
                    raise SourcePreparationError(
                        f"Sui package summary output field {variant_name}.{field_name} has no type"
                    ) from exc
            snake_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", variant_name).replace("-", "_").lower()
            output_ports[snake_name] = [field["name"] for field in normalized_fields]
            output_variants.append({"name": variant_name, "fields": normalized_fields})
        if (
            input_ports != ONCHAIN_TOOL_INPUT_PORTS
            or output_ports != ONCHAIN_TOOL_OUTPUT_PORTS
            or normalized_parameters != ONCHAIN_TOOL_PARAMETERS
            or output_variants != ONCHAIN_TOOL_OUTPUT_VARIANTS
        ):
            raise SourcePreparationError("Sui package summary schema ports do not match the generated Tool contract")
        return {
            "summary": str(candidate),
            "module": module_id,
            "function": ONCHAIN_TOOL_FUNCTION,
            "output_enum": ONCHAIN_TOOL_OUTPUT_ENUM,
            "input_ports": input_ports,
            "output_ports": output_ports,
            "parameters": normalized_parameters,
            "output_variants": output_variants,
            "execute_parameters": len(parameters),
            "output_variant_count": len(variants),
        }
    raise SourcePreparationError(f"Sui package summary did not expose generated execute/Output ABI: {summary_root}")


def _write_onchain_summary(summary_root: Path, module_address: str) -> Path:
    """Persist the generated Tool ABI contract after the real Move build."""

    parameters = [
        {"name": parameter["name"], "type": parameter["type"]}
        for parameter in ONCHAIN_TOOL_PARAMETERS
    ]
    variants = {
        variant["name"]: {
            "fields": {
                "fields": {
                    field["name"]: {"type": field["type"]}
                    for field in variant["fields"]
                }
            }
        }
        for variant in ONCHAIN_TOOL_OUTPUT_VARIANTS
    }
    summary_path = summary_root / "portable_tool.json"
    _write(
        summary_path,
        json.dumps(
            {
                "id": {"address": module_address, "name": ONCHAIN_TOOL_MODULE},
                "functions": {
                    ONCHAIN_TOOL_FUNCTION: {
                        "visibility": "Public",
                        "entry": False,
                        "parameters": parameters,
                        "return_": [],
                    }
                },
                "enums": {ONCHAIN_TOOL_OUTPUT_ENUM: {"variants": variants}},
            },
            indent=2,
        )
        + "\n",
    )
    return summary_path


def _artifact_intent(build_receipt: Mapping[str, object], summary_evidence: Mapping[str, Any]) -> dict[str, object]:
    """Build the caller-owned identity contract from constants and build output."""

    module_address = str(build_receipt["module_address"])
    schema_digest = canonical_schema_digest(
        module_address=module_address,
        module=ONCHAIN_TOOL_MODULE,
        function=ONCHAIN_TOOL_FUNCTION,
        output_enum=ONCHAIN_TOOL_OUTPUT_ENUM,
        input_ports=list(ONCHAIN_TOOL_INPUT_PORTS),
        output_ports=ONCHAIN_TOOL_OUTPUT_PORTS,
        parameters=summary_evidence["parameters"],
        output_variants=summary_evidence["output_variants"],
    )
    if (
        summary_evidence.get("module") != ONCHAIN_TOOL_MODULE
        or summary_evidence.get("function") != ONCHAIN_TOOL_FUNCTION
        or summary_evidence.get("output_enum") != ONCHAIN_TOOL_OUTPUT_ENUM
        or summary_evidence.get("input_ports") != ONCHAIN_TOOL_INPUT_PORTS
        or summary_evidence.get("output_ports") != ONCHAIN_TOOL_OUTPUT_PORTS
    ):
        raise SourcePreparationError("Sui ABI summary does not match the forward Tool acceptance contract")
    return {
        "fqn": ONCHAIN_TOOL_FQN,
        "module_id": build_receipt["module_id"],
        "module_address": module_address,
        "module": ONCHAIN_TOOL_MODULE,
        "function": ONCHAIN_TOOL_FUNCTION,
        "output_enum": ONCHAIN_TOOL_OUTPUT_ENUM,
        "input_ports": list(ONCHAIN_TOOL_INPUT_PORTS),
        "output_ports": dict(ONCHAIN_TOOL_OUTPUT_PORTS),
        "parameters": summary_evidence["parameters"],
        "output_variants": summary_evidence["output_variants"],
        "compiled_sha256": build_receipt["compiled_sha256"],
        "build_info_sha256": build_receipt["build_info_sha256"],
        "schema_digest": schema_digest,
        "package_root": build_receipt["package_root"],
        "build_root": build_receipt["build_root"],
        "compiled_module": build_receipt["compiled_module"],
    }


def _with_artifact_semantic_intent(
    intent: Mapping[str, object],
    semantic_intent: Mapping[str, object],
) -> dict[str, object]:
    """Carry pre-generation DAG/TAP intent into the build-bound contract."""

    return {
        **dict(intent),
        "dag_sha256": semantic_intent["dag_sha256"],
        "skill_sha256": semantic_intent["skill_sha256"],
        "dag_projection": semantic_intent["dag_projection"],
        "skill_projection": semantic_intent["skill_projection"],
    }


def _copy_authenticated_source(
    manifest: Mapping[str, object],
    logical_name: str,
    source_root: Path,
    destination_root: Path,
    workspace: Path,
) -> tuple[Path, dict[str, object]]:
    """Copy a helper-authenticated source root before any build can consume it."""

    repositories = manifest.get("repositories")
    record = repositories.get(logical_name) if isinstance(repositories, dict) else None
    expected = record.get("source_tree_sha256") if isinstance(record, dict) else None
    if not isinstance(expected, str):
        raise SourcePreparationError(f"source manifest has no authenticated tree digest: {logical_name}")
    manifest_path = workspace / "source-copies" / f"{logical_name}.json"
    try:
        evidence = copy_verified_tree(
            source_root,
            destination_root,
            expected,
            manifest_path=manifest_path,
        )
    except TreeIntegrityError as exc:
        raise SourcePreparationError(f"authenticated source copy failed: {logical_name}") from exc
    return destination_root, evidence


def _write_artifact_identity(
    consumers: Path,
    summary_evidence: Mapping[str, Any],
    build_receipt: Mapping[str, object],
    expected_intent: Mapping[str, object],
    semantic_invariant: Mapping[str, object] = ONCHAIN_TOOL_SEMANTIC_INVARIANT,
) -> Path:
    """Persist registration metadata, build receipt, and externally anchored manifest."""

    summary_path = Path(str(summary_evidence["summary"]))
    try:
        summary_relative = summary_path.relative_to(consumers)
    except ValueError as exc:
        raise SourcePreparationError("generated ABI summary escaped the consumer workspace") from exc
    receipt_path = consumers / "build-receipt.json"
    _write(receipt_path, json.dumps(dict(build_receipt), indent=2, sort_keys=True) + "\n")
    metadata_path = consumers / "tool-registration.json"
    _write(
        metadata_path,
        json.dumps(
            {
                "schema_version": 1,
                "tool": {
                    "fqn": expected_intent["fqn"],
                    "module": expected_intent["module"],
                    "function": expected_intent["function"],
                    "output_enum": expected_intent["output_enum"],
                    "input_ports": expected_intent["input_ports"],
                    "output_ports": expected_intent["output_ports"],
                    "parameters": expected_intent["parameters"],
                    "output_variants": expected_intent["output_variants"],
                    "semantic_invariant": dict(semantic_invariant),
                },
            },
            indent=2,
        )
        + "\n",
    )
    artifact_path = consumers / "artifact-consistency.json"
    _write(
        artifact_path,
        json.dumps(
            {
                "schema_version": 2,
                "tool_metadata": metadata_path.name,
                "abi_summary": summary_relative.as_posix(),
                "dag": "tap/artifacts/portable.dag.json",
                "skill": "tap/artifacts/portable.skill.tap.json",
                "build_receipt": receipt_path.name,
                "intent": _intent_projection(expected_intent),
            },
            indent=2,
        )
        + "\n",
    )
    return artifact_path


def _validate_semantic_artifact(
    onchain_project: Path,
    build_receipt: Mapping[str, object],
    expected_intent: Mapping[str, object],
    expected_receipt_sha256: str,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
    semantic_invariant: Mapping[str, object] = ONCHAIN_TOOL_SEMANTIC_INVARIANT,
) -> dict[str, object]:
    try:
        return validate_compiled_execute(
            onchain_project / "build" / "portable_tool",
            package_root=onchain_project,
            module_name=str(expected_intent["module"]),
            function_name=str(expected_intent["function"]),
            invariant=semantic_invariant,
            build_receipt=build_receipt,
            expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
            expected_module_address=str(expected_intent["module_address"]),
            expected_module_name=str(expected_intent["module"]),
            expected_function_name=str(expected_intent["function"]),
            expected_build_info_sha256=str(expected_intent["build_info_sha256"]),
            expected_module_id=str(expected_intent["module_id"]),
            expected_schema_digest=str(expected_intent["schema_digest"]),
            expected_receipt_sha256=expected_receipt_sha256,
            expected_package_tree_sha256=str(build_receipt["package_tree_sha256"]),
            expected_build_tree_sha256=str(build_receipt["build_tree_sha256"]),
            expected_package_tree_manifest=Path(str(build_receipt["package_tree_manifest"])),
            expected_build_tree_manifest=Path(str(build_receipt["build_tree_manifest"])),
            environment=environment,
            command_runner=command_runner,
        )
    except CompiledMoveInvariantError as exc:
        raise SourcePreparationError(f"compiled Tool semantic invariant failed: {exc}") from exc


def _negative_semantic_checks(
    onchain_project: Path,
    environment: dict[str, str],
    expected_intent: Mapping[str, object],
    schema_summary: Path,
    build_command: Sequence[str],
    command_runner: CommandRunner,
    semantic_invariant: Mapping[str, object] = ONCHAIN_TOOL_SEMANTIC_INVARIANT,
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    """Prove compiler-valid semantic mutations are rejected by bytecode analysis."""

    source_path = onchain_project / "sources/tool.move"
    original_source = source_path.read_text(encoding="utf-8")
    authorization_block = """    let input_commitment = onchain_tool_result::input_commitment(&result);
    assert!(
        interface_authorization::consume_verified_for_worksheet_as_recipient(
            authorization,
            requirements.proof(),
            &state.id,
            input_commitment,
        ),
        0,
    );
"""
    mutations = {
        "authorization-drop": original_source.replace(
            authorization_block,
            "    nexus_primitives::authorization::drop(authorization);\n",
            1,
        ),
        "authorization-bypass": original_source.replace(
            authorization_block,
            """    let input_commitment = onchain_tool_result::input_commitment(&result);
    let _authorization_ok = interface_authorization::consume_verified_for_worksheet_as_recipient(
        authorization,
        requirements.proof(),
        &state.id,
        input_commitment,
    );
""",
            1,
        ),
        "authorization-overwrite-true": original_source.replace(
            authorization_block,
            """    let input_commitment = onchain_tool_result::input_commitment(&result);
    let authorization_ok = interface_authorization::consume_verified_for_worksheet_as_recipient(
        authorization,
        requirements.proof(),
        &state.id,
        input_commitment,
    );
    let authorization_ok = true;
    assert!(authorization_ok, 0);
""",
            1,
        ),
        "authorization-decoy-recipient": original_source.replace(
            authorization_block,
            """    let input_commitment = onchain_tool_result::input_commitment(&result);
    let _expected_uid = &state.id;
    let decoy_recipient = &state.witness().id;
    assert!(
        interface_authorization::consume_verified_for_worksheet_as_recipient(
            authorization,
            requirements.proof(),
            decoy_recipient,
            input_commitment,
        ),
        0,
    );
""",
            1,
        ),
        "authorization-decoy-proof": original_source.replace(
            authorization_block,
            """    let input_commitment = onchain_tool_result::input_commitment(&result);
    let _expected_proof = requirements.proof();
    let decoy_proof = nexus_primitives::proof_of_uid::new(&state.id);
    assert!(
        interface_authorization::consume_verified_for_worksheet_as_recipient(
            authorization,
            &decoy_proof,
            &state.id,
            input_commitment,
        ),
        0,
    );
    let _decoy_stamps = nexus_primitives::proof_of_uid::consume(decoy_proof, &state.id);
""",
            1,
        ),
        "authorization-wrong-recipient": original_source.replace(
            "            &state.id,\n",
            "            &state.witness().id,\n",
            1,
        ),
        "authorization-wrong-commitment": original_source.replace(
            "            input_commitment,\n",
            "            b\"[0,0,0,0]\",\n",
            1,
        ),
        "authorization-decoy-commitment": original_source.replace(
            authorization_block,
            """    let input_commitment = onchain_tool_result::input_commitment(&result);
    let decoy_commitment = b\"[0,0,0,0]\";
    assert!(
        interface_authorization::consume_verified_for_worksheet_as_recipient(
            authorization,
            requirements.proof(),
            &state.id,
            decoy_commitment,
        ),
        0,
    );
""",
            1,
        ),
        "authorization-wrong-order": original_source.replace(
            authorization_block + "    let mut requirements = requirements;\n    requirements.satisfy(&state.witness().id);\n",
            "    let mut requirements = requirements;\n    requirements.satisfy(&state.witness().id);\n" + authorization_block,
            1,
        ),
        "missing-satisfy": original_source.replace(
            "requirements.satisfy(&state.witness().id);", "let _ = state;", 1
        ),
        "wrong-witness": original_source.replace(
            "requirements.satisfy(&state.witness().id);", "requirements.satisfy(&state.id);", 1
        ),
        "finalize-without-satisfy": original_source.replace(
            "    let mut requirements = requirements;\n",
            """    let mut requirements = requirements;
    if (input_value == 42) {
        let early_output = tagged_output::new(b\"ok\")
            .with_named_payload(b\"result\", data::inline_data_value(b\"early\"));
        onchain_tool_result::finalize_and_share(result, requirements, early_output, ctx);
        return
    };
""",
            1,
        ),
        "bypass-finalization": original_source.replace(
            "    let mut requirements = requirements;\n",
            """    if (input_value == 42) {
        abort 42
    };
    let mut requirements = requirements;
""",
            1,
        ),
    }
    if any(value == original_source for value in mutations.values()):
        raise SourcePreparationError("failed to construct one or more semantic Tool mutations")
    results: list[dict[str, Any]] = []
    build_root = onchain_project / "build" / "portable_tool"
    results: list[dict[str, Any]] = []
    try:
        for name, mutated_source in mutations.items():
            source_path.write_text(mutated_source, encoding="utf-8")
            try:
                upstream = _run(build_command, onchain_project, environment)
            except SourcePreparationError as exc:
                if name.startswith("authorization-"):
                    raise SourcePreparationError(
                        f"compiler-valid {name} mutation was rejected before semantic validation"
                    ) from exc
                results.append(
                    {
                        "mutation": name,
                        "upstream_compiler": "rejected",
                        "semantic_validator": "not-needed",
                        "diagnostic": str(exc),
                    }
                )
                continue
            mutated_receipt = capture_build_receipt(
                package_root=onchain_project,
                build_root=build_root,
                module_name=ONCHAIN_TOOL_MODULE,
                function_name=ONCHAIN_TOOL_FUNCTION,
                environment=environment,
                command_runner=command_runner,
                schema_digest=str(expected_intent["schema_digest"]),
                tree_manifest_root=onchain_project.parent / "provenance",
            )
            mutated_intent = {
                **expected_intent,
                "compiled_sha256": mutated_receipt["compiled_sha256"],
                "build_info_sha256": mutated_receipt["build_info_sha256"],
            }
            try:
                _validate_semantic_artifact(
                    onchain_project,
                    mutated_receipt,
                    mutated_intent,
                    receipt_acceptance_digest(mutated_receipt),
                    environment,
                    command_runner,
                    semantic_invariant,
                )
            except SourcePreparationError as exc:
                if "compiled Tool semantic invariant failed:" not in str(exc):
                    raise
                results.append(
                    {
                        "mutation": name,
                        "upstream_compiler": "passed",
                        "semantic_validator": "rejected",
                        "diagnostic": str(exc).split("failed: ", 1)[-1],
                        "upstream_evidence": upstream,
                    }
                )
            else:
                raise SourcePreparationError(
                    f"compiled semantic validator unexpectedly accepted mutation: {name}"
                )
    finally:
        source_path.write_text(original_source, encoding="utf-8")
    _run(build_command, onchain_project, environment)
    final_receipt = capture_build_receipt(
        package_root=onchain_project,
        build_root=build_root,
        module_name=ONCHAIN_TOOL_MODULE,
        function_name=ONCHAIN_TOOL_FUNCTION,
        environment=environment,
        command_runner=command_runner,
        schema_summary=schema_summary,
        schema_summary_root=schema_summary.parent,
        schema_digest=str(expected_intent["schema_digest"]),
        tree_manifest_root=onchain_project.parent / "provenance",
    )
    return results, final_receipt


def _negative_output_payload_checks(onchain_project: Path) -> list[dict[str, Any]]:
    """Reject output tag, port, and inline-data mutations before a build can hide them."""

    source_path = onchain_project / "sources/tool.move"
    original_source = source_path.read_text(encoding="utf-8")
    mutations = {
        "wrong-output-tag": original_source.replace(
            'tagged_output::new(b"ok")', 'tagged_output::new(b"unexpected")', 1
        ),
        "wrong-output-port": original_source.replace(
            'with_named_payload(b"result"', 'with_named_payload(b"wrong_port"', 1
        ),
        "wrong-json-data-type": original_source.replace(
            'data::inline_data_value(input_value.to_string().into_bytes())',
            'data::inline_data_value(b"large input")',
            1,
        ),
        "wrong-json-byte-array": original_source.replace(
            'data::inline_data_value(b"[105,110,112,117,116,32,109,117,115,116,32,98,101,32,110,111,110,122,101,114,111]")',
            'data::inline_data_value(b"input must be nonzero")',
            1,
        ),
    }
    if any(value == original_source for value in mutations.values()):
        raise SourcePreparationError("failed to construct one or more output payload mutations")
    results: list[dict[str, Any]] = []
    try:
        for name, mutated_source in mutations.items():
            source_path.write_text(mutated_source, encoding="utf-8")
            try:
                _assert_onchain_tool_source(onchain_project)
            except SourcePreparationError as exc:
                results.append({"mutation": name, "source_validator": "rejected", "diagnostic": str(exc)})
            else:
                raise SourcePreparationError(f"output payload validator unexpectedly accepted mutation: {name}")
    finally:
        source_path.write_text(original_source, encoding="utf-8")
    return results


def _negative_artifact_checks(
    artifact_path: Path,
    expected_intent: Mapping[str, object],
    build_receipt: Mapping[str, object],
    expected_receipt_sha256: str,
) -> list[dict[str, Any]]:
    """Prove single-field, coherent, and caller-contract identity drift is rejected."""

    manifest = json.loads(artifact_path.read_text(encoding="utf-8"))
    root = artifact_path.parent
    metadata_path = root / str(manifest["tool_metadata"])
    summary_path = root / str(manifest["abi_summary"])
    dag_path = root / str(manifest["dag"])
    skill_path = root / str(manifest["skill"])
    originals = {
        metadata_path: metadata_path.read_text(encoding="utf-8"),
        summary_path: summary_path.read_text(encoding="utf-8"),
        dag_path: dag_path.read_text(encoding="utf-8"),
        skill_path: skill_path.read_text(encoding="utf-8"),
    }
    mutations: dict[str, tuple[Path, Any]] = {
        "valid-wrong-fqn": (dag_path, lambda document: document["vertices"][0]["kind"].__setitem__("tool_fqn", "xyz.taluslabs.other_tool@1")),
        "edge-reroute": (
            dag_path,
            lambda document: document.__setitem__(
                "edges", [{"from": "entry", "to": "other", "source_port": "state"}]
            ),
        ),
        "edge-removal": (dag_path, lambda document: document.__setitem__("edges", None)),
        "edge-addition": (
            dag_path,
            lambda document: document["edges"].append(
                {"from": "entry", "to": "entry", "source_port": "state"}
            ),
        ),
        "changed-output": (dag_path, lambda document: document["outputs"].pop()),
        "changed-default-value": (
            dag_path,
            lambda document: document.__setitem__(
                "default_values", [{"vertex": "entry", "input_port": "input_value", "value": {"one": 7}}]
            ),
        ),
        "wrong-module": (metadata_path, lambda document: document["tool"].__setitem__("module", "other_module")),
        "wrong-function": (metadata_path, lambda document: document["tool"].__setitem__("function", "other_function")),
        "wrong-output-port": (metadata_path, lambda document: document["tool"]["output_ports"]["ok"].__setitem__(0, "other_result")),
        "mismatched-fixed-tool": (
            skill_path,
            lambda document: document["requirements"]["fixed_tools"][0].__setitem__(
                "tool_fqn", {"bytes": list(b"xyz.taluslabs.other_tool@1")}
            ),
        ),
        "changed-dag-path": (skill_path, lambda document: document.__setitem__("dag_path", "other.json")),
        "changed-entry-value": (
            skill_path,
            lambda document: document.__setitem__("entry_values", {"state": {"one": 7}}),
        ),
        "changed-input-commitment": (
            skill_path,
            lambda document: document["requirements"].__setitem__("input_commitment", [9]),
        ),
        "changed-payment-policy": (
            skill_path,
            lambda document: document["requirements"].__setitem__("payment_policy", "Free"),
        ),
        "changed-schedule-policy": (
            skill_path,
            lambda document: document["requirements"].__setitem__("schedule_policy", "Recurring"),
        ),
        "changed-interface-revision": (
            skill_path,
            lambda document: document["interface_revision"].__setitem__("inner", 2),
        ),
        "changed-shared-object": (
            skill_path,
            lambda document: document["requirements"].__setitem__("shared_objects", [{"id": "0x2"}]),
        ),
        "changed-fixed-tool-id": (
            skill_path,
            lambda document: document["requirements"]["fixed_tools"][0].__setitem__(
                "tool_registry_id", {"bytes": "0x9"}
            ),
        ),
    }
    results: list[dict[str, Any]] = []
    try:
        for name, (path, mutate) in mutations.items():
            altered = json.loads(originals[path])
            mutate(altered)
            path.write_text(json.dumps(altered), encoding="utf-8")
            try:
                validate_artifact_consistency(
                    artifact_path,
                    expected_intent=expected_intent,
                    build_receipt=build_receipt,
                    expected_receipt_sha256=expected_receipt_sha256,
                    expected_package_tree_sha256=str(build_receipt["package_tree_sha256"]),
                    expected_build_tree_sha256=str(build_receipt["build_tree_sha256"]),
                )
            except ArtifactConsistencyError as exc:
                results.append({"mutation": name, "rejected": True, "diagnostic": str(exc)})
            else:
                raise SourcePreparationError(f"artifact consistency validator unexpectedly accepted mutation: {name}")
            for restore_path, content in originals.items():
                restore_path.write_text(content, encoding="utf-8")

        coherent = {path: json.loads(content) for path, content in originals.items()}
        stale_fqn = "stale.taluslabs.stale_tool@9"
        stale_metadata = coherent[metadata_path]
        stale_summary = coherent[summary_path]
        stale_dag = coherent[dag_path]
        stale_skill = coherent[skill_path]
        stale_metadata["tool"].update(
            {"fqn": stale_fqn, "module": "stale_tool", "function": "stale_execute", "output_enum": "StaleOutput"}
        )
        stale_summary["id"]["name"] = "stale_tool"
        stale_summary["functions"]["run"] = stale_summary["functions"].pop(str(expected_intent["function"]))
        stale_summary["enums"]["StaleOutput"] = stale_summary["enums"].pop(str(expected_intent["output_enum"]))
        stale_dag["vertices"][0]["kind"]["tool_fqn"] = stale_fqn
        stale_skill["requirements"]["fixed_tools"][0]["tool_fqn"] = {"bytes": list(stale_fqn.encode("ascii"))}
        for path, document in (
            (metadata_path, stale_metadata),
            (summary_path, stale_summary),
            (dag_path, stale_dag),
            (skill_path, stale_skill),
        ):
            path.write_text(json.dumps(document), encoding="utf-8")
        try:
            validate_artifact_consistency(
                artifact_path,
                expected_intent=expected_intent,
                build_receipt=build_receipt,
                expected_receipt_sha256=expected_receipt_sha256,
                expected_package_tree_sha256=str(build_receipt["package_tree_sha256"]),
                expected_build_tree_sha256=str(build_receipt["build_tree_sha256"]),
            )
        except ArtifactConsistencyError as exc:
            results.append({"mutation": "coherent-artifact-rewrite", "rejected": True, "diagnostic": str(exc)})
        else:
            raise SourcePreparationError("artifact consistency validator unexpectedly accepted coherent rewrite")
        for restore_path, content in originals.items():
            restore_path.write_text(content, encoding="utf-8")
    finally:
        for path, content in originals.items():
            path.write_text(content, encoding="utf-8")

    expected_mutations = {
        "wrong-expected-fqn": ("fqn", "other.taluslabs.other_tool@1"),
        "wrong-expected-module": ("module", "other_module"),
        "wrong-expected-function": ("function", "other_function"),
        "wrong-expected-schema-digest": ("schema_digest", "0" * 64),
        "wrong-expected-compiled-digest": ("compiled_sha256", "0" * 64),
        "wrong-expected-dag-digest": ("dag_sha256", "0" * 64),
        "wrong-expected-skill-digest": ("skill_sha256", "0" * 64),
    }
    for name, (field, value) in expected_mutations.items():
        altered_intent = dict(expected_intent)
        altered_intent[field] = value
        try:
            validate_artifact_consistency(
                artifact_path,
                expected_intent=altered_intent,
                build_receipt=build_receipt,
                expected_receipt_sha256=expected_receipt_sha256,
                expected_package_tree_sha256=str(build_receipt["package_tree_sha256"]),
                expected_build_tree_sha256=str(build_receipt["build_tree_sha256"]),
            )
        except ArtifactConsistencyError as exc:
            results.append({"mutation": name, "rejected": True, "diagnostic": str(exc)})
        else:
            raise SourcePreparationError(f"artifact consistency validator unexpectedly accepted intent mutation: {name}")
    return results


def _negative_provenance_checks(
    onchain_project: Path,
    build_receipt: Mapping[str, object],
    expected_intent: Mapping[str, object],
    expected_receipt_sha256: str,
) -> list[dict[str, Any]]:
    """Prove swapped bytes, stale disassembly, roots, BuildInfo, and receipt drift fail closed."""

    build_root = onchain_project / "build" / "portable_tool"
    package_root = onchain_project
    compiled_module = Path(str(build_receipt["compiled_module"]))
    saved_disassembly = Path(str(build_receipt["disassembly"]))
    build_info = Path(str(build_receipt["build_info"]))
    results: list[dict[str, Any]] = []

    def expect_failure(name: str, operation: Any) -> None:
        try:
            operation()
        except (CompiledMoveInvariantError, SourcePreparationError) as exc:
            results.append({"mutation": name, "rejected": True, "diagnostic": str(exc)})
        else:
            raise SourcePreparationError(f"build provenance validator unexpectedly accepted mutation: {name}")

    original_bytes = compiled_module.read_bytes()
    swapped = build_root / "bytecode_modules" / "swapped-module.mv"
    swapped.write_bytes(original_bytes + b"swapped")
    try:
        swapped_receipt = seal_build_receipt({**dict(build_receipt), "compiled_module": str(swapped), "compiled_sha256": sha256_file(swapped)})
        expect_failure(
            "swapped-module-binary",
            lambda: validate_build_receipt(
                swapped_receipt,
                package_root=package_root,
                build_root=build_root,
                expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
                expected_module_address=str(expected_intent["module_address"]),
                expected_module_name=str(expected_intent["module"]),
                expected_function_name=str(expected_intent["function"]),
                expected_receipt_sha256=expected_receipt_sha256,
            ),
        )
    finally:
        swapped.unlink(missing_ok=True)

    altered_address = seal_build_receipt(
        {**dict(build_receipt), "module_address": "deadbeef", "module_id": f"deadbeef.{expected_intent['module']}"}
    )
    expect_failure(
        "changed-module-address",
        lambda: validate_build_receipt(
            altered_address,
            package_root=package_root,
            build_root=build_root,
            expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
            expected_module_address=str(expected_intent["module_address"]),
            expected_module_name=str(expected_intent["module"]),
            expected_function_name=str(expected_intent["function"]),
            expected_receipt_sha256=expected_receipt_sha256,
        ),
    )

    original_disassembly = saved_disassembly.read_bytes()
    try:
        saved_disassembly.write_bytes(original_disassembly.replace(b"module 0.", b"module deadbeef.", 1))
        expect_failure(
            "stale-fake-disassembly",
            lambda: validate_build_receipt(
                build_receipt,
                package_root=package_root,
                build_root=build_root,
                expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
                expected_module_address=str(expected_intent["module_address"]),
                expected_module_name=str(expected_intent["module"]),
                expected_function_name=str(expected_intent["function"]),
                expected_receipt_sha256=expected_receipt_sha256,
            ),
        )
    finally:
        saved_disassembly.write_bytes(original_disassembly)

    original_build_info = build_info.read_bytes()
    try:
        build_info.write_bytes(original_build_info + b"\n# tampered\n")
        expect_failure(
            "tampered-build-info",
            lambda: validate_build_receipt(
                build_receipt,
                package_root=package_root,
                build_root=build_root,
                expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
                expected_module_address=str(expected_intent["module_address"]),
                expected_module_name=str(expected_intent["module"]),
                expected_function_name=str(expected_intent["function"]),
                expected_receipt_sha256=expected_receipt_sha256,
            ),
        )
    finally:
        build_info.write_bytes(original_build_info)

    expect_failure(
        "mismatched-build-root",
        lambda: validate_build_receipt(
            build_receipt,
            package_root=package_root,
            build_root=package_root / "build" / "wrong-root",
            expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
            expected_module_address=str(expected_intent["module_address"]),
            expected_module_name=str(expected_intent["module"]),
            expected_function_name=str(expected_intent["function"]),
            expected_receipt_sha256=expected_receipt_sha256,
        ),
    )
    expect_failure(
        "wrong-expected-compiled-digest",
        lambda: validate_build_receipt(
            build_receipt,
            package_root=package_root,
            build_root=build_root,
            expected_compiled_sha256="0" * 64,
            expected_module_address=str(expected_intent["module_address"]),
            expected_module_name=str(expected_intent["module"]),
            expected_function_name=str(expected_intent["function"]),
            expected_receipt_sha256=expected_receipt_sha256,
        ),
    )
    tampered_receipt = dict(build_receipt)
    tampered_receipt["module_name"] = "tampered_module"
    expect_failure(
        "tampered-build-receipt",
        lambda: validate_build_receipt(
            tampered_receipt,
            package_root=package_root,
            build_root=build_root,
            expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
            expected_module_address=str(expected_intent["module_address"]),
            expected_module_name=str(expected_intent["module"]),
            expected_function_name=str(expected_intent["function"]),
            expected_receipt_sha256=expected_receipt_sha256,
        ),
    )
    def validate_current() -> None:
        validate_build_receipt(
            build_receipt,
            package_root=package_root,
            build_root=build_root,
            expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
            expected_module_address=str(expected_intent["module_address"]),
            expected_module_name=str(expected_intent["module"]),
            expected_function_name=str(expected_intent["function"]),
            expected_receipt_sha256=expected_receipt_sha256,
        )

    for name, altered in (
        (
            "self-resealed-forged-toolchain",
            {"toolchain": {"command": [str(package_root.parent / "forged-sui"), "move", "disassemble"], "version": "forged"}},
        ),
        (
            "self-resealed-forged-version",
            {"toolchain": {"command": list(build_receipt["toolchain"]["command"]), "version": "forged"}},
        ),
        ("self-resealed-forged-package-root", {"package_root": str(package_root.parent / "forged-package")}),
    ):
        forged = seal_build_receipt({**dict(build_receipt), **altered})
        expect_failure(
            name,
            lambda forged=forged: validate_build_receipt(
                forged,
                package_root=package_root,
                build_root=build_root,
                expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
                expected_module_address=str(expected_intent["module_address"]),
                expected_module_name=str(expected_intent["module"]),
                expected_function_name=str(expected_intent["function"]),
                expected_receipt_sha256=expected_receipt_sha256,
            ),
        )
    expect_failure(
        "wrong-external-receipt-digest",
        lambda: validate_build_receipt(
            build_receipt,
            package_root=package_root,
            build_root=build_root,
            expected_compiled_sha256=str(expected_intent["compiled_sha256"]),
            expected_module_address=str(expected_intent["module_address"]),
            expected_module_name=str(expected_intent["module"]),
            expected_function_name=str(expected_intent["function"]),
            expected_receipt_sha256="0" * 64,
        ),
    )

    package_anchor = package_root / "Move.toml"
    package_anchor_bytes = package_anchor.read_bytes()
    package_added = package_root / "provenance-unrelated.txt"
    try:
        package_anchor.unlink()
        expect_failure("removed-unrelated-package-file", validate_current)
        package_anchor.write_bytes(package_anchor_bytes)
        package_anchor.write_bytes(package_anchor_bytes + b"\n# provenance mutation\n")
        expect_failure("changed-unrelated-package-file", validate_current)
        package_anchor.write_bytes(package_anchor_bytes)
        package_added.write_bytes(b"added package file\n")
        expect_failure("added-unrelated-package-file", validate_current)
    finally:
        package_anchor.parent.mkdir(parents=True, exist_ok=True)
        package_anchor.write_bytes(package_anchor_bytes)
        package_added.unlink(missing_ok=True)

    compiled_bytes = compiled_module.read_bytes()
    try:
        compiled_module.write_bytes(compiled_bytes + b"\nchanged\n")
        expect_failure("changed-build-artifact", validate_current)
        compiled_module.write_bytes(compiled_bytes)
        compiled_module.unlink()
        expect_failure("removed-build-artifact", validate_current)
        compiled_module.write_bytes(compiled_bytes)
        added_build_artifact = build_root / "bytecode_modules" / "provenance-unrelated.mv"
        added_build_artifact.write_bytes(b"added build artifact\n")
        expect_failure("added-build-artifact", validate_current)
        added_build_artifact.unlink(missing_ok=True)
    finally:
        compiled_module.parent.mkdir(parents=True, exist_ok=True)
        compiled_module.write_bytes(compiled_bytes)
        (build_root / "bytecode_modules" / "provenance-unrelated.mv").unlink(missing_ok=True)

    package_mode = package_anchor.stat().st_mode & 0o7777
    build_mode = compiled_module.stat().st_mode & 0o7777
    try:
        os.chmod(package_anchor, package_mode ^ 0o100)
        expect_failure("changed-package-mode", validate_current)
        os.chmod(package_anchor, package_mode)
        os.chmod(compiled_module, build_mode ^ 0o100)
        expect_failure("changed-build-artifact-mode", validate_current)
    finally:
        os.chmod(package_anchor, package_mode)
        os.chmod(compiled_module, build_mode)

    escape_target = package_root.parent / "provenance-escape-target"
    escape_link = package_root / "provenance-escape-link"
    escape_target.write_bytes(b"outside package root\n")
    try:
        escape_link.symlink_to(os.path.relpath(escape_target, package_root))
        expect_failure("escaping-symlink-target", validate_current)
    finally:
        escape_link.unlink(missing_ok=True)
        escape_target.unlink(missing_ok=True)

    collision_a = package_root / "ProvenanceCollision"
    collision_b = package_root / "provenancecollision"
    try:
        collision_a.write_bytes(b"a")
        collision_b.write_bytes(b"b")
        expect_failure("path-normalization-collision", validate_current)
    finally:
        collision_a.unlink(missing_ok=True)
        collision_b.unlink(missing_ok=True)

    special_path = package_root / "provenance-special-file"
    try:
        os.mkfifo(special_path)
        expect_failure("special-file", validate_current)
    finally:
        special_path.unlink(missing_ok=True)

    validate_current()
    return results


def _negative_contract_checks(
    dag_path: Path,
    skill_path: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    original_dag = dag_path.read_text(encoding="utf-8")
    dag_document = json.loads(original_dag)
    try:
        for name, mutate in (
            ("wrong-fqn", lambda document: document["vertices"][0]["kind"].__setitem__("tool_fqn", "not-a-fqn")),
            ("wrong-ports", lambda document: document["vertices"][0].__setitem__("entry_ports", [])),
            ("malformed-dag", None),
        ):
            if mutate is None:
                dag_path.write_text("{", encoding="utf-8")
            else:
                altered = json.loads(json.dumps(dag_document))
                mutate(altered)
                dag_path.write_text(json.dumps(altered), encoding="utf-8")
            try:
                _assert_dag_document(dag_path, expected_fqn=ONCHAIN_TOOL_FQN)
            except SourcePreparationError:
                results.append({"mutation": name, "rejected": True, "validator": "repository-owned"})
            else:
                raise SourcePreparationError(f"negative DAG mutation unexpectedly passed: {name}")
    finally:
        dag_path.write_text(original_dag, encoding="utf-8")

    original_skill = skill_path.read_text(encoding="utf-8")
    try:
        altered = json.loads(original_skill)
        altered["name"] = ""
        skill_path.write_text(json.dumps(altered), encoding="utf-8")
        try:
            _assert_skill_document(skill_path, expected_name="portable-skill")
        except SourcePreparationError:
            results.append({"mutation": "invalid-skill-config", "rejected": True, "validator": "repository-owned"})
        else:
            raise SourcePreparationError("negative skill mutation unexpectedly passed")
    finally:
        skill_path.write_text(original_skill, encoding="utf-8")
    return results


def _guarded_environment(workspace: Path) -> dict[str, str]:
    trace_bin = shutil.which("strace")
    if trace_bin is None:
        raise SourcePreparationError(
            "credible forward network enforcement is unavailable: strace syscall tracing is not installed"
        )
    isolation_bin = shutil.which("bwrap")
    if isolation_bin is None:
        raise SourcePreparationError(
            "credible forward network enforcement is unavailable: bwrap network isolation is not installed"
        )
    trace_root = workspace / "observations"
    trace_root.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workspace / "home"),
        "NEXUS_PORTABILITY_ALLOWED_ROOTS": json.dumps([str(workspace.resolve())]),
        "NEXUS_PORTABILITY_NETWORK_POLICY": "deny",
        "NEXUS_PORTABILITY_TRACE_MODE": "strace-network",
        "NEXUS_PORTABILITY_TRACE_BIN": trace_bin,
        "NEXUS_PORTABILITY_TRACE_ROOT": str(trace_root),
        "NEXUS_PORTABILITY_TRACE_LOG": str(trace_root / "events.jsonl"),
        "NEXUS_PORTABILITY_ISOLATION_MODE": "bwrap-unshare-net",
        "NEXUS_PORTABILITY_ISOLATION_BIN": isolation_bin,
        "CARGO_NET_OFFLINE": "true",
        "SUI_MOVE_OFFLINE": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(workspace / "empty-git-config"),
    }
    # Keep the installed compiler toolchain available while isolating generated
    # files in this workspace. The Rust proof invokes rustc directly and never
    # performs Cargo dependency resolution.
    environment["RUSTUP_HOME"] = os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup"))
    return environment


def _prepare_sui_build_config(workspace: Path) -> Path:
    """Create an empty, non-wallet Sui config so Move builds never initialize one."""

    config_root = workspace / "sui-config"
    config_root.mkdir(parents=True, exist_ok=True)
    keystore = config_root / "empty-keystore.yaml"
    config = config_root / "client.yaml"
    _write(keystore, "[]\n")
    _write(
        config,
        "keystore:\n"
        f"  File: {keystore}\n"
        "envs: []\n",
    )
    return config


def _sui_move_command(config: Path, action: str, project: Path) -> list[str]:
    """Return a non-mutating Move build/test command bound to the empty config."""

    command = [
        "sui",
        "move",
        "--client.config",
        str(config),
        action,
        "--path",
        str(project),
        "--build-env",
        "local",
        "--force",
    ]
    if action == "build":
        command.append("--disassemble")
    return command


class ForwardCleanupError(SourcePreparationError):
    """Report a forward workspace that could not be removed completely."""

    def __init__(self, workspace: Path, returncode: int, remaining: list[str], detail: str) -> None:
        self.workspace = str(workspace)
        self.returncode = returncode
        self.remaining = remaining
        self.detail = detail
        super().__init__(
            f"forward workspace cleanup failed (returncode={returncode}): {detail}; "
            f"recoverable_paths={remaining or [str(workspace)]}"
        )

    def details(self) -> dict[str, object]:
        return {
            "workspace": self.workspace,
            "returncode": self.returncode,
            "recoverable_paths": list(self.remaining),
            "detail": self.detail,
        }


def _remaining_workspace_paths(workspace: Path) -> list[str]:
    try:
        return [str(path) for path in sorted(workspace.rglob("*")) if path.exists()]
    except OSError:
        return [str(workspace)]


def _cleanup_forward_workspace(workspace: Path) -> None:
    """Remove only the private forward workspace and verify removal succeeded."""

    try:
        shutil.rmtree(workspace)
    except OSError as exc:
        remaining = _remaining_workspace_paths(workspace)
        raise ForwardCleanupError(workspace, int(exc.errno or 1), remaining, str(exc)) from exc
    if workspace.exists():
        remaining = _remaining_workspace_paths(workspace)
        raise ForwardCleanupError(workspace, 1, remaining, "workspace still exists after cleanup")


def _attach_forward_cleanup_failure(primary: BaseException, cleanup_error: ForwardCleanupError) -> None:
    details = cleanup_error.details()
    setattr(primary, "_forward_cleanup_error", details)
    try:
        primary.add_note(json.dumps({"forward_cleanup": details}, sort_keys=True))
    except (AttributeError, RuntimeError):
        pass


@contextmanager
def _tracked_forward_workspace() -> Iterator[Path]:
    """Track and clean a forward workspace for its complete lifetime, including signals."""

    workspace: Path | None = None
    cleanup_attempted = False
    cleanup_error: ForwardCleanupError | None = None

    def cleanup_once() -> None:
        nonlocal cleanup_attempted, cleanup_error
        if cleanup_attempted:
            return
        cleanup_attempted = True
        if workspace is not None:
            try:
                _cleanup_forward_workspace(workspace)
            except ForwardCleanupError as error:
                cleanup_error = error
                raise

    primary: BaseException | None = None
    signal_guard = _source_preparation_signal_guard(cleanup_once)
    signal_guard.__enter__()
    try:
        workspace = Path(tempfile.mkdtemp(prefix="nexus-forward-"))
        yield workspace
    except BaseException as error:
        primary = error
        raise
    finally:
        final_cleanup_error: ForwardCleanupError | None = cleanup_error
        try:
            with _ignore_source_preparation_signals():
                cleanup_once()
        except ForwardCleanupError as error:
            final_cleanup_error = error
        finally:
            signal_guard.__exit__(None, None, None)
        if final_cleanup_error is not None:
            if primary is not None:
                _attach_forward_cleanup_failure(primary, final_cleanup_error)
            else:
                raise final_cleanup_error


def _source_acquisition_evidence(manifest: Mapping[str, object]) -> dict[str, object]:
    """Record the exact approved archive inputs before execution isolation begins."""

    repositories = manifest.get("repositories")
    if not isinstance(repositories, Mapping) or set(repositories) != set(DEFAULT_REPOSITORIES):
        raise SourcePreparationError("forward source acquisition must contain exactly the approved public archives")
    records: list[dict[str, object]] = []
    for logical_name in sorted(DEFAULT_REPOSITORIES):
        expected = DEFAULT_REPOSITORIES[logical_name]
        record = repositories.get(logical_name)
        if not isinstance(record, Mapping):
            raise SourcePreparationError(f"forward source acquisition record is missing: {logical_name}")
        if (
            record.get("repository") != expected.repository
            or record.get("ref") != expected.ref
            or record.get("archive_url") != expected.archive_url
            or record.get("archive_sha256") != expected.archive_sha256
            or record.get("reviewed_archive_sha256") != expected.archive_sha256
        ):
            raise SourcePreparationError(f"forward source acquisition is not pinned: {logical_name}")
        records.append(
            {
                "logical_name": logical_name,
                "repository": expected.repository,
                "ref": expected.ref,
                "archive_url": expected.archive_url,
                "archive_sha256": expected.archive_sha256,
                "phase": "pre-execution-acquisition",
            }
        )
    return {
        "phase": "pre-execution-acquisition",
        "network_allowed": True,
        "repositories": records,
        "sui_toolchain": {
            "version": SUI_TOOLCHAIN_VERSION,
            "source_revision": SUI_REVISION,
            "archive_sha256": DEFAULT_REPOSITORIES["sui"].archive_sha256,
            "mapping": "installed version revision suffix equals the first 12 characters of the full source commit",
        },
        "execution_boundary": "starts after all approved archives are authenticated and copied",
    }


def run_forward_check() -> dict[str, Any]:
    workspace_lifetime = _tracked_forward_workspace()
    workspace = workspace_lifetime.__enter__()
    report: dict[str, Any] | None = None
    try:
        with prepared_sources(only=("nexus-sdk", "nexus-move-packages", "sui")) as manifest:
            source_manifest_path = Path(str(manifest["manifest"]))
            validated_manifest = load_manifest(source_manifest_path)
            validated_repositories = validated_manifest.get("repositories")
            if not isinstance(validated_repositories, Mapping):
                raise SourcePreparationError("forward source manifest has no validated repository records")
            roots = {
                name: Path(str(record["root"]))
                for name, record in validated_repositories.items()
                if isinstance(name, str) and isinstance(record, Mapping) and isinstance(record.get("root"), str)
            }
            if set(roots) != set(DEFAULT_REPOSITORIES):
                raise SourcePreparationError("forward source manifest root inventory is incomplete")
            sdk_source_root = roots["nexus-sdk"]
            move_source_root = roots["nexus-move-packages"]
            sui_source_root = roots["sui"]
            source_acquisition = _source_acquisition_evidence(manifest)
            sdk_root, sdk_copy_evidence = _copy_authenticated_source(
                manifest,
                "nexus-sdk",
                sdk_source_root,
                workspace / "authenticated-sources/nexus-sdk",
                workspace,
            )
            move_root, move_copy_evidence = _copy_authenticated_source(
                manifest,
                "nexus-move-packages",
                move_source_root,
                workspace / "authenticated-sources/nexus-move-packages",
                workspace,
            )
            move_package_roots = _public_move_package_roots(move_root)
            interface_identity = _approved_public_interface_identity(move_root)
            source_acquisition["approved_interface"] = dict(interface_identity)
            semantic_invariant = {
                **ONCHAIN_TOOL_SEMANTIC_INVARIANT,
                "authorization_package_address": interface_identity["package_address"],
                "authorization_call": interface_identity["call"],
                "authorization_module_id": interface_identity["module_id"],
            }
            consumers = workspace / "consumer"
            rust_project = consumers / "rust-tool"
            move_project = consumers / "tap"
            move_logic_test_project = consumers / "tap-logic-test"
            onchain_project = consumers / "onchain-tool"
            onchain_logic_test_project = consumers / "onchain-tool-logic-test"
            intended_dag = _generate_dag_document()
            intended_skill = _generate_skill_document()
            semantic_intent = _semantic_intent(intended_dag, intended_skill)
            _rust_tool(rust_project, sdk_root)
            fixture_root = Path(__file__).resolve().parents[1] / "nexus-tap-development/fixtures"
            fixture_projects: dict[str, Path] = {}
            for fixture_name in ("direct", "delayed"):
                source = fixture_root / fixture_name
                if not source.is_dir():
                    raise SourcePreparationError(f"repository-owned TAP fixture is missing: {fixture_name}")
                destination = consumers / f"fixture-{fixture_name}"
                shutil.copytree(source, destination)
                fixture_projects[fixture_name] = destination
            fixture_framework_evidence = {
                name: _prepare_repository_fixture(project, sui_source_root)
                for name, project in fixture_projects.items()
            }
            tap_framework_evidence = _move_tap(
                move_project,
                move_root,
                dag_document=intended_dag,
                skill_document=intended_skill,
                sui_root=sui_source_root,
            )
            _validate_generated_semantic_artifacts(move_project, semantic_intent)
            tap_logic_framework_evidence = _move_tap_logic_test(
                move_logic_test_project, move_project, sui_source_root
            )
            onchain_framework_evidence = _onchain_tool(
                onchain_project,
                move_root,
                sui_source_root,
                interface_address=interface_identity["package_address"],
            )
            onchain_logic_framework_evidence = _move_onchain_logic_test(
                onchain_logic_test_project, onchain_project, sui_source_root
            )
            manifest_closure = _validate_generated_manifest_closure(
                consumers,
                workspace,
                roots=(
                    rust_project / "rust-proof-manifest.json",
                    move_project / "tap/Move.toml",
                    move_logic_test_project / "Move.toml",
                    onchain_project / "Move.toml",
                    onchain_logic_test_project / "Move.toml",
                    *(project / "Move.toml" for project in fixture_projects.values()),
                ),
            )
            environment = _guarded_environment(workspace)
            sui_config = _prepare_sui_build_config(workspace)
            environment["NEXUS_PORTABILITY_SUI_CLIENT_CONFIG"] = str(sui_config)
            command_runner = _run_isolated_subprocess
            rust_trace = _assert_closed_rust_proof(rust_project, sdk_root, workspace)
            rust_binary = rust_project / "portable-forward-tool-tests"
            rust_compile_command = [
                "rustc",
                "--edition=2021",
                "--test",
                str(rust_project / "src/lib.rs"),
                "-o",
                str(rust_binary),
            ]
            rust_test_command = [str(rust_binary)]
            _assert_closed_rust_commands(
                [{"command": rust_compile_command}, {"command": rust_test_command}], environment
            )
            commands = [
                _run(rust_compile_command, workspace, environment),
                _run(rust_test_command, workspace, environment),
            ]
            move_commands: dict[str, list[str]] = {}
            move_test_counts: dict[str, int] = {}
            onchain_build_command = _sui_move_command(sui_config, "build", onchain_project)
            move_commands["onchain_tool_build"] = onchain_build_command
            commands.append(_run(onchain_build_command, workspace, environment))
            _assert_public_interface_lock_binding(onchain_project)
            manifest_closure = _validate_generated_manifest_closure(
                consumers,
                workspace,
                roots=(
                    rust_project / "rust-proof-manifest.json",
                    move_project / "tap/Move.toml",
                    move_logic_test_project / "Move.toml",
                    onchain_project / "Move.toml",
                    onchain_logic_test_project / "Move.toml",
                    *(project / "Move.toml" for project in fixture_projects.values()),
                ),
            )
            for name, project in (
                ("tap", move_project / "tap"),
                ("tap_logic", move_logic_test_project),
                ("onchain_tool", onchain_logic_test_project),
            ):
                build_command = _sui_move_command(sui_config, "build", project)
                test_command = _sui_move_command(sui_config, "test", project)
                move_commands[f"{name}_build"] = build_command
                move_commands[f"{name}_test"] = test_command
                commands.append(_run(build_command, workspace, environment))
                test_result = _run_move_test(test_command, workspace, environment)
                commands.append(test_result)
                move_test_counts[name] = int(test_result["move_test_count"])
            if set(move_test_counts) != {"tap", "tap_logic", "onchain_tool"} or any(
                count < 1 for count in move_test_counts.values()
            ):
                raise SourcePreparationError("forward Move test report must contain a positive count for every consumer")
            fixture_test_counts: dict[str, int] = {}
            tap_verifier = str(
                Path(__file__).resolve().parents[1] / "nexus-tap-development/scripts/verify_tap_artifacts.py"
            )
            for fixture_name, project in fixture_projects.items():
                verifier_command = [sys.executable, tap_verifier, str(project), "--require-artifacts", "--json"]
                commands.append(_run(verifier_command, workspace, environment))
                build_command = _sui_move_command(sui_config, "build", project)
                test_command = _sui_move_command(sui_config, "test", project)
                move_commands[f"fixture_{fixture_name}_build"] = build_command
                move_commands[f"fixture_{fixture_name}_test"] = test_command
                commands.append(_run(build_command, workspace, environment))
                test_result = _run_move_test(test_command, workspace, environment)
                commands.append(test_result)
                fixture_test_counts[fixture_name] = int(test_result["move_test_count"])
            if set(fixture_test_counts) != {"direct", "delayed"} or any(
                count < 1 for count in fixture_test_counts.values()
            ):
                raise SourcePreparationError("repository-owned direct/delayed TAP fixtures must have positive Move test counts")
            build_root = onchain_project / "build" / "portable_tool"
            summary_root = consumers / "onchain-summary"
            try:
                build_receipt = capture_build_receipt(
                    package_root=onchain_project,
                    build_root=build_root,
                    module_name=ONCHAIN_TOOL_MODULE,
                    function_name=ONCHAIN_TOOL_FUNCTION,
                    environment=environment,
                    command_runner=command_runner,
                    tree_manifest_root=consumers / "provenance",
                )
            except CompiledMoveInvariantError as exc:
                raise SourcePreparationError(f"successful Tool build has no trusted provenance receipt: {exc}") from exc
            _assert_sui_toolchain_version(str(build_receipt.get("toolchain", {}).get("version", "")))
            summary_path = _write_onchain_summary(summary_root, str(build_receipt["module_address"]))
            summary_evidence = _assert_onchain_summary(summary_root)
            schema_digest = canonical_schema_digest(
                module_address=str(build_receipt["module_address"]),
                module=ONCHAIN_TOOL_MODULE,
                function=ONCHAIN_TOOL_FUNCTION,
                output_enum=ONCHAIN_TOOL_OUTPUT_ENUM,
                input_ports=list(ONCHAIN_TOOL_INPUT_PORTS),
                output_ports=ONCHAIN_TOOL_OUTPUT_PORTS,
                parameters=summary_evidence["parameters"],
                output_variants=summary_evidence["output_variants"],
            )
            build_receipt = seal_build_receipt(
                {
                    **dict(build_receipt),
                    "schema_summary": str(summary_path.resolve()),
                    "schema_summary_root": str(summary_path.parent.resolve()),
                    "schema_summary_sha256": sha256_file(summary_path),
                    "schema_digest": schema_digest,
                }
            )
            receipt_acceptance_sha256 = receipt_acceptance_digest(build_receipt)
            expected_intent = _with_artifact_semantic_intent(
                _artifact_intent(build_receipt, summary_evidence), semantic_intent
            )
            artifact_path = _write_artifact_identity(
                consumers, summary_evidence, build_receipt, expected_intent, semantic_invariant
            )
            # The first receipt supplies the identity needed to create the
            # generated metadata/manifest. Refresh it once those files exist
            # so the package tree snapshot covers every package entry while
            # the receipt itself remains outside that root.
            build_receipt = capture_build_receipt(
                package_root=onchain_project,
                build_root=build_root,
                module_name=ONCHAIN_TOOL_MODULE,
                function_name=ONCHAIN_TOOL_FUNCTION,
                environment=environment,
                command_runner=command_runner,
                schema_summary=summary_path,
                schema_summary_root=summary_path.parent,
                schema_digest=schema_digest,
                tree_manifest_root=consumers / "provenance",
            )
            _assert_sui_toolchain_version(str(build_receipt.get("toolchain", {}).get("version", "")))
            expected_intent = _with_artifact_semantic_intent(
                _artifact_intent(build_receipt, summary_evidence), semantic_intent
            )
            artifact_path = _write_artifact_identity(
                consumers, summary_evidence, build_receipt, expected_intent, semantic_invariant
            )
            receipt_acceptance_sha256 = receipt_acceptance_digest(build_receipt)
            semantic_evidence = _validate_semantic_artifact(
                onchain_project,
                build_receipt,
                expected_intent,
                receipt_acceptance_sha256,
                environment,
                command_runner,
                semantic_invariant,
            )
            artifact_evidence = validate_artifact_consistency(
                artifact_path,
                expected_intent=expected_intent,
                build_receipt=build_receipt,
                expected_receipt_sha256=receipt_acceptance_sha256,
                expected_package_tree_sha256=str(build_receipt["package_tree_sha256"]),
                expected_build_tree_sha256=str(build_receipt["build_tree_sha256"]),
            )
            semantic_negative_validations, build_receipt = _negative_semantic_checks(
                onchain_project,
                environment,
                expected_intent,
                summary_path,
                onchain_build_command,
                command_runner,
                semantic_invariant,
            )
            receipt_acceptance_sha256 = receipt_acceptance_digest(build_receipt)
            expected_intent = _with_artifact_semantic_intent(
                _artifact_intent(build_receipt, summary_evidence), semantic_intent
            )
            artifact_path = _write_artifact_identity(
                consumers, summary_evidence, build_receipt, expected_intent, semantic_invariant
            )
            semantic_evidence = _validate_semantic_artifact(
                onchain_project,
                build_receipt,
                expected_intent,
                receipt_acceptance_sha256,
                environment,
                command_runner,
                semantic_invariant,
            )
            artifact_evidence = validate_artifact_consistency(
                artifact_path,
                expected_intent=expected_intent,
                build_receipt=build_receipt,
                expected_receipt_sha256=receipt_acceptance_sha256,
                expected_package_tree_sha256=str(build_receipt["package_tree_sha256"]),
                expected_build_tree_sha256=str(build_receipt["build_tree_sha256"]),
            )
            provenance_negative_validations = _negative_provenance_checks(
                onchain_project,
                build_receipt,
                expected_intent,
                receipt_acceptance_sha256,
            )
            artifact_negative_validations = _negative_artifact_checks(
                artifact_path,
                expected_intent,
                build_receipt,
                receipt_acceptance_sha256,
            )
            tap_artifact_command = [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "nexus-tap-development/scripts/verify_tap_artifacts.py"),
                str(move_project),
                "--require-artifacts",
                "--json",
            ]
            _assert_dag_document(move_project / "portable.dag.json", expected_fqn=ONCHAIN_TOOL_FQN)
            _assert_skill_document(move_project / "portable.skill.tap.json", expected_name="portable-skill")
            commands.append(_run(tap_artifact_command, workspace, environment))
            structural_negative_validations = _negative_contract_checks(
                move_project / "portable.dag.json",
                move_project / "portable.skill.tap.json",
            )
            output_payload_negative_validations = _negative_output_payload_checks(onchain_project)
            manifest_closure = _validate_generated_manifest_closure(
                consumers,
                workspace,
                roots=(
                    rust_project / "rust-proof-manifest.json",
                    move_project / "tap/Move.toml",
                    move_logic_test_project / "Move.toml",
                    onchain_project / "Move.toml",
                    onchain_logic_test_project / "Move.toml",
                    *(project / "Move.toml" for project in fixture_projects.values()),
                ),
            )
            _assert_closed_rust_commands(commands, environment)
            rust_trace["commands"] = [
                list(result["command"])
                for result in commands
                if isinstance(result, Mapping) and isinstance(result.get("command"), list)
            ]
            rust_trace["environment_keys"] = sorted(environment)
            rust_trace["manifest_closure"] = manifest_closure
            observed_trace = _observed_forward_trace(workspace, manifest_closure)
            _assert_observed_toolchain_commands(observed_trace)
            rust_trace["observation"] = observed_trace
            rust_trace["commands"] = list(observed_trace["commands"])
            rust_trace["network_access"] = observed_trace["network_access"]
            rust_trace["external_dependencies"] = observed_trace["external_dependencies"]
            rust_trace["acquisition_events"] = observed_trace["acquisition_events"]
            _write(rust_project / "source-trace.json", json.dumps(rust_trace, indent=2, sort_keys=True) + "\n")
            testnet = SuiTestnetEvidenceClient("https://graphql.testnet.sui.io/graphql")
            testnet_evidence = testnet.collect(
                package_id="0x2",
                object_id="0x6",
                module="coin",
                function="balance",
                struct="Coin",
            )
            # Revalidate the authenticated extraction roots after all builds;
            # the generated copies, not these roots, are the only build inputs.
            load_manifest(source_manifest_path)
            _sui_framework_package_roots(sui_source_root)
            framework_copies = {
                "tap": tap_framework_evidence,
                "tap_logic": tap_logic_framework_evidence,
                "onchain_tool": onchain_framework_evidence,
                "onchain_tool_logic": onchain_logic_framework_evidence,
                "fixture_direct": fixture_framework_evidence["direct"],
                "fixture_delayed": fixture_framework_evidence["delayed"],
            }
            if any(not isinstance(value, Mapping) for value in framework_copies.values()):
                raise SourcePreparationError("forward report is missing copied Sui framework evidence")
            report = {
                "status": "passed",
                "source_acquisition": source_acquisition,
                "manifest_closure": manifest_closure,
                "sources": {
                    "nexus-sdk": {
                        **dict(manifest["repositories"]["nexus-sdk"]),
                        "verified_copy_root": sdk_copy_evidence["root"],
                        "verified_copy_tree_sha256": sdk_copy_evidence["tree_sha256"],
                    },
                    "nexus-move-packages": {
                        **dict(manifest["repositories"]["nexus-move-packages"]),
                        "verified_copy_root": move_copy_evidence["root"],
                        "verified_copy_tree_sha256": move_copy_evidence["tree_sha256"],
                    },
                    "sui": {
                        **dict(manifest["repositories"]["sui"]),
                        "framework_package_paths": list(SUI_FRAMEWORK_PACKAGE_PATHS),
                        "framework_copies": framework_copies,
                    },
                },
                "move_interface_authority": {
                    "logical_repository": "nexus-move-packages",
                    "approved_package_identity": interface_identity,
                    "packages": [
                        {
                            "name": name,
                            "repository_relative_path": f"packages/{name}",
                            "root": str(move_package_roots[name]),
                            "published_metadata": str(move_package_roots[name] / "Published.toml"),
                        }
                        for name in PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES
                    ],
                    "generated_dependency_paths": {
                        "portable_tool": {
                            "nexus_primitives": "deps/primitives",
                            "nexus_interface": "deps/interface",
                            "std": "deps/sui-framework/packages/move-stdlib",
                            "sui": "deps/sui-framework/packages/sui-framework",
                        },
                        "portable_tap_logic_test": {
                            "std": "deps/sui-framework/packages/move-stdlib",
                            "sui": "deps/sui-framework/packages/sui-framework",
                        },
                        "portable_tap": {
                            "std": "tap/deps/sui-framework/packages/move-stdlib",
                            "sui": "tap/deps/sui-framework/packages/sui-framework",
                        },
                        "portable_tool_logic_test": {
                            "std": "deps/sui-framework/packages/move-stdlib",
                            "sui": "deps/sui-framework/packages/sui-framework",
                        },
                    },
                    "implementation_dependency_paths": "forbidden",
                    "kernel": {
                        "repository_relative_path": "packages/kernel",
                        "role": "transitive support closure; not a direct application dependency",
                    },
                },
                "generated": {
                        "rust_tool": str(rust_project),
                        "move_tap": str(move_project),
                        "tap_artifacts": str(move_project / "artifacts"),
                        "dag": str(move_project / "artifacts/portable.dag.json"),
                        "skill": str(move_project / "artifacts/portable.skill.tap.json"),
                        "move_tap_logic_test": str(move_logic_test_project),
                        "onchain_tool": str(onchain_project),
                        "onchain_tool_logic_test": str(onchain_logic_test_project),
                        "fixture_direct": str(fixture_projects["direct"]),
                        "fixture_delayed": str(fixture_projects["delayed"]),
                    },
                "rust_proof_trace": rust_trace,
                "execution_observation": observed_trace,
                "commands": commands,
                "observed_commands": observed_trace["commands"],
                "move_test_counts": move_test_counts,
                "fixture_test_counts": fixture_test_counts,
                "fixture_contracts": {
                    name: {
                        "dependency_template": evidence["fixture_contract"]["dependency_template"],
                        "rewrite": evidence["rewrite"],
                        "test_contract": evidence["fixture_contract"]["test_contract"],
                    }
                    for name, evidence in fixture_framework_evidence.items()
                },
                "move_test_projects": {
                    "tap": str(move_project / "tap"),
                    "tap_logic": str(move_logic_test_project),
                    "onchain_tool": str(onchain_logic_test_project),
                    "fixture_direct": str(fixture_projects["direct"]),
                    "fixture_delayed": str(fixture_projects["delayed"]),
                },
                "testnet_evidence": {
                    "endpoint": testnet_evidence["endpoint"],
                    "network": testnet_evidence["network"],
                    "calls": testnet_evidence["calls"],
                    "sha256": testnet_evidence["sha256"],
                    "observed": [
                        "chain_identifier",
                        "latest_checkpoint",
                        "object",
                        "package",
                        "module",
                        "function",
                        "struct",
                    ],
                },
                "validation": {
                        "tap_artifacts": "passed",
                        "compiled_move": "passed",
                        "artifact_consistency": "passed",
                        "fixture_artifacts": "passed",
                    },
                "schema_summary": summary_evidence,
                "compiled_semantic_invariant": semantic_evidence,
                "artifact_consistency": artifact_evidence,
                "negative_validations": [
                    *semantic_negative_validations,
                    *provenance_negative_validations,
                    *artifact_negative_validations,
                    *structural_negative_validations,
                    *output_payload_negative_validations,
                ],
                "guard": {
                    "cwd": str(workspace),
                    "allowed_roots": [str(workspace.resolve())],
                    "pre_existing_source_discovery": "not used",
                    "wallet_or_environment_config": "empty non-wallet build config only; no environment selected",
                    "network_mutation": "not used",
                },
                "cleanup": "pending",
            }
            return report
    finally:
        try:
            workspace_lifetime.__exit__(*sys.exc_info())
        except ForwardCleanupError:
            if report is not None:
                report["cleanup"] = "failed"
            raise
        if report is not None:
            report["cleanup"] = "completed"


def main() -> int:
    try:
        report = run_forward_check()
    except ForwardCleanupError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": str(exc),
                    "cleanup": "failed",
                    "cleanup_error": exc.details(),
                    "cleanup_workspace": exc.workspace,
                    "cleanup_returncode": exc.returncode,
                    "cleanup_recoverable_paths": exc.remaining,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except (SourcePreparationError, TestnetEvidenceError, OSError, ValueError) as exc:
        payload: dict[str, object] = {
            "status": "blocked",
            "error": str(exc),
            "cleanup": "attempted",
        }
        cleanup_error = getattr(exc, "_forward_cleanup_error", None)
        if isinstance(cleanup_error, Mapping):
            payload["cleanup"] = "failed"
            payload["cleanup_error"] = dict(cleanup_error)
        observation = getattr(exc, "forward_observation", None)
        if isinstance(observation, Mapping):
            payload["observed_network_attempt"] = dict(observation)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
