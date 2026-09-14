#!/usr/bin/env python3
"""Build and parse network-explicit Talus Vision navigation links.

A Talus Vision page is an indexed projection for human navigation. It is not
chain evidence: link only identifiers that exact read-only evidence already
returned, and keep the link beside, never inside, the evidence ledger.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
from typing import Sequence
from urllib.parse import quote, unquote, urlsplit


VISION_HOST = "vision.talus.network"
VISION_NETWORKS = ("testnet", "mainnet")
_OBJECT_ID_RE = re.compile(r"0x[0-9a-f]{64}\Z")
_ZERO_OBJECT_ID = "0x" + "0" * 64
_SKILL_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
# Mirrors the Vision explorer's Tool FQN grammar: domain.name@version.
_TOOL_FQN_RE = re.compile(r"[a-z][a-z0-9_-]+(?:\.[a-z][a-z0-9_-]+)+\.[a-z][a-z0-9_-]+@[0-9]+\Z")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_BASE58_ALPHABET)}
_DIGEST_BYTES = 32

# kind -> (route segment, identifier kinds accepted for that segment)
_ROUTES: dict[str, tuple[str, tuple[str, ...]]] = {
    "tx": ("tx", ("digest",)),
    "object": ("object", ("object",)),
    "workflow": ("workflow", ("object",)),
    "execution": ("execution", ("object",)),
    "payment": ("payment", ("object",)),
    "task": ("task", ("object",)),
    "tool": ("tool", ("tool",)),
    "agent": ("agent", ("object",)),
    "skill": ("skill", ("object", "skill_index")),
    "leader": ("leader", ("object",)),
    "profile": ("profile", ("object",)),
}
VISION_KINDS = tuple(_ROUTES)
_KIND_BY_SEGMENT = {segment: kind for kind, (segment, _parts) in _ROUTES.items()}


class VisionLinkError(ValueError):
    """Raised when an identifier cannot be linked to a Talus Vision page."""


@dataclass(frozen=True)
class VisionLink:
    kind: str
    network: str
    identifiers: tuple[str, ...]
    url: str


def _require_object_id(value: object) -> str:
    if not isinstance(value, str) or _OBJECT_ID_RE.fullmatch(value) is None or value == _ZERO_OBJECT_ID:
        raise VisionLinkError("identifier must be a canonical, non-zero 0x-prefixed 64-hex Sui ID")
    return value


def _require_digest(value: object) -> str:
    if not isinstance(value, str) or not value or any(character not in _BASE58_INDEX for character in value):
        raise VisionLinkError("transaction digest must be canonical Sui base58")
    number = 0
    for character in value:
        number = number * 58 + _BASE58_INDEX[character]
    leading_zeroes = len(value) - len(value.lstrip("1"))
    payload_length = (number.bit_length() + 7) // 8
    if leading_zeroes + payload_length != _DIGEST_BYTES:
        raise VisionLinkError("transaction digest must encode exactly 32 bytes")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    if "1" * leading_zeroes + encoded != value:
        raise VisionLinkError("transaction digest must be canonical Sui base58")
    return value


def _require_tool(value: object) -> str:
    if isinstance(value, str) and _TOOL_FQN_RE.fullmatch(value) is not None:
        return value
    try:
        return _require_object_id(value)
    except VisionLinkError:
        raise VisionLinkError("Tool identifier must be a domain.name@version FQN or a canonical Tool object ID") from None


def _require_skill_index(value: object) -> str:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(text, str) or _SKILL_INDEX_RE.fullmatch(text) is None:
        raise VisionLinkError("skill index must be a non-negative integer")
    return text


_VALIDATORS = {
    "digest": _require_digest,
    "object": _require_object_id,
    "tool": _require_tool,
    "skill_index": _require_skill_index,
}


def _require_network(network: object) -> str:
    if network not in VISION_NETWORKS:
        raise VisionLinkError("network must be exactly 'testnet' or 'mainnet'; Vision has no devnet or localnet")
    return network  # type: ignore[return-value]


def vision_url(kind: str, *identifiers: object, network: str) -> str:
    """Return a Talus Vision URL that always carries an explicit network query."""

    network = _require_network(network)
    if kind not in _ROUTES:
        raise VisionLinkError(f"unsupported Talus Vision link kind: {kind!r}")
    segment, parts = _ROUTES[kind]
    if len(identifiers) != len(parts):
        raise VisionLinkError(f"{kind} links require {len(parts)} identifier(s)")
    validated = [_VALIDATORS[part](value) for part, value in zip(parts, identifiers)]
    path = "/".join(quote(value, safe="") for value in (segment, *validated))
    return "https://" + VISION_HOST + "/" + path + "?network=" + network


def parse_vision_url(url: str) -> VisionLink:
    """Parse a Talus Vision URL, accepting only the canonical form vision_url builds."""

    if not isinstance(url, str) or any(character.isspace() for character in url):
        raise VisionLinkError("Talus Vision URL is malformed")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise VisionLinkError("Talus Vision URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc != VISION_HOST
        or port is not None
        or parsed.fragment
    ):
        raise VisionLinkError("Talus Vision URL must use the canonical HTTPS host without credentials, port, or fragment")
    query = parsed.query
    if not query.startswith("network="):
        raise VisionLinkError("Talus Vision URL must carry an explicit ?network= query")
    network = _require_network(query.removeprefix("network="))
    segments = parsed.path.split("/")[1:]
    if not segments or segments[0] not in _KIND_BY_SEGMENT:
        raise VisionLinkError("Talus Vision URL does not name a supported detail route")
    kind = _KIND_BY_SEGMENT[segments[0]]
    identifiers = tuple(unquote(segment) for segment in segments[1:])
    canonical = vision_url(kind, *identifiers, network=network)
    if canonical != url:
        raise VisionLinkError("Talus Vision URL is not in canonical form")
    return VisionLink(kind=kind, network=network, identifiers=identifiers, url=canonical)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", required=True, choices=VISION_NETWORKS)
    parser.add_argument("--kind", required=True, choices=VISION_KINDS)
    parser.add_argument("--id", required=True, dest="identifier", help="digest, object ID, or Tool FQN")
    parser.add_argument("--skill-index", help="skill index for --kind skill (with --id as the Agent ID)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    identifiers = [args.identifier]
    if args.skill_index is not None:
        identifiers.append(args.skill_index)
    try:
        print(vision_url(args.kind, *identifiers, network=args.network))
    except VisionLinkError as exc:
        print(f"vision link failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
