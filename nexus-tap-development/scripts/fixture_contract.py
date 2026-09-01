"""Validate repository-owned TAP fixture dependency and test contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
import re
import tomllib
from typing import Any


class FixtureContractError(ValueError):
    """Raised when a checked-in fixture contract is incomplete or unsafe."""


PUBLIC_SUI_SOURCE = {
    "repository": "MystenLabs/sui",
    "revision": "d8459684b41eb09ab23fe16a9dd84173270bbaba",
    "archive_sha256": "1b974c1b10413e873b98df2740a877a930a08b40c80af58b52f737385f8bdf44",
    "archive_url": "https://codeload.github.com/MystenLabs/sui/tar.gz/d8459684b41eb09ab23fe16a9dd84173270bbaba",
    "toolchain": "sui 1.78.0-d8459684b41e",
    "framework_paths": {
        "std": "crates/sui-framework/packages/move-stdlib",
        "sui": "crates/sui-framework/packages/sui-framework",
    },
}
TEMPLATE_DEPENDENCIES = {
    "std": {"local": "deps/sui-framework/packages/move-stdlib", "package": "MoveStdlib"},
    "sui": {"local": "deps/sui-framework/packages/sui-framework", "package": "Sui"},
}
TEMPLATE_PACKAGE_NAMES = {"std": "MoveStdlib", "sui": "Sui"}
TEST_RE = re.compile(
    r"(?m)^\s*#\[test(?P<expected>,\s*expected_failure\(abort_code\s*=\s*(?P<abort>\d+)\))?\]\s*\n\s*fun\s+(?P<name>[A-Za-z0-9_]+)"
)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureContractError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FixtureContractError(f"{label} must be a JSON object")
    return value


def _read_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise FixtureContractError(f"{label} is not valid TOML: {exc}") from exc
    if not isinstance(value, dict):
        raise FixtureContractError(f"{label} must be a TOML table")
    return value


def _sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise FixtureContractError(f"cannot hash fixture contract file: {path}") from exc
    return digest


def _relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise FixtureContractError(f"{label} must be a repository-relative path")
    return value


def _exact_mapping(value: object, expected: Mapping[str, object], label: str) -> None:
    if not isinstance(value, Mapping) or dict(value) != dict(expected):
        raise FixtureContractError(f"{label} does not match the approved public template")


def _validate_template(project: Path, fixture_name: str, package_name: str) -> dict[str, Any]:
    path = project / "dependency-template.json"
    document = _read_json(path, "dependency-template.json")
    if document.get("schema_version") != 1 or document.get("fixture") != fixture_name:
        raise FixtureContractError("dependency-template.json has the wrong schema or fixture identity")
    if document.get("package") != package_name:
        raise FixtureContractError("dependency-template.json package identity is stale")
    _exact_mapping(document.get("source"), PUBLIC_SUI_SOURCE, "dependency-template.json source")
    template = document.get("template")
    if not isinstance(template, Mapping) or template.get("manifest") != "Move.toml":
        raise FixtureContractError("dependency-template.json must name the root Move.toml template")
    _exact_mapping(template.get("dependencies"), TEMPLATE_DEPENDENCIES, "dependency-template.json dependencies")
    package_manifests = template.get("package_manifests")
    expected_manifests = {
        alias: f"{specification['local']}/Move.toml" for alias, specification in TEMPLATE_DEPENDENCIES.items()
    }
    _exact_mapping(package_manifests, expected_manifests, "dependency-template.json package_manifests")
    rewrite = document.get("rewrite")
    expected_rewrite = {
        "mode": "replace-template-with-authenticated-public-sui-framework",
        "destination": "deps/sui-framework/packages",
        "source": "source",
        "excluded_files": ["Move.lock"],
    }
    _exact_mapping(rewrite, expected_rewrite, "dependency-template.json rewrite")
    for alias, specification in TEMPLATE_DEPENDENCIES.items():
        local = _relative_path(specification["local"], f"dependency-template.json {alias}.local")
        if local != TEMPLATE_DEPENDENCIES[alias]["local"]:
            raise FixtureContractError("dependency-template.json framework path is not canonical")
        manifest_path = project / expected_manifests[alias]
        manifest = _read_toml(manifest_path, f"template {alias} Move.toml")
        package = manifest.get("package")
        if not isinstance(package, Mapping) or package.get("name") != TEMPLATE_PACKAGE_NAMES[alias]:
            raise FixtureContractError(f"template {alias} package identity is not canonical")
    root_manifest = _read_toml(project / "Move.toml", "fixture Move.toml")
    root_package = root_manifest.get("package")
    if not isinstance(root_package, Mapping) or root_package.get("implicit-dependencies") is not False:
        raise FixtureContractError("fixture Move.toml must disable implicit dependencies")
    dependencies = root_manifest.get("dependencies")
    _exact_mapping(dependencies, TEMPLATE_DEPENDENCIES, "fixture Move.toml dependencies")
    return {
        "path": "dependency-template.json",
        "sha256": _sha256_file(path),
        "source": dict(document["source"]),
        "template": dict(template),
        "rewrite": dict(rewrite),
    }


def _validate_test_inventory(project: Path, fixture_name: str, package_name: str) -> dict[str, Any]:
    contract_path = project / "fixture-contract.json"
    document = _read_json(contract_path, "fixture-contract.json")
    if document.get("schema_version") != 1 or document.get("fixture") != fixture_name:
        raise FixtureContractError("fixture-contract.json has the wrong schema or fixture identity")
    if document.get("package") != package_name or document.get("dependency_template") != "dependency-template.json":
        raise FixtureContractError("fixture-contract.json package/dependency identity is stale")
    if document.get("rewrite_provenance") != "dependency-template.json":
        raise FixtureContractError("fixture-contract.json lacks dependency rewrite provenance")
    source_module = document.get("source_module")
    test_module = document.get("test_module")
    if not isinstance(source_module, str) or not source_module or not isinstance(test_module, str) or not test_module:
        raise FixtureContractError("fixture-contract.json module identities are required")
    source_path = project / "sources" / f"{source_module}.move"
    test_path = project / "tests" / f"{test_module}.test.move"
    try:
        source_text = source_path.read_text(encoding="utf-8")
        test_text = test_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FixtureContractError("fixture source/test files are unavailable") from exc
    if "public fun execute" not in source_text:
        raise FixtureContractError("fixture source has no public execute path")
    discovered: dict[str, dict[str, int | None]] = {}
    for match in TEST_RE.finditer(test_text):
        discovered[match.group("name")] = {
            "abort_code": int(match.group("abort")) if match.group("abort") is not None else None
        }
    success_tests = document.get("success_tests")
    expected_failure_tests = document.get("expected_failure_tests")
    if not isinstance(success_tests, list) or not success_tests or not all(isinstance(name, str) for name in success_tests):
        raise FixtureContractError("fixture-contract.json success_tests must be a non-empty list")
    if not isinstance(expected_failure_tests, list) or not expected_failure_tests:
        raise FixtureContractError("fixture-contract.json expected_failure_tests must be a non-empty list")
    expected_failures: dict[str, int] = {}
    for index, entry in enumerate(expected_failure_tests):
        if not isinstance(entry, Mapping) or set(entry) != {"name", "abort_code", "branch"}:
            raise FixtureContractError(f"fixture-contract.json expected failure {index} is malformed")
        name = entry["name"]
        code = entry["abort_code"]
        if not isinstance(name, str) or not isinstance(code, int) or isinstance(code, bool) or code < 0:
            raise FixtureContractError(f"fixture-contract.json expected failure {index} is malformed")
        expected_failures[name] = code
    if set(success_tests) & set(expected_failures):
        raise FixtureContractError("fixture-contract.json success and expected-failure tests overlap")
    if set(discovered) != set(success_tests) | set(expected_failures):
        raise FixtureContractError("fixture-contract.json test inventory does not match Move tests")
    for name in success_tests:
        if discovered[name]["abort_code"] is not None:
            raise FixtureContractError(f"fixture success test is marked expected failure: {name}")
    for name, code in expected_failures.items():
        if discovered[name]["abort_code"] != code:
            raise FixtureContractError(f"fixture expected-failure abort code is stale: {name}")
    counts = document.get("test_counts")
    expected_counts = {
        "success": len(success_tests),
        "expected_failure": len(expected_failures),
        "total": len(discovered),
    }
    _exact_mapping(counts, expected_counts, "fixture-contract.json test_counts")
    return {
        "path": "fixture-contract.json",
        "sha256": _sha256_file(contract_path),
        "source_module": source_module,
        "test_module": test_module,
        "test_inventory": expected_counts,
        "success_tests": list(success_tests),
        "expected_failure_tests": [dict(entry) for entry in expected_failure_tests],
    }


def validate_fixture_contract(project: Path) -> dict[str, Any]:
    """Validate a checked-in fixture's public template and Move branch inventory."""

    project = project.resolve()
    fixture_name = project.name
    if fixture_name.startswith("fixture-"):
        fixture_name = fixture_name.removeprefix("fixture-")
    manifest = _read_toml(project / "Move.toml", "fixture Move.toml")
    package = manifest.get("package")
    if not isinstance(package, Mapping) or not isinstance(package.get("name"), str) or not package["name"]:
        raise FixtureContractError("fixture Move.toml package identity is missing")
    dependency = _validate_template(project, fixture_name, package["name"])
    tests = _validate_test_inventory(project, fixture_name, package["name"])
    return {
        "fixture": fixture_name,
        "package": package["name"],
        "dependency_template": dependency,
        "test_contract": tests,
    }


def validate_fixture_rewrite(project: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the post-rewrite public Sui framework dependency surface."""

    project = project.resolve()
    manifest = _read_toml(project / "Move.toml", "rewritten fixture Move.toml")
    _exact_mapping(manifest.get("dependencies"), TEMPLATE_DEPENDENCIES, "rewritten fixture dependencies")
    package_manifests: dict[str, str] = {}
    for alias, specification in TEMPLATE_DEPENDENCIES.items():
        target = (project / specification["local"]).resolve()
        try:
            target.relative_to(project)
        except ValueError as exc:
            raise FixtureContractError(f"rewritten fixture dependency escapes project: {alias}") from exc
        manifest_path = target / "Move.toml"
        if target.is_symlink() or not target.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise FixtureContractError(f"rewritten fixture dependency is not a local package: {alias}")
        package = _read_toml(manifest_path, f"rewritten {alias} Move.toml").get("package")
        if not isinstance(package, Mapping) or package.get("name") != TEMPLATE_PACKAGE_NAMES[alias]:
            raise FixtureContractError(f"rewritten fixture dependency package alias is stale: {alias}")
        package_manifests[alias] = str(manifest_path.relative_to(project))
    source = contract.get("dependency_template", {}).get("source") if isinstance(contract, Mapping) else None
    if not isinstance(source, Mapping) or dict(source) != PUBLIC_SUI_SOURCE:
        raise FixtureContractError("rewritten fixture lost public Sui source provenance")
    return {
        "mode": "replace-template-with-authenticated-public-sui-framework",
        "template_sha256": contract["dependency_template"]["sha256"],
        "rewritten_manifest_sha256": _sha256_file(project / "Move.toml"),
        "source": dict(source),
        "package_manifests": package_manifests,
    }
