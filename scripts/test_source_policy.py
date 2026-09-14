#!/usr/bin/env python3
"""Regression tests for the standalone public-source policy."""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _token(*parts: str) -> str:
    return "".join(parts)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PublicSourcePolicyTests(unittest.TestCase):
    def test_default_catalog_contains_only_public_sources(self) -> None:
        helper = _load("source_policy_helper", ROOT / "scripts/prepare_sources.py")
        self.assertEqual(set(helper.DEFAULT_REPOSITORIES), {"nexus-sdk", "nexus-move-packages", "sui"})
        self.assertEqual(helper.DEFAULT_REPOSITORIES["sui"].repository, "MystenLabs/sui")
        self.assertEqual(helper.DEFAULT_REPOSITORIES["sui"].ref, "d8459684b41eb09ab23fe16a9dd84173270bbaba")
        self.assertEqual(helper.SUI_TOOLCHAIN_VERSION, "sui 1.78.0-d8459684b41e")
        self.assertEqual(
            helper.DEFAULT_REPOSITORIES["sui"].archive_sha256,
            "1b974c1b10413e873b98df2740a877a930a08b40c80af58b52f737385f8bdf44",
        )
        self.assertEqual(
            helper.DEFAULT_REPOSITORIES["sui"].required_paths,
            helper.SUI_FRAMEWORK_REQUIRED_PATHS,
        )
        self.assertTrue(all(spec.archive_url.startswith("https://" + "codeload.github.com/") for spec in helper.DEFAULT_REPOSITORIES.values()))

    def test_production_catalog_rejects_unpinned_public_refs_and_checksums(self) -> None:
        helper = _load("source_policy_pinned", ROOT / "scripts/prepare_sources.py")
        with self.assertRaisesRegex(helper.SourcePreparationError, "pinned"):
            helper._normalise_specs(None, {"sui": "main"}, {}, ["sui"])
        with self.assertRaisesRegex(helper.SourcePreparationError, "pinned reviewed checksum"):
            helper._normalise_specs(
                None,
                {},
                {"sui": "0" * 64},
                ["sui"],
            )

    def test_production_entrypoints_reject_caller_catalog_replacement(self) -> None:
        helper = _load("source_policy_production", ROOT / "scripts/prepare_sources.py")
        for entrypoint in (helper.prepare_sources, helper.prepared_sources):
            parameters = inspect.signature(entrypoint).parameters
            self.assertEqual(set(parameters), {"overrides", "checksums", "only"})
            self.assertNotIn("specs", parameters)
            self.assertNotIn("downloader", parameters)
            self.assertNotIn("workspace_parent", parameters)
        with self.assertRaises(TypeError):
            helper.prepare_sources(specs={"evil": object()})
        with self.assertRaises(TypeError):
            with helper.prepared_sources(specs={"evil": object()}):
                pass
        with self.assertRaises(TypeError):
            helper.prepare_sources(downloader=lambda *_args: None)
        with self.assertRaises(TypeError):
            with helper.prepared_sources(workspace_parent=ROOT):
                pass

    def test_maintained_text_uses_only_public_source_authorities(self) -> None:
        validator = _load("source_policy_public_authorities", ROOT / "scripts/validate_skills.py")
        self.assertEqual(
            validator.PUBLIC_GITHUB_REPOSITORIES,
            {
                "Talus-Network/nexus-sdk",
                "Talus-Network/nexus-move-packages",
                "MystenLabs/sui",
            },
        )
        for path in ROOT.rglob("*"):
            if (
                not path.is_file()
                or ".git" in path.parts
                or "__pycache__" in path.parts
                or path.suffix == ".pyc"
                or "scripts" in path.relative_to(ROOT).parts
            ):
                continue
            text = path.read_text(encoding="utf-8")
            for url in validator.URL_RE.findall(text):
                clean_url = url.rstrip(".,;`")
                if clean_url.endswith("://"):
                    continue
                allowed, reason = validator._classify_public_url(
                    clean_url, allow_published_documentation=True
                )
                with self.subTest(path=path, url=clean_url):
                    self.assertTrue(allowed, reason)

    def test_public_documentation_urls_are_contextual_not_source_authority(self) -> None:
        validator = _load("source_policy_documentation_context", ROOT / "scripts/validate_skills.py")
        documentation_urls = tuple(
            "https://" + "docs.talus.network" + path
            for path in (
                "/guides/getting-started/prepare-onchain-development",
                "/guides/nexus-api",
                "/guides/nexus-api/connect-a-dapp",
                "/guides/nexus-api/typescript-client",
                "/guides/nexus-api/troubleshooting",
                "/guides/nexus-api/tutorial",
                "/guides/nexus-api/tutorial/01-get-a-key",
                "/guides/nexus-api/tutorial/02-read-the-network",
                "/guides/nexus-api/tutorial/03-stream-events",
                "/guides/nexus-api/tutorial/04-assemble-the-dapp",
                "/guides/nexus-api/tutorial/05-port-to-react",
                "/guides/agent-usage/execute-and-settle-agent",
                "/guides/tap-development/build-tap-move-package",
                "/guides/tokenomics/fund-agent-and-user-executions",
                "/guides/tool-development/build-offchain-tool",
                "/guides/tool-development/build-onchain-tool",
                "/guides/tool-development/tool-communication",
                "/guides/tool-development/verify-offchain-tool-result",
                "/reference/toolkit/rust",
                "/reference/cli/tool",
            )
        )
        api_documentation_urls = (
            "https://" + "api.taluslabs.dev/",
            "https://" + "api.taluslabs.dev/docs",
            "https://" + "api.taluslabs.dev/openapi.json",
        )
        for url in (*documentation_urls, *api_documentation_urls):
            with self.subTest(url=url):
                self.assertFalse(validator._classify_public_url(url)[0])
                self.assertTrue(
                    validator._classify_public_url(url, allow_published_documentation=True)[0]
                )

        documentation_content = "\n".join((*documentation_urls, *api_documentation_urls))
        with tempfile.TemporaryDirectory(prefix="source-policy-reviewed-documentation-") as directory:
            root = Path(directory)
            path = root / "references/page.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(documentation_content, encoding="utf-8")
            errors: list[str] = []
            validator.validate_portability(root, errors)
            self.assertEqual(errors, [])

        operational_content = "\n".join(api_documentation_urls)
        with tempfile.TemporaryDirectory(prefix="source-policy-api-operational-") as directory:
            root = Path(directory)
            path = root / "scripts/prepare_sources.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(operational_content, encoding="utf-8")
            errors = []
            validator.validate_portability(root, errors)
            self.assertTrue(errors)

        for variant in (
            "https://" + "docs.talus.network/guides/nexus-api/unknown",
            "https://" + "api.taluslabs.dev/unknown",
            "https://" + "api.taluslabs.dev/docs?version=1",
            "https://" + "api.taluslabs.dev/docs#reference",
            "https://" + "user:secret@api.taluslabs.dev/docs",
            "https://" + "www.api.taluslabs.dev/docs",
            "https://" + "api.taluslabs.dev" + ":443/docs",
        ):
            with self.subTest(variant=variant):
                self.assertFalse(
                    validator._classify_public_url(
                        variant, allow_published_documentation=True
                    )[0]
                )

        for relative in ("skill/SKILL.md", "references/page.md", "evals/evals.json"):
            with tempfile.TemporaryDirectory(prefix="source-policy-documentation-approved-") as directory:
                root = Path(directory)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(documentation_urls[0], encoding="utf-8")
                errors: list[str] = []
                validator.validate_portability(root, errors)
                with self.subTest(kind="approved", relative=relative):
                    self.assertEqual(errors, [])

        rejected_locations = (
            "scripts/prepare_sources.py",
            "scripts/forward_portability.py",
            "scripts/testnet_evidence.py",
            "source.txt",
        )
        for relative in rejected_locations:
            with tempfile.TemporaryDirectory(prefix="source-policy-documentation-rejected-") as directory:
                root = Path(directory)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(documentation_urls[0], encoding="utf-8")
                errors = []
                validator.validate_portability(root, errors)
                with self.subTest(kind="rejected", relative=relative):
                    self.assertTrue(errors)

        wrong_case = "https://" + "docs.talus.network/reference/cli/unknown"
        with tempfile.TemporaryDirectory(prefix="source-policy-documentation-case-") as directory:
            root = Path(directory)
            (root / "references/page.md").parent.mkdir(parents=True, exist_ok=True)
            (root / "references/page.md").write_text(wrong_case, encoding="utf-8")
            errors = []
            validator.validate_portability(root, errors)
            self.assertTrue(errors)

        unapproved_repository = _token("ExampleOrg/", "internal-docs")
        codeload = "https://" + "codeload.github.com/" + unapproved_repository + "/tar.gz/main"
        self.assertFalse(validator._classify_public_url(codeload, allow_published_documentation=True)[0])
        unapproved_url = "https://" + "github.com/" + unapproved_repository + "/blob/main/reference/cli/tap.md"
        self.assertFalse(validator._classify_public_url(unapproved_url, allow_published_documentation=True)[0])

    def test_semantic_documentation_authorities_fail_closed(self) -> None:
        validator = _load("source_policy_validator", ROOT / "scripts/validate_skills.py")
        variants = (
            _token("documentation", " repository"),
            _token("documentation", " archive"),
            _token("helper-resolved ", "documentation", " repository"),
            _token("public SDK/", "Docs/Move Packages sources"),
            _token("documentation", " repository authority"),
            _token("Docs", " repository authority"),
        )
        for variant in variants:
            with tempfile.TemporaryDirectory(prefix="source-policy-semantic-") as directory:
                root = Path(directory)
                (root / "sample.md").write_text(variant, encoding="utf-8")
                errors: list[str] = []
                validator.validate_portability(root, errors)
                with self.subTest(variant=variant):
                    self.assertTrue(errors, variant)

    def test_external_url_policy_accepts_only_reviewed_destinations(self) -> None:
        validator = _load("source_policy_url_validator", ROOT / "scripts/validate_skills.py")
        approved = (
            "https://" + "github.com/Talus-Network/nexus-sdk/tree/main/cli",
            "https://" + "github.com/Talus-Network/nexus-move-packages/tree/main/packages",
            "https://" + "codeload.github.com/Talus-Network/nexus-sdk/tar.gz/" + "1f67d5b02b7caa5411e449eb171eda5c178f83fe",
            "https://" + "github.com/MystenLabs/sui/tree/d8459684b41eb09ab23fe16a9dd84173270bbaba/crates/sui-framework/packages",
            "https://" + "codeload.github.com/MystenLabs/sui/tar.gz/d8459684b41eb09ab23fe16a9dd84173270bbaba",
            "https://graphql.testnet.sui.io/graphql",
            "https://docs.sui.io/references/sui-api",
            "http://" + "localhost:8080",
            "HTTPS://GITHUB.COM/Talus-Network/nexus-sdk/tree/main/cli",
        )
        for index, url in enumerate(approved):
            with tempfile.TemporaryDirectory(prefix="source-policy-approved-url-") as directory:
                root = Path(directory)
                (root / "sample.md").write_text(url, encoding="utf-8")
                errors: list[str] = []
                validator.validate_portability(root, errors)
                with self.subTest(kind="approved", index=index):
                    self.assertEqual(errors, [])

        mvr_approved = tuple(
            "https://" + "www.moveregistry.com/package/" + name
            for name in validator.PUBLIC_MVR_PACKAGE_NAMES
        ) + tuple(
            "https://" + network + ".mvr.mystenlabs.com/v1/names/%40talus%2Fnexus-" + suffix
            for network in ("testnet", "mainnet")
            for suffix in ("interface", "primitives", "registry", "tool", "scheduler", "workflow")
        )
        for url in mvr_approved:
            with tempfile.TemporaryDirectory(prefix="source-policy-mvr-approved-") as directory:
                root = Path(directory)
                (root / "sample.md").write_text(url, encoding="utf-8")
                errors: list[str] = []
                validator.validate_portability(root, errors)
                self.assertEqual(errors, [], url)

        rejected = (
            "https://" + "evil.invalid/source",
            "HT" + "TPS" + "://github.com/ExampleOrg/internal-tools/tree/main",
            "https://" + "github.com/Evil/repo",
            "https://" + "codeload.github.com/Evil/repo/tar.gz/main",
            "https://" + "codeload.github.com/Talus-Network/nexus-sdk/tar.gz/main",
            "https://" + "codeload.github.com/MystenLabs/sui/tar.gz/main",
            "https://github.com/Talus-Network/nexus-sdk" + "?ref=main",
            "https://github.com/Talus-Network/nexus-sdk" + "#readme",
            "https://" + "www.moveregistry.com/package/" + "@talus/" + "nexus-policy",
            "https://" + "www.moveregistry.com/package/" + "@talus/" + "nexus-kernel",
            "https://" + "www.moveregistry.com/package/" + "@talus/" + "nexus-interface?network=testnet",
            "https://testnet.mvr.mystenlabs.com/v1/names/" + "%40talus%2Fnexus-policy",
            "https://testnet.mvr.mystenlabs.com/v1/names/" + "%40talus%2Fnexus-interface?version=1",
            "https://mainnet.mvr.mystenlabs.com/v1/names/" + "@talus/nexus-interface",
            "https://" + "user:secret@github.com/Talus-Network/nexus-sdk",
            "http://" + "evil.invalid/source",
            "https://" + "docs.sui.io/develop/unreviewed-page",
            "https://" + "docs.talus.network/guides/unknown",
            "HT" + "TPS" + "://evil.invalid/source",
            "S" + "SH" + "://github.com/Talus-Network/nexus-sdk",
            "g" + "it" + "://github.com/Talus-Network/nexus-sdk",
            "f" + "ile" + "://" + "/tmp/source",
            "g" + "it+ssh" + "://github.com/Talus-Network/nexus-sdk",
            "g" + "it@" + "github.com" + ":Talus-Network/nexus-sdk.git",
            "s" + "sh@" + "host.example" + ":Talus-Network/nexus-sdk.git",
            "https://" + "github.com" + ":" + "443/Talus-Network/nexus-sdk",
        )
        for index, url in enumerate(rejected):
            with tempfile.TemporaryDirectory(prefix="source-policy-rejected-url-") as directory:
                root = Path(directory)
                (root / "sample.md").write_text(url, encoding="utf-8")
                errors = []
                validator.validate_portability(root, errors)
                with self.subTest(kind="rejected", index=index):
                    self.assertTrue(errors, url)

    def test_public_skills_install_reference_is_root_readme_only(self) -> None:
        validator = _load("source_policy_install_context", ROOT / "scripts/validate_skills.py")
        selector = validator.PUBLIC_INSTALL_GITHUB_REPOSITORY
        install_url = "https://" + "github.com/" + selector
        commands = "\n".join(
            (
                "npx skills add " + selector,
            )
        )
        readme = (
            "# Public Skills\n\n"
            "## Install from the public repository\n\n"
            "```bash\n"
            + commands
            + "\n```\n\n"
        )
        with tempfile.TemporaryDirectory(prefix="source-policy-install-readme-") as directory:
            root = Path(directory)
            (root / "README.md").write_text(readme, encoding="utf-8")
            errors: list[str] = []
            validator.validate_portability(root, errors)
            self.assertEqual(errors, [])

        self.assertFalse(validator._classify_public_url(install_url)[0])
        with tempfile.TemporaryDirectory(prefix="source-policy-install-url-rejected-") as directory:
            root = Path(directory)
            (root / "README.md").write_text(
                "## Install from the public repository\n\n[Public Skills repository](" + install_url + ")\n",
                encoding="utf-8",
            )
            errors = []
            validator.validate_portability(root, errors)
            self.assertTrue(errors)

        for relative in (
            "sample.md",
            "sample.py",
            "sample.json",
            "scripts/prepare_sources.py",
            "scripts/forward_portability.py",
        ):
            with tempfile.TemporaryDirectory(prefix="source-policy-install-rejected-") as directory:
                root = Path(directory)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("npx skills add " + selector + " --all\n", encoding="utf-8")
                errors = []
                validator.validate_portability(root, errors)
                with self.subTest(relative=relative):
                    self.assertTrue(any("public Skills install selector" in error for error in errors))

        variants = (
            "npx skills add talus-network/" + "skills",
            "npx  skills add Talus-Network/" + "skills",
            "npx skills add Talus-Network " + "/ skills",
            "npx skills add Talus-Network/" + "skills --all",
            "NPX SKILLS ADD TALUS-NETWORK/" + "SKILLS --LIST",
        )
        for variant in variants:
            with tempfile.TemporaryDirectory(prefix="source-policy-install-variant-") as directory:
                root = Path(directory)
                (root / "README.md").write_text(
                    "## Install from the public repository\n\n```bash\n" + variant + "\n```\n",
                    encoding="utf-8",
                )
                errors = []
                validator.validate_portability(root, errors)
                with self.subTest(variant=variant):
                    self.assertTrue(errors)
        for relative in ("sample.md", "sample.py", "sample.json"):
            for variant in variants:
                with tempfile.TemporaryDirectory(prefix="source-policy-install-variant-file-") as directory:
                    root = Path(directory)
                    path = root / relative
                    path.write_text(variant, encoding="utf-8")
                    errors = []
                    validator.validate_portability(root, errors)
                    with self.subTest(relative=relative, variant=variant):
                        self.assertTrue(errors)

        with tempfile.TemporaryDirectory(prefix="source-policy-install-outside-section-") as directory:
            root = Path(directory)
            (root / "README.md").write_text(
                "# Public Skills\n\n"
                + selector
                + "\n\n"
                "## Install from the public repository\n\n"
                "```bash\n"
                "npx skills add "
                + selector
                + " --all\n```\n",
                encoding="utf-8",
            )
            errors = []
            validator.validate_portability(root, errors)
            self.assertTrue(any("public Skills install selector" in error for error in errors))

    def test_eval_validator_rejects_owning_activation_for_expected_sibling(self) -> None:
        validator = _load("source_policy_eval_routing_validator", ROOT / "scripts/validate_skills.py")
        entry = {
            "id": "routing-case",
            "prompt": "route this request",
            "sources": ["https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool"],
            "expected_output": "route to sibling",
            "expectations": ["observe the route", "keep boundaries"],
            "trigger": "implicit",
            "should_trigger": True,
            "expected_skill": "nexus-onchain-tool-development",
        }
        document = {
            "skill_name": "nexus-offchain-tool-development",
            "source_constraint": "public source only",
            "evals": [entry, {**entry, "id": "second-case", "expected_skill": None}],
        }
        with tempfile.TemporaryDirectory(
            prefix="source-policy-eval-routing-", dir=ROOT
        ) as directory:
            path = Path(directory) / "evals.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            errors: list[str] = []
            validator.validate_eval_file(document["skill_name"], path, errors)
        self.assertTrue(any("expected_skill requires should_trigger=false" in error for error in errors))

    def test_source_selector_policy_allows_only_public_repositories(self) -> None:
        validator = _load("source_policy_selector_validator", ROOT / "scripts/validate_skills.py")
        with tempfile.TemporaryDirectory(prefix="source-policy-selector-approved-") as directory:
            root = Path(directory)
            (root / "sample.md").write_text("--only nexus-sdk --only nexus-move-packages --only sui", encoding="utf-8")
            errors: list[str] = []
            validator.validate_portability(root, errors)
            self.assertEqual(errors, [])
        with tempfile.TemporaryDirectory(prefix="source-policy-selector-rejected-") as directory:
            root = Path(directory)
            (root / "sample.md").write_text("--only " + "private-" + "implementation", encoding="utf-8")
            errors = []
            validator.validate_portability(root, errors)
            self.assertTrue(any("source selector" in error for error in errors))
        with tempfile.TemporaryDirectory(prefix="source-policy-selector-install-rejected-") as directory:
            root = Path(directory)
            (root / "sample.md").write_text(
                "npx skills add " + validator.PUBLIC_INSTALL_GITHUB_REPOSITORY + " --all",
                encoding="utf-8",
            )
            errors = []
            validator.validate_portability(root, errors)
            self.assertTrue(any("public Skills install selector" in error for error in errors))

    def test_unapproved_repository_is_rejected_even_without_a_full_url(self) -> None:
        validator = _load("source_policy_unapproved_repository", ROOT / "scripts/validate_skills.py")
        unapproved_repository = "Talus-Network/" + "internal-tools"
        unapproved_url = "https://" + "github.com/" + unapproved_repository + "/tree/main"
        self.assertFalse(validator._classify_public_url(unapproved_url)[0])
        with tempfile.TemporaryDirectory(prefix="source-policy-unapproved-repository-") as directory:
            root = Path(directory)
            (root / "sample.md").write_text(unapproved_repository, encoding="utf-8")
            errors: list[str] = []
            validator.validate_portability(root, errors)
            self.assertTrue(any("approved public repository" in error for error in errors))

    def test_external_url_policy_rejects_redirects_outside_allowlist(self) -> None:
        validator = _load("source_policy_redirect_validator", ROOT / "scripts/validate_skills.py")
        approved = "https://" + "github.com/Talus-Network/nexus-sdk"
        redirected = "https://" + "evil.invalid/landing"
        with tempfile.TemporaryDirectory(prefix="source-policy-redirect-") as directory:
            root = Path(directory)
            (root / "sample.md").write_text(approved, encoding="utf-8")
            errors: list[str] = []
            validator.validate_portability(root, errors, url_resolver=lambda _url: redirected)
            self.assertTrue(any("redirect" in error for error in errors))

    def test_testnet_boundary_is_present_and_explicit(self) -> None:
        text = (ROOT / "scripts/testnet_evidence.py").read_text(encoding="utf-8")
        self.assertIn("TESTNET_GRAPHQL_HOST", text)
        self.assertIn("GRAPHQL_READ_OPERATIONS", text)
        self.assertIn("required=True", text)
        self.assertNotIn("TESTNET_RPC_HOSTS", text)
        self.assertNotIn("READ_ONLY_METHODS", text)
        self.assertNotIn("--rpc-url", text)
        self.assertNotIn("SUI_RPC_URL", text)

    def test_readme_uses_only_a_repository_local_entrypoint(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        selector = "Talus-Network/" + "skills"
        self.assertIn("SKILLS_BUNDLE_ROOT", text)
        self.assertIn("scripts/validate_skills.py", text)
        install_match = re.search(
            r"## Install from the public repository\n\n.*?```bash\n(.*?)\n```",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(install_match)
        install_block = install_match.group(1)
        commands = [line for line in install_block.splitlines() if line.startswith("npx skills add ")]
        self.assertEqual(
            commands,
            [
                "npx skills add " + selector,
            ],
        )
        self.assertIn("canonical command is release/install UX only", text)

    def test_graphql_examples_make_the_transport_claim_explicit(self) -> None:
        maintained = (
            ROOT / "README.md",
            ROOT / "nexus-cli-payment-tracking/SKILL.md",
            ROOT / "nexus-offchain-tool-development/SKILL.md",
            ROOT / "nexus-tap-development/SKILL.md",
            ROOT / "nexus-onchain-task-debugging/SKILL.md",
        )
        for path in maintained:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("graphql.testnet.sui.io/graphql", text)
                self.assertIn("--graphql-url", text)
                self.assertNotIn("JSON-RPC", text)
                self.assertNotIn("--rpc-url", text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("read-only GraphQL queries", readme)
        self.assertNotIn("JSON-RPC", readme)
        source_map = (ROOT / "nexus-onchain-tool-development/references/source-map.md").read_text(encoding="utf-8")
        self.assertIn("GraphQL read method shape", source_map)
        evals = json.loads((ROOT / "nexus-onchain-task-debugging/evals/evals.json").read_text(encoding="utf-8"))
        self.assertIn("read-only GraphQL queries", evals["evals"][1]["expectations"][1])
        eval_text = json.dumps(evals)
        self.assertNotIn("JSON-RPC", eval_text)
        self.assertNotIn("--rpc-url", eval_text)


if __name__ == "__main__":
    unittest.main()
