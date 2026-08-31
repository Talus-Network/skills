#!/usr/bin/env python3
"""Tests for the public Move Registry package map and identity boundary."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("mvr_registry_under_test", ROOT / "mvr_registry.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response:
    def __init__(self, url: str, *, body: str = "", status: int = 200) -> None:
        self.url = url
        self.body = body.encode("utf-8")
        self.status = status
        self.closed = False

    def geturl(self) -> str:
        return self.url

    def read(self, _limit: int = -1) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


class MissingStatusResponse:
    def __init__(self, url: str, *, body: str = "") -> None:
        self.url = url
        self.body = body.encode("utf-8")
        self.closed = False

    def geturl(self) -> str:
        return self.url

    def read(self, _limit: int = -1) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


class MvrRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MODULE.load_catalog()

    def test_catalog_contains_only_six_registered_packages_and_two_explicit_exclusions(self) -> None:
        self.assertEqual(
            [record["name"] for record in MODULE.records(self.catalog)],
            list(MODULE.DIRECT_PACKAGE_NAMES),
        )
        self.assertEqual(self.catalog["not_registered"], list(MODULE.NOT_REGISTERED_NAMES))
        for name in MODULE.NOT_REGISTERED_NAMES:
            with self.subTest(name=name):
                with self.assertRaisesRegex(MODULE.MvrRegistryError, "not registered"):
                    MODULE.record_for_name(self.catalog, name)

    def test_catalog_builds_canonical_page_and_api_urls(self) -> None:
        for record in MODULE.records(self.catalog):
            for network in MODULE.NETWORKS:
                observed = record["networks"][network]
                self.assertIn(f"{network}.mvr.mystenlabs.com/v1/names/", observed["api_url"])
                self.assertIn("%40talus%2Fnexus-", observed["api_url"])

    def _valid_payload(self, record: dict[str, object], network: str) -> dict[str, object]:
        evidence = record["networks"][network]
        return {
            "name": record["name"],
            "version": record["version"],
            "package_address": evidence["package_address"],
            "git_info": {
                "repository_url": self.catalog["source_repository"],
                "path": record["source_path"],
                "tag": evidence["source_tag"],
            },
        }

    def test_api_identity_and_source_metadata_are_bound_to_each_network(self) -> None:
        for original in MODULE.records(self.catalog):
            record = dict(original)
            for network in MODULE.NETWORKS:
                with self.subTest(name=record["name"], network=network):
                    payload = self._valid_payload(record, network)
                    self.assertEqual(MODULE.validate_api_payload(payload, record, network)["name"], record["name"])

    def test_catalog_and_api_versions_are_type_exact_integers(self) -> None:
        for invalid in (True, False, 1.0, "1"):
            mutation = copy.deepcopy(self.catalog)
            mutation["packages"][0]["version"] = invalid
            with self.subTest(source="catalog", value=invalid), self.assertRaises(MODULE.MvrRegistryError):
                MODULE.validate_catalog(mutation)
        record = dict(MODULE.records(self.catalog)[0])
        payload = self._valid_payload(record, "testnet")
        for invalid in (True, False, 1.0, "1"):
            with self.subTest(source="api", value=invalid), self.assertRaises(MODULE.MvrRegistryError):
                MODULE.validate_api_payload({**payload, "version": invalid}, record, "testnet")

    def test_wrong_or_missing_identity_and_generic_page_200_fail_closed(self) -> None:
        record = dict(MODULE.records(self.catalog)[0])
        payload = self._valid_payload(record, "testnet")
        mutations = {
            "missing": {},
            "wrong-name": {**payload, "name": "@talus/nexus-policy"},
            "wrong-version": {**payload, "version": 2},
            "wrong-address": {**payload, "package_address": "0x" + "1" * 64},
            "wrong-source": {**payload, "git_info": {**payload["git_info"], "repository_url": "https://" + "example.invalid/source"}},
        }
        for name, mutated in mutations.items():
            with self.subTest(mutation=name), self.assertRaises(MODULE.MvrRegistryError):
                MODULE.validate_live_record(record, "testnet", page_status=200, page_final_url=record["page_url"], api_status=200, api_payload=mutated)

    def test_page_redirect_and_non_success_fail_closed(self) -> None:
        record = dict(MODULE.records(self.catalog)[0])
        payload = self._valid_payload(record, "mainnet")
        with self.assertRaises(MODULE.MvrRegistryError):
            MODULE.validate_live_record(record, "mainnet", page_status=200, page_final_url=record["page_url"] + "/", api_status=200, api_payload=payload)
        with self.assertRaises(MODULE.MvrRegistryError):
            MODULE.validate_live_record(record, "mainnet", page_status=200, page_final_url=record["page_url"], api_status=200, api_payload=None)
        with self.assertRaises(MODULE.MvrRegistryError):
            MODULE.validate_live_record(record, "mainnet", page_status=404, page_final_url=record["page_url"], api_status=200, api_payload=payload)

    def test_live_page_and_api_status_must_be_exact_200(self) -> None:
        record = dict(MODULE.records(self.catalog)[0])
        payload = self._valid_payload(record, "testnet")
        for status in (None, True, False, 200.0, "200", 201, 206):
            with self.subTest(kind="page", status=status), self.assertRaises(MODULE.MvrRegistryError):
                MODULE.validate_live_record(record, "testnet", page_status=status, page_final_url=record["page_url"], api_status=200, api_payload=payload)
            with self.subTest(kind="api", status=status), self.assertRaises(MODULE.MvrRegistryError):
                MODULE.validate_live_record(
                    record,
                    "testnet",
                    page_status=200,
                    page_final_url=record["page_url"],
                    api_status=status,
                    api_payload=payload,
                )

    def test_live_fetchers_return_the_actual_200_status(self) -> None:
        record = dict(MODULE.records(self.catalog)[0])
        page = Response(record["page_url"], body="<html>public package</html>")
        page_status, page_final_url = MODULE.fetch_page(record["page_url"], opener=lambda *_args, **_kwargs: page)
        self.assertEqual((page_status, page_final_url), (200, record["page_url"]))
        payload = self._valid_payload(record, "testnet")
        api_url = record["networks"]["testnet"]["api_url"]
        api = Response(api_url, body=json.dumps(payload))
        api_status, api_final_url, observed = MODULE.fetch_json(api_url, opener=lambda *_args, **_kwargs: api)
        self.assertEqual((api_status, api_final_url), (200, api_url))
        self.assertEqual(observed, payload)
        self.assertTrue(page.closed)
        self.assertTrue(api.closed)

        for status in (True, False, 200.0, "200", 201, 206):
            with self.subTest(kind="page", status=status), self.assertRaises(MODULE.MvrRegistryError):
                MODULE.fetch_page(record["page_url"], opener=lambda *_args, status=status, **_kwargs: Response(record["page_url"], status=status))
            with self.subTest(kind="api", status=status), self.assertRaises(MODULE.MvrRegistryError):
                MODULE.fetch_json(api_url, opener=lambda *_args, status=status, **_kwargs: Response(api_url, body=json.dumps(payload), status=status))
        with self.assertRaisesRegex(MODULE.MvrRegistryError, "status is required"):
            MODULE.fetch_page(record["page_url"], opener=lambda *_args, **_kwargs: MissingStatusResponse(record["page_url"], body="public"))
        with self.assertRaisesRegex(MODULE.MvrRegistryError, "status is required"):
            MODULE.fetch_json(api_url, opener=lambda *_args, **_kwargs: MissingStatusResponse(api_url, body=json.dumps(payload)))

    def test_collect_live_records_the_fetcher_statuses(self) -> None:
        def fake_page(url: str, **_kwargs: object) -> tuple[int, str]:
            return 200, url

        def fake_json(url: str, **_kwargs: object) -> tuple[int, str, dict[str, object]]:
            for record in MODULE.records(self.catalog):
                for network in MODULE.NETWORKS:
                    if url == record["networks"][network]["api_url"]:
                        return 200, url, self._valid_payload(record, network)
            raise AssertionError(f"unexpected API URL: {url}")

        with (
            mock.patch.object(MODULE, "fetch_page", side_effect=fake_page),
            mock.patch.object(MODULE, "fetch_json", side_effect=fake_json),
        ):
            report = MODULE.collect_live(self.catalog)
        self.assertEqual(len(report["records"]), 12)
        self.assertTrue(all(record["page_status"] == 200 and record["api_status"] == 200 for record in report["records"]))

    def test_catalog_mutations_cannot_add_unregistered_or_unknown_package(self) -> None:
        mutation = copy.deepcopy(self.catalog)
        mutation["packages"][0]["name"] = "@talus/nexus-policy"
        with self.assertRaises(MODULE.MvrRegistryError):
            MODULE.validate_catalog(mutation)
        mutation = copy.deepcopy(self.catalog)
        mutation["packages"].append(copy.deepcopy(mutation["packages"][0]))
        with self.assertRaises(MODULE.MvrRegistryError):
            MODULE.validate_catalog(mutation)


if __name__ == "__main__":
    unittest.main()
