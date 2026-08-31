#!/usr/bin/env python3
"""Validate and collect anonymous public Move Registry package evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


CATALOG_PATH = Path(__file__).with_name("mvr_packages.json")
PUBLIC_SOURCE_REPOSITORY = "https://github.com/Talus-Network/nexus-move-packages"
NETWORKS = ("testnet", "mainnet")
DIRECT_PACKAGE_NAMES = (
    "@talus/nexus-interface",
    "@talus/nexus-primitives",
    "@talus/nexus-registry",
    "@talus/nexus-tool",
    "@talus/nexus-scheduler",
    "@talus/nexus-workflow",
)
NOT_REGISTERED_NAMES = ("@talus/nexus-policy", "@talus/nexus-kernel")
HEX_ADDRESS_RE = re.compile(r"0x[0-9a-f]{64}\Z")
NAME_RE = re.compile(r"@talus/nexus-(?:interface|primitives|registry|tool|scheduler|workflow)\Z")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class MvrRegistryError(ValueError):
    """Raised when MVR package identity or public metadata is not proven."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MvrRegistryError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MvrRegistryError(f"{label} must be non-empty text")
    return value


def _is_exact_version_one(value: object) -> bool:
    return type(value) is int and value == 1


def _response_status(response: object, label: str) -> int:
    value = getattr(response, "status", None)
    if value is None:
        raise MvrRegistryError(f"{label} response status is required")
    if type(value) is not int:
        raise MvrRegistryError(f"{label} response status must be an integer: {value!r}")
    return value


def _catalog_records(catalog: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    records = catalog.get("packages")
    if not isinstance(records, list):
        raise MvrRegistryError("catalog packages must be a list")
    return tuple(_mapping(record, "package record") for record in records)


def validate_catalog(catalog: object) -> Mapping[str, Any]:
    document = _mapping(catalog, "MVR catalog")
    if document.get("schema_version") != 1:
        raise MvrRegistryError("MVR catalog schema_version must be 1")
    registry = _mapping(document.get("registry"), "registry")
    page_base = _text(registry.get("page_base_url"), "registry.page_base_url").rstrip("/")
    if page_base != "https://www.moveregistry.com/package":
        raise MvrRegistryError("registry page base is not the canonical public MVR page host")
    api_bases = _mapping(registry.get("api_base_urls"), "registry.api_base_urls")
    if set(api_bases) != set(NETWORKS):
        raise MvrRegistryError("registry API bases must cover exactly Testnet and Mainnet")
    for network in NETWORKS:
        expected = "https://" + network + ".mvr.mystenlabs.com/v1/names"
        if api_bases.get(network) != expected:
            raise MvrRegistryError(f"registry API base is not canonical for {network}")
    if _text(document.get("source_repository"), "source_repository") != PUBLIC_SOURCE_REPOSITORY:
        raise MvrRegistryError("MVR source repository is not the public Move Packages repository")
    records = _catalog_records(document)
    if tuple(record.get("name") for record in records) != DIRECT_PACKAGE_NAMES:
        raise MvrRegistryError("catalog must contain the six direct public Nexus packages in canonical order")
    for record in records:
        name = _text(record.get("name"), "package.name")
        if NAME_RE.fullmatch(name) is None:
            raise MvrRegistryError(f"unapproved MVR package name: {name}")
        dependency_key = _text(record.get("dependency_key"), f"{name}.dependency_key")
        expected_key = name.removeprefix("@talus/").replace("-", "_")
        if dependency_key != expected_key:
            raise MvrRegistryError(f"{name} dependency key is not {expected_key}")
        source_path = _text(record.get("source_path"), f"{name}.source_path")
        if source_path != f"packages/{name.removeprefix('@talus/nexus-')}":
            raise MvrRegistryError(f"{name} source path is not its public package path")
        if record.get("page_url") != f"{page_base}/{name}":
            raise MvrRegistryError(f"{name} page URL is not canonical")
        if not _is_exact_version_one(record.get("version")):
            raise MvrRegistryError(f"{name} must identify registered version 1")
        networks = _mapping(record.get("networks"), f"{name}.networks")
        if set(networks) != set(NETWORKS):
            raise MvrRegistryError(f"{name} must have Testnet and Mainnet evidence")
        for network in NETWORKS:
            observed = _mapping(networks.get(network), f"{name}.{network}")
            expected_api = api_bases[network] + "/" + quote(name, safe="")
            if observed.get("api_url") != expected_api:
                raise MvrRegistryError(f"{name} API URL is not canonical for {network}")
            address = _text(observed.get("package_address"), f"{name}.{network}.package_address")
            if HEX_ADDRESS_RE.fullmatch(address) is None:
                raise MvrRegistryError(f"{name} has an invalid {network} package address")
            if observed.get("source_tag") != f"{network}/v1":
                raise MvrRegistryError(f"{name} source tag is not {network}/v1")
    not_registered = document.get("not_registered")
    if tuple(not_registered) != NOT_REGISTERED_NAMES:
        raise MvrRegistryError("catalog must explicitly record the unregistered policy and kernel names")
    if set(DIRECT_PACKAGE_NAMES) & set(NOT_REGISTERED_NAMES):
        raise MvrRegistryError("an unregistered name cannot also be a verified package")
    return document


def load_catalog(path: str | Path = CATALOG_PATH) -> Mapping[str, Any]:
    catalog_path = Path(path)
    try:
        with catalog_path.open(encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise MvrRegistryError(f"cannot read MVR catalog {catalog_path}: {exc}") from exc
    return validate_catalog(document)


def records(catalog: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    validate_catalog(catalog)
    return _catalog_records(catalog)


def record_for_name(catalog: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    for record in records(catalog):
        if record["name"] == name:
            return record
    if name in NOT_REGISTERED_NAMES:
        raise MvrRegistryError(f"MVR package is not registered: {name}")
    raise MvrRegistryError(f"MVR package is not in the reviewed catalog: {name}")


def validate_api_payload(payload: object, record: Mapping[str, Any], network: str) -> Mapping[str, Any]:
    if network not in NETWORKS:
        raise MvrRegistryError(f"unsupported MVR network: {network}")
    name = _text(record.get("name"), "expected package name")
    observed = _mapping(payload, f"MVR API response for {name} on {network}")
    if observed.get("name") != name:
        raise MvrRegistryError(f"MVR API identity mismatch for {name} on {network}")
    if (
        not _is_exact_version_one(record.get("version"))
        or not _is_exact_version_one(observed.get("version"))
        or observed.get("version") != record.get("version")
    ):
        raise MvrRegistryError(f"MVR API version mismatch for {name} on {network}")
    expected_network = _mapping(_mapping(record.get("networks"), f"{name}.networks").get(network), f"{name}.{network}")
    if observed.get("package_address") != expected_network.get("package_address"):
        raise MvrRegistryError(f"MVR API package address mismatch for {name} on {network}")
    git_info = _mapping(observed.get("git_info"), f"{name} git_info")
    if git_info.get("repository_url") != PUBLIC_SOURCE_REPOSITORY:
        raise MvrRegistryError(f"MVR API source repository mismatch for {name} on {network}")
    if git_info.get("path") != record.get("source_path"):
        raise MvrRegistryError(f"MVR API source path mismatch for {name} on {network}")
    if git_info.get("tag") != expected_network.get("source_tag"):
        raise MvrRegistryError(f"MVR API source tag mismatch for {name} on {network}")
    return observed


def validate_live_record(
    record: Mapping[str, Any],
    network: str,
    *,
    page_status: int,
    page_final_url: str,
    api_status: int,
    api_payload: object,
) -> Mapping[str, Any]:
    if type(page_status) is not int or page_status != 200:
        raise MvrRegistryError(f"MVR page must return HTTP 200 for {record.get('name')}: HTTP {page_status}")
    if page_final_url != record.get("page_url"):
        raise MvrRegistryError(f"MVR page redirected away from the canonical package page: {page_final_url}")
    if type(api_status) is not int or api_status != 200:
        raise MvrRegistryError(f"MVR API must return HTTP 200 for {record.get('name')}: HTTP {api_status}")
    # The public page is an application shell and may return generic HTML. The
    # API identity is therefore mandatory; a page HTTP 200 never proves a name.
    return validate_api_payload(api_payload, record, network)


def _read_response(response: object) -> bytes:
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise MvrRegistryError("MVR response is not readable")
    body = reader(MAX_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes):
        body = str(body).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        raise MvrRegistryError("MVR response exceeds the size limit")
    return body


def fetch_json(url: str, *, opener: Callable[..., object] = urlopen, timeout: float = 20.0) -> tuple[int, str, Mapping[str, Any]]:
    request = Request(url, headers={"User-Agent": "talus-skills-mvr/1", "Accept": "application/json"})
    response: object | None = None
    try:
        response = opener(request, timeout=timeout)
        status = _response_status(response, "MVR API")
        final_url = response.geturl() if callable(getattr(response, "geturl", None)) else str(getattr(response, "geturl", url))
        if status != 200:
            raise MvrRegistryError(f"MVR API returned HTTP {status}: {url}")
        try:
            payload = json.loads(_read_response(response).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MvrRegistryError(f"MVR API response is not valid JSON: {url}") from exc
        return status, final_url, _mapping(payload, "MVR API response")
    except HTTPError as exc:
        close = getattr(exc, "close", None)
        if callable(close):
            close()
        raise MvrRegistryError(f"MVR API returned HTTP {exc.code}: {url}") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def fetch_page(url: str, *, opener: Callable[..., object] = urlopen, timeout: float = 20.0) -> tuple[int, str]:
    request = Request(url, headers={"User-Agent": "talus-skills-mvr/1", "Accept": "text/html"})
    response: object | None = None
    try:
        response = opener(request, timeout=timeout)
        status = _response_status(response, "MVR page")
        final_url = response.geturl() if callable(getattr(response, "geturl", None)) else str(getattr(response, "geturl", url))
        if status != 200:
            raise MvrRegistryError(f"MVR page returned HTTP {status}: {url}")
        _read_response(response)
        return status, final_url
    except HTTPError as exc:
        close = getattr(exc, "close", None)
        if callable(close):
            close()
        raise MvrRegistryError(f"MVR page returned HTTP {exc.code}: {url}") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def collect_live(catalog: Mapping[str, Any], *, opener: Callable[..., object] = urlopen, timeout: float = 20.0) -> dict[str, Any]:
    validate_catalog(catalog)
    collected: list[dict[str, Any]] = []
    for network in NETWORKS:
        for record in records(catalog):
            page_status, page_final_url = fetch_page(record["page_url"], opener=opener, timeout=timeout)
            api_record = _mapping(record["networks"], f"{record['name']}.networks")[network]
            api_status, api_final_url, payload = fetch_json(api_record["api_url"], opener=opener, timeout=timeout)
            if api_final_url != api_record["api_url"]:
                raise MvrRegistryError(f"MVR API redirected away from the canonical endpoint: {api_final_url}")
            validated = validate_live_record(
                record,
                network,
                page_status=page_status,
                page_final_url=page_final_url,
                api_status=api_status,
                api_payload=payload,
            )
            collected.append(
                {
                    "name": record["name"],
                    "network": network,
                    "page_url": record["page_url"],
                    "page_status": page_status,
                    "api_url": api_record["api_url"],
                    "api_status": api_status,
                    "api_identity": {
                        "name": validated["name"],
                        "version": validated["version"],
                        "package_address": validated["package_address"],
                        "source_repository": validated["git_info"]["repository_url"],
                        "source_path": validated["git_info"]["path"],
                        "source_tag": validated["git_info"]["tag"],
                    },
                }
            )
    return {
        "schema_version": 1,
        "status": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": collected,
        "not_registered": list(catalog["not_registered"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--live", action="store_true", help="collect live page/API evidence for both networks")
    args = parser.parse_args(argv)
    catalog = load_catalog(args.catalog)
    if args.live:
        result = collect_live(catalog)
    else:
        result = {"schema_version": 1, "status": "catalog-valid", "packages": [record["name"] for record in records(catalog)], "not_registered": list(catalog["not_registered"])}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MvrRegistryError as error:
        print(f"MVR registry validation failed: {error}")
        raise SystemExit(1)
