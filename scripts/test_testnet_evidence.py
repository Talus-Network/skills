#!/usr/bin/env python3
"""Tests for the explicit read-only Sui testnet GraphQL boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest import mock
import sys
import unittest
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parent))

from testnet_evidence import (  # noqa: E402
    GRAPHQL_READ_OPERATIONS,
    MAX_MOVE_UNSIGNED_INTEGER,
    OFFICIAL_TESTNET_CHAIN_IDENTIFIER,
    SuiTestnetEvidenceClient,
    TestnetEvidenceError,
    _urlopen_transport,
    _validate_graphql_url,
    _require_graphql_move_content,
    _require_graphql_owner,
    _require_graphql_type_signature,
    _require_observation,
)


def _fqn(package: str, module: str, name: str) -> str:
    return f"{package}::{module}::{name}"


PACKAGE_DIGEST = "1" * 31 + "2"
OBJECT_DIGEST = "1" * 31 + "3"
FULL_TWO = "0x" + "0" * 63 + "2"
FULL_SIX = "0x" + "0" * 63 + "6"
FULL_SEVEN = "0x" + "0" * 63 + "7"


def _full_address(value: str) -> str:
    return "0x" + value[2:].lower().zfill(64) if value.startswith("0x") else value


def _type_signature(
    repr_value: str = "u64",
    *,
    package: str = "0x2",
    module: str = "coin",
    name: str = "Coin",
    type_parameters: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    package = _full_address(package)
    if repr_value.startswith("0x2::"):
        repr_value = FULL_TWO + repr_value[3:]
    return {
        "repr": repr_value,
        "signature": {
            "body": {
                "datatype": {
                    "package": package,
                    "module": module,
                    "type": name,
                    "typeParameters": [] if type_parameters is None else type_parameters,
                }
            }
        },
    }


def _primitive_signature(repr_value: str = "u64") -> dict[str, object]:
    return {"repr": repr_value, "signature": {"primitive": repr_value}}


def _graphql_function(name: str = "balance", module: str = "coin", package: str = "0x2") -> dict[str, object]:
    package = _full_address(package)
    return {
        "name": name,
        "fullyQualifiedName": _fqn(package, module, name),
        "isEntry": False,
        "visibility": "PUBLIC",
        "typeParameters": [{"constraints": []}],
        "parameters": [_primitive_signature()],
        "return": [_primitive_signature()],
    }


def _graphql_struct(name: str = "Coin", module: str = "coin", package: str = "0x2") -> dict[str, object]:
    package = _full_address(package)
    return {
        "name": name,
        "fullyQualifiedName": _fqn(package, module, name),
        "abilities": ["KEY"],
        "typeParameters": [{"constraints": [], "isPhantom": False}],
        "fields": [{"name": "value", "type": _primitive_signature()}],
    }


def _connection(nodes: list[dict[str, object]], *, has_next: bool = False, cursor: str | None = None) -> dict[str, object]:
    return {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}


def _graphql_module(name: str = "coin", package: str = "0x2") -> dict[str, object]:
    package = _full_address(package)
    return {
        "name": name,
        "fullyQualifiedName": f"{package}::{name}",
        "functions": _connection(
            [{"name": "balance", "fullyQualifiedName": _fqn(package, name, "balance")}]
        ),
        "structs": _connection(
            [{"name": "Coin", "fullyQualifiedName": _fqn(package, name, "Coin")}]
        ),
    }


def _graphql_package(package: str = "0x2") -> dict[str, object]:
    package = _full_address(package)
    return {
        "address": package,
        "version": 1,
        "digest": PACKAGE_DIGEST,
        "modules": _connection([{"name": "coin", "fullyQualifiedName": f"{package}::coin"}]),
    }


def _graphql_object(object_id: str = "0x6") -> dict[str, object]:
    object_id = _full_address(object_id)
    return {
        "address": object_id,
        "version": 1,
        "digest": OBJECT_DIGEST,
        "owner": {"__typename": "Shared", "initialSharedVersion": 1},
        "asMoveObject": {
            "contents": {
                "type": _type_signature(
                    "0x2::clock::Clock",
                    module="clock",
                    name="Clock",
                ),
                "json": {"id": object_id, "timestamp_ms": "1"},
                "bcs": "AQ==",
            }
        },
        "asMovePackage": None,
    }


class TestnetEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[dict[str, object]] = []

        def transport(request, _timeout):
            body = json.loads(request.data.decode("utf-8"))
            self.requests.append(body)
            query = body["query"]
            if "chainIdentifier" in query:
                data = {"chainIdentifier": OFFICIAL_TESTNET_CHAIN_IDENTIFIER}
            elif "checkpoint" in query:
                data = {"checkpoint": {"sequenceNumber": 42}}
            elif "object(address" in query:
                data = {"object": _graphql_object("0x6")}
            elif "function(name" in query:
                data = {"package": {"module": {"function": _graphql_function()}}}
            elif "struct(name" in query:
                data = {"package": {"module": {"struct": _graphql_struct()}}}
            elif "module(name" in query:
                data = {"package": {"module": _graphql_module()}}
            else:
                data = {"package": _graphql_package()}
            return json.dumps({"data": data}).encode()

        self.transport = transport

    def test_collects_explicit_testnet_identity_and_read_only_queries(self) -> None:
        client = SuiTestnetEvidenceClient(
            "https://graphql.testnet.sui.io/graphql",
            transport=self.transport,
        )
        evidence = client.collect(
            package_id="0x2",
            object_id="0x6",
            module="coin",
            function="balance",
            struct="Coin",
        )
        self.assertEqual(evidence["network"], "testnet")
        self.assertEqual(evidence["endpoint"], "https://graphql.testnet.sui.io/graphql")
        self.assertEqual(len(evidence["calls"]), 7)
        self.assertTrue(evidence["sha256"])
        self.assertEqual(len(self.requests), 7)
        self.assertTrue(all("mutation" not in request["query"].lower() for request in self.requests))
        self.assertTrue(all(set(request) == {"query"} for request in self.requests))

    def test_endpoint_must_be_the_official_graphql_url(self) -> None:
        urls = (
            "",
            "http://" + "graphql.testnet.sui.io/graphql",
            "https://" + "example.invalid/graphql",
            "https://" + "graphql.testnet.sui.io",
            "https://graphql.testnet.sui.io/graphql" + "?wallet=secret",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(TestnetEvidenceError):
                SuiTestnetEvidenceClient(url, transport=self.transport)

        for url in (
            "https://" + "graphql.testnet.sui.io" + ":443/graphql",
            "https://" + "graphql.testnet.sui.io" + ":8443/graphql",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(TestnetEvidenceError, "explicit port"):
                _validate_graphql_url(url)

    def test_graphql_transport_validates_final_redirect_before_reading(self) -> None:
        class Response:
            def __init__(self, final_url: str) -> None:
                self.final_url = final_url
                self.read_called = False

            def __enter__(self):
                return self

            def __exit__(self, _kind, _value, _traceback):
                return False

            def geturl(self) -> str:
                return self.final_url

            def read(self, _limit: int) -> bytes:
                self.read_called = True
                return b"{}"

        request = Request("https://graphql.testnet.sui.io/graphql", data=b"{}", method="POST")
        for final_url in (
            "https://" + "graphql.testnet.sui.io" + ":443/graphql",
            "https://" + "graphql.testnet.sui.io/other",
            "https://" + "evil.invalid/graphql",
        ):
            response = Response(final_url)
            with self.subTest(final_url=final_url), mock.patch(
                "testnet_evidence.urlopen", return_value=response
            ), self.assertRaises(TestnetEvidenceError):
                _urlopen_transport(request, 1.0)
            self.assertFalse(response.read_called)

        response = Response("https://graphql.testnet.sui.io/graphql")
        with mock.patch("testnet_evidence.urlopen", return_value=response):
            self.assertEqual(_urlopen_transport(request, 1.0), b"{}")
        self.assertTrue(response.read_called)

    def test_deprecated_json_rpc_hosts_are_rejected_before_transport(self) -> None:
        for host in ("fullnode.testnet.sui.io", "rpc.testnet.sui.io"):
            called = False

            def transport(_request, _timeout):
                nonlocal called
                called = True
                raise AssertionError("deprecated endpoint must not be contacted")

            with self.subTest(host=host), self.assertRaisesRegex(TestnetEvidenceError, "deprecated"):
                SuiTestnetEvidenceClient("https://" + host, transport=transport)
            self.assertFalse(called)

    def test_non_read_graphql_operation_and_empty_parameters_are_rejected(self) -> None:
        client = SuiTestnetEvidenceClient("https://graphql.testnet.sui.io/graphql", transport=self.transport)
        self.assertRaisesRegex(TestnetEvidenceError, "not allowlisted", client.call, "sui_executeTransactionBlock")
        self.assertRaisesRegex(TestnetEvidenceError, "positional list", client.call, "sui_getPackage", {"id": "0x2"})
        self.assertEqual(
            GRAPHQL_READ_OPERATIONS,
            frozenset(
                {
                    "sui_getChainIdentifier",
                    "sui_getLatestCheckpointSequenceNumber",
                    "sui_getObject",
                    "sui_getPackage",
                    "sui_getNormalizedMoveModule",
                    "sui_getNormalizedMoveFunction",
                    "sui_getNormalizedMoveStruct",
                }
            ),
        )

    def test_graphql_request_never_uses_json_rpc_and_json_rpc_envelope_is_rejected(self) -> None:
        seen: list[dict[str, object]] = []

        def transport(request, _timeout):
            body = json.loads(request.data.decode("utf-8"))
            seen.append(body)
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()

        client = SuiTestnetEvidenceClient("https://graphql.testnet.sui.io/graphql", transport=transport)
        with self.assertRaisesRegex(TestnetEvidenceError, "non-GraphQL"):
            client.call("sui_getChainIdentifier")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("jsonrpc", seen[0])
        self.assertIn("chainIdentifier", seen[0]["query"])

    def test_wrong_chain_and_graphql_errors_are_actionable(self) -> None:
        def wrong_chain(request, _timeout):
            query = json.loads(request.data.decode("utf-8"))["query"]
            if "chainIdentifier" in query:
                return json.dumps({"data": {"chainIdentifier": "mainnet"}}).encode()
            return json.dumps({"data": {"checkpoint": {"sequenceNumber": 1}}}).encode()

        with self.assertRaisesRegex(TestnetEvidenceError, "official Sui testnet"):
            SuiTestnetEvidenceClient("https://graphql.testnet.sui.io/graphql", transport=wrong_chain).collect()

        def graphql_error(_request, _timeout):
            return json.dumps({"errors": [{"message": "schema unavailable"}]}).encode()

        client = SuiTestnetEvidenceClient("https://graphql.testnet.sui.io/graphql", transport=graphql_error)
        with self.assertRaisesRegex(TestnetEvidenceError, "schema unavailable"):
            client.call("sui_getChainIdentifier")

    def test_null_or_incomplete_requested_observations_fail_closed(self) -> None:
        cases = {
            "package": (None, {"expected_package_id": "0x2"}),
            "object": ({"address": "0x6"}, {"expected_object_id": "0x6"}),
            "module": ({}, {"expected_package_id": "0x2", "expected_module": "coin"}),
            "function": ({"name": "balance"}, {"expected_package_id": "0x2", "expected_module": "coin", "expected_function": "balance"}),
            "struct": ({"name": "Coin"}, {"expected_package_id": "0x2", "expected_module": "coin", "expected_struct": "Coin"}),
        }
        for label, (value, expected) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                _require_observation(value, label, **expected)

    def test_graphql_identity_fields_are_nonempty_and_complete(self) -> None:
        cases = (
            ("package-address", "package", {**_graphql_package(), "address": ""}),
            ("object-address", "object", {**_graphql_object(), "address": ""}),
            ("module-name", "module", {**_graphql_module(), "name": ""}),
            ("function-name", "function", {**_graphql_function(), "name": ""}),
            ("struct-fqn", "struct", {**_graphql_struct(), "fullyQualifiedName": " "}),
            ("package-unnamed-module", "package", {**_graphql_package(), "modules": _connection([{"name": "coin", "fullyQualifiedName": "0x2::"}])}),
            ("module-unnamed-function", "module", {**_graphql_module(), "functions": _connection([{"name": " ", "fullyQualifiedName": "0x2::coin::balance"}])}),
        )
        for label, kind, value in cases:
            with self.subTest(label=label), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                _require_observation(value, kind)

    def test_graphql_observations_require_complete_connections_and_fields(self) -> None:
        cases = (
            ("module-missing-functions", "module", {**_graphql_module(), "functions": None}),
            ("module-missing-page-info", "module", {**_graphql_module(), "functions": {"nodes": _graphql_module()["functions"]["nodes"]}}),
            ("function-missing-signature", "function", {key: value for key, value in _graphql_function().items() if key != "return"}),
            ("struct-missing-fields", "struct", {key: value for key, value in _graphql_struct().items() if key != "fields"}),
            ("object-missing-owner", "object", {key: value for key, value in _graphql_object().items() if key != "owner"}),
            ("object-both-content-variants", "object", {**_graphql_object(), "asMovePackage": {"modules": _connection([{"name": "coin", "fullyQualifiedName": "0x6::coin"}])}}),
            ("object-no-content-variant", "object", {**_graphql_object(), "asMoveObject": None}),
        )
        for label, kind, value in cases:
            with self.subTest(label=label), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                _require_observation(value, kind)

    def test_graphql_owner_addresses_are_canonical_on_direct_and_collect_paths(self) -> None:
        owners = (
            {"__typename": "AddressOwner", "address": {"address": FULL_TWO}},
            {"__typename": "ObjectOwner", "address": {"address": FULL_SIX}},
            {
                "__typename": "ConsensusAddressOwner",
                "startVersion": 1,
                "address": {"address": FULL_TWO},
            },
        )
        for owner in owners:
            with self.subTest(owner=owner["__typename"]):
                _require_graphql_owner(owner, "owner")
        invalid_owners = (
            {"__typename": "AddressOwner", "address": {"address": "0x2"}},
            {"__typename": "AddressOwner", "address": {"address": "not-a-sui-id"}},
            {"__typename": "ObjectOwner", "address": {"address": ""}},
            {
                "__typename": "ConsensusAddressOwner",
                "startVersion": 1,
                "address": {"address": "0x"},
            },
        )
        for owner in invalid_owners:
            with self.subTest(owner=owner), self.assertRaisesRegex(TestnetEvidenceError, "(?:canonical|missing or incomplete)"):
                _require_graphql_owner(owner, "owner")

        def malformed_owner_transport(request, _timeout):
            query = json.loads(request.data.decode("utf-8"))["query"]
            if "chainIdentifier" in query:
                data = {"chainIdentifier": OFFICIAL_TESTNET_CHAIN_IDENTIFIER}
            elif "checkpoint" in query:
                data = {"checkpoint": {"sequenceNumber": 7}}
            else:
                malformed = _graphql_object("0x6")
                malformed["owner"] = {
                    "__typename": "AddressOwner",
                    "address": {"address": "not-a-sui-id"},
                }
                data = {"object": malformed}
            return json.dumps({"data": data}).encode()

        with self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
            SuiTestnetEvidenceClient(
                "https://graphql.testnet.sui.io/graphql", transport=malformed_owner_transport
            ).collect(object_id="0x6")

    def test_requested_graphql_identities_are_canonical_and_bound(self) -> None:
        full_two = "0x" + "0" * 63 + "2"
        full_six = "0x" + "0" * 63 + "6"
        valid = (
            ("package", _graphql_package("0x2"), {"expected_package_id": full_two}),
            ("object", _graphql_object("0x6"), {"expected_object_id": full_six}),
            ("module", _graphql_module(), {"expected_package_id": full_two, "expected_module": "coin"}),
            ("function", _graphql_function(), {"expected_package_id": full_two, "expected_module": "coin", "expected_function": "balance"}),
            ("struct", _graphql_struct(), {"expected_package_id": full_two, "expected_module": "coin", "expected_struct": "Coin"}),
        )
        for kind, value, expected in valid:
            with self.subTest(kind=kind):
                _require_observation(value, kind, **expected)
        mismatches = (
            ("package", {**_graphql_package(), "address": "0x3"}, {"expected_package_id": "0x2"}),
            ("object", {**_graphql_object(), "address": "0x7"}, {"expected_object_id": "0x6"}),
            ("module", {**_graphql_module(), "fullyQualifiedName": "0x3::coin"}, {"expected_package_id": "0x2", "expected_module": "coin"}),
            ("function", {**_graphql_function(), "fullyQualifiedName": "0x2::other::balance"}, {"expected_package_id": "0x2", "expected_module": "coin", "expected_function": "balance"}),
            ("struct", {**_graphql_struct(), "name": "Other"}, {"expected_package_id": "0x2", "expected_module": "coin", "expected_struct": "Coin"}),
        )
        for kind, value, expected in mismatches:
            with self.subTest(kind=kind), self.assertRaisesRegex(TestnetEvidenceError, "(?:does not match|canonical)"):
                _require_observation(value, kind, **expected)

    def test_move_fqn_segments_use_canonical_identifier_grammar(self) -> None:
        invalid = (
            ("module", {**_graphql_module(name="9coin"), "fullyQualifiedName": "0x2::9coin"}),
            ("module", {**_graphql_module(name="coin-module"), "fullyQualifiedName": "0x2::coin-module"}),
            ("module", {**_graphql_module(name="é"), "fullyQualifiedName": "0x2::é"}),
            ("function", {**_graphql_function(name="bad name"), "fullyQualifiedName": "0x2::coin::bad name"}),
            ("function", {**_graphql_function(name="valid-name"), "fullyQualifiedName": "0x2::coin::valid-name"}),
            ("struct", {**_graphql_struct(name="9Coin"), "fullyQualifiedName": "0x2::coin::9Coin"}),
            ("struct", {**_graphql_struct(name="Coin "), "fullyQualifiedName": "0x2::coin::Coin "}),
        )
        for kind, value in invalid:
            with self.subTest(kind=kind, value=value), self.assertRaisesRegex(
                TestnetEvidenceError, "(?:missing or incomplete|canonical)"
            ):
                _require_observation(value, kind)

    def test_package_and_object_digests_are_canonical_sui_base58(self) -> None:
        valid = (
            ("package", _graphql_package()),
            ("object", _graphql_object()),
        )
        for kind, value in valid:
            with self.subTest(kind=kind):
                _require_observation(value, kind)
        invalid_digests = (
            "1" * 30 + "2",  # 31 decoded bytes
            "1" * 32 + "2",  # 33 decoded bytes
            "0" * 32,  # forbidden base58 zero
            "O" * 43,  # forbidden base58 letter
            "1" * 31 + "2 ",  # non-canonical whitespace
        )
        for digest in invalid_digests:
            for kind, value in valid:
                altered = copy.deepcopy(value)
                altered["digest"] = digest
                with self.subTest(kind=kind, digest=digest), self.assertRaisesRegex(
                    TestnetEvidenceError, "missing or incomplete"
                ):
                    _require_observation(altered, kind)

    def test_graphql_nested_signatures_and_move_content_are_complete(self) -> None:
        valid_datatype = _type_signature(
            "0x2::coin::Coin<$0>",
            type_parameters=[{"typeParameter": 0}],
        )
        _require_graphql_type_signature(valid_datatype, "function")
        _require_graphql_type_signature(_type_signature("0x2::clock::Clock", module="clock", name="Clock"), "object")
        valid_content = {"type": valid_datatype, "json": {"id": FULL_SIX}, "bcs": "AQ=="}
        _require_graphql_move_content(valid_content, "object")
        invalid_signatures = (
            {"body": {}},
            {"body": {"datatype": {"package": "0x2", "module": "coin", "type": "Coin"}}},
            {"body": {"datatype": {"package": "0x2", "module": "coin", "type": "Coin", "typeParameters": [{}]}}},
            {"body": {"datatype": {"package": "0x2", "module": "coin", "type": "Coin", "typeParameters": [{"typeParameter": False}]}}},
            {"body": {"vector": []}},
            {"body": {"vector": {"body": {}}}},
            {"body": {"unknown": "placeholder"}},
        )
        for index, signature in enumerate(invalid_signatures):
            with self.subTest(signature=index), self.assertRaisesRegex(TestnetEvidenceError, "(?:missing or incomplete|canonical)"):
                _require_graphql_type_signature({"repr": "invalid", "signature": signature}, "function")
        invalid_contents = ({}, "", False, 0, [], "placeholder")
        for content_value in invalid_contents:
            with self.subTest(content=repr(content_value)), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                _require_graphql_move_content({"type": valid_datatype, "json": content_value, "bcs": None}, "object")
        with self.assertRaisesRegex(TestnetEvidenceError, "(?:missing or incomplete|canonical)"):
            _require_graphql_move_content({"type": valid_datatype, "json": {"id": "0x6"}, "bcs": ""}, "object")

    def test_graphql_json_scalars_match_move_unsigned_and_boolean_values(self) -> None:
        valid_type = _type_signature("0x2::coin::Coin")
        _require_graphql_move_content(
            {
                "type": valid_type,
                "json": {
                    "id": FULL_SIX,
                    "enabled": True,
                    "disabled": False,
                    "zero": 0,
                    "maximum": MAX_MOVE_UNSIGNED_INTEGER,
                    "nested": {"values": [False, 0, MAX_MOVE_UNSIGNED_INTEGER], "empty": []},
                },
                "bcs": None,
            },
            "object",
        )
        invalid_values = (
            -1,
            MAX_MOVE_UNSIGNED_INTEGER + 1,
            1.0,
            -0.0,
            float("nan"),
            float("inf"),
            object(),
            {"items": [-1]},
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)), self.assertRaisesRegex(
                TestnetEvidenceError, "missing or incomplete"
            ):
                _require_graphql_move_content(
                    {"type": valid_type, "json": {"id": FULL_SIX, "value": value}, "bcs": None}, "object"
                )

    def test_graphql_signatures_and_nested_json_content_are_semantically_bound(self) -> None:
        contradictory = (
            _type_signature("u64"),
            {
                "repr": f"{FULL_TWO}::coin::Coin<$0>",
                "signature": {"body": {"primitive": "u64"}},
            },
            {
                "repr": "vector<u8>",
                "signature": {"body": {"vector": {"primitive": "bool"}}},
            },
        )
        for signature in contradictory:
            with self.subTest(signature=signature), self.assertRaisesRegex(TestnetEvidenceError, "do not agree"):
                _require_graphql_type_signature(signature, "function")

        valid_type = _type_signature("0x2::coin::Coin")
        invalid_json = (
            {"placeholder": "value"},
            {"id": None},
            {"id": False},
            {"id": 0},
            {"id": "0x" + "0" * 64},
            {"id": FULL_SIX, "objectId": FULL_SIX},
            {"id": FULL_SIX, "nested": {"object_id": "0x"}},
            {"id": FULL_SIX, "nested": {"object_id": {}}},
            {"id": FULL_SIX, "nested": {"items": [None]}},
        )
        for value in invalid_json:
            with self.subTest(value=value), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                _require_graphql_move_content({"type": valid_type, "json": value, "bcs": None}, "object")

        _require_graphql_move_content(
            {"type": valid_type, "json": {"id": FULL_SIX, "enabled": False, "amount": 0}, "bcs": None},
            "object",
        )
        _require_graphql_move_content(
            {
                "type": valid_type,
                "json": {
                    "id": FULL_SIX,
                    "valid": True,
                    "paid": False,
                    "metadata": {},
                    "items": [],
                    "amount": 0,
                },
                "bcs": None,
            },
            "object",
        )
        for identity_key in ("objectId", "object_id"):
            with self.subTest(identity_key=identity_key):
                _require_graphql_move_content(
                    {"type": valid_type, "json": {identity_key: FULL_SIX, "valid": True}, "bcs": None},
                    "object",
                    expected_object_id="0x6",
                )
        with self.assertRaisesRegex(TestnetEvidenceError, "does not match"):
            _require_graphql_move_content(
                {"type": valid_type, "json": {"id": FULL_SEVEN}, "bcs": None},
                "object",
                expected_object_id="0x6",
            )
        _require_graphql_move_content({"type": valid_type, "bcs": "AQ=="}, "object")
        for primitive in (_primitive_signature("u64"), _primitive_signature("bool")):
            with self.subTest(primitive=primitive), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                _require_graphql_move_content(
                    {"type": primitive, "json": {"id": "0x6"}, "bcs": "AQ=="}, "object"
                )
        for bcs in ("placeholder", "!!!!", "", "===="):
            with self.subTest(bcs=bcs), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                _require_graphql_move_content(
                    {"type": valid_type, "bcs": bcs}, "object"
                )

    def test_noncanonical_returned_observations_are_rejected_before_hashing(self) -> None:
        noncanonical = (
            ("package-address", "package", {**_graphql_package(), "address": "0x2"}),
            ("package-module-fqn", "package", {**_graphql_package(), "modules": _connection([{"name": "coin", "fullyQualifiedName": "0x2::coin"}])}),
            ("object-address", "object", {**_graphql_object(), "address": "0x6"}),
            ("object-content-id", "object", {**_graphql_object(), "asMoveObject": {"contents": {**_graphql_object()["asMoveObject"]["contents"], "json": {"id": "0x6", "timestamp_ms": "1"}}}}),
            ("module-fqn", "module", {**_graphql_module(), "fullyQualifiedName": "0x2::coin"}),
            ("function-fqn", "function", {**_graphql_function(), "fullyQualifiedName": "0x2::coin::balance"}),
            ("struct-fqn", "struct", {**_graphql_struct(), "fullyQualifiedName": "0x2::coin::Coin"}),
            ("owner-address", "owner", {"__typename": "AddressOwner", "address": {"address": "0x2"}}),
        )
        for label, kind, value in noncanonical:
            with self.subTest(label=label), self.assertRaisesRegex(TestnetEvidenceError, "canonical"):
                if kind == "owner":
                    _require_graphql_owner(value, "owner")
                else:
                    _require_observation(value, kind)

        valid_package = _require_observation(_graphql_package(), "package", expected_package_id="0x2")
        valid_object = _require_observation(_graphql_object(), "object", expected_object_id="0x6")
        self.assertEqual(valid_package["address"], FULL_TWO)
        self.assertEqual(valid_object["address"], FULL_SIX)

    def test_original_graphql_repr_must_be_canonical(self) -> None:
        valid = _type_signature(f"{FULL_TWO}::coin::Coin")
        for repr_value in (
            f" {FULL_TWO}::coin::Coin",
            f"{FULL_TWO}::coin::Coin ",
            f"{FULL_TWO.upper()}::coin::Coin",
            f"0x2::coin::Coin",
        ):
            altered = copy.deepcopy(valid)
            altered["repr"] = repr_value
            with self.subTest(repr_value=repr_value), self.assertRaisesRegex(
                TestnetEvidenceError, "missing or incomplete"
            ):
                _require_graphql_type_signature(altered, "object")

    def test_explicit_blank_selectors_fail_before_any_network_call(self) -> None:
        for selectors in (
            {"package_id": ""},
            {"package_id": "   "},
            {"object_id": ""},
            {"object_id": "   "},
            {"module": ""},
            {"module": "   "},
            {"package_id": "0x2", "module": "coin", "function": ""},
            {"package_id": "0x2", "module": "coin", "function": "   "},
            {"package_id": "0x2", "module": "coin", "struct": ""},
            {"package_id": "0x2", "module": "coin", "struct": "   "},
        ):
            called = False

            def transport(_request, _timeout):
                nonlocal called
                called = True
                raise AssertionError("blank selectors must fail before transport")

            with self.subTest(selectors=selectors), self.assertRaises(TestnetEvidenceError):
                SuiTestnetEvidenceClient(
                    "https://graphql.testnet.sui.io/graphql", transport=transport
                ).collect(**selectors)
            self.assertFalse(called)

    def test_graphql_connections_are_paginated_and_merged(self) -> None:
        requests: list[dict[str, object]] = []

        def transport(request, _timeout):
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            query = body["query"]
            if "chainIdentifier" in query:
                data = {"chainIdentifier": OFFICIAL_TESTNET_CHAIN_IDENTIFIER}
            elif "checkpoint" in query:
                data = {"checkpoint": {"sequenceNumber": 7}}
            elif "after: \"package-cursor\"" in query:
                data = {
                    "package": {
                        "address": FULL_TWO,
                        "version": 1,
                        "digest": PACKAGE_DIGEST,
                        "modules": _connection(
                            [{"name": "other", "fullyQualifiedName": f"{FULL_TWO}::other"}]
                        ),
                    }
                }
            else:
                data = {
                    "package": {
                        "address": FULL_TWO,
                        "version": 1,
                        "digest": PACKAGE_DIGEST,
                        "modules": _connection(
                            [{"name": "coin", "fullyQualifiedName": f"{FULL_TWO}::coin"}],
                            has_next=True,
                            cursor="package-cursor",
                        ),
                    }
                }
            return json.dumps({"data": data}).encode()

        evidence = SuiTestnetEvidenceClient(
            "https://graphql.testnet.sui.io/graphql", transport=transport
        ).collect(package_id="0x2")
        self.assertEqual(len(evidence["observations"]["package"]["modules"]["nodes"]), 2)
        self.assertTrue(any(call.get("after") == "package-cursor" for call in evidence["calls"]))
        self.assertEqual(len(requests), 4)

    def test_graphql_pagination_requires_exhaustion_and_cursor_progress(self) -> None:
        def make_transport(*, cursor: str | None, repeated: bool = False):
            def transport(request, _timeout):
                query = json.loads(request.data.decode("utf-8"))["query"]
                if "chainIdentifier" in query:
                    data = {"chainIdentifier": OFFICIAL_TESTNET_CHAIN_IDENTIFIER}
                elif "checkpoint" in query:
                    data = {"checkpoint": {"sequenceNumber": 7}}
                elif cursor is None:
                    data = {"package": {**_graphql_package(), "modules": _connection([{"name": "coin", "fullyQualifiedName": f"{FULL_TWO}::coin"}], has_next=True, cursor="cursor")}}
                else:
                    next_cursor = cursor if repeated else None
                    data = {"package": {**_graphql_package(), "modules": _connection([{"name": "other", "fullyQualifiedName": f"{FULL_TWO}::other"}], has_next=repeated, cursor=next_cursor)}}
                return json.dumps({"data": data}).encode()

            return transport

        for label, transport in (
            ("repeated", make_transport(cursor="cursor", repeated=True)),
            ("missing-cursor", make_transport(cursor=None, repeated=False)),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(TestnetEvidenceError, "missing or incomplete"):
                SuiTestnetEvidenceClient("https://graphql.testnet.sui.io/graphql", transport=transport).collect(package_id="0x2")

    def test_malformed_and_oversized_graphql_responses_fail_closed(self) -> None:
        malformed = SuiTestnetEvidenceClient(
            "https://graphql.testnet.sui.io/graphql", transport=lambda _request, _timeout: b"not-json"
        )
        with self.assertRaisesRegex(TestnetEvidenceError, "malformed JSON"):
            malformed.call("sui_getChainIdentifier")
        oversized = SuiTestnetEvidenceClient(
            "https://graphql.testnet.sui.io/graphql",
            transport=lambda _request, _timeout: b"x" * (8 * 1024 * 1024 + 1),
        )
        with self.assertRaisesRegex(TestnetEvidenceError, "bounded size"):
            oversized.call("sui_getChainIdentifier")

    def test_protocol_argument_rejects_private_json_rpc_shape(self) -> None:
        with self.assertRaisesRegex(TestnetEvidenceError, "only GraphQL"):
            _require_observation({}, "package", protocol="json-rpc")
        with self.assertRaisesRegex(TestnetEvidenceError, "non-GraphQL"):
            _require_observation({"jsonrpc": "2.0", "id": 1, "result": {}}, "package")


if __name__ == "__main__":
    unittest.main()
