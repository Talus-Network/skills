#!/usr/bin/env python3
"""Read the published Nexus Setup page without using a repository checkout.

This module is deliberately small and read-only.  It is the live authority for
version-sensitive Skills guidance; stable examples remain bundled in the
Skills repository and deployed-state checks use ``testnet_evidence.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen


CANONICAL_SETUP_URL = "https://docs.talus.network/guides/getting-started/setup"
EXPECTED_SETUP_VERSION = "v2.0.0"
PUBLISHED_DOCS_HOST = "docs.talus.network"
MAX_SETUP_BYTES = 2 * 1024 * 1024
USER_AGENT = "talus-skills-public-docs/1"
MAX_SETUP_ATTEMPTS = 3
DEFAULT_SETUP_BACKOFF_SECONDS = 0.25


class PublishedDocsError(RuntimeError):
    """Raised when the published Setup authority is unavailable or invalid."""


@dataclass(frozen=True)
class SetupPage:
    """The validated, read-only result of one published Setup-page fetch."""

    requested_url: str
    final_url: str
    version: str
    content_bytes: int


def _canonical_setup_destination(value: object, *, allow_trailing_slash: bool = True) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(character.isspace() for character in value):
        raise PublishedDocsError("published Setup URL must be a canonical HTTPS URL")
    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise PublishedDocsError("published Setup URL is malformed") from exc
    expected_path = urlsplit(CANONICAL_SETUP_URL).path
    path = parsed.path
    valid_path = path == expected_path or (allow_trailing_slash and path == expected_path + "/")
    if (
        parsed.scheme.casefold() != "https"
        or hostname != PUBLISHED_DOCS_HOST
        or parsed.netloc.casefold() != PUBLISHED_DOCS_HOST
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not valid_path
    ):
        raise PublishedDocsError("published Setup URL must be the canonical HTTPS destination")
    return CANONICAL_SETUP_URL if path == expected_path else CANONICAL_SETUP_URL + "/"


def extract_setup_version(content: str) -> str:
    """Extract the release version stated by the Setup page.

    The page also contains versions for dependencies and manifests.  Prefer a
    version on the release/install sentence, then accept an explicit version
    label.  Requiring a semantic version and a release-oriented label prevents
    unrelated dependency versions from becoming the authority.
    """

    if not isinstance(content, str) or not content.strip():
        raise PublishedDocsError("published Setup page is empty")
    patterns = (
        re.compile(r"(?is)\b(?:installable\s+(?:cli/sdk\s+)?release|release\s+version|release)\b[^\n]{0,120}?\bv?(\d+\.\d+\.\d+)\b"),
        re.compile(r"(?im)^\s*\*{0,2}(?:version|release)\*{0,2}\s*[:=]\s*`?v?(\d+\.\d+\.\d+)\b"),
    )
    for pattern in patterns:
        match = pattern.search(content)
        if match is not None:
            return "v" + match.group(1)
    raise PublishedDocsError("published Setup page does not expose a parseable release version")


def _decode_response(response: object) -> str:
    headers = getattr(response, "headers", {})
    content_type = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
    charset_match = re.search(r"(?i)charset=([A-Za-z0-9._-]+)", str(content_type))
    encoding = charset_match.group(1) if charset_match else "utf-8"
    try:
        payload = response.read(MAX_SETUP_BYTES + 1)  # type: ignore[attr-defined]
    except (OSError, ValueError) as exc:
        raise PublishedDocsError(f"published Setup page could not be read: {exc}") from exc
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SETUP_BYTES:
        raise PublishedDocsError("published Setup page is empty or exceeds the size limit")
    try:
        return payload.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise PublishedDocsError("published Setup page is not valid text") from exc


def _response_header(response: object, name: str) -> str:
    headers = getattr(response, "headers", {})
    items = getattr(headers, "items", None)
    if callable(items):
        for key, value in items():
            if str(key).casefold() == name.casefold():
                return str(value)
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return str(value)
    return ""


def _is_cloudflare_challenge(response: object, status: object) -> bool:
    return status == 403 and _response_header(response, "Cf-Mitigated").strip().casefold() == "challenge"


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def fetch_setup_page(
    *,
    url: str = CANONICAL_SETUP_URL,
    expected_version: str = EXPECTED_SETUP_VERSION,
    opener: Callable[..., object] = urlopen,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_SETUP_ATTEMPTS,
    backoff_seconds: float = DEFAULT_SETUP_BACKOFF_SECONDS,
) -> SetupPage:
    """Fetch and validate the published Setup page with bounded challenge retries."""

    requested = _canonical_setup_destination(url)
    if not isinstance(expected_version, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", expected_version):
        raise PublishedDocsError("expected Setup version must be semantic-version shaped")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= MAX_SETUP_ATTEMPTS:
        raise PublishedDocsError(f"Setup challenge attempts must be an integer from 1 to {MAX_SETUP_ATTEMPTS}")
    if isinstance(backoff_seconds, bool) or not isinstance(backoff_seconds, (int, float)) or not 0 <= backoff_seconds <= 5:
        raise PublishedDocsError("Setup challenge backoff must be between 0 and 5 seconds")
    request = Request(requested, headers={"User-Agent": USER_AGENT, "Accept": "text/markdown,text/html;q=0.9,*/*;q=0.1"})
    final_url = ""
    content = ""
    for attempt in range(max_attempts):
        response = None
        try:
            response = opener(request, timeout=timeout)
        except HTTPError as exc:
            status = getattr(exc, "code", None)
            challenge = _is_cloudflare_challenge(exc, status)
            _close_response(exc)
            if not challenge:
                raise PublishedDocsError(f"published Setup page returned HTTP status {status}") from exc
            if attempt + 1 == max_attempts:
                raise PublishedDocsError(f"published Setup page remains blocked by a Cloudflare challenge after {max_attempts} attempts") from exc
            sleep(float(backoff_seconds) * (2**attempt))
            continue
        except (OSError, TimeoutError) as exc:
            raise PublishedDocsError(f"published Setup page is unavailable: {exc}") from exc
        try:
            status = getattr(response, "status", None)
            challenge = _is_cloudflare_challenge(response, status)
            if status is not None and (not isinstance(status, int) or not 200 <= status < 300) and not challenge:
                raise PublishedDocsError(f"published Setup page returned HTTP status {status}")
            if challenge:
                final_url = ""
                content = ""
            else:
                final_value = response.geturl() if callable(getattr(response, "geturl", None)) else getattr(response, "geturl", None)
                final_url = _canonical_setup_destination(final_value)
                content = _decode_response(response)
        finally:
            _close_response(response)
        if not challenge:
            break
        if attempt + 1 == max_attempts:
            raise PublishedDocsError(f"published Setup page remains blocked by a Cloudflare challenge after {max_attempts} attempts")
        sleep(float(backoff_seconds) * (2**attempt))
    else:
        raise PublishedDocsError("published Setup page availability check did not complete")
    version = extract_setup_version(content)
    if version != expected_version:
        raise PublishedDocsError(f"published Setup version mismatch: expected {expected_version}, found {version}")
    return SetupPage(requested_url=requested, final_url=final_url, version=version, content_bytes=len(content.encode("utf-8")))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=CANONICAL_SETUP_URL)
    parser.add_argument("--expected-version", default=EXPECTED_SETUP_VERSION)
    args = parser.parse_args(argv)
    try:
        page = fetch_setup_page(url=args.url, expected_version=args.expected_version)
    except PublishedDocsError as exc:
        print(f"published Setup check failed: {exc}")
        return 1
    print(json.dumps({"requested_url": page.requested_url, "final_url": page.final_url, "version": page.version, "content_bytes": page.content_bytes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
