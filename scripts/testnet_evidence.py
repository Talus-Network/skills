#!/usr/bin/env python3
"""Collect bounded, read-only evidence from the official Sui testnet GraphQL endpoint."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from vision_links import VisionLinkError, vision_url


TESTNET_GRAPHQL_HOST = "graphql.testnet.sui.io"
UNSUPPORTED_TESTNET_HOSTS = frozenset({"fullnode.testnet.sui.io", "rpc.testnet.sui.io"})
OFFICIAL_TESTNET_CHAIN_IDENTIFIER = "69WiPg3DAQiwdxfncX6wYQ2siKwAe6L9BZthQea3JNMD"
GRAPHQL_READ_OPERATIONS = frozenset(
    {
        "sui_getChainIdentifier",
        "sui_getLatestCheckpointSequenceNumber",
        "sui_getObject",
        "sui_getPackage",
        "sui_getNormalizedMoveModule",
        "sui_getNormalizedMoveFunction",
        "sui_getNormalizedMoveStruct",
    }
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
GRAPHQL_PAGE_SIZE = 50
MAX_GRAPHQL_PAGES = 128


class TestnetEvidenceError(RuntimeError):
    """Raised when public testnet evidence cannot be proven safely."""


Transport = Callable[[Request, float], bytes]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_graphql_url(graphql_url: str) -> str:
    if not isinstance(graphql_url, str) or not graphql_url.strip():
        raise TestnetEvidenceError("an explicit Sui testnet GraphQL URL is required")
    if graphql_url != graphql_url.strip() or any(character.isspace() for character in graphql_url):
        raise TestnetEvidenceError("GraphQL URL must be the canonical official endpoint")
    try:
        parsed = urlparse(graphql_url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise TestnetEvidenceError("GraphQL URL is malformed") from exc
    if hostname in UNSUPPORTED_TESTNET_HOSTS:
        raise TestnetEvidenceError(
            "deprecated Sui testnet RPC endpoint is unsupported; use https://graphql.testnet.sui.io/graphql"
        )
    if parsed.scheme.lower() != "https" or hostname != TESTNET_GRAPHQL_HOST:
        raise TestnetEvidenceError("URL must be the official HTTPS Sui testnet GraphQL endpoint")
    if (
        parsed.netloc.lower() != TESTNET_GRAPHQL_HOST
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise TestnetEvidenceError(
            "GraphQL URL must not contain credentials, an explicit port, parameters, queries, or fragments"
        )
    if parsed.path != "/graphql":
        raise TestnetEvidenceError("GraphQL URL must use the /graphql endpoint")
    return "https://" + f"{TESTNET_GRAPHQL_HOST}/graphql"


def _canonical_sui_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise TestnetEvidenceError(f"{label} must be a hexadecimal Sui identifier")
    digits = value[2:]
    if not digits or len(digits) > 64 or any(character not in "0123456789abcdefABCDEF" for character in digits):
        raise TestnetEvidenceError(f"{label} must be a hexadecimal Sui identifier")
    return f"0x{digits.lower().zfill(64)}"


def _require_canonical_returned_identifier(value: object, label: str) -> str:
    """Validate an authority-returned identifier without rewriting it."""

    canonical = _canonical_sui_identifier(value, label)
    if value != canonical:
        raise TestnetEvidenceError(f"testnet {label} returned identifier is not canonical")
    return canonical


_MOVE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SUI_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SUI_BASE58_INDEX = {character: index for index, character in enumerate(_SUI_BASE58_ALPHABET)}
SUI_DIGEST_BYTES = 32


def _canonical_move_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _MOVE_IDENTIFIER_RE.fullmatch(value) is None:
        raise _observation_error(label)
    return value


def _canonical_sui_digest(value: object, label: str) -> str:
    """Require a canonical Sui base58 digest encoding exactly 32 bytes."""

    if not isinstance(value, str) or not value or any(character not in _SUI_BASE58_INDEX for character in value):
        raise _observation_error(label)
    leading_zeroes = len(value) - len(value.lstrip("1"))
    number = 0
    for character in value:
        number = number * 58 + _SUI_BASE58_INDEX[character]
    payload = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    decoded = b"\x00" * leading_zeroes + payload
    if len(decoded) != SUI_DIGEST_BYTES:
        raise _observation_error(label)
    if number:
        encoded = ""
        remaining = number
        while remaining:
            remaining, remainder = divmod(remaining, 58)
            encoded = _SUI_BASE58_ALPHABET[remainder] + encoded
    else:
        encoded = ""
    canonical = "1" * leading_zeroes + encoded
    if canonical != value:
        raise _observation_error(label)
    return value


def _validate_identifier(value: str, label: str) -> str:
    return _canonical_sui_identifier(value, label)


def _require_chain_identifier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TestnetEvidenceError("testnet chain identity observation is missing or incomplete")
    if value != OFFICIAL_TESTNET_CHAIN_IDENTIFIER:
        raise TestnetEvidenceError("returned chain identifier is not the official Sui testnet identity")
    return value


def _require_checkpoint(value: object) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TestnetEvidenceError("testnet checkpoint observation is missing or incomplete")
    if isinstance(value, str) and (not value or not value.isdigit()):
        raise TestnetEvidenceError("testnet checkpoint observation is missing or incomplete")
    if isinstance(value, int) and value < 0:
        raise TestnetEvidenceError("testnet checkpoint observation is missing or incomplete")
    return value


def _observation_error(label: str) -> TestnetEvidenceError:
    return TestnetEvidenceError(f"testnet {label} observation is missing or incomplete")


def _require_non_empty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _observation_error(label)
    return value


def _canonical_fully_qualified_name(value: object, label: str, parts: int) -> tuple[str, ...]:
    text = _require_non_empty_text(value, label)
    segments = text.split("::")
    if len(segments) != parts or any(not segment or segment != segment.strip() for segment in segments):
        raise _observation_error(label)
    package = _require_canonical_returned_identifier(segments[0], label)
    try:
        names = tuple(_canonical_move_identifier(segment, label) for segment in segments[1:])
    except TestnetEvidenceError:
        raise
    return (package, *names)


def _require_identifier_match(value: object, expected: str | None, label: str, field: str) -> str:
    try:
        actual = _require_canonical_returned_identifier(value, label)
    except TestnetEvidenceError as exc:
        if "not canonical" in str(exc):
            raise
        raise _observation_error(label) from exc
    if expected is not None and actual != _canonical_sui_identifier(expected, label):
        raise TestnetEvidenceError(f"testnet {label} {field} does not match the requested identifier")
    return actual


def _require_fully_qualified_name_match(
    value: object,
    label: str,
    *,
    parts: int,
    expected_package_id: str | None = None,
    expected_module: str | None = None,
    expected_name: str | None = None,
    field: str = "fully qualified name",
) -> tuple[str, ...]:
    actual = _canonical_fully_qualified_name(value, label, parts)
    if expected_package_id is not None and actual[0] != _canonical_sui_identifier(expected_package_id, label):
        raise TestnetEvidenceError(f"testnet {label} {field} does not match the requested package")
    if expected_module is not None and actual[1] != _canonical_move_identifier(expected_module, label):
        raise TestnetEvidenceError(f"testnet {label} {field} does not match the requested module")
    if expected_name is not None and actual[-1] != _canonical_move_identifier(expected_name, label):
        raise TestnetEvidenceError(f"testnet {label} {field} does not match the requested name")
    return actual


def _require_version(value: object, label: str) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _observation_error(label)
    if isinstance(value, str):
        if not value.strip() or not value.strip().isdigit():
            raise _observation_error(label)
    elif value < 0:
        raise _observation_error(label)
    return value


def _require_sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise _observation_error(label)
    return value


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise _observation_error(label)
    return value


def _require_connection(
    value: object,
    label: str,
    *,
    require_node: bool = False,
    require_page_info: bool = False,
    require_fully_qualified_name: bool = False,
    fqn_parts: int | None = None,
    expected_package_id: str | None = None,
    expected_module: str | None = None,
) -> Mapping[str, object]:
    connection = _require_mapping(value, label)
    if "nodes" not in connection or connection["nodes"] is None:
        raise _observation_error(label)
    nodes = connection["nodes"]
    if not isinstance(nodes, list):
        raise _observation_error(label)
    if require_node and not nodes:
        raise _observation_error(label)
    for node in nodes:
        node_mapping = _require_mapping(node, label)
        node_name = _require_non_empty_text(node_mapping.get("name"), label)
        if require_fully_qualified_name:
            fqn = _require_fully_qualified_name_match(
                node_mapping.get("fullyQualifiedName"),
                label,
                parts=fqn_parts or 2,
                expected_package_id=expected_package_id,
                expected_module=expected_module,
                expected_name=node_name,
            )
            if fqn[-1] != node_name:
                raise TestnetEvidenceError(f"testnet {label} node name does not match its fully qualified name")
        elif "fullyQualifiedName" in node_mapping:
            _canonical_fully_qualified_name(node_mapping.get("fullyQualifiedName"), label, fqn_parts or 2)
    if require_page_info:
        _require_page_info(connection, label)
    return connection


def _require_page_info(connection: Mapping[str, object], label: str) -> tuple[bool, str | None]:
    page_info = _require_mapping(connection.get("pageInfo"), label)
    if not isinstance(page_info.get("hasNextPage"), bool):
        raise _observation_error(label)
    end_cursor = page_info.get("endCursor")
    if end_cursor is not None:
        _require_non_empty_text(end_cursor, label)
    if page_info["hasNextPage"] and end_cursor is None:
        raise _observation_error(label)
    return page_info["hasNextPage"], end_cursor


def _require_graphql_owner(value: object, label: str) -> None:
    owner = _require_mapping(value, label)
    owner_type = _require_non_empty_text(owner.get("__typename"), label)
    if owner_type in {"AddressOwner", "ObjectOwner"}:
        address = _require_mapping(owner.get("address"), label)
        _require_identifier_match(address.get("address"), None, label, "owner address")
    elif owner_type == "Shared":
        _require_version(owner.get("initialSharedVersion"), label)
    elif owner_type == "ConsensusAddressOwner":
        _require_version(owner.get("startVersion"), label)
        address = _require_mapping(owner.get("address"), label)
        _require_identifier_match(address.get("address"), None, label, "owner address")
    elif owner_type == "Immutable":
        if "_" not in owner:
            raise _observation_error(label)
    else:
        raise _observation_error(label)


_GRAPHQL_PRIMITIVE_TYPES = frozenset({"address", "bool", "signer", "u8", "u16", "u32", "u64", "u128", "u256"})
_GRAPHQL_REFERENCE_KINDS = frozenset({"&", "&mut"})


def _canonical_graphql_signature_body(value: object, label: str) -> tuple[object, ...]:
    body = _require_mapping(value, label)
    constructors = {"datatype", "primitive", "typeParameter", "vector"}
    present = [key for key in body if key in constructors]
    if len(present) != 1 or any(key not in constructors for key in body):
        raise _observation_error(label)
    constructor = present[0]
    nested = body[constructor]
    if constructor == "datatype":
        datatype = _require_mapping(nested, label)
        required = {"package", "module", "type", "typeParameters"}
        if not required.issubset(datatype) or any(key not in required for key in datatype):
            raise _observation_error(label)
        package = _require_canonical_returned_identifier(datatype.get("package"), label)
        module = _canonical_move_identifier(datatype.get("module"), label)
        type_name = _canonical_move_identifier(datatype.get("type"), label)
        arguments = tuple(
            _canonical_graphql_signature_node(argument, label)
            for argument in _require_sequence(datatype.get("typeParameters"), label)
        )
        return ("datatype", package, module, type_name, arguments)
    elif constructor == "primitive":
        primitive = _require_non_empty_text(nested, label)
        if primitive not in _GRAPHQL_PRIMITIVE_TYPES:
            raise _observation_error(label)
        return ("primitive", primitive)
    elif constructor == "typeParameter":
        if isinstance(nested, bool) or not isinstance(nested, int) or nested < 0:
            raise _observation_error(label)
        return ("typeParameter", nested)
    else:
        return ("vector", _canonical_graphql_signature_node(nested, label))


def _canonical_graphql_signature_node(value: object, label: str) -> tuple[object, ...]:
    signature = _require_mapping(value, label)
    reference: str | None = None
    if "body" in signature:
        if set(signature) - {"body", "ref"}:
            raise _observation_error(label)
        body = signature.get("body")
        if "ref" in signature:
            reference_value = _require_non_empty_text(signature.get("ref"), label)
            if reference_value not in _GRAPHQL_REFERENCE_KINDS:
                raise _observation_error(label)
            reference = reference_value
    else:
        body = signature
    canonical = _canonical_graphql_signature_body(body, label)
    return ("reference", reference, canonical) if reference is not None else canonical


def _require_graphql_signature_body(value: object, label: str) -> None:
    _canonical_graphql_signature_body(value, label)


def _require_graphql_signature_node(value: object, label: str) -> None:
    """Compatibility validation seam; active paths use the canonical return value."""

    _canonical_graphql_signature_node(value, label)


class _GraphQLReprParser:
    """Parse the bounded Move type representation emitted by Sui GraphQL."""

    def __init__(self, text: str, label: str) -> None:
        self.text = text
        self.label = label
        self.position = 0

    def parse(self) -> tuple[object, ...]:
        if self.text != self.text.strip():
            raise _observation_error(self.label)
        parsed = self._parse_type()
        if self.position != len(self.text):
            raise _observation_error(self.label)
        return parsed

    def _skip_spaces(self) -> None:
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1

    def _parse_type(self) -> tuple[object, ...]:
        self._skip_spaces()
        reference: str | None = None
        if self.text.startswith("&mut", self.position):
            reference = "&mut"
            self.position += len("&mut")
            self._skip_spaces()
        elif self.text.startswith("&", self.position):
            reference = "&"
            self.position += 1
            self._skip_spaces()
        canonical = self._parse_atom()
        return ("reference", reference, canonical) if reference is not None else canonical

    def _parse_atom(self) -> tuple[object, ...]:
        start = self.position
        while self.position < len(self.text) and self.text[self.position] not in "<>,\t\r\n ":
            self.position += 1
        token = self.text[start : self.position]
        if not token:
            raise _observation_error(self.label)
        if token == "vector":
            self._skip_spaces()
            if self.position >= len(self.text) or self.text[self.position] != "<":
                raise _observation_error(self.label)
            self.position += 1
            element = self._parse_type()
            if self.position >= len(self.text) or self.text[self.position] != ">":
                raise _observation_error(self.label)
            self.position += 1
            return ("vector", element)
        if token.startswith("$"):
            if not token[1:].isdigit():
                raise _observation_error(self.label)
            canonical = ("typeParameter", int(token[1:]))
        elif token in _GRAPHQL_PRIMITIVE_TYPES:
            canonical = ("primitive", token)
        else:
            segments = token.split("::")
            if len(segments) != 3 or any(not segment or segment != segment.strip() for segment in segments):
                raise _observation_error(self.label)
            package = _require_canonical_returned_identifier(segments[0], self.label)
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", segment) for segment in segments[1:]):
                raise _observation_error(self.label)
            canonical = ("datatype", package, segments[1], segments[2], ())
        self._skip_spaces()
        if self.position < len(self.text) and self.text[self.position] == "<":
            if canonical[0] != "datatype":
                raise _observation_error(self.label)
            self.position += 1
            arguments: list[tuple[object, ...]] = []
            while True:
                arguments.append(self._parse_type())
                if self.position >= len(self.text):
                    raise _observation_error(self.label)
                self._skip_spaces()
                if self.position >= len(self.text):
                    raise _observation_error(self.label)
                if self.text[self.position] == ">":
                    self.position += 1
                    break
                if self.text[self.position] != ",":
                    raise _observation_error(self.label)
                self.position += 1
            canonical = (*canonical[:4], tuple(arguments))
        return canonical


def _format_graphql_signature(canonical: tuple[object, ...]) -> str:
    kind = canonical[0]
    if kind == "primitive":
        return str(canonical[1])
    if kind == "typeParameter":
        return f"${canonical[1]}"
    if kind == "vector":
        return f"vector<{_format_graphql_signature(canonical[1])}>"
    if kind == "datatype":
        package, module, type_name, arguments = canonical[1:]
        suffix = ""
        if arguments:
            suffix = "<" + ", ".join(_format_graphql_signature(argument) for argument in arguments) + ">"
        return f"{package}::{module}::{type_name}{suffix}"
    if kind == "reference":
        reference, nested = canonical[1:]
        separator = "" if reference == "&" else " "
        return f"{reference}{separator}{_format_graphql_signature(nested)}"
    raise _observation_error("GraphQL type")


def _canonical_graphql_repr(value: object, label: str) -> tuple[object, ...]:
    raw = _require_non_empty_text(value, label)
    try:
        parsed = _GraphQLReprParser(raw, label).parse()
    except TestnetEvidenceError as exc:
        raise _observation_error(label) from exc
    if raw != _format_graphql_signature(parsed):
        raise _observation_error(label)
    return parsed


def _require_graphql_type_signature(value: object, label: str) -> None:
    type_mapping = _require_mapping(value, label)
    repr_value = _require_non_empty_text(type_mapping.get("repr"), label)
    if "signature" not in type_mapping:
        raise _observation_error(label)
    structured = _canonical_graphql_signature_node(type_mapping.get("signature"), label)
    represented = _canonical_graphql_repr(repr_value, label)
    if represented != structured:
        raise TestnetEvidenceError(f"testnet {label} type repr and signature do not agree")


def _require_graphql_type_parameter(value: object, label: str) -> None:
    parameter = _require_mapping(value, label)
    constraints = _require_sequence(parameter.get("constraints"), label)
    for constraint in constraints:
        _require_non_empty_text(constraint, label)


def _require_graphql_function(
    nested: Mapping[str, object],
    label: str,
    *,
    expected_package_id: str | None = None,
    expected_module: str | None = None,
    expected_function: str | None = None,
) -> None:
    required = {"name", "fullyQualifiedName", "isEntry", "visibility", "typeParameters", "parameters", "return"}
    if not required.issubset(nested):
        raise _observation_error(label)
    function_name = _require_non_empty_text(nested.get("name"), label)
    _require_fully_qualified_name_match(
        nested.get("fullyQualifiedName"),
        label,
        parts=3,
        expected_package_id=expected_package_id,
        expected_module=expected_module,
        expected_name=expected_function or function_name,
    )
    if function_name != nested["fullyQualifiedName"].rsplit("::", 1)[-1]:
        raise TestnetEvidenceError(f"testnet {label} name does not match its fully qualified name")
    if not isinstance(nested.get("isEntry"), bool):
        raise _observation_error(label)
    _require_non_empty_text(nested.get("visibility"), label)
    for parameter in _require_sequence(nested.get("typeParameters"), label):
        _require_graphql_type_parameter(parameter, label)
    for parameter in _require_sequence(nested.get("parameters"), label):
        _require_graphql_type_signature(parameter, label)
    for return_type in _require_sequence(nested.get("return"), label):
        _require_graphql_type_signature(return_type, label)


def _require_graphql_struct(
    nested: Mapping[str, object],
    label: str,
    *,
    expected_package_id: str | None = None,
    expected_module: str | None = None,
    expected_struct: str | None = None,
) -> None:
    required = {"name", "fullyQualifiedName", "abilities", "typeParameters", "fields"}
    if not required.issubset(nested):
        raise _observation_error(label)
    struct_name = _require_non_empty_text(nested.get("name"), label)
    _require_fully_qualified_name_match(
        nested.get("fullyQualifiedName"),
        label,
        parts=3,
        expected_package_id=expected_package_id,
        expected_module=expected_module,
        expected_name=expected_struct or struct_name,
    )
    if struct_name != nested["fullyQualifiedName"].rsplit("::", 1)[-1]:
        raise TestnetEvidenceError(f"testnet {label} name does not match its fully qualified name")
    for ability in _require_sequence(nested.get("abilities"), label):
        _require_non_empty_text(ability, label)
    for parameter in _require_sequence(nested.get("typeParameters"), label):
        _require_graphql_type_parameter(parameter, label)
        if not isinstance(parameter, Mapping) or not isinstance(parameter.get("isPhantom"), bool):
            raise _observation_error(label)
    fields = _require_sequence(nested.get("fields"), label)
    for field in fields:
        field_mapping = _require_mapping(field, label)
        _require_non_empty_text(field_mapping.get("name"), label)
        _require_graphql_type_signature(field_mapping.get("type"), label)
    if not fields and not nested.get("abilities"):
        raise _observation_error(label)


_GRAPHQL_IDENTITY_KEYS = frozenset({"id", "objectId", "object_id"})
MAX_MOVE_UNSIGNED_INTEGER = (1 << 256) - 1


def _is_graphql_identifier_field(name: str) -> bool:
    return name in _GRAPHQL_IDENTITY_KEYS


def _require_graphql_json_value(value: object, label: str, field_name: str | None = None) -> None:
    if value is None:
        raise _observation_error(label)
    identifier_field = field_name is not None and _is_graphql_identifier_field(field_name)
    if identifier_field and not isinstance(value, str):
        raise _observation_error(label)
    if isinstance(value, Mapping):
        if not value:
            return
        for key, nested in value.items():
            key_text = _require_non_empty_text(key, label)
            _require_graphql_json_value(nested, label, key_text)
        return
    if isinstance(value, list):
        if not value:
            return
        for nested in value:
            _require_graphql_json_value(nested, label, field_name)
        return
    if isinstance(value, str):
        if not value.strip():
            raise _observation_error(label)
        if identifier_field:
            try:
                _require_canonical_returned_identifier(value, label)
            except TestnetEvidenceError as exc:
                if "not canonical" in str(exc):
                    raise
                raise _observation_error(label) from exc
        return
    if isinstance(value, bool):
        if identifier_field:
            raise _observation_error(label)
        return
    if isinstance(value, int):
        if identifier_field or value < 0 or value > MAX_MOVE_UNSIGNED_INTEGER:
            raise _observation_error(label)
        return
    if isinstance(value, float):
        raise _observation_error(label)
    raise _observation_error(label)


def _root_graphql_identity(value: Mapping[str, object], label: str) -> tuple[str, str]:
    identities = [
        (key, nested)
        for key, nested in value.items()
        if key in _GRAPHQL_IDENTITY_KEYS and isinstance(nested, str)
    ]
    if len(identities) != 1:
        raise _observation_error(label)
    key, nested = identities[0]
    identity = _require_canonical_returned_identifier(nested, f"{label} JSON {key}")
    if identity == "0x" + "0" * 64:
        raise _observation_error(label)
    return key, identity


def _require_graphql_object_type(value: object, label: str) -> None:
    type_mapping = _require_mapping(value, label)
    _require_graphql_type_signature(type_mapping, label)
    canonical = _canonical_graphql_signature_node(type_mapping.get("signature"), label)
    if canonical[0] == "reference" or canonical[0] != "datatype":
        raise _observation_error(label)


def _require_graphql_move_content(
    value: object,
    label: str,
    *,
    expected_object_id: str | None = None,
) -> None:
    content = _require_mapping(value, label)
    _require_graphql_object_type(content.get("type"), label)
    if "json" not in content and "bcs" not in content:
        raise _observation_error(label)
    valid_variant = False
    if "json" in content and content["json"] is not None:
        json_content = content["json"]
        if not isinstance(json_content, Mapping) or not json_content:
            raise _observation_error(label)
        _identity_key, json_object_id = _root_graphql_identity(json_content, label)
        _require_graphql_json_value(json_content, label)
        if expected_object_id is not None:
            try:
                expected_id = _canonical_sui_identifier(expected_object_id, label)
            except TestnetEvidenceError as exc:
                if "not canonical" in str(exc):
                    raise
                raise _observation_error(label) from exc
            if json_object_id != expected_id:
                raise TestnetEvidenceError(f"testnet {label} JSON object identifier does not match the object address")
        valid_variant = True
    if "bcs" in content and content["bcs"] is not None:
        bcs = _require_non_empty_text(content["bcs"], label)
        try:
            decoded = base64.b64decode(bcs, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise _observation_error(label) from exc
        if not decoded or base64.b64encode(decoded).decode("ascii") != bcs:
            raise _observation_error(label)
        valid_variant = True
    if not valid_variant:
        raise _observation_error(label)


def _require_graphql_object(
    nested: Mapping[str, object],
    label: str,
    *,
    expected_object_id: str | None = None,
) -> None:
    required = {"address", "version", "digest", "owner", "asMoveObject", "asMovePackage"}
    if not required.issubset(nested):
        raise _observation_error(label)
    address = _require_identifier_match(nested.get("address"), expected_object_id, label, "object address")
    _require_version(nested.get("version"), label)
    _canonical_sui_digest(nested.get("digest"), label)
    _require_graphql_owner(nested.get("owner"), label)
    move_object = nested.get("asMoveObject")
    move_package = nested.get("asMovePackage")
    if (move_object is None) == (move_package is None):
        raise _observation_error(label)
    if move_object is not None:
        move_object_mapping = _require_mapping(move_object, label)
        _require_graphql_move_content(
            move_object_mapping.get("contents"), label, expected_object_id=address
        )
    else:
        package_mapping = _require_mapping(move_package, label)
        _require_connection(
            package_mapping.get("modules"),
            label,
            require_node=True,
            require_page_info=True,
            require_fully_qualified_name=True,
            fqn_parts=2,
            expected_package_id=address,
        )


def _require_package_observation(
    nested: Mapping[str, object],
    label: str,
    *,
    protocol: str | None = None,
    expected_package_id: str | None = None,
    expected_object_id: str | None = None,
) -> None:
    if protocol not in {None, "graphql"}:
        raise TestnetEvidenceError("testnet evidence accepts only GraphQL observations")
    if label == "object":
        _require_graphql_object(nested, label, expected_object_id=expected_object_id)
        return
    if label != "package" or "address" not in nested:
        raise _observation_error(label)
    address = _require_identifier_match(nested.get("address"), expected_package_id, label, "package address")
    _require_version(nested.get("version"), label)
    _canonical_sui_digest(nested.get("digest"), label)
    _require_connection(
        nested.get("modules"),
        label,
        require_node=True,
        require_page_info=True,
        require_fully_qualified_name=True,
        fqn_parts=2,
        expected_package_id=address,
    )


def _require_normalized_module(
    nested: Mapping[str, object],
    label: str,
    *,
    protocol: str | None = None,
    expected_package_id: str | None = None,
    expected_module: str | None = None,
) -> None:
    if protocol not in {None, "graphql"}:
        raise TestnetEvidenceError("testnet evidence accepts only GraphQL observations")
    module_name = _require_non_empty_text(nested.get("name"), label)
    _require_fully_qualified_name_match(
        nested.get("fullyQualifiedName"),
        label,
        parts=2,
        expected_package_id=expected_package_id,
        expected_name=expected_module or module_name,
    )
    if module_name != nested["fullyQualifiedName"].rsplit("::", 1)[-1]:
        raise TestnetEvidenceError(f"testnet {label} name does not match its fully qualified name")
    module_package_id = expected_package_id or nested["fullyQualifiedName"].split("::", 1)[0]
    _require_connection(
        nested.get("functions"),
        label,
        require_page_info=True,
        require_fully_qualified_name=True,
        fqn_parts=3,
        expected_package_id=module_package_id,
        expected_module=module_name,
    )
    _require_connection(
        nested.get("structs"),
        label,
        require_page_info=True,
        require_fully_qualified_name=True,
        fqn_parts=3,
        expected_package_id=module_package_id,
        expected_module=module_name,
    )


def _require_normalized_function(
    nested: Mapping[str, object],
    label: str,
    *,
    protocol: str | None = None,
    expected_package_id: str | None = None,
    expected_module: str | None = None,
    expected_function: str | None = None,
) -> None:
    if protocol not in {None, "graphql"} or not {"typeParameters", "parameters", "return"}.issubset(nested):
        raise _observation_error(label)
    _require_graphql_function(
        nested,
        label,
        expected_package_id=expected_package_id,
        expected_module=expected_module,
        expected_function=expected_function,
    )


def _require_normalized_struct(
    nested: Mapping[str, object],
    label: str,
    *,
    protocol: str | None = None,
    expected_package_id: str | None = None,
    expected_module: str | None = None,
    expected_struct: str | None = None,
) -> None:
    if protocol not in {None, "graphql"} or not {"abilities", "typeParameters", "fields"}.issubset(nested):
        raise _observation_error(label)
    _require_graphql_struct(
        nested,
        label,
        expected_package_id=expected_package_id,
        expected_module=expected_module,
        expected_struct=expected_struct,
    )


def _require_observation(
    value: object,
    label: str,
    *,
    protocol: str | None = None,
    expected_package_id: str | None = None,
    expected_object_id: str | None = None,
    expected_module: str | None = None,
    expected_function: str | None = None,
    expected_struct: str | None = None,
) -> Mapping[str, object]:
    if protocol not in {None, "graphql"}:
        raise TestnetEvidenceError("testnet evidence accepts only GraphQL observations")
    if not isinstance(value, Mapping) or not value:
        raise _observation_error(label)
    if any(key in value for key in ("jsonrpc", "result", "error", "method", "params")):
        raise TestnetEvidenceError("testnet evidence received a non-GraphQL response")
    if "data" in value:
        raise _observation_error(label)
    nested = value
    if not isinstance(nested, Mapping) or not nested:
        raise _observation_error(label)
    if label in {"object", "package"}:
        _require_package_observation(
            nested,
            label,
            protocol=protocol,
            expected_package_id=expected_package_id,
            expected_object_id=expected_object_id,
        )
    elif label == "module":
        _require_normalized_module(
            nested,
            label,
            protocol=protocol,
            expected_package_id=expected_package_id,
            expected_module=expected_module,
        )
    elif label == "function":
        _require_normalized_function(
            nested,
            label,
            protocol=protocol,
            expected_package_id=expected_package_id,
            expected_module=expected_module,
            expected_function=expected_function,
        )
    elif label == "struct":
        _require_normalized_struct(
            nested,
            label,
            protocol=protocol,
            expected_package_id=expected_package_id,
            expected_module=expected_module,
            expected_struct=expected_struct,
        )
    else:
        raise _observation_error(label)
    # Response validation above is deliberately strict. Preserve the accepted
    # response bytes/shape exactly; only request selectors are canonicalized.
    return value


def _urlopen_transport(request: Request, timeout: float) -> bytes:
    request_url = getattr(request, "full_url", None)
    if not isinstance(request_url, str):
        raise TestnetEvidenceError("GraphQL transport request has no canonical URL")
    _validate_graphql_url(request_url)
    try:
        response = urlopen(request, timeout=timeout)
        with response:
            geturl = getattr(response, "geturl", None)
            final_url = geturl() if callable(geturl) else geturl
            if not isinstance(final_url, str):
                raise TestnetEvidenceError("GraphQL response has no final URL")
            _validate_graphql_url(final_url)
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except TestnetEvidenceError:
        raise
    except (HTTPError, URLError, OSError) as exc:
        raise TestnetEvidenceError(f"read-only testnet GraphQL request failed: {exc}") from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise TestnetEvidenceError("testnet GraphQL response exceeds the bounded response size")
    return payload


@dataclass(frozen=True)
class SuiTestnetEvidenceClient:
    """Public testnet GraphQL client restricted to read-only query operations."""

    graphql_url: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    transport: Transport = _urlopen_transport

    def __post_init__(self) -> None:
        object.__setattr__(self, "graphql_url", _validate_graphql_url(self.graphql_url))
        if self.timeout <= 0 or self.timeout > 120:
            raise TestnetEvidenceError("GraphQL timeout must be greater than zero and at most 120 seconds")
        object.__setattr__(self, "_request_id", 0)
        object.__setattr__(self, "_calls", [])

    def call(
        self,
        method: str,
        params: Sequence[object] = (),
        *,
        _page_connection: str | None = None,
        _after: str | None = None,
    ) -> Any:
        """Perform one allowlisted GraphQL read and retain a response digest."""

        if method not in GRAPHQL_READ_OPERATIONS:
            raise TestnetEvidenceError(f"GraphQL read operation is not allowlisted: {method}")
        if not isinstance(params, (list, tuple)):
            raise TestnetEvidenceError("GraphQL operation parameters must be a positional list")
        if _page_connection is not None and not _page_connection.strip():
            raise TestnetEvidenceError("GraphQL page connection must be non-empty")
        if _after is not None and not _after.strip():
            raise TestnetEvidenceError("GraphQL page cursor must be non-empty")
        request_id = int(self._request_id) + 1
        object.__setattr__(self, "_request_id", request_id)
        body = self._graphql_body(method, list(params), page_connection=_page_connection, after=_after)
        request = Request(
            self.graphql_url,
            data=_canonical_json(body),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            raw = self.transport(request, self.timeout)
        except TestnetEvidenceError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            raise TestnetEvidenceError(f"read-only testnet GraphQL transport failed: {exc}") from exc
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
            raise TestnetEvidenceError("testnet GraphQL response is missing or exceeds the bounded size")
        try:
            document = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TestnetEvidenceError("testnet GraphQL response returned malformed JSON") from exc
        if not isinstance(document, dict):
            raise TestnetEvidenceError("testnet GraphQL response is not an object")
        if any(key in document for key in ("jsonrpc", "id", "method", "params", "result", "error")):
            raise TestnetEvidenceError("testnet GraphQL endpoint returned a non-GraphQL response")
        if document.get("errors"):
            raise TestnetEvidenceError(f"testnet GraphQL query returned an error for {method}: {document['errors']}")
        data = document.get("data")
        if not isinstance(data, dict):
            raise TestnetEvidenceError(f"testnet GraphQL response has no data for {method}")
        result = self._graphql_result(method, data)
        calls = list(self._calls)
        call_record: dict[str, object] = {
            "id": request_id,
            "method": method,
            "params": list(params),
            "result_sha256": hashlib.sha256(bytes(raw)).hexdigest(),
        }
        if _page_connection is not None:
            call_record["page_connection"] = _page_connection
        if _after is not None:
            call_record["after"] = _after
        calls.append(call_record)
        object.__setattr__(self, "_calls", calls)
        return result

    @staticmethod
    def _graphql_body(
        method: str,
        params: list[object],
        *,
        page_connection: str | None = None,
        after: str | None = None,
    ) -> dict[str, str]:
        """Map stable read names to complete, cursor-aware testnet GraphQL queries."""

        if method == "sui_getChainIdentifier":
            if page_connection is not None or after is not None:
                raise TestnetEvidenceError("chain identity does not support GraphQL pagination")
            return {"query": "{ chainIdentifier }"}
        if method == "sui_getLatestCheckpointSequenceNumber":
            if page_connection is not None or after is not None:
                raise TestnetEvidenceError("checkpoint does not support GraphQL pagination")
            return {"query": "{ checkpoint { sequenceNumber } }"}

        def connection(name: str, node_selection: str) -> str:
            if page_connection is not None and page_connection != name:
                return ""
            cursor = f", after: {json.dumps(after)}" if after is not None else ""
            return (
                f"{name}(first: {GRAPHQL_PAGE_SIZE}{cursor}) "
                "{ pageInfo { hasNextPage endCursor } nodes { "
                + node_selection
                + " } }"
            )

        if method in {"sui_getObject", "sui_getPackage"}:
            if not params or not isinstance(params[0], str) or not params[0].strip():
                raise TestnetEvidenceError(f"{method} requires a package/object identifier")
            if page_connection not in {None, "modules"}:
                raise TestnetEvidenceError(f"{method} does not support GraphQL page {page_connection!r}")
            identifier = json.dumps(params[0])
            modules = connection("modules", "name fullyQualifiedName")
            if method == "sui_getObject":
                query = (
                    "{ object(address: "
                    + identifier
                    + ") { address version digest owner { __typename "
                    "... on AddressOwner { address { address } } "
                    "... on ObjectOwner { address { address } } "
                    "... on Shared { initialSharedVersion } "
                    "... on ConsensusAddressOwner { startVersion address { address } } "
                    "... on Immutable { _ } } "
                    "asMoveObject { contents { type { repr signature } json bcs } } "
                    "asMovePackage { "
                    + modules
                    + " } } }"
                )
            else:
                query = (
                    "{ package(address: "
                    + identifier
                    + ") { address version digest "
                    + modules
                    + " } }"
                )
            return {"query": query}

        if method in {
            "sui_getNormalizedMoveModule",
            "sui_getNormalizedMoveFunction",
            "sui_getNormalizedMoveStruct",
        }:
            if len(params) < 2 or not all(isinstance(item, str) and item.strip() for item in params[:2]):
                raise TestnetEvidenceError(f"{method} requires package and module names")
            package_id = json.dumps(params[0])
            module_name = json.dumps(params[1])
            if method == "sui_getNormalizedMoveModule":
                if page_connection not in {None, "functions", "structs"}:
                    raise TestnetEvidenceError(f"module does not support GraphQL page {page_connection!r}")
                selection = (
                    "name fullyQualifiedName "
                    + connection("functions", "name fullyQualifiedName")
                    + connection("structs", "name fullyQualifiedName")
                )
            elif method == "sui_getNormalizedMoveFunction":
                if page_connection is not None or after is not None:
                    raise TestnetEvidenceError("normalized function lists are returned completely in one read")
                if len(params) < 3 or not isinstance(params[2], str) or not params[2].strip():
                    raise TestnetEvidenceError("function evidence requires a function name")
                selection = (
                    f"function(name: {json.dumps(params[2])}) {{ "
                    "name fullyQualifiedName isEntry visibility "
                    "typeParameters { constraints } "
                    "parameters { repr signature } return { repr signature } }"
                )
            else:
                if page_connection is not None or after is not None:
                    raise TestnetEvidenceError("normalized struct lists are returned completely in one read")
                if len(params) < 3 or not isinstance(params[2], str) or not params[2].strip():
                    raise TestnetEvidenceError("struct evidence requires a struct name")
                selection = (
                    f"struct(name: {json.dumps(params[2])}) {{ "
                    "name fullyQualifiedName abilities typeParameters { constraints isPhantom } "
                    "fields { name type { repr signature } } }"
                )
            return {"query": f"{{ package(address: {package_id}) {{ module(name: {module_name}) {{ {selection} }} }} }}"}
        raise TestnetEvidenceError(f"no GraphQL mapping exists for read method: {method}")

    def _graphql_complete(
        self,
        method: str,
        params: Sequence[object],
        connections: Sequence[tuple[str, tuple[str, ...], bool]],
    ) -> Any:
        """Read a GraphQL result and exhaust each requested connection safely."""

        result = self.call(method, params)
        if not connections:
            return result
        if not isinstance(result, dict):
            raise _observation_error(method)

        def at_path(value: Mapping[str, object], path: tuple[str, ...]) -> Mapping[str, object] | None:
            current: object = value
            for part in path:
                if not isinstance(current, Mapping):
                    return None
                current = current.get(part)
            return current if isinstance(current, Mapping) else None

        def replace_at_path(value: dict[str, object], path: tuple[str, ...], replacement: Mapping[str, object]) -> None:
            current: object = value
            for part in path[:-1]:
                if not isinstance(current, dict) or part not in current:
                    raise _observation_error(method)
                current = current[part]
            if not isinstance(current, dict) or not path:
                raise _observation_error(method)
            current[path[-1]] = dict(replacement)

        for connection_name, path, require_node in connections:
            connection = at_path(result, path)
            if connection is None:
                continue
            fqn_parts = 2 if connection_name == "modules" else 3
            _require_connection(
                connection,
                method,
                require_node=require_node,
                require_page_info=True,
                require_fully_qualified_name=True,
                fqn_parts=fqn_parts,
            )

            def node_key(node: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
                return (
                    str(node["name"]),
                    _canonical_fully_qualified_name(node["fullyQualifiedName"], method, fqn_parts),
                )

            seen_cursors: set[str] = set()
            seen_nodes = {node_key(node) for node in connection["nodes"] if isinstance(node, Mapping)}
            for _ in range(MAX_GRAPHQL_PAGES):
                has_next, cursor = _require_page_info(connection, method)
                if not has_next:
                    break
                if cursor is None or cursor in seen_cursors:
                    raise _observation_error(method)
                seen_cursors.add(cursor)
                page = self.call(method, params, _page_connection=connection_name, _after=cursor)
                if not isinstance(page, dict):
                    raise _observation_error(method)
                page_connection = at_path(page, path)
                if page_connection is None:
                    raise _observation_error(method)
                _require_connection(
                    page_connection,
                    method,
                    require_node=require_node,
                    require_page_info=True,
                    require_fully_qualified_name=True,
                    fqn_parts=fqn_parts,
                )
                page_nodes = page_connection["nodes"]
                if has_next and not page_nodes:
                    raise _observation_error(method)
                for node in page_nodes:
                    key = node_key(node)
                    if key in seen_nodes:
                        raise _observation_error(method)
                    seen_nodes.add(key)
                merged = dict(connection)
                merged["nodes"] = [*connection["nodes"], *page_nodes]
                merged["pageInfo"] = page_connection["pageInfo"]
                replace_at_path(result, path, merged)
                connection = merged
            else:
                raise _observation_error(method)
        return result

    @staticmethod
    def _graphql_result(method: str, data: Mapping[str, object]) -> object:
        if method == "sui_getChainIdentifier":
            return data.get("chainIdentifier")
        if method == "sui_getLatestCheckpointSequenceNumber":
            checkpoint = data.get("checkpoint")
            return checkpoint.get("sequenceNumber") if isinstance(checkpoint, dict) else None
        if method == "sui_getObject":
            return data.get("object")
        if method == "sui_getPackage":
            return data.get("package")
        package = data.get("package")
        if not isinstance(package, dict):
            return None
        module = package.get("module")
        if not isinstance(module, dict):
            return None
        if method == "sui_getNormalizedMoveFunction":
            return module.get("function")
        if method == "sui_getNormalizedMoveStruct":
            return module.get("struct")
        return module

    def collect(
        self,
        *,
        package_id: str | None = None,
        object_id: str | None = None,
        module: str | None = None,
        function: str | None = None,
        struct: str | None = None,
    ) -> dict[str, object]:
        """Collect network identity and optional package/module/object reads."""

        if package_id is not None:
            package_id = _validate_identifier(package_id, "package-id")
        if object_id is not None:
            object_id = _validate_identifier(object_id, "object-id")
        if module is not None:
            module = _canonical_move_identifier(module, "module")
        if function is not None:
            function = _canonical_move_identifier(function, "function")
        if struct is not None:
            struct = _canonical_move_identifier(struct, "struct")
        if module is not None and package_id is None:
            raise TestnetEvidenceError("module evidence requires --package-id")
        if (function is not None or struct is not None) and module is None:
            raise TestnetEvidenceError("function or struct evidence requires --module")
        chain_identifier = _require_chain_identifier(self.call("sui_getChainIdentifier"))
        latest_checkpoint = _require_checkpoint(self.call("sui_getLatestCheckpointSequenceNumber"))
        observations: dict[str, object] = {
            "chain_identifier": chain_identifier,
            "latest_checkpoint": latest_checkpoint,
        }
        if object_id is not None:
            observations["object"] = _require_observation(
                self._graphql_complete(
                    "sui_getObject",
                    [
                        object_id,
                        {
                            "showType": True,
                            "showOwner": True,
                            "showPreviousTransaction": True,
                            "showContent": True,
                            "showBcs": False,
                            "showStorageRebate": False,
                            "showDisplay": False,
                        },
                    ],
                    (("modules", ("asMovePackage", "modules"), True),),
                ),
                "object",
                expected_object_id=object_id,
            )
        if package_id is not None:
            observations["package"] = _require_observation(
                self._graphql_complete("sui_getPackage", [package_id], (("modules", ("modules",), True),)),
                "package",
                expected_package_id=package_id,
            )
        if module is not None:
            observations["module"] = _require_observation(
                self._graphql_complete(
                    "sui_getNormalizedMoveModule",
                    [package_id, module],
                    (
                        ("functions", ("functions",), False),
                        ("structs", ("structs",), False),
                    ),
                ),
                "module",
                expected_package_id=package_id,
                expected_module=module,
            )
        if function is not None:
            observations["function"] = _require_observation(
                self._graphql_complete("sui_getNormalizedMoveFunction", [package_id, module, function], ()),
                "function",
                expected_package_id=package_id,
                expected_module=module,
                expected_function=function,
            )
        if struct is not None:
            observations["struct"] = _require_observation(
                self._graphql_complete("sui_getNormalizedMoveStruct", [package_id, module, struct], ()),
                "struct",
                expected_package_id=package_id,
                expected_module=module,
                expected_struct=struct,
            )
        # Navigation only: links are derived from returned, validated addresses.
        vision_links: dict[str, str] = {}
        for label in ("object", "package"):
            observation = observations.get(label)
            if isinstance(observation, Mapping):
                try:
                    vision_links[label] = vision_url("object", observation.get("address"), network="testnet")
                except VisionLinkError:
                    continue
        evidence: dict[str, object] = {
            "schema_version": 1,
            "network": "testnet",
            "endpoint": self.graphql_url,
            "canonicalization": "request selectors are canonicalized for lookup; returned address-bearing IDs, FQNs, and Move repr values are validated as canonical before hashing",
            "observations": observations,
            "vision_links": vision_links,
            "calls": list(self._calls),
            "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        evidence["sha256"] = _digest(evidence)
        return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphql-url", required=True, help="explicit official Sui testnet GraphQL endpoint")
    parser.add_argument("--package-id", help="optional package identifier to inspect")
    parser.add_argument("--object-id", help="optional object identifier to inspect")
    parser.add_argument("--module", help="optional Move module name")
    parser.add_argument("--function", help="optional Move function name")
    parser.add_argument("--struct", help="optional Move struct name")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = SuiTestnetEvidenceClient(args.graphql_url, timeout=args.timeout).collect(
            package_id=args.package_id,
            object_id=args.object_id,
            module=args.module,
            function=args.function,
            struct=args.struct,
        )
    except TestnetEvidenceError as exc:
        print(f"testnet evidence failed: {exc}")
        return 2
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
