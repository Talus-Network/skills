#!/usr/bin/env python3
"""Validate a repository-owned TAP package and its local artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

_SHARED_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if _SHARED_SCRIPTS.is_dir():
    sys.path.insert(0, str(_SHARED_SCRIPTS))

from move_manifest_closure import MoveManifestClosureError, validate_move_manifest_closure  # noqa: E402
from fixture_contract import FixtureContractError, validate_fixture_contract  # noqa: E402


class TapArtifactError(ValueError):
    """Raised when a TAP artifact is malformed or escapes its project."""


_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
_FQN_RE = re.compile(r"[A-Za-z0-9_.-]+@[0-9]+\Z")
_VERTEX_VARIANTS = frozenset({"on_chain", "off_chain"})


def _canonical_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise TapArtifactError(f"{label} must be a canonical lowercase identifier")
    return value


def _canonical_fqn(value: object, label: str) -> str:
    if not isinstance(value, str) or not _FQN_RE.fullmatch(value):
        raise TapArtifactError(f"{label} must be a canonical Tool FQN")
    return value


def _port_names(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise TapArtifactError(f"{label} must be a non-empty port list")
    names: list[str] = []
    for index, port in enumerate(value):
        if not isinstance(port, dict) or set(port) != {"name"}:
            raise TapArtifactError(f"{label}[{index}] must declare exactly one name")
        names.append(_canonical_identifier(port.get("name"), f"{label}[{index}].name"))
    if len(set(names)) != len(names):
        raise TapArtifactError(f"{label} contains duplicate ports")
    return names


def _output_port_names(value: object, label: str) -> dict[str, list[str]]:
    if not isinstance(value, dict) or not value:
        raise TapArtifactError(f"{label} must be a non-empty output-port mapping")
    result: dict[str, list[str]] = {}
    for variant, ports in value.items():
        variant_name = _canonical_identifier(variant, f"{label} variant")
        result[variant_name] = _port_names(ports, f"{label}[{variant_name!r}]")
    return result


def _bytes_value(value: object, label: str) -> bytes:
    if isinstance(value, list) and all(
        isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255 for item in value
    ):
        return bytes(value)
    if isinstance(value, str) and value and value.startswith("0x"):
        try:
            return bytes.fromhex(value[2:])
        except ValueError as exc:
            raise TapArtifactError(f"{label} must contain valid bytes") from exc
    raise TapArtifactError(f"{label} must contain a byte list or 0x string")


def _fixed_tool_fqn(value: object, label: str) -> str:
    if not isinstance(value, dict) or set(value) != {"bytes"}:
        raise TapArtifactError(f"{label} must contain exactly a bytes field")
    raw = _bytes_value(value["bytes"], f"{label}.bytes")
    try:
        decoded = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TapArtifactError(f"{label}.bytes must encode an ASCII Tool FQN") from exc
    return _canonical_fqn(decoded, label)


def _registry_id(value: object, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"bytes"}:
        raise TapArtifactError(f"{label} must contain exactly a bytes field")
    raw = value["bytes"]
    if isinstance(raw, list):
        _bytes_value(raw, f"{label}.bytes")
    elif not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", raw):
        raise TapArtifactError(f"{label}.bytes must contain a byte list or hexadecimal string")


def _policy_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*\Z", value):
        raise TapArtifactError(f"{label} must be a non-empty policy identifier")
    return value


def _shared_objects(value: object, label: str) -> None:
    if not isinstance(value, list):
        raise TapArtifactError(f"{label} must be a list")
    for index, shared in enumerate(value):
        if not isinstance(shared, dict) or set(shared) != {"id", "mutable"}:
            raise TapArtifactError(f"{label}[{index}] binding is malformed")
        if not isinstance(shared["id"], str) or not re.fullmatch(r"0x[0-9a-fA-F]+\Z", shared["id"]):
            raise TapArtifactError(f"{label}[{index}].id must be a hexadecimal object ID")
        if not isinstance(shared["mutable"], bool):
            raise TapArtifactError(f"{label}[{index}].mutable must be boolean")


def _validate_acyclic(path: Path, vertex_names: set[str], edges: list[dict[str, str]]) -> list[str]:
    """Return a stable topological order and reject every directed cycle."""

    adjacency = {name: [] for name in vertex_names}
    indegree = {name: 0 for name in vertex_names}
    for index, edge in enumerate(edges):
        source = edge["from"]
        target = edge["to"]
        if source == target:
            raise TapArtifactError(f"{path}: DAG contains a self-loop at vertex {source!r} (edge {index})")
        adjacency[source].append((target, index))
        indegree[target] += 1
    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target, _index in sorted(adjacency[current], key=lambda item: (item[0], item[1])):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(order) != len(vertex_names):
        cycle_vertices = sorted(name for name, degree in indegree.items() if degree > 0)
        cycle_set = set(cycle_vertices)
        cycle_edges = [
            f"{edge['from']}->{edge['to']} (edge {index})"
            for index, edge in enumerate(edges)
            if edge["from"] in cycle_set and edge["to"] in cycle_set
        ]
        raise TapArtifactError(
            f"{path}: DAG contains a cycle involving vertices {cycle_vertices!r} and edges {cycle_edges!r}"
        )
    return order


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _check_relative_file(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise TapArtifactError(f"{label} must be a project-relative path")
    path = (root / value).resolve()
    if not _inside(root, path) or not path.is_file():
        raise TapArtifactError(f"{label} does not resolve to a project file")
    return path


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TapArtifactError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TapArtifactError(f"{label} must be a JSON object")
    return value


def _validate_manifest(package_dir: Path, project_root: Path) -> dict[str, Any]:
    manifest_path = package_dir / "Move.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TapArtifactError(f"Move.toml is invalid: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("package"), dict):
        raise TapArtifactError("Move.toml has no package table")
    if "environments" in manifest:
        environments = manifest["environments"]
        if not isinstance(environments, dict) or any(str(alias).lower() in {"mainnet", "testnet"} for alias in environments):
            raise TapArtifactError("system network aliases are not valid package overrides")
    try:
        closure = validate_move_manifest_closure(package_dir, manifest_path)
    except MoveManifestClosureError as exc:
        raise TapArtifactError(str(exc)) from exc
    fixture_contract = None
    if (package_dir / "dependency-template.json").exists() or (package_dir / "fixture-contract.json").exists():
        try:
            fixture_contract = validate_fixture_contract(package_dir)
        except FixtureContractError as exc:
            raise TapArtifactError(f"fixture contract is invalid: {exc}") from exc
    return {
        "path": str(manifest_path.relative_to(project_root)),
        "package": manifest["package"],
        "manifests": [str(Path(path).relative_to(project_root)) for path in closure["manifests"]],
        "dependency_counts": dict(closure["dependency_counts"]),
        "fixture_contract": fixture_contract,
    }


def _validate_dag(path: Path, project_root: Path) -> dict[str, Any]:
    document = _read_json(path, str(path))
    vertices = document.get("vertices")
    edges = document.get("edges")
    outputs = document.get("outputs")
    if not isinstance(vertices, list) or not vertices:
        raise TapArtifactError(f"{path}: vertices must be a non-empty list")
    if not isinstance(edges, list):
        raise TapArtifactError(f"{path}: edges must be a list")
    if not isinstance(outputs, list) or not outputs:
        raise TapArtifactError(f"{path}: outputs must be a non-empty list")

    vertex_names: set[str] = set()
    vertex_records: dict[str, dict[str, Any]] = {}
    for index, vertex in enumerate(vertices):
        if not isinstance(vertex, dict):
            raise TapArtifactError(f"{path}: vertex {index} is malformed")
        name = _canonical_identifier(vertex.get("name"), f"{path}: vertex {index}.name")
        if name in vertex_names:
            raise TapArtifactError(f"{path}: duplicate vertex name {name!r}")
        kind = vertex.get("kind")
        if not isinstance(kind, dict) or kind.get("variant") not in _VERTEX_VARIANTS:
            raise TapArtifactError(f"{path}: vertex {name!r} has an unknown kind variant")
        fqn = _canonical_fqn(kind.get("tool_fqn"), f"{path}: vertex {name!r}.kind.tool_fqn")
        entry_ports = _port_names(vertex.get("entry_ports"), f"{path}: vertex {name!r}.entry_ports")
        if "output_ports" not in vertex:
            raise TapArtifactError(f"{path}: vertex {name!r} must declare output_ports")
        output_ports = _output_port_names(vertex["output_ports"], f"{path}: vertex {name!r}.output_ports")
        vertex_names.add(name)
        vertex_records[name] = {
            "variant": kind["variant"],
            "fqn": fqn,
            "entry_ports": set(entry_ports),
            "output_ports": output_ports,
        }

    def edge_port(record: dict[str, Any], port: str, *, source: bool, label: str) -> None:
        output_ports = record["output_ports"]
        if source:
            if not any(port in ports for ports in output_ports.values()):
                raise TapArtifactError(f"{path}: {label} is not a declared output port")
        elif port not in record["entry_ports"]:
            direction = "source" if source else "target"
            raise TapArtifactError(f"{path}: {label} is not a declared {direction} port")

    edge_records: list[dict[str, str]] = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise TapArtifactError(f"{path}: edge {index} is malformed")
        values: dict[str, str] = {}
        for field in ("from", "to", "source_port", "target_port"):
            values[field] = _canonical_identifier(edge.get(field), f"{path}: edge {index}.{field}")
        source_name = values["from"]
        target_name = values["to"]
        if source_name not in vertex_records or target_name not in vertex_records:
            raise TapArtifactError(f"{path}: edge {index} references an undeclared vertex")
        edge_port(vertex_records[source_name], values["source_port"], source=True, label=f"edge {index}.source_port")
        edge_port(vertex_records[target_name], values["target_port"], source=False, label=f"edge {index}.target_port")
        edge_records.append(values)

    topological_order = _validate_acyclic(path, vertex_names, edge_records)
    tool_fqns = [record["fqn"] for record in vertex_records.values()]
    repeated_tool_fqns = sorted(fqn for fqn, count in Counter(tool_fqns).items() if count > 1)
    if repeated_tool_fqns:
        raise TapArtifactError(f"{path}: DAG contains repeated Tool FQN identities: {repeated_tool_fqns!r}")

    seen_outputs: set[tuple[str, str, str]] = set()
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            raise TapArtifactError(f"{path}: output {index} is malformed")
        values = {
            "vertex": _canonical_identifier(output.get("vertex"), f"{path}: output {index}.vertex"),
            "output_variant": _canonical_identifier(
                output.get("output_variant"), f"{path}: output {index}.output_variant"
            ),
            "output_port": _canonical_identifier(output.get("output_port"), f"{path}: output {index}.output_port"),
        }
        if values["vertex"] not in vertex_records:
            raise TapArtifactError(f"{path}: output {index} references an undeclared vertex")
        declared = vertex_records[values["vertex"]]["output_ports"]
        if values["output_port"] not in declared.get(values["output_variant"], []):
            raise TapArtifactError(f"{path}: output {index} references an undeclared variant or port")
        identity = (values["vertex"], values["output_variant"], values["output_port"])
        if identity in seen_outputs:
            raise TapArtifactError(f"{path}: duplicate output binding {identity!r}")
        seen_outputs.add(identity)
    shared_objects = document.get("shared_objects", [])
    _shared_objects(shared_objects, f"{path}: shared_objects")
    return {
        "path": str(path.relative_to(project_root)),
        "vertices": len(vertices),
        "edges": len(edges),
        "outputs": len(outputs),
        "topological_order": topological_order,
        "vertex_fqns": {name: record["fqn"] for name, record in vertex_records.items()},
        "tool_fqns": tool_fqns,
        "entry_ports": {name: sorted(record["entry_ports"]) for name, record in vertex_records.items()},
        "output_ports": {
            name: {variant: list(ports) for variant, ports in record["output_ports"].items()}
            for name, record in vertex_records.items()
        },
    }


def _validate_skill(
    path: Path, project_root: Path, validated_dags: Mapping[Path, Mapping[str, Any]]
) -> dict[str, Any]:
    document = _read_json(path, str(path))
    name = _canonical_identifier(document.get("name"), f"{path}: skill name")
    dag_path = _check_relative_file(path.parent, document.get("dag_path"), "skill dag_path")
    expected_dag_name = path.name.removesuffix(".skill.tap.json") + ".dag.json"
    if (
        dag_path.suffixes[-2:] != [".dag", ".json"]
        or dag_path.name != expected_dag_name
        or dag_path not in validated_dags
    ):
        raise TapArtifactError(f"{path}: skill dag_path must name the exact validated *.dag.json artifact")
    requirements = document.get("requirements")
    if not isinstance(requirements, dict):
        raise TapArtifactError(f"{path}: skill requirements are required")
    commitment = requirements.get("input_commitment")
    if not isinstance(commitment, list) or not commitment:
        raise TapArtifactError(f"{path}: skill input_commitment must be a non-empty byte list")
    _bytes_value(commitment, f"{path}: skill requirements.input_commitment")
    _policy_value(requirements.get("payment_policy"), f"{path}: skill requirements.payment_policy")
    _policy_value(requirements.get("schedule_policy"), f"{path}: skill requirements.schedule_policy")
    fixed_tools = requirements.get("fixed_tools")
    if not isinstance(fixed_tools, list) or not fixed_tools:
        raise TapArtifactError(f"{path}: skill requirements.fixed_tools must be a non-empty list")
    dag_info = validated_dags[dag_path]
    dag_tool_fqns = dag_info.get("tool_fqns")
    if not isinstance(dag_tool_fqns, list) or not all(isinstance(fqn, str) for fqn in dag_tool_fqns):
        raise TapArtifactError(f"{path}: validated DAG has no Tool FQN inventory")
    fixed_tool_fqns: list[str] = []
    for index, fixed_tool in enumerate(fixed_tools):
        if not isinstance(fixed_tool, dict) or set(fixed_tool) != {"tool_registry_id", "tool_fqn"}:
            raise TapArtifactError(f"{path}: fixed Tool {index} binding is malformed")
        _registry_id(fixed_tool["tool_registry_id"], f"{path}: fixed Tool {index}.tool_registry_id")
        fixed_fqn = _fixed_tool_fqn(fixed_tool["tool_fqn"], f"{path}: fixed Tool {index}.tool_fqn")
        fixed_tool_fqns.append(fixed_fqn)
    repeated_fixed_fqns = sorted(fqn for fqn, count in Counter(fixed_tool_fqns).items() if count > 1)
    if repeated_fixed_fqns:
        raise TapArtifactError(f"{path}: fixed Tool bindings contain duplicates: {repeated_fixed_fqns!r}")
    dag_counts = Counter(dag_tool_fqns)
    fixed_counts = Counter(fixed_tool_fqns)
    if dag_counts != fixed_counts:
        missing = sorted((dag_counts - fixed_counts).elements())
        extra = sorted((fixed_counts - dag_counts).elements())
        raise TapArtifactError(
            f"{path}: fixed Tool bindings are not a bijection with DAG Tool vertices; "
            f"missing={missing!r}, extra={extra!r}"
        )
    shared_objects = requirements.get("shared_objects", [])
    _shared_objects(shared_objects, f"{path}: skill requirements.shared_objects")
    interface_revision = document.get("interface_revision")
    if (
        not isinstance(interface_revision, dict)
        or not isinstance(interface_revision.get("inner"), int)
        or isinstance(interface_revision.get("inner"), bool)
        or interface_revision["inner"] < 0
    ):
        raise TapArtifactError(f"{path}: interface_revision.inner must be a non-negative integer")
    return {
        "path": str(path.relative_to(project_root)),
        "name": name,
        "dag_path": str(dag_path.relative_to(project_root)),
        "fixed_tools": len(fixed_tools),
        "dag_tool_fqns": list(dag_tool_fqns),
        "fixed_tool_fqns": fixed_tool_fqns,
        "interface_revision": interface_revision["inner"],
    }


def verify_tap_artifacts(root: Path, *, require_artifacts: bool = False) -> dict[str, Any]:
    project_root = root.resolve()
    if not project_root.is_dir():
        raise TapArtifactError(f"project root is not a directory: {root}")
    package_dir = project_root / "tap" if (project_root / "tap" / "Move.toml").is_file() else project_root
    checks: list[dict[str, Any]] = []
    try:
        checks.append({"id": "move-manifest", "status": "pass", **_validate_manifest(package_dir, project_root)})
    except TapArtifactError as exc:
        checks.append({"id": "move-manifest", "status": "fail", "detail": str(exc)})
    artifact_root = project_root / "artifacts"
    dag_paths = sorted(artifact_root.glob("*.dag.json")) if artifact_root.is_dir() else []
    skill_paths = sorted(artifact_root.glob("*.skill.tap.json")) if artifact_root.is_dir() else []
    if require_artifacts and (not dag_paths or not skill_paths):
        checks.append({"id": "artifacts-present", "status": "fail", "detail": "required DAG and skill artifacts are missing"})
    elif artifact_root.is_dir():
        checks.append({"id": "artifacts-present", "status": "pass", "detail": "artifact directory is present"})
    validated_dags: dict[Path, dict[str, Any]] = {}
    for path in dag_paths:
        try:
            dag_evidence = _validate_dag(path, project_root)
            checks.append({"id": f"dag:{path.name}", "status": "pass", **dag_evidence})
            validated_dags[path.resolve()] = dag_evidence
        except TapArtifactError as exc:
            checks.append({"id": f"dag:{path.name}", "status": "fail", "detail": str(exc)})
    for path in skill_paths:
        try:
            checks.append(
                {"id": f"skill:{path.name}", "status": "pass", **_validate_skill(path, project_root, validated_dags)}
            )
        except TapArtifactError as exc:
            checks.append({"id": f"skill:{path.name}", "status": "fail", "detail": str(exc)})
    status = "pass" if all(check["status"] == "pass" for check in checks) else "fail"
    return {
        "status": status,
        "evidence": "repository-owned-structural",
        "project": str(project_root),
        "checks": checks,
        "runtime_proof": "not-proven",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--require-artifacts", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = verify_tap_artifacts(args.root, require_artifacts=args.require_artifacts)
    except TapArtifactError as exc:
        report = {"status": "fail", "detail": str(exc), "runtime_proof": "not-proven"}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['status']}: {report.get('detail', 'TAP artifacts checked')}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
