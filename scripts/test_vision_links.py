#!/usr/bin/env python3
"""Tests for network-explicit Talus Vision navigation links."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vision_links import (  # noqa: E402
    VISION_KINDS,
    VisionLinkError,
    main,
    parse_vision_url,
    vision_url,
)


BASE = "https://" + "vision.talus.network"
EXECUTION = "0x" + "ab" * 32
AGENT = "0x" + "0" * 63 + "7"
DIGEST = "1" * 31 + "2"
TOOL_FQN = "xyz.taluslabs.math.add@1"


def _base58(payload: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(payload, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    return "1" * (len(payload) - len(payload.lstrip(b"\x00"))) + encoded


class VisionLinkTests(unittest.TestCase):
    def test_every_kind_builds_a_network_explicit_route(self) -> None:
        cases = {
            "tx": ((DIGEST,), "/tx/" + DIGEST),
            "object": ((EXECUTION,), "/object/" + EXECUTION),
            "workflow": ((EXECUTION,), "/workflow/" + EXECUTION),
            "execution": ((EXECUTION,), "/execution/" + EXECUTION),
            "payment": ((EXECUTION,), "/payment/" + EXECUTION),
            "task": ((EXECUTION,), "/task/" + EXECUTION),
            "tool": ((TOOL_FQN,), "/tool/xyz.taluslabs.math.add%401"),
            "agent": ((AGENT,), "/agent/" + AGENT),
            "skill": ((AGENT, 0), "/skill/" + AGENT + "/0"),
            "leader": ((EXECUTION,), "/leader/" + EXECUTION),
            "profile": ((EXECUTION,), "/profile/" + EXECUTION),
        }
        self.assertEqual(set(cases), set(VISION_KINDS))
        for kind, (identifiers, path) in cases.items():
            for network in ("testnet", "mainnet"):
                with self.subTest(kind=kind, network=network):
                    url = vision_url(kind, *identifiers, network=network)
                    self.assertEqual(url, BASE + path + "?network=" + network)
                    link = parse_vision_url(url)
                    self.assertEqual((link.kind, link.network, link.url), (kind, network, url))

    def test_network_must_be_explicit_and_supported(self) -> None:
        for network in (None, "", "devnet", "localnet", "Testnet", "testnet "):
            with self.subTest(network=network), self.assertRaisesRegex(VisionLinkError, "network"):
                vision_url("execution", EXECUTION, network=network)  # type: ignore[arg-type]

    def test_object_ids_must_already_be_canonical(self) -> None:
        for identifier in ("0x2a", "0x" + "AB" * 32, "ab" * 32, "0x" + "0" * 64, "0x" + "ab" * 33, "", None):
            with self.subTest(identifier=identifier), self.assertRaises(VisionLinkError):
                vision_url("execution", identifier, network="testnet")

    def test_digests_tools_and_skill_indexes_are_validated(self) -> None:
        self.assertIn(_base58(b"\x00" + bytes(range(1, 32))), vision_url("tx", _base58(b"\x00" + bytes(range(1, 32))), network="testnet"))
        for digest in ("1" * 32 + "2", "1" * 30 + "2", "0" * 32, "O" * 43, EXECUTION):
            with self.subTest(digest=digest), self.assertRaises(VisionLinkError):
                vision_url("tx", digest, network="testnet")
        self.assertTrue(vision_url("tool", EXECUTION, network="testnet").endswith("/tool/" + EXECUTION + "?network=testnet"))
        for fqn in ("xyz.add@1", "Xyz.taluslabs.add@1", "xyz.taluslabs.add", "xyz.taluslabs.add@v1"):
            with self.subTest(fqn=fqn), self.assertRaises(VisionLinkError):
                vision_url("tool", fqn, network="testnet")
        for index in (-1, True, "01", "a", None):
            with self.subTest(index=index), self.assertRaises(VisionLinkError):
                vision_url("skill", AGENT, index, network="testnet")
        with self.assertRaisesRegex(VisionLinkError, "identifier"):
            vision_url("skill", AGENT, network="testnet")
        with self.assertRaisesRegex(VisionLinkError, "unsupported"):
            vision_url("dag", EXECUTION, network="testnet")

    def test_parser_accepts_only_the_canonical_form(self) -> None:
        path = "/execution/" + EXECUTION
        for url in (
            BASE + path,
            BASE + path + "?network=devnet",
            BASE + path + "?network=testnet&ref=1",
            BASE + path + "?network=testnet#top",
            BASE + path + "/?network=testnet",
            BASE + ":443" + path + "?network=testnet",
            "http://" + "vision.talus.network" + path + "?network=testnet",
            "https://" + "www.vision.talus.network" + path + "?network=testnet",
            "https://" + "user@vision.talus.network" + path + "?network=testnet",
            BASE + "/dag/" + EXECUTION + "?network=testnet",
            BASE + "/?network=testnet",
            BASE + "/tool/" + TOOL_FQN + "?network=testnet",
            BASE + "/execution/0x2a?network=testnet",
        ):
            with self.subTest(url=url), self.assertRaises(VisionLinkError):
                parse_vision_url(url)

    def test_cli_prints_link_or_fails_closed(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(["--network", "testnet", "--kind", "skill", "--id", AGENT, "--skill-index", "2"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().strip(), BASE + "/skill/" + AGENT + "/2?network=testnet")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(["--network", "testnet", "--kind", "execution", "--id", "0x2a"])
        self.assertEqual(status, 2)
        self.assertIn("vision link failed", stdout.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--kind", "execution", "--id", EXECUTION])


if __name__ == "__main__":
    unittest.main()
