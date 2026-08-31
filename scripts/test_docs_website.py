#!/usr/bin/env python3
"""Regression tests for the published Setup-page authority."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_module():
    path = ROOT / "scripts/docs_website.py"
    spec = importlib.util.spec_from_file_location("skills_docs_website", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, body: bytes, final_url: str, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.final_url = final_url
        self.status = status
        self.headers = headers or {"Content-Type": "text/markdown; charset=utf-8"}
        self.closed = False

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]

    def geturl(self) -> str:
        return self.final_url

    def close(self) -> None:
        self.closed = True


class DocsWebsiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.body = b"# Developer Setup\n\nThe installable CLI/SDK release is `v2.0.0`.\n"

    def test_extracts_release_version_without_using_dependency_versions(self) -> None:
        self.assertEqual(self.module.extract_setup_version(self.body.decode()), "v2.0.0")
        with self.assertRaisesRegex(self.module.PublishedDocsError, "parseable"):
            self.module.extract_setup_version("# Setup\nSui 1.78.0\nTGE v1.1.2\n")

    def test_fetch_accepts_canonical_destination_and_closes_response(self) -> None:
        response = Response(self.body, self.module.CANONICAL_SETUP_URL + "/")
        calls: list[tuple[str, float]] = []

        def opener(request, *, timeout):
            calls.append((request.full_url, timeout))
            return response

        page = self.module.fetch_setup_page(opener=opener)
        self.assertEqual(page.requested_url, self.module.CANONICAL_SETUP_URL)
        self.assertEqual(page.final_url, self.module.CANONICAL_SETUP_URL + "/")
        self.assertEqual(page.version, "v2.0.0")
        self.assertEqual(calls, [(self.module.CANONICAL_SETUP_URL, 30.0)])
        self.assertTrue(response.closed)

    def test_retries_only_explicit_cloudflare_challenges_with_bounded_backoff(self) -> None:
        challenge_headers = {"Cf-Mitigated": "challenge", "Server": "cloudflare"}
        all_responses = [
            Response(b"", self.module.CANONICAL_SETUP_URL, status=403, headers=challenge_headers),
            Response(b"", self.module.CANONICAL_SETUP_URL, status=403, headers=challenge_headers),
            Response(self.body, self.module.CANONICAL_SETUP_URL),
        ]
        responses = list(all_responses)
        sleeps: list[float] = []

        def opener(_request, *, timeout):
            self.assertEqual(timeout, 30.0)
            return responses.pop(0)

        page = self.module.fetch_setup_page(opener=opener, sleep=sleeps.append)
        self.assertEqual(page.version, "v2.0.0")
        self.assertEqual(sleeps, [0.25, 0.5])
        self.assertTrue(all(response.closed for response in all_responses))

    def test_exhausted_cloudflare_challenge_fails_without_fallback(self) -> None:
        responses = [
            Response(b"", self.module.CANONICAL_SETUP_URL, status=403, headers={"Cf-Mitigated": "challenge"})
            for _ in range(self.module.MAX_SETUP_ATTEMPTS)
        ]
        sleeps: list[float] = []

        def opener(_request, *, timeout):
            return responses.pop(0)

        with self.assertRaisesRegex(self.module.PublishedDocsError, "Cloudflare challenge after 3 attempts"):
            self.module.fetch_setup_page(opener=opener, sleep=sleeps.append)
        self.assertEqual(sleeps, [0.25, 0.5])
        self.assertEqual(responses, [])

    def test_non_challenge_http_403_is_not_retried(self) -> None:
        response = Response(b"forbidden", self.module.CANONICAL_SETUP_URL, status=403, headers={"Server": "cloudflare"})
        sleeps: list[float] = []
        with self.assertRaisesRegex(self.module.PublishedDocsError, "HTTP status 403"):
            self.module.fetch_setup_page(opener=lambda *_args, **_kwargs: response, sleep=sleeps.append)
        self.assertEqual(sleeps, [])
        self.assertTrue(response.closed)

    def test_rejects_noncanonical_destinations_before_opener(self) -> None:
        calls = 0

        def opener(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("opener should not be called")

        for value in (
            "https://" + "github.com/ExampleOrg/internal-tools",
            "https://" + "docs.talus.network/other-page",
            "https://" + "docs.talus.network" + ":" + "443/guides/getting-started/setup",
            "https://" + "user:secret@docs.talus.network/guides/getting-started/setup",
            "http://" + "docs.talus.network/guides/getting-started/setup",
            "https://" + "docs.talus.network/guides/getting-started/setup?ref=main",
        ):
            with self.subTest(value=value):
                with self.assertRaises(self.module.PublishedDocsError):
                    self.module.fetch_setup_page(url=value, opener=opener)
        self.assertEqual(calls, 0)

    def test_rejects_unsafe_redirect_and_version_mismatch(self) -> None:
        for final_url in (
            "https://" + "evil.invalid/setup",
            "https://" + "github.com/ExampleOrg/internal-docs/blob/main/guides/getting-started/setup.md",
        ):
            response = Response(self.body, final_url)
            with self.subTest(final_url=final_url), self.assertRaisesRegex(self.module.PublishedDocsError, "canonical|destination"):
                self.module.fetch_setup_page(opener=lambda *_args, **_kwargs: response)
            self.assertTrue(response.closed)

        stale = Response(b"# Developer Setup\nInstallable release: `v1.0.0`.\n", self.module.CANONICAL_SETUP_URL)
        with self.assertRaisesRegex(self.module.PublishedDocsError, "version mismatch"):
            self.module.fetch_setup_page(opener=lambda *_args, **_kwargs: stale)
        self.assertTrue(stale.closed)

    def test_rejects_empty_or_oversized_page_and_network_failure(self) -> None:
        empty = Response(b"", self.module.CANONICAL_SETUP_URL)
        with self.assertRaisesRegex(self.module.PublishedDocsError, "empty"):
            self.module.fetch_setup_page(opener=lambda *_args, **_kwargs: empty)
        self.assertTrue(empty.closed)

        oversized = Response(b"x" * (self.module.MAX_SETUP_BYTES + 1), self.module.CANONICAL_SETUP_URL)
        with self.assertRaisesRegex(self.module.PublishedDocsError, "size limit"):
            self.module.fetch_setup_page(opener=lambda *_args, **_kwargs: oversized)
        self.assertTrue(oversized.closed)

        with self.assertRaisesRegex(self.module.PublishedDocsError, "unavailable"):
            self.module.fetch_setup_page(opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))

        failed = Response(self.body, self.module.CANONICAL_SETUP_URL)
        failed.status = 503
        with self.assertRaisesRegex(self.module.PublishedDocsError, "HTTP status 503"):
            self.module.fetch_setup_page(opener=lambda *_args, **_kwargs: failed)
        self.assertTrue(failed.closed)

    def test_cli_and_module_are_read_only(self) -> None:
        source = (ROOT / "scripts/docs_website.py").read_text(encoding="utf-8")
        self.assertNotIn("prepare_sources", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("open(", source)
        self.assertIn("urlopen", source)
        self.assertIn("Cf-Mitigated", source)
        self.assertNotIn("wallet", source.casefold())


if __name__ == "__main__":
    unittest.main()
