#!/usr/bin/env python3
"""Validate compiled Move semantics against a digest-bound build receipt.

The public validator trusts only an explicit acceptance contract, the selected
Sui toolchain, and the bytes emitted by the successful build. It never treats
a caller-provided ``.mvb`` text file as authoritative: the actual compiled
``.mv`` module is hashed, an injected observed runner executes ``sui move
disassemble`` and ``sui --version``, and the derived representation is parsed
before the call-graph checks run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping, Sequence

from tree_integrity import (
    TreeIntegrityError,
    tree_digest as _descriptor_tree_digest,
    tree_entries as _descriptor_tree_entries,
    validate_root as _descriptor_validate_root,
    validate_tree_manifest as _descriptor_validate_tree_manifest,
    write_tree_manifest as _descriptor_write_tree_manifest,
)


RECEIPT_SCHEMA_VERSION = 1
TREE_MANIFEST_SCHEMA_VERSION = 1


class CompiledMoveInvariantError(ValueError):
    """Raised when a compiled Move artifact violates its trust or Tool invariant."""


@dataclass(frozen=True)
class ObservedCommandResult:
    """Captured result returned by the caller-owned isolated command runner."""

    returncode: int
    stdout: bytes
    stderr: bytes
    observation: Mapping[str, object]


CommandRunner = Callable[[Sequence[str], Path, Mapping[str, str] | None], ObservedCommandResult]


@dataclass(frozen=True)
class Instruction:
    offset: int
    opcode: str
    operands: str
    callee_module_id: str | None = None

    @property
    def call_target(self) -> str | None:
        if self.opcode != "Call":
            return None
        match = CALL_TARGET_RE.match(self.operands)
        return f"{match.group(1)}::{match.group(2)}" if match else None

    @property
    def call_target_id(self) -> str | None:
        if self.opcode != "Call" or self.callee_module_id is None:
            return None
        match = CALL_TARGET_RE.match(self.operands)
        return f"{self.callee_module_id}::{match.group(2)}" if match else None

    @property
    def encoded(self) -> str:
        return f"{self.opcode} {self.operands}" if self.opcode == "Call" else f"{self.opcode}{self.operands}"


@dataclass(frozen=True)
class CompiledFunction:
    module_name: str
    name: str
    signature: str
    parameters: tuple[tuple[str, str], ...]
    instructions: tuple[Instruction, ...]


@dataclass(frozen=True)
class CompiledModule:
    address: str
    name: str
    functions: Mapping[str, CompiledFunction]
    module_aliases: Mapping[str, str | None] = field(default_factory=dict)


MODULE_RE = re.compile(r"^module (?P<address>[^.]+)\.(?P<name>[A-Za-z_][A-Za-z0-9_]*) \{$")
USE_RE = re.compile(
    r"^\s*use\s+(?P<address>[A-Za-z_][A-Za-z0-9_]*|(?:0x)?[0-9A-Fa-f]+)"
    r"::(?P<module>[A-Za-z_][A-Za-z0-9_]*)(?:\s+as\s+(?P<alias>[A-Za-z0-9_]+))?;\s*$"
)
CALL_TARGET_RE = re.compile(
    r"(?P<module>[A-Za-z0-9_]+)::(?P<function>[A-Za-z_][A-Za-z0-9_]*)(?:<[^>]*>)?\("
)
COMPILED_ADDRESS_RE = re.compile(r"(?:0x)?[0-9A-Fa-f]{1,64}\Z")
FUNCTION_RE = re.compile(
    r"^(?P<visibility>(?:(?:public|entry)\s+)*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<parameters>.*)\)"
    r"(?:\s*->\s*.*)?\s*\{$"
)
INSTRUCTION_RE = re.compile(r"^\s*(?P<offset>\d+):\s+(?P<body>.+)$")
LOCAL_RE = re.compile(
    r"(?:MoveLoc|CopyLoc|MutBorrowLoc|ImmBorrowLoc)\[\d+\]\("
    r"(?P<name>[^:#(]+)(?:#[^:]*)?:\s*(?P<type>[^)]+)\)"
)
STORE_LOCAL_RE = re.compile(
    r"StLoc\[\d+\]\((?P<name>[^:#(]+)(?:#[^:]*)?:\s*(?P<type>[^)]+)\)"
)
FIELD_RE = re.compile(
    r"(?:ImmBorrowField|MutBorrowField)\[\d+\]\("
    r"(?P<field>[^:]+):\s*(?P<type>[^)]+)\)"
)
BRANCH_RE = re.compile(r"^(?:BrFalse|BrTrue|Branch|Jump)\((?P<target>\d+)\)")


def _canonical_json_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    """Return the canonical SHA-256 digest used in build and artifact receipts."""

    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, RuntimeError) as exc:
        raise CompiledMoveInvariantError(f"cannot hash compiled provenance file: {path}") from exc


def seal_build_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    """Return a receipt with a self-digest over every other receipt field."""

    sealed = dict(receipt)
    sealed.pop("receipt_sha256", None)
    sealed["receipt_sha256"] = sha256_bytes(_canonical_json_bytes(sealed))
    return sealed


def _build_info_package_name(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("  package_name:"):
                value = line.split(":", 1)[1].strip()
                if value:
                    return value
    except (OSError, RuntimeError) as exc:
        raise CompiledMoveInvariantError(f"BuildInfo is unreadable: {path}") from exc
    raise CompiledMoveInvariantError(f"BuildInfo has no package_name: {path}")


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CompiledMoveInvariantError(f"build receipt {label} is missing")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise CompiledMoveInvariantError(f"build receipt {label} must be an absolute runtime path")
    if candidate.is_symlink():
        raise CompiledMoveInvariantError(f"build receipt {label} must not be a symlink")
    return candidate.resolve()


def _contained(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CompiledMoveInvariantError(f"build receipt {label} escapes its recorded root") from exc


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CompiledMoveInvariantError(f"build receipt {label} is not a regular file: {path}")


def _digest_value(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CompiledMoveInvariantError(f"build receipt {label} is not a SHA-256 digest")
    return value


def receipt_acceptance_digest(receipt: Mapping[str, object]) -> str:
    """Return the caller-held digest over a complete sealed receipt.

    ``receipt_sha256`` is deliberately only a self-seal.  The acceptance
    digest is computed by the caller after the successful build and must not
    be written into the receipt or any generated artifact.
    """

    if not isinstance(receipt, Mapping):
        raise CompiledMoveInvariantError("build receipt must be a JSON object")
    try:
        return sha256_bytes(_canonical_json_bytes(dict(receipt)))
    except (TypeError, ValueError) as exc:
        raise CompiledMoveInvariantError("build receipt cannot be canonically digested") from exc


def _manifest_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CompiledMoveInvariantError(f"{label} is missing")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise CompiledMoveInvariantError(f"{label} must be an absolute runtime path")
    if candidate.is_symlink():
        raise CompiledMoveInvariantError(f"{label} must not be a symlink")
    try:
        return candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise CompiledMoveInvariantError(f"{label} is unreadable: {candidate}") from exc


def _outside_root(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        return
    raise CompiledMoveInvariantError(f"{label} must be outside the recorded root")


def _validated_root(root: Path, label: str) -> Path:
    try:
        return _descriptor_validate_root(root, label)
    except TreeIntegrityError as exc:
        raise CompiledMoveInvariantError(str(exc)) from exc


def _tree_entries(root: Path) -> list[dict[str, object]]:
    try:
        return _descriptor_tree_entries(root)
    except TreeIntegrityError as exc:
        raise CompiledMoveInvariantError(str(exc)) from exc


def _tree_digest(entries: Sequence[Mapping[str, object]]) -> str:
    try:
        return _descriptor_tree_digest(entries)
    except (TypeError, ValueError) as exc:
        raise CompiledMoveInvariantError("tree entries cannot be canonically digested") from exc


def write_tree_manifest(root: Path, manifest_path: Path) -> dict[str, object]:
    try:
        return _descriptor_write_tree_manifest(root, manifest_path)
    except TreeIntegrityError as exc:
        raise CompiledMoveInvariantError(str(exc)) from exc


def validate_tree_manifest(
    root: Path,
    manifest_path: Path,
    expected_tree_sha256: str,
    label: str,
) -> dict[str, object]:
    try:
        return _descriptor_validate_tree_manifest(root, manifest_path, expected_tree_sha256, label)
    except TreeIntegrityError as exc:
        raise CompiledMoveInvariantError(str(exc)) from exc


def make_build_receipt(
    *,
    package_root: Path,
    build_root: Path,
    compiled_module: Path,
    module_address: str,
    module_name: str,
    function_name: str,
    trusted_disassembly_sha256: str,
    toolchain: Mapping[str, object],
    disassembly_path: Path | None = None,
    schema_summary: Path | None = None,
    schema_summary_root: Path | None = None,
    schema_digest: str | None = None,
    tree_manifest_root: Path | None = None,
    package_tree_manifest: Path | None = None,
    build_tree_manifest: Path | None = None,
) -> dict[str, object]:
    """Create the digest-bound receipt captured immediately after a build."""

    package_root = _validated_root(package_root, "package root")
    build_root = _validated_root(build_root, "build root")
    _contained(build_root, package_root, "build root")
    if tree_manifest_root is not None:
        if package_tree_manifest is not None or build_tree_manifest is not None:
            raise CompiledMoveInvariantError("tree manifest paths cannot be combined with tree_manifest_root")
        tree_manifest_root = Path(tree_manifest_root).resolve()
        package_tree_manifest = tree_manifest_root / "package-tree.json"
        build_tree_manifest = tree_manifest_root / "build-tree.json"
    if package_tree_manifest is None or build_tree_manifest is None:
        raise CompiledMoveInvariantError("external package and build tree manifest paths are required")
    package_tree_manifest = _manifest_path(str(package_tree_manifest), "package tree manifest")
    build_tree_manifest = _manifest_path(str(build_tree_manifest), "build tree manifest")
    if package_tree_manifest == build_tree_manifest:
        raise CompiledMoveInvariantError("package and build tree manifests must be distinct")
    _outside_root(package_tree_manifest, package_root, "package tree manifest")
    _outside_root(package_tree_manifest, build_root, "package tree manifest")
    _outside_root(build_tree_manifest, package_root, "build tree manifest")
    _outside_root(build_tree_manifest, build_root, "build tree manifest")
    if compiled_module.is_symlink():
        raise CompiledMoveInvariantError("compiled module must not be a symlink")
    compiled_module = compiled_module.resolve()
    build_info = build_root / "BuildInfo.yaml"
    if build_info.is_symlink():
        raise CompiledMoveInvariantError("build provenance files must not be symlinks")
    _regular_file(compiled_module, "compiled module")
    _regular_file(build_info, "BuildInfo")
    _contained(compiled_module, build_root, "compiled module")
    _contained(build_info.resolve(), build_root, "BuildInfo")
    package_name = _build_info_package_name(build_info)
    package_tree = write_tree_manifest(package_root, package_tree_manifest)
    build_tree = write_tree_manifest(build_root, build_tree_manifest)
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "package_root": str(package_root),
        "build_root": str(build_root),
        "compiled_module": str(compiled_module),
        "compiled_sha256": sha256_file(compiled_module),
        "module_id": f"{module_address}.{module_name}",
        "module_address": module_address,
        "module_name": module_name,
        "function_name": function_name,
        "package_name": package_name,
        "build_info": str(build_info.resolve()),
        "build_info_sha256": sha256_file(build_info),
        "trusted_disassembly_sha256": trusted_disassembly_sha256,
        "toolchain": dict(toolchain),
        "package_tree_manifest": package_tree["path"],
        "package_tree_sha256": package_tree["tree_sha256"],
        "build_tree_manifest": build_tree["path"],
        "build_tree_sha256": build_tree["tree_sha256"],
    }
    if disassembly_path is not None:
        if disassembly_path.is_symlink():
            raise CompiledMoveInvariantError("saved disassembly must not be a symlink")
        disassembly_path = disassembly_path.resolve()
        _regular_file(disassembly_path, "saved disassembly")
        _contained(disassembly_path, build_root, "saved disassembly")
        receipt["disassembly"] = str(disassembly_path)
        receipt["disassembly_sha256"] = sha256_file(disassembly_path)
    if schema_summary is not None:
        if schema_summary.is_symlink():
            raise CompiledMoveInvariantError("schema summary must not be a symlink")
        schema_summary = schema_summary.resolve()
        _regular_file(schema_summary, "schema summary")
        summary_root = (schema_summary_root or package_root).resolve()
        _contained(summary_root, package_root.parent, "schema summary root")
        _contained(schema_summary, summary_root, "schema summary")
        receipt["schema_summary_root"] = str(summary_root)
        receipt["schema_summary"] = str(schema_summary)
        receipt["schema_summary_sha256"] = sha256_file(schema_summary)
    if schema_digest is not None:
        receipt["schema_digest"] = schema_digest
    return seal_build_receipt(receipt)


def validate_build_receipt(
    receipt: Mapping[str, object],
    *,
    package_root: Path | None = None,
    build_root: Path | None = None,
    expected_compiled_sha256: str | None = None,
    expected_compiled_digest: str | None = None,
    expected_module_id: str | None = None,
    expected_module_address: str | None = None,
    expected_module_name: str | None = None,
    expected_function_name: str | None = None,
    expected_build_info_sha256: str | None = None,
    expected_schema_digest: str | None = None,
    expected_receipt_sha256: str | None = None,
    expected_package_tree_sha256: str | None = None,
    expected_build_tree_sha256: str | None = None,
    expected_package_tree_manifest: Path | None = None,
    expected_build_tree_manifest: Path | None = None,
) -> dict[str, object]:
    """Verify receipt integrity, roots, tree snapshots, BuildInfo, and compiled bytes."""

    expected_compiled_sha256 = expected_compiled_sha256 or expected_compiled_digest
    if not isinstance(receipt, Mapping):
        raise CompiledMoveInvariantError("build receipt must be a JSON object")
    if expected_receipt_sha256 is None:
        raise CompiledMoveInvariantError("an external expected build receipt digest is required")
    expected_receipt_sha256 = _digest_value(expected_receipt_sha256, "expected receipt SHA-256")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise CompiledMoveInvariantError("build receipt has an unsupported schema version")
    receipt_digest = receipt.get("receipt_sha256")
    try:
        expected_self_digest = sha256_bytes(
            _canonical_json_bytes({key: value for key, value in receipt.items() if key != "receipt_sha256"})
        )
    except (TypeError, ValueError) as exc:
        raise CompiledMoveInvariantError("build receipt cannot be canonically digested") from exc
    if not isinstance(receipt_digest, str) or receipt_digest != expected_self_digest:
        raise CompiledMoveInvariantError("build receipt self-digest is invalid or tampered")
    if receipt_acceptance_digest(receipt) != expected_receipt_sha256:
        raise CompiledMoveInvariantError("complete build receipt digest does not match the caller acceptance contract")

    recorded_package_root = _absolute_path(receipt.get("package_root"), "package_root")
    recorded_build_root = _absolute_path(receipt.get("build_root"), "build_root")
    expected_package_root_path = _validated_root(package_root, "caller package root") if package_root is not None else None
    expected_build_root_path = _validated_root(build_root, "caller build root") if build_root is not None else None
    if expected_package_root_path is not None and recorded_package_root != expected_package_root_path:
        raise CompiledMoveInvariantError("build receipt package root does not match the caller package root")
    if expected_build_root_path is not None and recorded_build_root != expected_build_root_path:
        raise CompiledMoveInvariantError("build receipt build root does not match the caller build root")
    _contained(recorded_build_root, recorded_package_root, "build root")
    if not recorded_package_root.is_dir():
        raise CompiledMoveInvariantError(f"recorded package root is missing: {recorded_package_root}")
    if not recorded_build_root.is_dir():
        raise CompiledMoveInvariantError(f"recorded build root is missing: {recorded_build_root}")

    compiled_module = _absolute_path(receipt.get("compiled_module"), "compiled_module")
    _contained(compiled_module, recorded_build_root, "compiled module")
    compiled_relative = compiled_module.relative_to(recorded_build_root)
    if compiled_module.suffix != ".mv" or "bytecode_modules" not in compiled_relative.parts:
        raise CompiledMoveInvariantError("build receipt does not point at a compiled .mv module")
    if "dependencies" in compiled_relative.parts:
        raise CompiledMoveInvariantError("build receipt points at a dependency module, not the built Tool module")
    _regular_file(compiled_module, "compiled module")
    recorded_compiled_sha256 = _digest_value(receipt.get("compiled_sha256"), "compiled_sha256")
    actual_compiled_sha256 = sha256_file(compiled_module)
    if actual_compiled_sha256 != recorded_compiled_sha256:
        raise CompiledMoveInvariantError("compiled module SHA-256 does not match the build receipt")
    if expected_compiled_sha256 is not None and actual_compiled_sha256 != expected_compiled_sha256:
        raise CompiledMoveInvariantError("compiled module SHA-256 does not match the caller acceptance contract")

    module_address = receipt.get("module_address")
    module_name = receipt.get("module_name")
    function_name = receipt.get("function_name")
    module_id = receipt.get("module_id")
    if not all(isinstance(value, str) and value for value in (module_address, module_name, function_name, module_id)):
        raise CompiledMoveInvariantError("build receipt has incomplete compiled module identity")
    if module_id != f"{module_address}.{module_name}":
        raise CompiledMoveInvariantError("build receipt module ID is inconsistent with its address and name")
    for label, actual, expected in (
        ("module ID", module_id, expected_module_id),
        ("module address", module_address, expected_module_address),
        ("module name", module_name, expected_module_name),
        ("function", function_name, expected_function_name),
    ):
        if expected is not None and actual != expected:
            raise CompiledMoveInvariantError(f"build receipt {label} does not match the caller acceptance contract")

    build_info = _absolute_path(receipt.get("build_info"), "build_info")
    expected_build_info = (recorded_build_root / "BuildInfo.yaml").resolve()
    if build_info != expected_build_info:
        raise CompiledMoveInvariantError("build receipt BuildInfo path does not match the build root")
    _regular_file(build_info, "BuildInfo")
    recorded_build_info_sha256 = _digest_value(receipt.get("build_info_sha256"), "build_info_sha256")
    actual_build_info_sha256 = sha256_file(build_info)
    if actual_build_info_sha256 != recorded_build_info_sha256:
        raise CompiledMoveInvariantError("BuildInfo SHA-256 does not match the build receipt")
    if expected_build_info_sha256 is not None and actual_build_info_sha256 != expected_build_info_sha256:
        raise CompiledMoveInvariantError("BuildInfo SHA-256 does not match the caller acceptance contract")
    if _build_info_package_name(build_info) != receipt.get("package_name"):
        raise CompiledMoveInvariantError("BuildInfo package identity does not match the build receipt")

    disassembly = receipt.get("disassembly")
    if disassembly is not None:
        disassembly_path = _absolute_path(disassembly, "disassembly")
        _contained(disassembly_path, recorded_build_root, "disassembly")
        _regular_file(disassembly_path, "saved disassembly")
        if sha256_file(disassembly_path) != _digest_value(receipt.get("disassembly_sha256"), "disassembly_sha256"):
            raise CompiledMoveInvariantError("saved disassembly SHA-256 does not match the build receipt")

    schema_summary = receipt.get("schema_summary")
    if schema_summary is not None:
        schema_summary_path = _absolute_path(schema_summary, "schema_summary")
        summary_root = _absolute_path(
            receipt.get("schema_summary_root", str(recorded_package_root)),
            "schema_summary_root",
        )
        _contained(summary_root, recorded_package_root.parent, "schema summary root")
        _contained(schema_summary_path, summary_root, "schema summary")
        _regular_file(schema_summary_path, "schema summary")
        if sha256_file(schema_summary_path) != _digest_value(receipt.get("schema_summary_sha256"), "schema_summary_sha256"):
            raise CompiledMoveInvariantError("schema summary SHA-256 does not match the build receipt")
    trusted_disassembly_sha256 = _digest_value(
        receipt.get("trusted_disassembly_sha256"), "trusted_disassembly_sha256"
    )
    toolchain = receipt.get("toolchain")
    if (
        not isinstance(toolchain, Mapping)
        or not isinstance(toolchain.get("command"), list)
        or not toolchain.get("command")
        or any(not isinstance(item, str) or not item for item in toolchain["command"])
    ):
        raise CompiledMoveInvariantError("build receipt toolchain command is missing")
    if not isinstance(toolchain.get("version"), str) or not toolchain["version"]:
        raise CompiledMoveInvariantError("build receipt toolchain version is missing")
    if expected_schema_digest is not None and receipt.get("schema_digest") != expected_schema_digest:
        raise CompiledMoveInvariantError("schema digest does not match the caller acceptance contract")

    package_tree_manifest = _manifest_path(receipt.get("package_tree_manifest"), "package tree manifest")
    build_tree_manifest = _manifest_path(receipt.get("build_tree_manifest"), "build tree manifest")
    if package_tree_manifest == build_tree_manifest:
        raise CompiledMoveInvariantError("build receipt package and build tree manifests must be distinct")
    _outside_root(package_tree_manifest, recorded_package_root, "package tree manifest")
    _outside_root(package_tree_manifest, recorded_build_root, "package tree manifest")
    _outside_root(build_tree_manifest, recorded_package_root, "build tree manifest")
    _outside_root(build_tree_manifest, recorded_build_root, "build tree manifest")
    recorded_package_tree_sha256 = _digest_value(
        receipt.get("package_tree_sha256"), "package_tree_sha256"
    )
    recorded_build_tree_sha256 = _digest_value(receipt.get("build_tree_sha256"), "build_tree_sha256")
    if expected_package_tree_sha256 is not None and recorded_package_tree_sha256 != _digest_value(
        expected_package_tree_sha256, "expected package tree SHA-256"
    ):
        raise CompiledMoveInvariantError("package tree SHA-256 does not match the caller acceptance contract")
    if expected_build_tree_sha256 is not None and recorded_build_tree_sha256 != _digest_value(
        expected_build_tree_sha256, "expected build tree SHA-256"
    ):
        raise CompiledMoveInvariantError("build tree SHA-256 does not match the caller acceptance contract")
    if expected_package_tree_manifest is not None and package_tree_manifest != _manifest_path(
        str(expected_package_tree_manifest), "expected package tree manifest"
    ):
        raise CompiledMoveInvariantError("package tree manifest does not match the caller acceptance contract")
    if expected_build_tree_manifest is not None and build_tree_manifest != _manifest_path(
        str(expected_build_tree_manifest), "expected build tree manifest"
    ):
        raise CompiledMoveInvariantError("build tree manifest does not match the caller acceptance contract")
    build_tree_evidence = validate_tree_manifest(
        recorded_build_root, build_tree_manifest, recorded_build_tree_sha256, "build"
    )
    package_tree_evidence = validate_tree_manifest(
        recorded_package_root, package_tree_manifest, recorded_package_tree_sha256, "package"
    )

    return {
        "receipt_sha256": receipt_digest,
        "package_root": str(recorded_package_root),
        "build_root": str(recorded_build_root),
        "compiled_module": str(compiled_module),
        "compiled_sha256": actual_compiled_sha256,
        "module_id": str(module_id),
        "module_address": str(module_address),
        "module_name": str(module_name),
        "function_name": str(function_name),
        "build_info": str(build_info),
        "build_info_sha256": actual_build_info_sha256,
        "schema_digest": receipt.get("schema_digest"),
        "disassembly": str(disassembly) if isinstance(disassembly, str) else None,
        "disassembly_sha256": receipt.get("disassembly_sha256"),
        "trusted_disassembly_sha256": trusted_disassembly_sha256,
        "package_tree_manifest": package_tree_evidence["manifest"],
        "package_tree_sha256": package_tree_evidence["tree_sha256"],
        "build_tree_manifest": build_tree_evidence["manifest"],
        "build_tree_sha256": build_tree_evidence["tree_sha256"],
        "toolchain": dict(toolchain),
    }


def _split_signature_items(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char in "<({":
            depth += 1
        elif char in ">)}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        items.append(tail)
    return items


def _parse_parameters(value: str) -> tuple[tuple[str, str], ...]:
    parameters: list[tuple[str, str]] = []
    for item in _split_signature_items(value):
        if ":" not in item:
            raise CompiledMoveInvariantError(f"compiled function parameter has no type: {item!r}")
        name, type_name = item.split(":", 1)
        parameters.append((name.split("#", 1)[0].strip(), type_name.strip()))
    return tuple(parameters)


def _normalise_compiled_address(value: object) -> str | None:
    if not isinstance(value, str) or COMPILED_ADDRESS_RE.fullmatch(value) is None:
        return None
    digits = value[2:] if value.lower().startswith("0x") else value
    return "0x" + digits.lower().zfill(64)


def _parse_instruction(
    line: str, module_aliases: Mapping[str, str | None] | None = None
) -> Instruction | None:
    match = INSTRUCTION_RE.match(line)
    if not match:
        return None
    body = match.group("body")
    opcode_match = re.match(r"(?P<opcode>[A-Za-z_][A-Za-z0-9_]*)(?P<rest>.*)$", body)
    if opcode_match is None:
        raise CompiledMoveInvariantError(f"compiled instruction has no opcode: {body!r}")
    callee_module_id = None
    if opcode_match.group("opcode") == "Call" and module_aliases is not None:
        target_match = CALL_TARGET_RE.match(opcode_match.group("rest").lstrip())
        if target_match is not None:
            callee_module_id = module_aliases.get(target_match.group("module"))
    return Instruction(
        int(match.group("offset")),
        opcode_match.group("opcode"),
        opcode_match.group("rest").lstrip(),
        callee_module_id,
    )


def parse_disassembly_text(text: str, *, source: str = "trusted Sui disassembly") -> tuple[CompiledModule, ...]:
    """Parse text emitted by Sui; callers must not use this as a trust anchor."""

    lines = text.splitlines()
    modules: list[CompiledModule] = []
    index = 0
    while index < len(lines):
        module_match = MODULE_RE.match(lines[index])
        if not module_match:
            index += 1
            continue
        module_address = module_match.group("address")
        module_name = module_match.group("name")
        index += 1
        functions: dict[str, CompiledFunction] = {}
        module_end = index
        depth = 1
        while module_end < len(lines):
            depth += lines[module_end].count("{") - lines[module_end].count("}")
            if depth == 0:
                break
            module_end += 1
        if depth != 0:
            raise CompiledMoveInvariantError(f"{source} module {module_name!r} is not closed")
        module_aliases: dict[str, str | None] = {}
        for line in lines[index:module_end]:
            use_match = USE_RE.match(line)
            if use_match is None:
                continue
            alias = use_match.group("alias") or use_match.group("module")
            address = _normalise_compiled_address(use_match.group("address"))
            module_id = (
                f"{address}::{use_match.group('module')}" if address is not None else None
            )
            if alias in module_aliases and module_aliases[alias] != module_id:
                module_aliases[alias] = None
            else:
                module_aliases[alias] = module_id
        while index < module_end:
            function_match = FUNCTION_RE.match(lines[index])
            if not function_match:
                index += 1
                continue
            signature = lines[index]
            function_name = function_match.group("name")
            parameters = _parse_parameters(function_match.group("parameters"))
            index += 1
            instructions: list[Instruction] = []
            while index < len(lines) and lines[index] != "}":
                instruction = _parse_instruction(lines[index], module_aliases)
                if instruction is not None:
                    instructions.append(instruction)
                index += 1
            functions[function_name] = CompiledFunction(
                module_name=module_name,
                name=function_name,
                signature=signature,
                parameters=parameters,
                instructions=tuple(instructions),
            )
            if index < len(lines) and lines[index] == "}":
                index += 1
        modules.append(CompiledModule(module_address, module_name, functions, module_aliases))
        index = module_end + 1
    if not modules:
        raise CompiledMoveInvariantError(f"{source} contains no module")
    return tuple(modules)


def parse_disassembly(path: Path) -> tuple[CompiledModule, ...]:
    """Parse a text file for parser unit tests; public validation never trusts this path."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompiledMoveInvariantError(f"compiled Move disassembly is unreadable: {path}") from exc
    return parse_disassembly_text(text, source=str(path))


def _saved_disassembly_matches_trusted(saved: bytes, trusted: bytes) -> bool:
    """Compare a saved text witness structurally without using it as authority."""

    try:
        return parse_disassembly_text(saved.decode("utf-8"), source="saved disassembly") == parse_disassembly_text(
            trusted.decode("utf-8"), source="trusted Sui disassembly"
        )
    except (UnicodeDecodeError, CompiledMoveInvariantError):
        return False


def _find_compiled_module(build_root: Path, module_name: str) -> Path:
    candidates = sorted(
        candidate
        for candidate in build_root.rglob(f"{module_name}.mv")
        if "dependencies" not in candidate.relative_to(build_root).parts
    ) if build_root.is_dir() else []
    if len(candidates) != 1:
        raise CompiledMoveInvariantError(
            f"expected one compiled .mv module for {module_name!r}, found {len(candidates)} under {build_root}"
        )
    return candidates[0]


def _find_saved_disassembly(build_root: Path, module_name: str) -> Path | None:
    candidates = sorted(
        candidate
        for candidate in build_root.rglob(f"{module_name}.mvb")
        if "dependencies" not in candidate.relative_to(build_root).parts
    ) if build_root.is_dir() else []
    if len(candidates) > 1:
        raise CompiledMoveInvariantError(f"ambiguous saved disassembly for {module_name!r} under {build_root}")
    return candidates[0] if candidates else None


def find_disassembly(build_root: Path, module_name: str) -> Path:
    """Return a saved disassembly for diagnostics only; it is never authoritative."""

    saved = _find_saved_disassembly(build_root, module_name)
    if saved is None:
        raise CompiledMoveInvariantError(f"no saved disassembly for {module_name!r} under {build_root}")
    return saved


def _trusted_disassemble(
    compiled_module: Path,
    package_root: Path,
    *,
    build_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> tuple[tuple[CompiledModule, ...], bytes, list[str], str]:
    if command_runner is None:
        raise CompiledMoveInvariantError(
            "an observed command runner is required for trusted Sui disassembly and version evidence"
        )
    search_path = environment.get("PATH") if environment is not None else None
    sui = shutil.which("sui", path=search_path)
    if sui is None:
        raise CompiledMoveInvariantError("trusted Sui disassembler is unavailable on PATH")
    with tempfile.TemporaryDirectory(prefix="trusted-disassembly-", dir=package_root) as directory:
        # Sui's disassemble command may refresh the package build when resolving
        # local dependencies. Disassemble an exact private copy so the receipt
        # continues to describe the successful-build bytes, never a refreshed
        # or caller-supplied module.
        copied_module = Path(directory) / compiled_module.name
        shutil.copyfile(compiled_module, copied_module)
        preserved_files: dict[Path, bytes] = {compiled_module: compiled_module.read_bytes()}
        if build_root is not None:
            build_info = build_root / "BuildInfo.yaml"
            if build_info.is_file():
                preserved_files[build_info] = build_info.read_bytes()
            for saved in build_root.rglob("*.mvb"):
                if "dependencies" not in saved.relative_to(build_root).parts and saved.is_file():
                    preserved_files[saved] = saved.read_bytes()
        command = [sui, "move"]
        client_config = environment.get("NEXUS_PORTABILITY_SUI_CLIENT_CONFIG") if environment is not None else None
        if client_config:
            command.extend(["--client.config", client_config])
        command.extend(
            [
                "disassemble",
                str(copied_module),
                "--path",
                str(package_root),
                "--build-env",
                "local",
            ]
        )
        try:
            result = command_runner(command, package_root, environment)
            if not isinstance(result, ObservedCommandResult) or not _is_observed_result(result, command):
                raise CompiledMoveInvariantError("trusted Sui disassembly returned unobserved command evidence")
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
                raise CompiledMoveInvariantError(f"trusted Sui disassembler failed: {stderr}")
            trusted_bytes = result.stdout
            modules = parse_disassembly_text(trusted_bytes.decode("utf-8"), source="trusted Sui disassembly")
            version_command = [sui, "--version"]
            version_result = command_runner(version_command, package_root, environment)
            if not isinstance(version_result, ObservedCommandResult) or not _is_observed_result(
                version_result, version_command
            ):
                raise CompiledMoveInvariantError("trusted Sui version query returned unobserved command evidence")
            if version_result.returncode != 0:
                stderr = version_result.stderr.decode("utf-8", errors="replace")[-2000:]
                raise CompiledMoveInvariantError(f"trusted Sui version query failed: {stderr}")
            version = version_result.stdout.decode("utf-8", errors="replace").strip()
            if not version:
                raise CompiledMoveInvariantError("trusted Sui version query returned no version")
            return modules, trusted_bytes, command, version
        finally:
            for preserved_path, preserved_bytes in preserved_files.items():
                if not preserved_path.exists() or preserved_path.read_bytes() != preserved_bytes:
                    preserved_path.parent.mkdir(parents=True, exist_ok=True)
                    preserved_path.write_bytes(preserved_bytes)


def _is_observed_result(result: ObservedCommandResult, command: Sequence[str]) -> bool:
    """Require the runner to attest the exact command and isolation boundary."""

    observation = result.observation
    return (
        isinstance(result.returncode, int)
        and isinstance(result.stdout, bytes)
        and isinstance(result.stderr, bytes)
        and isinstance(observation, Mapping)
        and observation.get("observed") is True
        and observation.get("command") == list(command)
        and observation.get("mechanism") == "strace -f -e trace=network"
        and observation.get("isolation") == "bwrap --unshare-net"
        and isinstance(observation.get("network_events"), list)
        and isinstance(observation.get("network_event_count"), int)
        and observation.get("network_event_count") == len(observation["network_events"])
    )


def _validate_toolchain_identity(
    receipt: Mapping[str, object], command: Sequence[str], version: str
) -> None:
    toolchain = receipt.get("toolchain")
    if not isinstance(toolchain, Mapping):
        raise CompiledMoveInvariantError("build receipt toolchain identity is missing")
    recorded_command = toolchain.get("command")
    recorded_version = toolchain.get("version")
    if (
        not isinstance(recorded_command, list)
        or not recorded_command
        or any(not isinstance(item, str) or not item for item in recorded_command)
        or not isinstance(recorded_version, str)
        or not recorded_version
    ):
        raise CompiledMoveInvariantError("build receipt toolchain identity is malformed")
    try:
        recorded_binary = Path(recorded_command[0]).resolve()
        current_binary = Path(command[0]).resolve()
    except (OSError, RuntimeError) as exc:
        raise CompiledMoveInvariantError("cannot resolve Sui toolchain identity") from exc
    resolved_binary = toolchain.get("resolved_binary")
    if resolved_binary is not None:
        if not isinstance(resolved_binary, str) or not resolved_binary:
            raise CompiledMoveInvariantError("build receipt resolved Sui executable is malformed")
        try:
            resolved_binary_path = Path(resolved_binary).resolve()
        except (OSError, RuntimeError) as exc:
            raise CompiledMoveInvariantError("cannot resolve build receipt Sui executable") from exc
        if resolved_binary_path != recorded_binary:
            raise CompiledMoveInvariantError("build receipt resolved Sui executable disagrees with its command")
    if recorded_binary != current_binary:
        raise CompiledMoveInvariantError("Sui executable does not match the build receipt toolchain")
    if recorded_version != version:
        raise CompiledMoveInvariantError("Sui version does not match the build receipt toolchain")


def _instruction_edges(function: CompiledFunction) -> dict[int, tuple[int, ...]]:
    offsets = [instruction.offset for instruction in function.instructions]
    if not offsets:
        raise CompiledMoveInvariantError(f"compiled function {function.name!r} contains no instructions")
    next_offset = {offset: offsets[index + 1] for index, offset in enumerate(offsets[:-1])}
    edges: dict[int, tuple[int, ...]] = {}
    for instruction in function.instructions:
        fallthrough = next_offset.get(instruction.offset)
        branch = (
            BRANCH_RE.match(instruction.encoded)
            if instruction.opcode in {"BrFalse", "BrTrue", "Branch", "Jump"}
            else None
        )
        if branch:
            target = int(branch.group("target"))
            if target not in offsets:
                raise CompiledMoveInvariantError(
                    f"compiled function {function.name!r} branches to missing instruction {target}"
                )
            if instruction.opcode in {"BrFalse", "BrTrue"} and fallthrough is not None:
                edges[instruction.offset] = (target, fallthrough)
            else:
                edges[instruction.offset] = (target,)
        elif instruction.opcode in {"Ret", "Abort"}:
            edges[instruction.offset] = ()
        elif fallthrough is not None:
            edges[instruction.offset] = (fallthrough,)
        else:
            raise CompiledMoveInvariantError(f"compiled function {function.name!r} falls off the end")
    return edges


def _local_reference(instruction: Instruction) -> tuple[str, str] | None:
    match = LOCAL_RE.search(instruction.encoded)
    return (match.group("name").strip(), match.group("type").strip()) if match else None


def _stored_local_reference(instruction: Instruction) -> tuple[str, str] | None:
    match = STORE_LOCAL_RE.search(instruction.encoded)
    return (match.group("name").strip(), match.group("type").strip()) if match else None


def _abort_only_path(
    start: int,
    *,
    edges: Mapping[int, Sequence[int]],
    instructions: Mapping[int, Instruction],
    satisfy_call: str,
    finalize_call: str,
    authorization_call: str,
) -> set[int] | None:
    """Return abort offsets when every path from ``start`` aborts first."""

    pending = [start]
    visited: set[int] = set()
    aborts: set[int] = set()
    while pending:
        offset = pending.pop()
        if offset in visited:
            return None
        visited.add(offset)
        instruction = instructions.get(offset)
        if instruction is None:
            return None
        if instruction.opcode == "Abort":
            aborts.add(offset)
            continue
        if instruction.opcode == "Ret":
            return None
        target = instruction.call_target
        if target in {satisfy_call, finalize_call, authorization_call}:
            return None
        successors = tuple(edges.get(offset, ()))
        if not successors:
            return None
        pending.extend(successors)
    return aborts or None


_AUTH_VERIFIER_RESULT = "verifier-result"
_AUTH_OTHER_BOOL = "other-bool"
_AUTH_OTHER = "other"
_AUTH_UNKNOWN = "unknown"


@dataclass(frozen=True)
class _AuthorizationDataflowState:
    offset: int
    stack: tuple[str, ...]
    locals: tuple[tuple[str, str], ...]
    outcome: str
    satisfied: bool


def _authorization_result_type(instruction: Instruction) -> str | None:
    """Return the abstract result kind for a call, if its disassembly exposes one."""

    if instruction.opcode != "Call":
        return None
    match = re.search(r"\)\s*:\s*(?P<type>[^,]+?)\s*$", instruction.operands)
    if match is None:
        return None
    return match.group("type").strip()


def _authorization_stack_pop(stack: list[str], instruction: Instruction) -> str:
    if not stack:
        raise CompiledMoveInvariantError(
            f"workflow authorization verifier result is not present for {instruction.opcode}"
        )
    return stack.pop()


def _authorization_result_failure_offsets(
    function: CompiledFunction,
    authorization_index: int,
    satisfy_index: int,
    finalize_index: int,
    *,
    edges: Mapping[int, Sequence[int]],
    instruction_by_offset: Mapping[int, Instruction],
    satisfy_call: str,
    finalize_call: str,
    authorization_call: str,
    authorization_identity: str,
) -> set[int]:
    """Prove path-sensitive verifier-result use before any successful mutation.

    The disassembler prints locals as versioned names (for example
    ``authorization_ok#1#0``).  The abstract state intentionally keys locals by
    their base name so a later ``StLoc`` overwrites and clears verifier taint even
    when the compiler assigned a new version.  Unknown instructions and values
    are fail-closed: they cannot manufacture a verifier result.
    """

    instructions = function.instructions
    verifier = instructions[authorization_index]
    if ": bool" not in verifier.operands:
        raise CompiledMoveInvariantError("workflow authorization verifier does not return a boolean")
    if authorization_index >= satisfy_index or authorization_index >= finalize_index:
        raise CompiledMoveInvariantError(
            "workflow authorization must be consumed before witness satisfaction and state mutation"
        )

    if not edges.get(verifier.offset):
        raise CompiledMoveInvariantError("workflow authorization verifier has no reachable continuation")
    worklist = [_AuthorizationDataflowState(verifier.offset, (), (), "unknown", False)]
    visited: set[_AuthorizationDataflowState] = set()
    failure_offsets: set[int] = set()
    authorized_finalization = False
    authorized_satisfaction = False

    def enqueue(
        offset: int,
        stack: Sequence[str],
        locals_map: Mapping[str, str],
        outcome: str,
        satisfied: bool,
    ) -> None:
        if offset not in instruction_by_offset:
            raise CompiledMoveInvariantError(
                f"workflow authorization reaches missing instruction {offset}"
            )
        worklist.append(
            _AuthorizationDataflowState(
                offset,
                tuple(stack),
                tuple(sorted(locals_map.items())),
                outcome,
                satisfied,
            )
        )

    while worklist:
        state = worklist.pop()
        if state in visited:
            continue
        visited.add(state)
        instruction = instruction_by_offset[state.offset]
        target = instruction.call_target
        is_verifier_call = (
            state.offset == verifier.offset
            and not state.stack
            and not state.locals
            and state.outcome == "unknown"
            and not state.satisfied
        )
        if state.offset == verifier.offset and not is_verifier_call:
            raise CompiledMoveInvariantError(
                "workflow authorization verifier is reachable more than once"
            )
        stack = [_AUTH_VERIFIER_RESULT] if is_verifier_call else list(state.stack)
        locals_map = dict(state.locals)
        outcome = state.outcome
        satisfied = state.satisfied

        if is_verifier_call:
            if target != authorization_call or instruction.call_target_id != authorization_identity:
                raise CompiledMoveInvariantError("workflow authorization verifier call is not the expected call")
        elif _call_target_matches(target, satisfy_call):
            if outcome != "authorized":
                raise CompiledMoveInvariantError(
                    "workflow authorization verifier result does not authorize witness satisfaction"
                )
            satisfied = True
            authorized_satisfaction = True
        elif _call_target_matches(target, finalize_call):
            if outcome != "authorized" or not satisfied:
                raise CompiledMoveInvariantError(
                    "workflow authorization verifier result does not authorize finalization after satisfaction"
                )
            authorized_finalization = True
        elif target == authorization_call and instruction.call_target_id == authorization_identity:
            raise CompiledMoveInvariantError(
                "workflow authorization verifier call is not a single post-call result"
            )

        if instruction.opcode in {"BrFalse", "BrTrue"}:
            condition = _authorization_stack_pop(stack, instruction)
            if condition != _AUTH_VERIFIER_RESULT:
                if outcome == "unknown" and not (
                    _AUTH_VERIFIER_RESULT in stack
                    or _AUTH_VERIFIER_RESULT in locals_map.values()
                ):
                    raise CompiledMoveInvariantError(
                        "workflow authorization verifier result does not control an abort gate"
                    )
                for successor in edges.get(instruction.offset, ()):
                    enqueue(successor, stack, locals_map, outcome, satisfied)
                continue
            branch_match = BRANCH_RE.match(instruction.encoded)
            if branch_match is None:
                raise CompiledMoveInvariantError("workflow authorization conditional branch is malformed")
            branch_target = int(branch_match.group("target"))
            successors = tuple(edges.get(instruction.offset, ()))
            fallthrough = next(
                (successor for successor in successors if successor != branch_target),
                None,
            )
            if instruction.opcode == "BrFalse":
                failure_start, authorized_start = branch_target, fallthrough
            else:
                failure_start, authorized_start = fallthrough, branch_target
            if failure_start is None:
                raise CompiledMoveInvariantError("workflow authorization verifier has no false branch")
            failure = _abort_only_path(
                failure_start,
                edges=edges,
                instructions=instruction_by_offset,
                satisfy_call=satisfy_call,
                finalize_call=finalize_call,
                authorization_call=authorization_call,
            )
            if failure is None:
                raise CompiledMoveInvariantError(
                    "workflow authorization verifier result does not control an abort before mutation"
                )
            failure_offsets.update(failure)
            if authorized_start is not None:
                enqueue(authorized_start, stack, locals_map, "authorized", satisfied)
            continue

        if instruction.opcode == "Abort":
            if outcome == "unknown":
                raise CompiledMoveInvariantError(
                    "workflow authorization verifier result does not reach an abort gate"
                )
            continue
        if instruction.opcode == "Ret":
            if outcome == "unknown":
                raise CompiledMoveInvariantError(
                    "workflow authorization verifier result does not reach an abort gate"
                )
            continue

        if instruction.opcode in {"MoveLoc", "CopyLoc"}:
            reference = _local_reference(instruction)
            if reference is None:
                stack.append(_AUTH_UNKNOWN)
            else:
                name, type_name = reference
                tag = locals_map.get(name, _AUTH_UNKNOWN)
                if tag == _AUTH_VERIFIER_RESULT and type_name != "bool":
                    tag = _AUTH_UNKNOWN
                stack.append(tag)
                if instruction.opcode == "MoveLoc":
                    locals_map.pop(name, None)
        elif instruction.opcode == "StLoc":
            stored = _stored_local_reference(instruction)
            value = _authorization_stack_pop(stack, instruction) if stack else _AUTH_UNKNOWN
            if stored is None:
                raise CompiledMoveInvariantError("workflow authorization local store is malformed")
            name, type_name = stored
            if value == _AUTH_VERIFIER_RESULT and type_name != "bool":
                raise CompiledMoveInvariantError(
                    "workflow authorization verifier result is stored without a boolean dataflow"
                )
            # Every store, including a constant or unrelated value, replaces the
            # prior local value.  This is the stale-load defense.
            locals_map[name] = value
        elif instruction.opcode in {"LdTrue", "LdFalse"}:
            stack.append(_AUTH_OTHER_BOOL)
        elif instruction.opcode in {"LdU8", "LdU16", "LdU32", "LdU64", "LdU128", "LdU256", "LdConst", "LdAddress"}:
            stack.append(_AUTH_OTHER_BOOL if re.search(r"\bbool\b", instruction.operands) else _AUTH_OTHER)
        elif instruction.opcode == "Pop":
            if stack and stack.pop() == _AUTH_VERIFIER_RESULT:
                raise CompiledMoveInvariantError(
                    "workflow authorization verifier result is discarded before an abort gate"
                )
        elif instruction.opcode in {"FreezeRef", "ReadRef", "Not", "Eq", "Neq", "Lt", "Le", "Gt", "Ge"}:
            if stack:
                stack.pop()
            stack.append(_AUTH_OTHER_BOOL if instruction.opcode in {"Not", "Eq", "Neq", "Lt", "Le", "Gt", "Ge"} else _AUTH_OTHER)
        elif is_verifier_call:
            pass
        elif instruction.opcode == "Call":
            # Calls consume their arguments; without an operand-level arity
            # model, dropping the tracked stack is the safe approximation.
            result_type = _authorization_result_type(instruction)
            stack.clear()
            if result_type is not None:
                stack.append(_AUTH_OTHER_BOOL if result_type == "bool" else _AUTH_OTHER)
        elif instruction.opcode not in {"Branch", "Jump", "Nop"}:
            # Do not preserve taint through an opcode whose stack/local effect
            # this validator does not understand.
            stack = [_AUTH_UNKNOWN if tag == _AUTH_VERIFIER_RESULT else tag for tag in stack]
            locals_map = {
                name: (_AUTH_UNKNOWN if tag == _AUTH_VERIFIER_RESULT else tag)
                for name, tag in locals_map.items()
            }

        successors = tuple(edges.get(instruction.offset, ()))
        for successor in successors:
            enqueue(successor, stack, locals_map, outcome, satisfied)

    if not failure_offsets:
        raise CompiledMoveInvariantError(
            "workflow authorization verifier result does not control an abort before mutation"
        )
    if not authorized_satisfaction or not authorized_finalization:
        raise CompiledMoveInvariantError(
            "workflow authorization has no authorized true path reaching satisfaction and finalization"
        )
    return failure_offsets


def _validate_witness_relationship(
    function: CompiledFunction,
    satisfy_index: int,
    expected: Mapping[str, object],
) -> None:
    instructions = function.instructions
    requirements_parameter = str(expected["requirements_parameter"])
    state_parameter = str(expected["state_parameter"])
    state_type = str(expected["state_type"])
    witness_type = str(expected["witness_type"])
    witness_uid_field = str(expected["witness_uid_field"])
    prior = instructions[:satisfy_index]
    if not prior or prior[-1].opcode != "ImmBorrowField":
        raise CompiledMoveInvariantError(
            "UIDRequirements::satisfy does not consume a UID loaded from a witness field"
        )
    field_match = FIELD_RE.search(prior[-1].encoded)
    field_name = field_match.group("field").strip() if field_match else ""
    field_owner, separator, field_leaf = field_name.rpartition(".")
    field_owner = field_owner.rsplit("::", 1)[-1] if separator else ""
    if (
        field_match is None
        or field_match.group("type").strip() != "UID"
        or field_owner != witness_type
        or field_leaf != witness_uid_field
    ):
        raise CompiledMoveInvariantError(
            f"UIDRequirements::satisfy witness UID field is not *.{witness_uid_field}: {prior[-1].operands}"
        )
    witness_call_index = satisfy_index - 2
    if witness_call_index < 0 or instructions[witness_call_index].opcode != "Call":
        raise CompiledMoveInvariantError("witness UID is not produced by a witness accessor call")
    witness_call = instructions[witness_call_index]
    call_operands = witness_call.operands
    if f"(&{state_type})" not in call_operands or f": &{witness_type}" not in call_operands:
        raise CompiledMoveInvariantError(
            "witness accessor does not consume the execute state and return the expected witness type"
        )
    state_move_found = False
    for instruction in instructions[max(0, witness_call_index - 4) : witness_call_index]:
        reference = _local_reference(instruction)
        if reference and reference[0] == state_parameter and reference[1] == f"&mut {state_type}":
            state_move_found = instruction.opcode in {"MoveLoc", "CopyLoc"}
    if not state_move_found:
        raise CompiledMoveInvariantError(
            f"witness accessor is not fed by execute state parameter {state_parameter!r}"
        )
    requirements_found = False
    for instruction in instructions[max(0, satisfy_index - 8) : satisfy_index]:
        if instruction.opcode == "MutBorrowLoc":
            reference = _local_reference(instruction)
            if reference and reference[0] == requirements_parameter and reference[1] == "UIDRequirements":
                requirements_found = True
    if not requirements_found:
        raise CompiledMoveInvariantError(
            f"UIDRequirements::satisfy is not fed by execute requirements parameter {requirements_parameter!r}"
        )


def _call_target_matches(actual: str | None, expected: str) -> bool:
    """Match a disassembler's module-qualified call without accepting another function."""

    if actual == expected:
        return True
    return actual is not None and actual.endswith(f"::{expected}")


def _authorization_call_identity(invariant: Mapping[str, object]) -> str:
    """Return the exact approved package-address/module/function identity."""

    call = invariant.get("authorization_call")
    package_address = invariant.get("authorization_package_address")
    if not isinstance(call, str) or CALL_TARGET_RE.fullmatch(call + "(") is None:
        raise CompiledMoveInvariantError("workflow authorization call identity is malformed")
    address = _normalise_compiled_address(package_address)
    if address is None or address == "0x" + "0" * 64:
        raise CompiledMoveInvariantError(
            "workflow authorization package address must be an exact non-zero hexadecimal address"
        )
    module, function = call.split("::", 1)
    module_id = f"{address}::{module}"
    declared_module_id = invariant.get("authorization_module_id")
    if declared_module_id is not None and declared_module_id != module_id:
        raise CompiledMoveInvariantError("workflow authorization module identity is not bound to its package address")
    return f"{module_id}::{function}"


def _authorization_call_matches(instruction: Instruction, invariant: Mapping[str, object]) -> bool:
    """Match authorization by suffix and the full imported package identity."""

    call = invariant.get("authorization_call")
    return (
        isinstance(call, str)
        and instruction.call_target == call
        and instruction.call_target_id == _authorization_call_identity(invariant)
    )


@dataclass(frozen=True)
class _AuthorizationOriginState:
    offset: int
    stack: tuple[frozenset[str], ...]
    locals: tuple[tuple[str, frozenset[str]], ...]


_ORIGIN_UNKNOWN = frozenset({"unknown"})
_ORIGIN_CONSTANT = frozenset({"constant"})


def _origin_local_name(reference: tuple[str, str] | None) -> str | None:
    return reference[0] if reference is not None else None


def _origin_pop(
    stack: list[frozenset[str]], count: int, instruction: Instruction
) -> tuple[frozenset[str], ...]:
    if len(stack) < count:
        raise CompiledMoveInvariantError(
            f"authorization origin analysis has too few stack values for {instruction.encoded}"
        )
    values = tuple(stack[-count:])
    del stack[-count:]
    return values


def _origin_call_arity(instruction: Instruction, expected: Mapping[str, object]) -> int:
    target = instruction.call_target
    if _call_target_matches(target, expected["proof_call"]) or _call_target_matches(
        target, expected["commitment_call"]
    ):
        return 1
    if _authorization_call_matches(instruction, expected):
        return 4
    if target is not None and target.endswith("::witness"):
        return 1
    if target is not None and target.endswith("::new") and "proof_of_uid" in target:
        return 1
    return 0


def _origin_for_field(
    stack: list[frozenset[str]],
    instruction: Instruction,
    *,
    state_origin: str,
    recipient_owner: str,
    recipient_field: str,
) -> frozenset[str]:
    owner = _origin_pop(stack, 1, instruction)[0] if stack else _ORIGIN_UNKNOWN
    field_match = FIELD_RE.search(instruction.encoded)
    if field_match is None:
        return _ORIGIN_UNKNOWN
    field_name = field_match.group("field").strip()
    field_owner, separator, field_leaf = field_name.rpartition(".")
    field_owner = field_owner.rsplit("::", 1)[-1] if separator else ""
    if (
        owner == frozenset({state_origin})
        and field_match.group("type").strip() == "UID"
        and field_owner == recipient_owner
        and field_leaf == recipient_field
    ):
        return frozenset({"state_uid"})
    return frozenset({"decoy_uid"})


def _trace_authorization_call_origins(
    function: CompiledFunction,
    authorization_index: int,
    expected: Mapping[str, object],
    *,
    edges: Mapping[int, Sequence[int]],
    instruction_by_offset: Mapping[int, Instruction],
) -> tuple[tuple[frozenset[str], ...], ...]:
    """Track concrete stack/local origins into the verifier call."""

    if not function.instructions:
        raise CompiledMoveInvariantError("authorization origin analysis has no instructions")
    first_offset = function.instructions[0].offset
    local_origins = {
        name.split("#", 1)[0]: frozenset({f"parameter:{name.split('#', 1)[0]}"})
        for name, _type_name in function.parameters
    }
    expected_call_offset = function.instructions[authorization_index].offset
    worklist = [
        _AuthorizationOriginState(
            first_offset,
            (),
            tuple(sorted(local_origins.items())),
        )
    ]
    visited: set[_AuthorizationOriginState] = set()
    call_origins: list[tuple[frozenset[str], ...]] = []

    def enqueue(offset: int, stack: Sequence[frozenset[str]], locals_map: Mapping[str, frozenset[str]]) -> None:
        if offset not in instruction_by_offset:
            raise CompiledMoveInvariantError(
                f"authorization origin analysis reaches missing instruction {offset}"
            )
        worklist.append(
            _AuthorizationOriginState(
                offset,
                tuple(stack),
                tuple(sorted(locals_map.items())),
            )
        )

    while worklist:
        state = worklist.pop()
        if state in visited:
            continue
        visited.add(state)
        instruction = instruction_by_offset[state.offset]
        stack = list(state.stack)
        locals_map = dict(state.locals)
        target = instruction.call_target

        if state.offset == expected_call_offset:
            if not _authorization_call_matches(instruction, expected):
                raise CompiledMoveInvariantError(
                    "authorization origin analysis did not reach the expected verifier call"
                )
            arguments = _origin_pop(stack, 4, instruction)
            call_origins.append(arguments)
            continue

        if instruction.opcode in {"MoveLoc", "CopyLoc", "MutBorrowLoc", "ImmBorrowLoc"}:
            reference = _local_reference(instruction)
            name = _origin_local_name(reference)
            value = locals_map.get(name, _ORIGIN_UNKNOWN) if name is not None else _ORIGIN_UNKNOWN
            stack.append(value)
            if instruction.opcode == "MoveLoc" and name is not None:
                locals_map.pop(name, None)
        elif instruction.opcode == "StLoc":
            reference = _stored_local_reference(instruction)
            if reference is None:
                raise CompiledMoveInvariantError("authorization origin local store is malformed")
            value = _origin_pop(stack, 1, instruction)[0] if stack else _ORIGIN_UNKNOWN
            locals_map[reference[0]] = value
        elif instruction.opcode in {
            "LdTrue",
            "LdFalse",
            "LdU8",
            "LdU16",
            "LdU32",
            "LdU64",
            "LdU128",
            "LdU256",
            "LdAddress",
            "LdConst",
        }:
            stack.append(_ORIGIN_CONSTANT)
        elif instruction.opcode == "ImmBorrowField" or instruction.opcode == "MutBorrowField":
            stack.append(
                _origin_for_field(
                    stack,
                    instruction,
                    state_origin=f"parameter:{expected['state_parameter']}",
                    recipient_owner=expected["recipient_owner"],
                    recipient_field=expected["recipient_field"],
                )
            )
        elif instruction.opcode == "FreezeRef":
            value = _origin_pop(stack, 1, instruction)[0] if stack else _ORIGIN_UNKNOWN
            stack.append(value)
        elif instruction.opcode == "Call":
            arity = _origin_call_arity(instruction, expected)
            arguments = _origin_pop(stack, arity, instruction) if arity else ()
            if _call_target_matches(target, expected["proof_call"]):
                result = (
                    frozenset({"requirements_proof"})
                    if arguments and arguments[0] == frozenset({f"parameter:{expected['requirements_parameter']}"})
                    else frozenset({"decoy_proof"})
                )
                stack.append(result)
            elif _call_target_matches(target, expected["commitment_call"]):
                result = (
                    frozenset({"input_commitment"})
                    if arguments and arguments[0] == frozenset({f"parameter:{expected['result_parameter']}"})
                    else frozenset({"decoy_commitment"})
                )
                stack.append(result)
            elif target is not None and _authorization_result_type(instruction) is not None:
                stack.append(_ORIGIN_UNKNOWN)
        elif instruction.opcode in {"BrFalse", "BrTrue"}:
            _origin_pop(stack, 1, instruction)
        elif instruction.opcode in {"Ret", "Abort"}:
            continue
        elif instruction.opcode in {"Branch", "Jump", "Nop"}:
            pass
        else:
            stack = [_ORIGIN_UNKNOWN for _value in stack]
            locals_map = {name: _ORIGIN_UNKNOWN for name in locals_map}

        for successor in edges.get(instruction.offset, ()):
            enqueue(successor, stack, locals_map)

    if not call_origins:
        raise CompiledMoveInvariantError("authorization origin analysis found no reachable verifier call")
    return tuple(call_origins)


def _validate_authorization_relationship(
    function: CompiledFunction,
    authorization_index: int,
    expected: Mapping[str, object],
    *,
    edges: Mapping[int, Sequence[int]],
    instruction_by_offset: Mapping[int, Instruction],
) -> None:
    """Prove that the canonical workflow proof is consumed with bound inputs."""

    instructions = function.instructions
    authorization_parameter = str(expected.get("authorization_parameter", "authorization"))
    authorization_types = str(expected.get("authorization_types", ""))
    authorization_instruction = instructions[authorization_index]
    required_type_fragments = (
        "ProvenValue",
        "AgentVertexAuthorization",
        "ProofOfUID",
        "UID",
        "vector<u8>",
    )
    if authorization_types and authorization_types not in authorization_instruction.operands:
        if any(fragment not in authorization_instruction.operands for fragment in required_type_fragments):
            raise CompiledMoveInvariantError(
                "canonical workflow authorization call has the wrong proof, worksheet, recipient, or commitment types"
            )
    prior = instructions[:authorization_index]
    proof_call = str(expected.get("authorization_proof_call", "proof_of_uid::proof"))
    commitment_call = str(
        expected.get("authorization_commitment_call", "onchain_tool_result::input_commitment")
    )
    origin_expectations = {
        "authorization_call": str(expected.get("authorization_call", "")),
        "authorization_package_address": str(expected.get("authorization_package_address", "")),
        "proof_call": proof_call,
        "commitment_call": commitment_call,
        "state_parameter": str(expected.get("state_parameter", "state")),
        "requirements_parameter": str(expected.get("requirements_parameter", "requirements")),
        "result_parameter": str(expected.get("result_parameter", "result")),
        "recipient_owner": str(expected.get("authorization_recipient_owner", "")),
        "recipient_field": str(expected.get("authorization_recipient_field", "id")),
    }
    origin_paths = _trace_authorization_call_origins(
        function,
        authorization_index,
        origin_expectations,
        edges=edges,
        instruction_by_offset=instruction_by_offset,
    )
    expected_origins = (
        frozenset({f"parameter:{authorization_parameter}"}),
        frozenset({"requirements_proof"}),
        frozenset({"state_uid"}),
        frozenset({"input_commitment"}),
    )
    if any(arguments != expected_origins for arguments in origin_paths):
        raise CompiledMoveInvariantError(
            "workflow authorization verifier call consumes unbound proof, UID, recipient, or commitment origins"
        )
    authorization_moved = False
    for instruction in prior[max(0, len(prior) - 16) :]:
        reference = _local_reference(instruction)
        if reference and reference[0] == authorization_parameter and "ProvenValue" in reference[1]:
            authorization_moved = instruction.opcode == "MoveLoc"
    if not authorization_moved:
        raise CompiledMoveInvariantError(
            f"canonical workflow authorization call does not consume execute parameter {authorization_parameter!r}"
        )

    if not any(_call_target_matches(instruction.call_target, proof_call) for instruction in prior[-16:]):
        raise CompiledMoveInvariantError("workflow authorization is not bound to requirements.proof()")
    if not any(_call_target_matches(instruction.call_target, commitment_call) for instruction in prior[-20:]):
        raise CompiledMoveInvariantError("workflow authorization is not bound to OnchainToolResult.input_commitment")
    commitment_moved = any(
        instruction.opcode == "MoveLoc"
        and (reference := _local_reference(instruction)) is not None
        and reference[0] == "input_commitment"
        for instruction in prior[-20:]
    )
    if not commitment_moved:
        raise CompiledMoveInvariantError("workflow authorization does not consume the computed input commitment")

    recipient_owner = str(expected.get("authorization_recipient_owner", ""))
    recipient_field = str(expected.get("authorization_recipient_field", "id"))
    recipient_found = False
    for instruction in prior[-20:]:
        field_match = FIELD_RE.search(instruction.encoded)
        if field_match is None:
            continue
        field_name = field_match.group("field").strip()
        field_owner, separator, field_leaf = field_name.rpartition(".")
        field_owner = field_owner.rsplit("::", 1)[-1] if separator else ""
        if (
            field_match.group("type").strip() == "UID"
            and field_owner == recipient_owner
            and field_leaf == recipient_field
        ):
            recipient_found = True
            break
    if not recipient_found:
        raise CompiledMoveInvariantError(
            f"workflow authorization recipient is not the execute state field *.{recipient_field}"
        )


def _validate_parsed_execute(
    modules: Sequence[CompiledModule],
    *,
    module_name: str,
    function_name: str,
    invariant: Mapping[str, object],
    expected_module_address: str,
) -> dict[str, object]:
    matching_modules = [module for module in modules if module.name == module_name]
    if len(matching_modules) != 1:
        raise CompiledMoveInvariantError(f"trusted compiled artifact has ambiguous module {module_name!r}")
    module = matching_modules[0]
    if module.address != expected_module_address:
        raise CompiledMoveInvariantError(
            f"trusted compiled module address {module.address!r} does not match the build receipt "
            f"{expected_module_address!r}"
        )
    function = module.functions.get(function_name)
    if function is None:
        raise CompiledMoveInvariantError(f"trusted compiled module {module_name!r} has no function {function_name!r}")
    expected_parameters = {
        str(item["name"]): str(item["type"])
        for item in invariant.get("parameters", [])
        if isinstance(item, Mapping) and "name" in item and "type" in item
    }
    compiled_parameters = dict(function.parameters)
    for name, type_name in expected_parameters.items():
        if compiled_parameters.get(name) != type_name:
            raise CompiledMoveInvariantError(
                f"compiled {module_name}::{function_name} parameter {name!r} has type "
                f"{compiled_parameters.get(name)!r}, expected {type_name!r}"
            )
    authorization_call = invariant.get("authorization_call")
    authorization_indices: list[int] = []
    authorization_identity: str | None = None
    if authorization_call is not None:
        authorization_call = str(authorization_call)
        authorization_identity = _authorization_call_identity(invariant)
        authorization_candidates = [
            index
            for index, instruction in enumerate(function.instructions)
            if instruction.call_target == authorization_call
        ]
        authorization_indices = [
            index
            for index, instruction in enumerate(function.instructions)
            if _authorization_call_matches(instruction, invariant)
        ]
        if len(authorization_candidates) != 1 or len(authorization_indices) != 1:
            raise CompiledMoveInvariantError(
                f"compiled {module_name}::{function_name} must contain exactly one exact {authorization_call} call"
            )
    satisfy_call = str(invariant["satisfy_call"])
    finalize_call = str(invariant["finalize_call"])
    output_call = str(invariant.get("output_call", "tagged_output::new"))
    satisfy_indices = [
        index for index, instruction in enumerate(function.instructions) if instruction.call_target == satisfy_call
    ]
    if len(satisfy_indices) != 1:
        raise CompiledMoveInvariantError(
            f"compiled {module_name}::{function_name} must contain exactly one {satisfy_call} call"
        )
    finalize_indices = [
        index for index, instruction in enumerate(function.instructions) if instruction.call_target == finalize_call
    ]
    if len(finalize_indices) != 1:
        raise CompiledMoveInvariantError(
            f"compiled {module_name}::{function_name} must contain exactly one {finalize_call} call"
        )
    satisfy_index = satisfy_indices[0]
    finalize_index = finalize_indices[0]
    edges = _instruction_edges(function)
    instruction_by_offset = {instruction.offset: instruction for instruction in function.instructions}
    authorization_failure_offsets: set[int] = set()
    if authorization_indices:
        authorization_index = authorization_indices[0]
        if authorization_index >= satisfy_index:
            raise CompiledMoveInvariantError(
                "workflow authorization must be consumed before witness satisfaction and state mutation"
            )
        _validate_authorization_relationship(
            function,
            authorization_index,
            invariant,
            edges=edges,
            instruction_by_offset=instruction_by_offset,
        )
        authorization_failure_offsets = _authorization_result_failure_offsets(
            function,
            authorization_index,
            satisfy_index,
            finalize_index,
            edges=edges,
            instruction_by_offset=instruction_by_offset,
            satisfy_call=satisfy_call,
            finalize_call=finalize_call,
            authorization_call=str(authorization_call),
            authorization_identity=str(authorization_identity),
        )
    if satisfy_index >= finalize_index:
        raise CompiledMoveInvariantError("witness satisfaction must precede finalization in compiled execute")
    _validate_witness_relationship(function, satisfy_index, invariant)
    satisfy_instruction = function.instructions[satisfy_index]
    if "&mut UIDRequirements" not in satisfy_instruction.operands or "&UID" not in satisfy_instruction.operands:
        raise CompiledMoveInvariantError("UIDRequirements::satisfy has the wrong compiled argument types")
    finalize_instruction = function.instructions[finalize_index]
    expected_finalize_types = str(
        invariant.get("finalize_types", "OnchainToolResult, UIDRequirements, TaggedOutput, &mut TxContext")
    )
    if expected_finalize_types not in finalize_instruction.operands:
        raise CompiledMoveInvariantError(
            f"{finalize_call} has compiled arguments {finalize_instruction.operands!r}, "
            f"expected {expected_finalize_types!r}"
        )
    initial = function.instructions[0].offset
    authorization_required = bool(authorization_indices)
    stack: list[tuple[int, bool, bool, bool, bool]] = [(initial, not authorization_required, False, False, False)]
    visited: set[tuple[int, bool, bool, bool, bool]] = set()
    exits = 0
    while stack:
        offset, authorized, satisfied, finalized, output_seen = stack.pop()
        state = (offset, authorized, satisfied, finalized, output_seen)
        if state in visited:
            continue
        visited.add(state)
        instruction = instruction_by_offset[offset]
        target = instruction.call_target
        next_authorized = authorized
        next_satisfied = satisfied
        next_finalized = finalized
        next_output = output_seen
        if authorization_indices and _authorization_call_matches(instruction, invariant):
            next_authorized = True
        elif target == satisfy_call:
            if authorization_required and not authorized:
                raise CompiledMoveInvariantError(
                    f"compiled path reaches {satisfy_call} before workflow authorization consumption"
                )
            next_satisfied = True
        elif target == finalize_call:
            if authorization_required and not authorized:
                raise CompiledMoveInvariantError(
                    f"compiled path reaches {finalize_call} before workflow authorization consumption"
                )
            if not satisfied:
                raise CompiledMoveInvariantError(f"compiled path reaches {finalize_call} before {satisfy_call}")
            if not output_seen:
                raise CompiledMoveInvariantError("compiled path finalizes without a TaggedOutput")
            next_finalized = True
        elif target == output_call:
            next_output = True
        if instruction.opcode in {"Ret", "Abort"}:
            exits += 1
            if instruction.opcode == "Abort" and offset in authorization_failure_offsets:
                continue
            if not next_authorized or not next_satisfied or not next_finalized or not next_output:
                raise CompiledMoveInvariantError(
                    "compiled execute has a return path that bypasses witness satisfaction, TaggedOutput, or finalization"
                )
        for next_offset in edges[offset]:
            stack.append((next_offset, next_authorized, next_satisfied, next_finalized, next_output))
    if exits == 0:
        raise CompiledMoveInvariantError(f"compiled {module_name}::{function_name} has no reachable return")
    return {
        "module": module_name,
        "module_address": module.address,
        "function": function_name,
        "satisfy_call": satisfy_call,
        "finalize_call": finalize_call,
        **(
            {
                "authorization_call": str(authorization_call),
                "authorization_identity": str(authorization_identity),
            }
            if authorization_call is not None
            else {}
        ),
        "reachable_returns": exits,
        "compiled_instructions": len(function.instructions),
    }


def capture_build_receipt(
    *,
    package_root: Path,
    build_root: Path,
    module_name: str,
    function_name: str,
    environment: Mapping[str, str] | None = None,
    schema_summary: Path | None = None,
    schema_summary_root: Path | None = None,
    schema_digest: str | None = None,
    tree_manifest_root: Path | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, object]:
    """Capture trusted identity and digests immediately after a successful build."""

    compiled_module = _find_compiled_module(build_root, module_name)
    saved_disassembly = _find_saved_disassembly(build_root, module_name)
    saved_disassembly_bytes = saved_disassembly.read_bytes() if saved_disassembly is not None else None
    modules, trusted_bytes, command, version = _trusted_disassemble(
        compiled_module,
        package_root.resolve(),
        build_root=build_root,
        environment=environment,
        command_runner=command_runner,
    )
    if saved_disassembly is not None and saved_disassembly_bytes is not None:
        # The CLI may rebuild its package while disassembling and replace the
        # generated .mvb. Preserve the successful build's saved witness so its
        # receipt can detect later stale/fake edits without parsing it.
        if not saved_disassembly.exists() or saved_disassembly.read_bytes() != saved_disassembly_bytes:
            saved_disassembly.parent.mkdir(parents=True, exist_ok=True)
            saved_disassembly.write_bytes(saved_disassembly_bytes)
        if not _saved_disassembly_matches_trusted(saved_disassembly_bytes, trusted_bytes):
            raise CompiledMoveInvariantError("saved disassembly does not match trusted Sui disassembly output")
    matches = [module for module in modules if module.name == module_name]
    if len(matches) != 1 or function_name not in matches[0].functions:
        raise CompiledMoveInvariantError(
            f"trusted Sui disassembly does not expose {module_name}::{function_name} exactly once"
        )
    module = matches[0]
    return make_build_receipt(
        package_root=package_root,
        build_root=build_root,
        compiled_module=compiled_module,
        module_address=module.address,
        module_name=module.name,
        function_name=function_name,
        trusted_disassembly_sha256=sha256_bytes(trusted_bytes),
        toolchain={
            "command": [command[0], "move", "disassemble"],
            "version": version,
            "resolved_binary": command[0],
        },
        disassembly_path=saved_disassembly,
        schema_summary=schema_summary,
        schema_summary_root=schema_summary_root,
        schema_digest=schema_digest,
        tree_manifest_root=tree_manifest_root,
    )


def validate_compiled_execute(
    build_root: Path,
    *,
    package_root: Path,
    module_name: str,
    function_name: str,
    invariant: Mapping[str, object],
    build_receipt: Mapping[str, object],
    expected_compiled_sha256: str | None = None,
    expected_module_address: str | None = None,
    expected_module_name: str | None = None,
    expected_function_name: str | None = None,
    expected_compiled_digest: str | None = None,
    expected_build_info_sha256: str | None = None,
    expected_module_id: str | None = None,
    expected_schema_digest: str | None = None,
    expected_receipt_sha256: str | None = None,
    expected_package_tree_sha256: str | None = None,
    expected_build_tree_sha256: str | None = None,
    expected_package_tree_manifest: Path | None = None,
    expected_build_tree_manifest: Path | None = None,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, object]:
    """Validate every reachable execute path from the anchored compiled module."""

    expected_compiled_sha256 = expected_compiled_sha256 or expected_compiled_digest
    if not expected_compiled_sha256 or not all(
        isinstance(value, str) and value
        for value in (expected_module_address, expected_module_name, expected_function_name)
    ):
        raise CompiledMoveInvariantError(
            "explicit compiled SHA-256 and module address/name/function acceptance inputs are required"
        )
    receipt_evidence = validate_build_receipt(
        build_receipt,
        package_root=package_root,
        build_root=build_root,
        expected_compiled_sha256=expected_compiled_sha256,
        expected_module_id=expected_module_id,
        expected_module_address=expected_module_address,
        expected_module_name=expected_module_name,
        expected_function_name=expected_function_name,
        expected_build_info_sha256=expected_build_info_sha256,
        expected_schema_digest=expected_schema_digest,
        expected_receipt_sha256=expected_receipt_sha256,
        expected_package_tree_sha256=expected_package_tree_sha256,
        expected_build_tree_sha256=expected_build_tree_sha256,
        expected_package_tree_manifest=expected_package_tree_manifest,
        expected_build_tree_manifest=expected_build_tree_manifest,
    )
    compiled_module = Path(str(receipt_evidence["compiled_module"]))
    modules, trusted_bytes, command, version = _trusted_disassemble(
        compiled_module,
        package_root.resolve(),
        build_root=Path(str(receipt_evidence["build_root"])),
        environment=environment,
        command_runner=command_runner,
    )
    _validate_toolchain_identity(build_receipt, command, version)
    # The Sui disassembler is allowed to refresh local build metadata while
    # deriving its output. Recheck both roots after it exits so any residual
    # file, mode, or symlink mutation cannot be hidden by the preflight tree
    # validation above.
    validate_tree_manifest(
        Path(str(receipt_evidence["build_root"])),
        Path(str(receipt_evidence["build_tree_manifest"])),
        str(receipt_evidence["build_tree_sha256"]),
        "build",
    )
    validate_tree_manifest(
        Path(str(receipt_evidence["package_root"])),
        Path(str(receipt_evidence["package_tree_manifest"])),
        str(receipt_evidence["package_tree_sha256"]),
        "package",
    )
    if sha256_bytes(trusted_bytes) != build_receipt.get("trusted_disassembly_sha256"):
        raise CompiledMoveInvariantError("trusted disassembly digest does not match the build receipt")
    saved_disassembly = build_receipt.get("disassembly")
    if saved_disassembly is not None:
        saved_bytes = Path(str(saved_disassembly)).read_bytes()
        if not _saved_disassembly_matches_trusted(saved_bytes, trusted_bytes):
            raise CompiledMoveInvariantError("saved disassembly does not match trusted Sui disassembly output")
    semantic = _validate_parsed_execute(
        modules,
        module_name=module_name,
        function_name=function_name,
        invariant=invariant,
        expected_module_address=expected_module_address,
    )
    semantic.update(
        {
            "compiled_module": str(compiled_module),
            "compiled_sha256": receipt_evidence["compiled_sha256"],
            "build_root": receipt_evidence["build_root"],
            "package_root": receipt_evidence["package_root"],
            "build_info": receipt_evidence["build_info"],
            "build_info_sha256": receipt_evidence["build_info_sha256"],
            "receipt_sha256": receipt_evidence["receipt_sha256"],
            "trusted_disassembly_sha256": sha256_bytes(trusted_bytes),
            "package_tree_manifest": receipt_evidence["package_tree_manifest"],
            "package_tree_sha256": receipt_evidence["package_tree_sha256"],
            "build_tree_manifest": receipt_evidence["build_tree_manifest"],
            "build_tree_sha256": receipt_evidence["build_tree_sha256"],
            "toolchain": {"command": command, "version": version},
        }
    )
    return semantic
