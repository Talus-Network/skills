#!/usr/bin/env python3
"""Validate generated Tool artifacts against an external intent anchor.

The JSON files are not allowed to bootstrap their own authority. The caller
must supply the expected Tool identity and compiled/schema digests, while a
digest-bound build receipt binds the ABI summary and compiled Move module to a
successful build. This is an offline consistency proof; live registration and
native execution remain separate evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from validate_compiled_move import CompiledMoveInvariantError, validate_build_receipt


MANIFEST_SCHEMA_VERSION = 2


class ArtifactConsistencyError(ValueError):
    """Raised when consumer artifacts disagree with external intent."""


def _canonical_json_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_semantic_digest(document: Mapping[str, Any]) -> str:
    """Digest the complete normalized JSON semantics, including every field."""

    return _sha256_bytes(_canonical_json_bytes(dict(document)))


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ArtifactConsistencyError(f"{label} must be a SHA-256 digest")
    return value


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        content = path.read_bytes()
        document = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactConsistencyError(f"{label} is unreadable or malformed: {path}") from exc
    if not isinstance(document, dict):
        raise ArtifactConsistencyError(f"{label} must be a JSON object: {path}")
    return document, _sha256_bytes(content)


def _resolve(root: Path, relative: object, label: str, *, allow_parent: bool = False) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ArtifactConsistencyError(f"{label} path is missing")
    candidate = (root / relative).resolve()
    boundary = root.resolve().parent if allow_parent else root.resolve()
    try:
        candidate.relative_to(boundary)
    except ValueError as exc:
        raise ArtifactConsistencyError(f"{label} path escapes artifact workspace: {relative!r}") from exc
    return candidate


def _snake_case(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return value.replace("-", "_").lower()


def normalize_abi_type(value: object) -> object:
    """Normalize Move type shapes while retaining references and generics."""

    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, list):
        return [normalize_abi_type(item) for item in value]
    if not isinstance(value, dict):
        raise ArtifactConsistencyError("ABI type is malformed")
    if "Reference" in value:
        reference = value["Reference"]
        if not isinstance(reference, list) or len(reference) != 2 or not isinstance(reference[0], bool):
            raise ArtifactConsistencyError("ABI reference type is malformed")
        return {"kind": "reference", "mutable": reference[0], "to": normalize_abi_type(reference[1])}
    if "MutableReference" in value or "ImmutableReference" in value:
        key = "MutableReference" if "MutableReference" in value else "ImmutableReference"
        return {"kind": "reference", "mutable": key == "MutableReference", "to": normalize_abi_type(value[key])}
    if "Datatype" in value:
        datatype = value["Datatype"]
        if not isinstance(datatype, dict):
            raise ArtifactConsistencyError("ABI datatype is malformed")
        module = datatype.get("module")
        name = datatype.get("name")
        if (
            not isinstance(module, dict)
            or not isinstance(module.get("address"), str)
            or not isinstance(module.get("name"), str)
            or not isinstance(name, str)
        ):
            raise ArtifactConsistencyError("ABI datatype identity is malformed")
        arguments = datatype.get("type_arguments", [])
        if not isinstance(arguments, list):
            raise ArtifactConsistencyError("ABI datatype arguments are malformed")
        return {
            "kind": "datatype",
            "module": {"address": module["address"], "name": module["name"]},
            "name": name,
            "type_arguments": [normalize_abi_type(argument) for argument in arguments],
        }
    if "vector" in value or "Vector" in value:
        element = value.get("vector", value.get("Vector"))
        return {"kind": "vector", "element": normalize_abi_type(element)}
    if "TypeParameter" in value:
        parameter = value["TypeParameter"]
        if not isinstance(parameter, int):
            raise ArtifactConsistencyError("ABI type parameter is malformed")
        return {"kind": "type_parameter", "index": parameter}
    if set(value) == {"phantom", "argument"}:
        if not isinstance(value["phantom"], bool):
            raise ArtifactConsistencyError("ABI generic argument phantom flag is malformed")
        return {"phantom": value["phantom"], "argument": normalize_abi_type(value["argument"])}
    return {str(key): normalize_abi_type(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}


def _normalize_parameter(parameter: object) -> dict[str, object]:
    if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str) or not parameter["name"]:
        raise ArtifactConsistencyError("ABI parameter identity is malformed")
    type_value = parameter.get("type_", parameter.get("type"))
    if type_value is None:
        raise ArtifactConsistencyError(f"ABI parameter {parameter['name']!r} has no type")
    return {"name": parameter["name"], "type": normalize_abi_type(type_value)}


def _normalize_output_variants(document: Mapping[str, Any]) -> list[dict[str, object]]:
    variants = document.get("variants")
    if not isinstance(variants, dict) or not variants:
        raise ArtifactConsistencyError("ABI summary Output enum has no variants")
    normalized: list[dict[str, object]] = []
    for variant_name, variant in variants.items():
        fields_container = variant.get("fields") if isinstance(variant, dict) else None
        fields = fields_container.get("fields") if isinstance(fields_container, dict) else None
        if not isinstance(variant_name, str) or not isinstance(fields, dict):
            raise ArtifactConsistencyError(f"ABI output variant {variant_name!r} has no field map")
        normalized_fields: list[dict[str, object]] = []
        for field_name, field in fields.items():
            if not isinstance(field_name, str) or not isinstance(field, dict):
                raise ArtifactConsistencyError(f"ABI output variant {variant_name!r} has malformed fields")
            type_value = field.get("type_", field.get("type"))
            if type_value is None:
                raise ArtifactConsistencyError(f"ABI output field {variant_name}.{field_name} has no type")
            normalized_fields.append({"name": field_name, "type": normalize_abi_type(type_value)})
        normalized.append({"name": variant_name, "fields": normalized_fields})
    return normalized


def _move_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("bytes"), list) and all(
        isinstance(item, int) and 0 <= item <= 255 for item in value["bytes"]
    ):
        try:
            return bytes(value["bytes"]).decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


def _tool_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != 1:
        raise ArtifactConsistencyError("Tool metadata has an unsupported schema version")
    tool = document.get("tool")
    if not isinstance(tool, dict):
        raise ArtifactConsistencyError("Tool metadata has no tool object")
    required = (
        "fqn",
        "module",
        "function",
        "output_enum",
        "input_ports",
        "output_ports",
        "parameters",
        "output_variants",
    )
    missing = [name for name in required if name not in tool]
    if missing:
        raise ArtifactConsistencyError(f"Tool metadata is missing fields: {', '.join(missing)}")
    if not all(isinstance(tool[name], str) and tool[name] for name in required[:4]):
        raise ArtifactConsistencyError("Tool metadata identity fields must be non-empty strings")
    input_ports = tool["input_ports"]
    output_ports = tool["output_ports"]
    if not isinstance(input_ports, list) or not input_ports or any(not isinstance(port, str) or not port for port in input_ports):
        raise ArtifactConsistencyError("Tool metadata input_ports must be a non-empty string list")
    if len(set(input_ports)) != len(input_ports):
        raise ArtifactConsistencyError("Tool metadata input_ports contains duplicates")
    try:
        parameters = [_normalize_parameter(parameter) for parameter in tool["parameters"]]
    except (TypeError, KeyError) as exc:
        raise ArtifactConsistencyError("Tool metadata parameters are malformed") from exc
    if not parameters:
        raise ArtifactConsistencyError("Tool metadata parameters are empty")
    try:
        output_variants = []
        for variant in tool["output_variants"]:
            if not isinstance(variant, dict) or not isinstance(variant.get("name"), str):
                raise ArtifactConsistencyError("Tool metadata output variants are malformed")
            fields = variant.get("fields")
            if not isinstance(fields, list):
                raise ArtifactConsistencyError("Tool metadata output variant fields are malformed")
            output_variants.append(
                {
                    "name": variant["name"],
                    "fields": [
                        {
                            "name": field["name"],
                            "type": normalize_abi_type(field["type"]),
                        }
                        for field in fields
                        if isinstance(field, dict) and isinstance(field.get("name"), str) and "type" in field
                    ],
                }
            )
    except (TypeError, KeyError) as exc:
        raise ArtifactConsistencyError("Tool metadata output variants are malformed") from exc
    if not output_variants or any(len(variant["fields"]) != len(tool["output_variants"][index].get("fields", [])) for index, variant in enumerate(output_variants)):
        raise ArtifactConsistencyError("Tool metadata output variants are malformed")
    tool = dict(tool)
    tool["parameters"] = parameters
    tool["output_variants"] = output_variants
    if not isinstance(output_ports, dict) or not output_ports:
        raise ArtifactConsistencyError("Tool metadata output_ports must be a non-empty object")
    for variant, ports in output_ports.items():
        if not isinstance(variant, str) or not variant or not isinstance(ports, list) or not ports or any(
            not isinstance(port, str) or not port for port in ports
        ):
            raise ArtifactConsistencyError("Tool metadata output_ports contains a malformed variant")
        if len(set(ports)) != len(ports):
            raise ArtifactConsistencyError("Tool metadata output_ports contains duplicate ports")
    return dict(tool)


def _summary_identity(document: Mapping[str, Any], expected: Mapping[str, object]) -> dict[str, Any]:
    summary_id = document.get("id")
    module = summary_id.get("name") if isinstance(summary_id, dict) else None
    if module != expected["module"]:
        raise ArtifactConsistencyError(
            f"ABI summary module {module!r} does not match expected module {expected['module']!r}"
        )
    functions = document.get("functions")
    function_name = str(expected["function"])
    if not isinstance(functions, dict) or function_name not in functions:
        raise ArtifactConsistencyError(f"ABI summary has no generated Tool function {module}::{function_name}")
    function = functions[function_name]
    if not isinstance(function, dict) or function.get("visibility") != "Public" or function.get("return_"):
        raise ArtifactConsistencyError("ABI summary Tool function is not a public no-return execute function")
    parameters = function.get("parameters")
    if not isinstance(parameters, list):
        raise ArtifactConsistencyError("ABI summary Tool function has no parameter list")
    normalized_parameters = [_normalize_parameter(parameter) for parameter in parameters]
    names = [item["name"] for item in normalized_parameters]
    if names[:3] != ["authorization", "requirements", "result"] or names[-1:] != ["ctx"]:
        raise ArtifactConsistencyError("ABI summary Tool framework parameter identity is not aligned")
    input_names = names[3:-1]
    enums = document.get("enums")
    output_enum = str(expected.get("output_enum") or "")
    if not output_enum:
        if not isinstance(enums, dict) or len(enums) != 1:
            raise ArtifactConsistencyError("ABI summary output enum is ambiguous without explicit intent")
        output_enum = next(iter(enums))
    output = enums.get(output_enum) if isinstance(enums, dict) else None
    if not isinstance(output, dict):
        raise ArtifactConsistencyError(f"ABI summary has no generated Output enum {output_enum!r}")
    normalized_variants = _normalize_output_variants(output)
    output_names = {
        _snake_case(str(variant["name"])): [str(field["name"]) for field in variant["fields"]]
        for variant in normalized_variants
    }
    identity = {
        "module": module,
        "function": function_name,
        "output_enum": output_enum,
        "input_ports": input_names,
        "output_ports": output_names,
        "parameters": normalized_parameters,
        "output_variants": normalized_variants,
    }
    expected_parameters = expected.get("parameters")
    if expected_parameters is not None and normalized_parameters != expected_parameters:
        raise ArtifactConsistencyError("ABI summary parameter types do not match caller intent")
    expected_variants = expected.get("output_variants")
    if expected_variants is not None and normalized_variants != expected_variants:
        raise ArtifactConsistencyError("ABI summary output field types do not match caller intent")
    return identity


def canonical_schema_digest(
    *,
    module_address: str,
    module: str,
    function: str,
    output_enum: str,
    input_ports: list[str],
    output_ports: Mapping[str, list[str]],
    parameters: Sequence[Mapping[str, object]],
    output_variants: Sequence[Mapping[str, object]],
) -> str:
    """Hash the normalized ABI identity independent of JSON formatting."""

    return _sha256_bytes(
        _canonical_json_bytes(
            {
                "module_address": module_address,
                "module": module,
                "function": function,
                "output_enum": output_enum,
                "input_ports": input_ports,
                "output_ports": dict(output_ports),
                "parameters": [dict(parameter) for parameter in parameters],
                "output_variants": [dict(variant) for variant in output_variants],
            }
        )
    )


def _normalize_intent(
    expected_intent: Mapping[str, object] | None,
    expected: Mapping[str, object] | None,
) -> dict[str, object]:
    if expected_intent is not None and expected is not None and dict(expected_intent) != dict(expected):
        raise ArtifactConsistencyError("two caller intent anchors disagree")
    anchor = expected_intent if expected_intent is not None else expected
    if not isinstance(anchor, Mapping):
        raise ArtifactConsistencyError(
            "explicit caller intent is required: expected FQN, module, function, compiled SHA-256, and schema digest"
        )
    normalized = dict(anchor)
    for canonical, aliases in (
        ("fqn", ("tool_fqn", "expected_fqn")),
        ("module", ("module_name", "expected_module")),
        ("function", ("function_name", "expected_function")),
        ("compiled_sha256", ("compiled_module_sha256", "compiled_module_digest", "compiled_digest")),
        ("schema_digest", ("schema_sha256", "expected_schema_digest")),
        ("dag_sha256", ("dag_digest", "expected_dag_digest", "dag_semantics_sha256")),
        ("skill_sha256", ("skill_digest", "expected_skill_digest", "skill_semantics_sha256")),
    ):
        if canonical not in normalized:
            for alias in aliases:
                if alias in normalized:
                    normalized[canonical] = normalized[alias]
                    break
    compiled_digest = normalized.get("compiled_sha256")
    if compiled_digest is None:
        compiled_digest = normalized.get("compiled_module_sha256")
    if compiled_digest is not None:
        normalized["compiled_sha256"] = compiled_digest
    required = (
        "fqn",
        "module",
        "function",
        "compiled_sha256",
        "schema_digest",
        "dag_sha256",
        "skill_sha256",
    )
    missing = [name for name in required if not isinstance(normalized.get(name), str) or not normalized[name]]
    if missing:
        raise ArtifactConsistencyError(f"caller intent is missing required fields: {', '.join(missing)}")
    if not isinstance(normalized.get("parameters"), list) or not normalized["parameters"]:
        raise ArtifactConsistencyError("caller intent is missing required fields: parameters")
    if not isinstance(normalized.get("output_variants"), list) or not normalized["output_variants"]:
        raise ArtifactConsistencyError("caller intent is missing required fields: output_variants")
    if not isinstance(normalized.get("module_address"), str) or not normalized["module_address"]:
        module_id = normalized.get("module_id")
        if isinstance(module_id, str) and "." in module_id:
            normalized["module_address"] = module_id.rsplit(".", 1)[0]
        else:
            raise ArtifactConsistencyError("caller intent is missing the expected compiled module address")
    for field in ("compiled_sha256", "schema_digest", "dag_sha256", "skill_sha256"):
        _require_digest(normalized[field], f"caller intent {field}")
    try:
        normalized["parameters"] = [_normalize_parameter(parameter) for parameter in normalized["parameters"]]
        variants = normalized["output_variants"]
        if not isinstance(variants, list) or not variants:
            raise ArtifactConsistencyError("caller intent output variants are missing")
        normalized_variants: list[dict[str, object]] = []
        for variant in variants:
            if not isinstance(variant, Mapping) or not isinstance(variant.get("name"), str):
                raise ArtifactConsistencyError("caller intent output variants are malformed")
            fields = variant.get("fields")
            if not isinstance(fields, list):
                raise ArtifactConsistencyError("caller intent output variant fields are malformed")
            normalized_variants.append(
                {
                    "name": variant["name"],
                    "fields": [
                        {
                            "name": field["name"],
                            "type": normalize_abi_type(field["type"]),
                        }
                        for field in fields
                        if isinstance(field, Mapping)
                        and isinstance(field.get("name"), str)
                        and "type" in field
                    ],
                }
            )
        if any(
            len(normalized_variants[index]["fields"]) != len(variants[index].get("fields", []))
            for index in range(len(normalized_variants))
        ):
            raise ArtifactConsistencyError("caller intent output variants are malformed")
        normalized["output_variants"] = normalized_variants
    except (TypeError, KeyError) as exc:
        raise ArtifactConsistencyError("caller intent ABI types are malformed") from exc
    return normalized


def _intent_projection(intent: Mapping[str, object]) -> dict[str, object]:
    fields = (
        "fqn",
        "module_id",
        "module_address",
        "module",
        "function",
        "output_enum",
        "input_ports",
        "output_ports",
        "parameters",
        "output_variants",
        "compiled_sha256",
        "build_info_sha256",
        "schema_digest",
        "package_root",
        "build_root",
        "compiled_module",
    )
    return {field: intent[field] for field in fields if field in intent}


def validate_artifact_consistency(
    manifest_path: Path,
    *,
    expected_intent: Mapping[str, object] | None = None,
    expected: Mapping[str, object] | None = None,
    build_receipt: Mapping[str, object] | None = None,
    expected_receipt_sha256: str | None = None,
    expected_package_tree_sha256: str | None = None,
    expected_build_tree_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate all generated artifacts against external intent and build bytes."""

    intent = _normalize_intent(expected_intent, expected)
    if expected_receipt_sha256 is None:
        raise ArtifactConsistencyError("an external expected build receipt digest is required")
    manifest_path = manifest_path.resolve()
    try:
        manifest, manifest_digest = _read_json(manifest_path, "artifact consistency manifest")
    except ArtifactConsistencyError:
        raise
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ArtifactConsistencyError("artifact consistency manifest requires provenance schema version 2")
    root = manifest_path.parent
    metadata_path = _resolve(root, manifest.get("tool_metadata"), "Tool metadata")
    summary_path = _resolve(root, manifest.get("abi_summary"), "ABI summary")
    dag_path = _resolve(root, manifest.get("dag"), "DAG")
    skill_path = _resolve(root, manifest.get("skill"), "TAP skill")
    receipt_path = _resolve(root, manifest.get("build_receipt"), "build receipt", allow_parent=True)
    receipt_document, receipt_file_digest = _read_json(receipt_path, "build receipt")
    if build_receipt is not None and dict(build_receipt) != receipt_document:
        raise ArtifactConsistencyError("in-memory build receipt does not match the persisted receipt")
    receipt = build_receipt if build_receipt is not None else receipt_document
    try:
        receipt_evidence = validate_build_receipt(
            receipt,
            package_root=Path(str(intent["package_root"])) if intent.get("package_root") else None,
            build_root=Path(str(intent["build_root"])) if intent.get("build_root") else None,
            expected_compiled_sha256=str(intent["compiled_sha256"]),
            expected_build_info_sha256=str(intent["build_info_sha256"])
            if intent.get("build_info_sha256")
            else None,
            expected_module_id=str(intent["module_id"]) if intent.get("module_id") else None,
            expected_module_address=str(intent["module_address"]),
            expected_module_name=str(intent["module"]),
            expected_function_name=str(intent["function"]),
            expected_schema_digest=str(intent["schema_digest"]),
            expected_receipt_sha256=expected_receipt_sha256,
            expected_package_tree_sha256=expected_package_tree_sha256,
            expected_build_tree_sha256=expected_build_tree_sha256,
        )
    except CompiledMoveInvariantError as exc:
        raise ArtifactConsistencyError(f"build provenance validation failed: {exc}") from exc
    summary_receipt_path = receipt.get("schema_summary")
    if not isinstance(summary_receipt_path, str) or Path(summary_receipt_path).resolve() != summary_path:
        raise ArtifactConsistencyError("ABI summary path does not match the digest-bound build receipt")

    metadata_document, metadata_digest = _read_json(metadata_path, "Tool metadata")
    summary_document, summary_digest = _read_json(summary_path, "ABI summary")
    dag_document, dag_digest = _read_json(dag_path, "DAG")
    skill_document, skill_digest = _read_json(skill_path, "TAP skill config")
    dag_semantic_digest = canonical_semantic_digest(dag_document)
    skill_semantic_digest = canonical_semantic_digest(skill_document)
    if dag_semantic_digest != intent["dag_sha256"]:
        raise ArtifactConsistencyError("canonical DAG semantics do not match the caller acceptance contract")
    if skill_semantic_digest != intent["skill_sha256"]:
        raise ArtifactConsistencyError("canonical TAP skill semantics do not match the caller acceptance contract")
    if isinstance(manifest.get("intent"), dict) and manifest["intent"] != _intent_projection(intent):
        raise ArtifactConsistencyError("manifest intent does not match the caller acceptance contract")
    metadata = _tool_metadata(metadata_document)
    summary_identity = _summary_identity(summary_document, {**metadata, **intent})
    expected_output_ports = intent.get("output_ports")
    expected_input_ports = intent.get("input_ports")
    if expected_input_ports is not None and summary_identity["input_ports"] != expected_input_ports:
        raise ArtifactConsistencyError("ABI summary input ports do not match caller intent")
    if expected_output_ports is not None and summary_identity["output_ports"] != expected_output_ports:
        raise ArtifactConsistencyError("ABI summary output ports do not match caller intent")
    if metadata["parameters"] != summary_identity["parameters"]:
        raise ArtifactConsistencyError("Tool metadata parameter types do not match ABI summary")
    if metadata["output_variants"] != summary_identity["output_variants"]:
        raise ArtifactConsistencyError("Tool metadata output field types do not match ABI summary")
    schema_digest = canonical_schema_digest(
        module_address=str(intent["module_address"]),
        module=str(summary_identity["module"]),
        function=str(summary_identity["function"]),
        output_enum=str(summary_identity["output_enum"]),
        input_ports=list(summary_identity["input_ports"]),
        output_ports=summary_identity["output_ports"],
        parameters=summary_identity["parameters"],
        output_variants=summary_identity["output_variants"],
    )
    if schema_digest != intent["schema_digest"] or schema_digest != receipt.get("schema_digest"):
        raise ArtifactConsistencyError("derived schema digest does not match caller/build intent")
    for field in (
        "fqn",
        "module",
        "function",
        "output_enum",
        "input_ports",
        "output_ports",
        "parameters",
        "output_variants",
    ):
        if field in intent and metadata.get(field) != intent[field]:
            raise ArtifactConsistencyError(f"Tool metadata {field} does not match caller intent")
    if metadata["module"] != summary_identity["module"] or metadata["function"] != summary_identity["function"]:
        raise ArtifactConsistencyError("Tool metadata module/function do not match ABI summary")
    if metadata["output_enum"] != summary_identity["output_enum"]:
        raise ArtifactConsistencyError("Tool metadata output enum does not match ABI summary")
    if metadata["input_ports"] != summary_identity["input_ports"]:
        raise ArtifactConsistencyError(
            f"Tool input ports {metadata['input_ports']!r} do not match ABI summary {summary_identity['input_ports']!r}"
        )
    if metadata["output_ports"] != summary_identity["output_ports"]:
        raise ArtifactConsistencyError(
            f"Tool output ports {metadata['output_ports']!r} do not match ABI summary {summary_identity['output_ports']!r}"
        )

    vertices = dag_document.get("vertices")
    if not isinstance(vertices, list) or not vertices:
        raise ArtifactConsistencyError("DAG has no vertices for Tool identity comparison")
    vertex_names: set[str] = set()
    dag_tool_fqns: list[str] = []
    for vertex in vertices:
        if not isinstance(vertex, dict) or not isinstance(vertex.get("name"), str) or not vertex["name"]:
            raise ArtifactConsistencyError("DAG contains a malformed vertex")
        vertex_names.add(vertex["name"])
        kind = vertex.get("kind")
        if not isinstance(kind, dict) or kind.get("tool_fqn") != intent["fqn"]:
            raise ArtifactConsistencyError(f"DAG vertex {vertex.get('name')!r} FQN does not match caller intent")
        dag_tool_fqns.append(str(kind["tool_fqn"]))
        ports = vertex.get("entry_ports")
        names = [port.get("name") for port in ports] if isinstance(ports, list) and all(isinstance(port, dict) for port in ports) else []
        if names != metadata["input_ports"]:
            raise ArtifactConsistencyError(f"DAG vertex {vertex['name']!r} entry ports do not match Tool schema")
    outputs = dag_document.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ArtifactConsistencyError("DAG has no declared Tool outputs")
    declared_outputs: set[tuple[str, str, str]] = set()
    for output in outputs:
        if not isinstance(output, dict):
            raise ArtifactConsistencyError("DAG contains a malformed output")
        vertex = output.get("vertex")
        variant = output.get("output_variant")
        port = output.get("output_port")
        if vertex not in vertex_names or variant not in metadata["output_ports"]:
            raise ArtifactConsistencyError("DAG output references an unknown vertex or variant")
        if port not in metadata["output_ports"][variant]:
            raise ArtifactConsistencyError(f"DAG output port {port!r} is absent from Tool metadata variant {variant!r}")
        declared_outputs.add((vertex, variant, port))
    expected_outputs = {
        (vertex["name"], variant, port)
        for vertex in vertices
        for variant, ports in metadata["output_ports"].items()
        for port in ports
    }
    if declared_outputs != expected_outputs:
        raise ArtifactConsistencyError(
            f"DAG declared outputs {sorted(declared_outputs)!r} do not match Tool schema outputs {sorted(expected_outputs)!r}"
        )

    requirements = skill_document.get("requirements")
    fixed_tools = requirements.get("fixed_tools") if isinstance(requirements, dict) else None
    if not isinstance(fixed_tools, list) or not fixed_tools:
        raise ArtifactConsistencyError("TAP skill has no fixed Tool entry for identity comparison")
    fixed_tool_fqns: list[str] = []
    for fixed_tool in fixed_tools:
        fixed_fqn = _move_string(fixed_tool.get("tool_fqn")) if isinstance(fixed_tool, dict) else None
        if fixed_fqn != intent["fqn"]:
            raise ArtifactConsistencyError(
                f"TAP fixed Tool FQN {fixed_fqn!r} does not match caller intent FQN {intent['fqn']!r}"
            )
        fixed_tool_fqns.append(str(fixed_fqn))
    if Counter(dag_tool_fqns) != Counter(fixed_tool_fqns):
        raise ArtifactConsistencyError(
            "TAP fixed Tool bindings are not a bijection with DAG Tool vertices: "
            f"dag={dag_tool_fqns!r}, fixed={fixed_tool_fqns!r}"
        )

    return {
        "manifest": str(manifest_path),
        "intent": _intent_projection(intent),
        "fqn": str(intent["fqn"]),
        "module": str(intent["module"]),
        "function": str(intent["function"]),
        "input_ports": list(summary_identity["input_ports"]),
        "output_ports": dict(summary_identity["output_ports"]),
        "vertices": len(vertices),
        "declared_outputs": len(declared_outputs),
        "fixed_tools": len(fixed_tools),
        "digests": {
            "manifest": manifest_digest,
            "tool_metadata": metadata_digest,
            "abi_summary": summary_digest,
            "dag": dag_digest,
            "skill": skill_digest,
            "dag_semantics": dag_semantic_digest,
            "skill_semantics": skill_semantic_digest,
            "build_receipt": receipt_file_digest,
            "build_receipt_self": str(receipt_evidence["receipt_sha256"]),
            "compiled_module": str(receipt_evidence["compiled_sha256"]),
            "package_tree": str(receipt_evidence["package_tree_sha256"]),
            "build_tree": str(receipt_evidence["build_tree_sha256"]),
            "schema": schema_digest,
            "trusted_disassembly": receipt_evidence.get("trusted_disassembly_sha256"),
        },
    }
