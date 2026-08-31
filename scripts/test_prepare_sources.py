#!/usr/bin/env python3
"""Tests for the disposable source preparation helper."""

from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare_sources as prepare_sources_module
from prepare_sources import (
    DEFAULT_REPOSITORIES,
    PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES,
    PUBLIC_MOVE_PACKAGE_CLOSURE_PATHS,
    RepositorySpec,
    SourcePreparationError,
    _StableArchive,
    _marker_path,
    _scan_archive_object,
    _download_archive,
    _validate_archive_url,
    _parse_checksums,
    _load_manifest_for_tests,
    _repository_root_for_tests,
    cleanup_manifest,
    cleanup_workspace,
    load_manifest,
    prepare_sources,
    _prepare_sources_for_specs,
    _prepared_sources_for_specs,
    prepared_sources,
    repository_root,
    safe_extract_archive,
)
from tree_integrity import tree_digest, tree_entries
from forward_portability import (
    _assert_public_move_manifest,
    _generate_dag_document,
    _generate_skill_document,
    _move_tap,
    _onchain_tool,
)


# Fixture archive injection is intentionally confined to the private test seam;
# production callers exercise the public catalog-only entrypoints above.
prepare_sources = _prepare_sources_for_specs
prepared_sources = _prepared_sources_for_specs
load_manifest = _load_manifest_for_tests
repository_root = _repository_root_for_tests


def archive_bytes(top_level: str, *, members: list[tuple[str, str]] | None = None, link: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        directory = tarfile.TarInfo(top_level)
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        readme = tarfile.TarInfo(f"{top_level}/README.md")
        content = b"fixture source\n"
        readme.size = len(content)
        bundle.addfile(readme, io.BytesIO(content))
        for name, value in members or []:
            info = tarfile.TarInfo(name)
            if link:
                info.type = tarfile.SYMTYPE
                info.linkname = value
                bundle.addfile(info)
            else:
                encoded = value.encode()
                info.size = len(encoded)
                bundle.addfile(info, io.BytesIO(encoded))
    return output.getvalue()


class PrepareSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = RepositorySpec(
            logical_name="fixture",
            repository="Talus-Network/fixture",
            ref="abc123",
            required_paths=("README.md",),
        )

    def fake_downloader(self, payload: bytes):
        def download(_url: str, destination: Path) -> None:
            destination.write_bytes(payload)

        return download

    def test_prepare_writes_manifest_and_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-test-") as parent_name:
            parent = Path(parent_name)
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=parent,
            )
            manifest_path = Path(str(manifest["manifest"]))
            root = repository_root(manifest_path, "fixture")
            self.assertEqual(root.name, "fixture-abc123")
            self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "fixture source\n")
            loaded = load_manifest(manifest_path)
            self.assertEqual(loaded["schema_version"], 1)
            record = loaded["repositories"]["fixture"]
            self.assertEqual(record["source_tree_sha256"], tree_digest(tree_entries(root)))
            self.assertTrue(Path(record["source_tree_manifest"]).is_file())
            cleanup_manifest(manifest_path)
            self.assertFalse(Path(str(manifest["workspace"])).exists())

    def test_context_manager_cleans_on_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-context-") as parent_name:
            with prepared_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=Path(parent_name),
            ) as manifest:
                workspace = Path(str(manifest["workspace"]))
                self.assertTrue(workspace.is_dir())
            self.assertFalse(workspace.exists())

    def test_identity_mismatch_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-identity-") as parent_name:
            parent = Path(parent_name)
            with self.assertRaisesRegex(SourcePreparationError, "identity mismatch"):
                prepare_sources(
                    specs={"fixture": self.spec},
                    downloader=self.fake_downloader(archive_bytes("other-abc123")),
                    workspace_parent=parent,
                )
            self.assertEqual(list(parent.iterdir()), [])

    def test_path_traversal_is_removed_before_write(self) -> None:
        escape_name = "fixture-abc123/.." + "/escaped.txt"
        with tempfile.TemporaryDirectory(prefix="source-helper-traversal-") as parent_name:
            parent = Path(parent_name)
            with self.assertRaisesRegex(SourcePreparationError, "unsafe member path"):
                prepare_sources(
                    specs={"fixture": self.spec},
                    downloader=self.fake_downloader(
                        archive_bytes("fixture-abc123", members=[(escape_name, "nope")])
                    ),
                    workspace_parent=parent,
                )
            self.assertEqual(list(parent.iterdir()), [])

    def test_absolute_member_is_rejected(self) -> None:
        absolute_name = "/" + "escaped.txt"
        with tempfile.TemporaryDirectory(prefix="source-helper-absolute-") as parent_name:
            with self.assertRaisesRegex(SourcePreparationError, "absolute member"):
                prepare_sources(
                    specs={"fixture": self.spec},
                    downloader=self.fake_downloader(
                        archive_bytes("fixture-abc123", members=[(absolute_name, "nope")])
                    ),
                    workspace_parent=Path(parent_name),
                )

    def test_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-link-") as parent_name:
            with self.assertRaisesRegex(SourcePreparationError, "links.*not allowed"):
                prepare_sources(
                    specs={"fixture": self.spec},
                    downloader=self.fake_downloader(
                        archive_bytes(
                            "fixture-abc123",
                            members=[("fixture-abc123/link", "../../outside")],
                            link=True,
                        )
                    ),
                    workspace_parent=Path(parent_name),
                )

    def test_manifest_root_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-manifest-") as parent_name:
            parent = Path(parent_name)
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=parent,
            )
            manifest_path = Path(str(manifest["manifest"]))
            altered = json.loads(manifest_path.read_text(encoding="utf-8"))
            altered["repositories"]["fixture"]["root"] = str(parent / "outside")
            manifest_path.write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(SourcePreparationError, "root/workspace relationship"):
                load_manifest(manifest_path)
            # Restore the trusted manifest path so cleanup remains explicit and safe.
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            cleanup_manifest(manifest_path)

    def test_manifest_mutation_matrix_is_fail_closed(self) -> None:
        mutations = {
            "evil-host": lambda record: record.__setitem__("archive_url", "https://" + "evil.invalid/archive.tar.gz"),
            "wrong-repository": lambda record: record.__setitem__("repository", "Talus-Network/other"),
            "empty-ref": lambda record: record.__setitem__("ref", ""),
            "wrong-checksum": lambda record: record.__setitem__("archive_sha256", "0" * 64),
            "wrong-logical-name": lambda record: record.__setitem__("logical_name", "other"),
            "altered-required-paths": lambda record: record.__setitem__("required_paths", ["README.md", "missing.txt"]),
            "altered-archive-path": lambda record: record.__setitem__("archive_relative", "archives/other.tar.gz"),
            "altered-root-path": lambda record: record.__setitem__("root_relative", "sources/other/root"),
        }
        with tempfile.TemporaryDirectory(prefix="source-helper-mutations-") as parent_name:
            parent = Path(parent_name)
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=parent,
            )
            manifest_path = Path(str(manifest["manifest"]))
            original = json.loads(manifest_path.read_text(encoding="utf-8"))
            for name, mutate in mutations.items():
                altered = json.loads(json.dumps(original))
                mutate(altered["repositories"]["fixture"])
                manifest_path.write_text(json.dumps(altered), encoding="utf-8")
                with self.assertRaises(SourcePreparationError, msg=name):
                    load_manifest(manifest_path)
            manifest_path.write_text(json.dumps(original), encoding="utf-8")
            cleanup_manifest(manifest_path)

    def test_manifest_requires_complete_archive_tree_and_workspace_evidence(self) -> None:
        required_record_fields = (
            "archive_relative",
            "archive_sha256",
            "archive_tree_entries",
            "archive_tree_sha256",
            "root",
            "root_relative",
            "source_tree_manifest",
            "source_tree_manifest_relative",
            "source_tree_sha256",
        )
        with tempfile.TemporaryDirectory(prefix="source-helper-complete-contract-") as parent_name:
            parent = Path(parent_name)
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=parent,
            )
            manifest_path = Path(str(manifest["manifest"]))
            original_payload = manifest_path.read_bytes()
            original = json.loads(original_payload)
            archive_path = Path(original["repositories"]["fixture"]["archive_relative"])
            archive_path = Path(str(original["workspace"])) / archive_path
            tree_manifest_path = Path(original["repositories"]["fixture"]["source_tree_manifest"])
            archive_payload = archive_path.read_bytes()
            tree_manifest_payload = tree_manifest_path.read_bytes()
            try:
                for field in required_record_fields:
                    altered = json.loads(json.dumps(original))
                    altered["repositories"]["fixture"].pop(field, None)
                    manifest_path.write_text(json.dumps(altered), encoding="utf-8")
                    try:
                        with self.assertRaisesRegex(SourcePreparationError, "identity/paths|archive evidence"):
                            load_manifest(manifest_path)
                    finally:
                        manifest_path.write_bytes(original_payload)
                altered = json.loads(json.dumps(original))
                altered["workspace"] = str(parent)
                manifest_path.write_text(json.dumps(altered), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(SourcePreparationError, "helper-owned|workspace"):
                        load_manifest(manifest_path)
                finally:
                    manifest_path.write_bytes(original_payload)
                altered = json.loads(json.dumps(original))
                altered["repositories"]["fixture"] = {"repository": self.spec.repository}
                manifest_path.write_text(json.dumps(altered), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(SourcePreparationError, "identity/paths|archive evidence|logical repository"):
                        load_manifest(manifest_path)
                finally:
                    manifest_path.write_bytes(original_payload)
                archive_path.unlink()
                with self.assertRaisesRegex(SourcePreparationError, "archive"):
                    load_manifest(manifest_path)
                archive_path.write_bytes(archive_payload)
                tree_manifest_path.unlink()
                with self.assertRaisesRegex(SourcePreparationError, "tree|manifest|root/workspace"):
                    load_manifest(manifest_path)
            finally:
                archive_path.write_bytes(archive_payload)
                tree_manifest_path.write_bytes(tree_manifest_payload)
                manifest_path.write_text(json.dumps(original), encoding="utf-8")
                cleanup_manifest(manifest_path)

    def test_authenticated_source_tree_mutations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-tree-mutations-") as parent_name:
            parent = Path(parent_name)
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=parent,
            )
            manifest_path = Path(str(manifest["manifest"]))
            loaded = load_manifest(manifest_path)
            record = loaded["repositories"]["fixture"]
            root = Path(record["root"])
            tracked = root / "README.md"
            original = tracked.read_bytes()
            original_mode = tracked.stat().st_mode & 0o7777

            def expect_tree_failure() -> None:
                with self.assertRaisesRegex(SourcePreparationError, "source (tree|identity)|tree digest"):
                    load_manifest(manifest_path)

            try:
                tracked.write_bytes(original + b"changed")
                expect_tree_failure()
                tracked.write_bytes(original)
                os.chmod(tracked, original_mode ^ 0o100)
                expect_tree_failure()
                os.chmod(tracked, original_mode)
                added = root / "added.txt"
                added.write_bytes(b"added")
                expect_tree_failure()
                added.unlink()
                tracked.unlink()
                expect_tree_failure()
                tracked.write_bytes(original)
                special = root / "named-pipe"
                os.mkfifo(special)
                try:
                    expect_tree_failure()
                finally:
                    special.unlink(missing_ok=True)
                outside = root.parent.parent / "outside-source.txt"
                outside.write_bytes(b"outside")
                escape = root / "escape-link"
                escape.symlink_to(os.path.relpath(outside, root))
                try:
                    expect_tree_failure()
                finally:
                    escape.unlink(missing_ok=True)
                    outside.unlink(missing_ok=True)
                nested = root / "nested"
                nested.mkdir()
                nested_escape = nested / "escape-link"
                nested_escape.symlink_to(os.path.relpath(root.parent.parent / "outside-nested.txt", nested))
                (root.parent.parent / "outside-nested.txt").write_bytes(b"outside")
                try:
                    expect_tree_failure()
                finally:
                    nested_escape.unlink(missing_ok=True)
                    (root.parent.parent / "outside-nested.txt").unlink(missing_ok=True)
                    nested.rmdir()
            finally:
                tracked.parent.mkdir(parents=True, exist_ok=True)
                tracked.write_bytes(original)
                os.chmod(tracked, original_mode)
                cleanup_manifest(manifest_path)

    def test_source_tree_manifest_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-tree-manifest-") as parent_name:
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=Path(parent_name),
            )
            manifest_path = Path(str(manifest["manifest"]))
            record = load_manifest(manifest_path)["repositories"]["fixture"]
            tree_manifest = Path(record["source_tree_manifest"])
            original = tree_manifest.read_bytes()
            document = json.loads(original)
            document["tree_sha256"] = "0" * 64
            tree_manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SourcePreparationError, "source tree|tree digest|manifest"):
                load_manifest(manifest_path)
            tree_manifest.write_bytes(original)
            cleanup_manifest(manifest_path)

    def test_archive_derived_tree_digest_matches_extracted_manifest(self) -> None:
        payload = archive_bytes("fixture-abc123", members=[("fixture-abc123/nested/value", "value")])
        with tempfile.TemporaryDirectory(prefix="source-helper-archive-projection-") as parent_name:
            parent = Path(parent_name)
            archive = parent / "fixture.tar.gz"
            archive.write_bytes(payload)
            with _StableArchive(archive) as stable:
                scan = _scan_archive_object(stable, "fixture-")
                projected_entries = scan.expected_entries
                projected_digest = scan.expected_tree_sha256
            self.assertEqual(projected_digest, tree_digest(projected_entries))
            destination = parent / "not-created-by-scan"
            self.assertFalse(destination.exists())
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(payload),
                workspace_parent=parent,
            )
            manifest_path = Path(str(manifest["manifest"]))
            record = manifest["repositories"]["fixture"]
            self.assertEqual(record["source_tree_sha256"], projected_digest)
            self.assertEqual(record["archive_tree_sha256"], projected_digest)
            self.assertEqual(record["archive_tree_entries"], len(projected_entries))
            cleanup_manifest(manifest_path)

    def test_coherent_tree_marker_and_manifest_rewrite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-coherent-tree-rewrite-") as parent_name:
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=Path(parent_name),
            )
            manifest_path = Path(str(manifest["manifest"]))
            workspace = Path(str(manifest["workspace"]))
            root = Path(manifest["repositories"]["fixture"]["root"])
            tree_manifest = Path(manifest["repositories"]["fixture"]["source_tree_manifest"])
            marker_path = _marker_path(workspace)
            original_payload = (root / "README.md").read_bytes()
            original_manifest = manifest_path.read_bytes()
            original_marker = marker_path.read_bytes()
            original_tree_manifest = tree_manifest.read_bytes()
            try:
                (root / "README.md").write_bytes(b"tampered source\n")
                rewritten_digest = tree_digest(tree_entries(root))
                rewritten_tree = json.loads(original_tree_manifest)
                rewritten_tree["entries"] = tree_entries(root)
                rewritten_tree["tree_sha256"] = rewritten_digest
                tree_manifest.write_text(json.dumps(rewritten_tree), encoding="utf-8")
                rewritten_manifest = json.loads(original_manifest)
                rewritten_record = rewritten_manifest["repositories"]["fixture"]
                rewritten_record["source_tree_sha256"] = rewritten_digest
                rewritten_record["archive_tree_sha256"] = rewritten_digest
                manifest_path.write_text(json.dumps(rewritten_manifest), encoding="utf-8")
                rewritten_marker = json.loads(original_marker)
                rewritten_archive = rewritten_marker["archives"]["fixture"]
                rewritten_archive["source_tree_sha256"] = rewritten_digest
                rewritten_archive["archive_tree_sha256"] = rewritten_digest
                marker_path.write_text(json.dumps(rewritten_marker), encoding="utf-8")
                with self.assertRaisesRegex(SourcePreparationError, "not derived from the retained archive"):
                    load_manifest(manifest_path)
            finally:
                (root / "README.md").write_bytes(original_payload)
                tree_manifest.write_bytes(original_tree_manifest)
                manifest_path.write_bytes(original_manifest)
                marker_path.write_bytes(original_marker)
                cleanup_manifest(manifest_path)

    def test_default_public_archive_checksums_are_reviewed(self) -> None:
        self.assertEqual(
            DEFAULT_REPOSITORIES["nexus-sdk"].archive_sha256,
            "a6b25bb7d98bde41fe172afe673a28e7b7731ad51947a81d6cb615c144ed55b1",
        )
        self.assertEqual(
            DEFAULT_REPOSITORIES["nexus-move-packages"].archive_sha256,
            "e88c6b977e87847f441564ecb9bcb75c269c5214d640721684139e93ddaa32f2",
        )
        self.assertEqual(
            DEFAULT_REPOSITORIES["sui"].repository,
            "MystenLabs/sui",
        )
        self.assertEqual(
            DEFAULT_REPOSITORIES["sui"].ref,
            "d8459684b41eb09ab23fe16a9dd84173270bbaba",
        )
        self.assertEqual(
            DEFAULT_REPOSITORIES["sui"].archive_sha256,
            "1b974c1b10413e873b98df2740a877a930a08b40c80af58b52f737385f8bdf44",
        )

    def test_pinned_archive_checksum_is_enforced_before_extraction(self) -> None:
        spec = RepositorySpec(
            logical_name="fixture",
            repository="Talus-Network/fixture",
            ref="abc123",
            required_paths=("README.md",),
            archive_sha256="0" * 64,
        )
        with tempfile.TemporaryDirectory(prefix="source-helper-pinned-checksum-") as parent_name:
            with self.assertRaisesRegex(SourcePreparationError, "reviewed specification"):
                prepare_sources(
                    specs={"fixture": spec},
                    downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                    workspace_parent=Path(parent_name),
                )
            self.assertEqual(list(Path(parent_name).iterdir()), [])

    def test_explicit_ignored_links_are_authenticated_and_not_extracted(self) -> None:
        spec = RepositorySpec(
            logical_name="fixture",
            repository="Talus-Network/fixture",
            ref="abc123",
            required_paths=("README.md",),
            allow_ignored_links=True,
        )
        payload = archive_bytes(
            "fixture-abc123",
            members=[("fixture-abc123/unneeded-link", "../../outside")],
            link=True,
        )
        with tempfile.TemporaryDirectory(prefix="source-helper-ignored-link-") as parent_name:
            manifest = prepare_sources(
                specs={"fixture": spec},
                downloader=self.fake_downloader(payload),
                workspace_parent=Path(parent_name),
            )
            record = manifest["repositories"]["fixture"]
            self.assertEqual(record["ignored_archive_links"], ["fixture-abc123/unneeded-link"])
            self.assertFalse(Path(record["root"], "unneeded-link").exists())
            cleanup_manifest(Path(str(manifest["manifest"])))

    def test_obsolete_archive_scan_wrappers_are_removed(self) -> None:
        self.assertFalse(hasattr(prepare_sources_module, "_scan_archive"))
        self.assertFalse(hasattr(prepare_sources_module, "_archive_root_name"))

    def test_stable_archive_rejects_path_inode_swap_between_passes(self) -> None:
        archive_fd, archive_name = tempfile.mkstemp(prefix="source-helper-archive-swap-", suffix=".tar.gz")
        os.close(archive_fd)
        archive = Path(archive_name)
        destination = Path(tempfile.mkdtemp(prefix="source-helper-archive-swap-dest-")) / "extract"
        archive.write_bytes(archive_bytes("fixture-abc123"))
        original_assert = prepare_sources_module._StableArchive.assert_stable
        swapped = False

        def replace_after_preflight(stable, label: str) -> None:
            nonlocal swapped
            original_assert(stable, label)
            if label == "source archive after preflight" and not swapped:
                swapped = True
                replacement = archive.with_name("replacement.tar.gz")
                replacement.write_bytes(archive_bytes("fixture-abc123", members=[("fixture-abc123/changed", "x")]))
                os.replace(replacement, archive)

        try:
            with mock.patch.object(prepare_sources_module._StableArchive, "assert_stable", replace_after_preflight):
                with self.assertRaisesRegex(SourcePreparationError, "different archive|stable descriptor"):
                    safe_extract_archive(archive, destination, "fixture-")
        finally:
            archive.unlink(missing_ok=True)
            destination.parent.rmdir()

    def test_configurable_valid_ref_is_bound_to_its_archive(self) -> None:
        spec = RepositorySpec(
            logical_name="fixture",
            repository="Talus-Network/fixture",
            ref="main",
            required_paths=("README.md",),
        )
        payload = archive_bytes("fixture-release-v2")
        checksum = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory(prefix="source-helper-ref-") as parent_name:
            manifest = prepare_sources(
                specs={"fixture": spec},
                overrides={"fixture": "release/v2"},
                checksums={"fixture": checksum},
                downloader=self.fake_downloader(payload),
                workspace_parent=Path(parent_name),
            )
            manifest_path = Path(str(manifest["manifest"]))
            loaded = load_manifest(manifest_path)
            self.assertEqual(loaded["repositories"]["fixture"]["ref"], "release/v2")
            self.assertEqual(loaded["repositories"]["fixture"]["reviewed_archive_sha256"], checksum)
            cleanup_manifest(manifest_path)

    def test_configurable_ref_requires_a_caller_checksum(self) -> None:
        spec = RepositorySpec(
            logical_name="fixture",
            repository="Talus-Network/fixture",
            ref="main",
            required_paths=("README.md",),
        )
        payload = archive_bytes("fixture-release-v2")
        with tempfile.TemporaryDirectory(prefix="source-helper-ref-required-") as parent_name:
            with self.assertRaisesRegex(SourcePreparationError, "caller-supplied archive checksum"):
                prepare_sources(
                    specs={"fixture": spec},
                    overrides={"fixture": "release/v2"},
                    downloader=self.fake_downloader(payload),
                    workspace_parent=Path(parent_name),
                )
            self.assertEqual(list(Path(parent_name).iterdir()), [])

    def test_configurable_ref_rejects_a_wrong_caller_checksum(self) -> None:
        spec = RepositorySpec(
            logical_name="fixture",
            repository="Talus-Network/fixture",
            ref="main",
            required_paths=("README.md",),
        )
        with tempfile.TemporaryDirectory(prefix="source-helper-ref-wrong-") as parent_name:
            with self.assertRaisesRegex(SourcePreparationError, "reviewed specification"):
                prepare_sources(
                    specs={"fixture": spec},
                    overrides={"fixture": "release/v2"},
                    checksums={"fixture": "0" * 64},
                    downloader=self.fake_downloader(archive_bytes("fixture-release-v2")),
                    workspace_parent=Path(parent_name),
                )
            self.assertEqual(list(Path(parent_name).iterdir()), [])

    def test_checksum_parser_rejects_repeated_and_malformed_values(self) -> None:
        with self.assertRaisesRegex(SourcePreparationError, "repeated"):
            _parse_checksums(["fixture=" + "0" * 64, "fixture=" + "1" * 64])
        with self.assertRaisesRegex(SourcePreparationError, "invalid"):
            _parse_checksums(["fixture=not-a-digest"])

    def test_public_move_package_inventory_and_generated_manifests(self) -> None:
        spec = DEFAULT_REPOSITORIES["nexus-move-packages"]
        self.assertEqual(
            spec.required_paths,
            ("README.md", *PUBLIC_MOVE_PACKAGE_CLOSURE_PATHS),
        )
        with tempfile.TemporaryDirectory(prefix="source-helper-public-move-") as directory:
            move_root = Path(directory) / "nexus-move-packages"
            for name in PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES:
                package = move_root / "packages" / name
                package.mkdir(parents=True)
                (package / "Move.toml").write_text(
                    f'[package]\nname = "nexus_{name}"\nversion = "1.0.0"\n',
                    encoding="utf-8",
                )
                (package / "Published.toml").write_text("[published.testnet]\n", encoding="utf-8")
            tap = Path(directory) / "tap"
            tool = Path(directory) / "tool"
            _move_tap(
                tap,
                move_root,
                dag_document=_generate_dag_document(),
                skill_document=_generate_skill_document(),
            )
            _onchain_tool(tool, move_root)
            implementation_root = "/".join(("private-runtime", "packages")) + "/"
            tap_manifest = (tap / "tap/Move.toml").read_text(encoding="utf-8")
            self.assertNotIn("[dependencies]", tap_manifest)
            tool_manifest = (tool / "Move.toml").read_text(encoding="utf-8")
            self.assertIn('nexus_primitives = { local = "deps/primitives" }', tool_manifest)
            self.assertIn('nexus_interface = { local = "deps/interface" }', tool_manifest)
            for project in (tap / "tap", tool):
                manifest = (project / "Move.toml").read_text(encoding="utf-8")
                self.assertNotIn(implementation_root, manifest)
                self.assertTrue((project / "deps/primitives/Published.toml").is_file())
                self.assertTrue((project / "deps/interface/Published.toml").is_file())
            bad_manifest = tool / "Move.toml"
            stale_dependency = "../../private-runtime/packages/primitives"
            bad_manifest.write_text(
                bad_manifest.read_text(encoding="utf-8").replace(
                    'nexus_primitives = { local = "deps/primitives" }',
                    f'nexus_primitives = {{ local = "{stale_dependency}" }}',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SourcePreparationError, "public nexus-move-packages root"):
                _assert_public_move_manifest(tool)

    def test_archive_member_count_limit_cleans_workspace(self) -> None:
        members = [(f"fixture-abc123/file-{index}", "x") for index in range(12)]
        with tempfile.TemporaryDirectory(prefix="source-helper-member-limit-") as parent_name:
            parent = Path(parent_name)
            with mock.patch("prepare_sources.MAX_ARCHIVE_MEMBERS", 4):
                with self.assertRaisesRegex(SourcePreparationError, "too many members"):
                    prepare_sources(
                        specs={"fixture": self.spec},
                        downloader=self.fake_downloader(archive_bytes("fixture-abc123", members=members)),
                        workspace_parent=parent,
                    )
            self.assertEqual(list(parent.iterdir()), [])

    def test_archive_member_size_limit_cleans_direct_extraction(self) -> None:
        archive = Path(tempfile.mkstemp(prefix="source-helper-member-size-", suffix=".tar.gz")[1])
        try:
            archive.write_bytes(
                archive_bytes(
                    "fixture-abc123",
                    members=[("fixture-abc123/large.bin", "expanded member")],
                )
            )
            with tempfile.TemporaryDirectory(prefix="source-helper-member-size-dest-") as directory:
                destination = Path(directory) / "extract"
                with mock.patch("prepare_sources.MAX_MEMBER_UNCOMPRESSED_BYTES", 1):
                    with self.assertRaisesRegex(SourcePreparationError, "member is larger"):
                        safe_extract_archive(archive, destination, "fixture-")
                self.assertFalse(destination.exists())
        finally:
            archive.unlink(missing_ok=True)

    def test_archive_aggregate_size_limit_cleans_workspace(self) -> None:
        members = [("fixture-abc123/one", "0123456789"), ("fixture-abc123/two", "abcdefghij")]
        with tempfile.TemporaryDirectory(prefix="source-helper-aggregate-limit-") as parent_name:
            parent = Path(parent_name)
            with mock.patch("prepare_sources.MAX_ARCHIVE_UNCOMPRESSED_BYTES", 20):
                with mock.patch("prepare_sources.MAX_ARCHIVE_EXPANSION_RATIO", 10_000.0):
                    with self.assertRaisesRegex(SourcePreparationError, "expands beyond"):
                        prepare_sources(
                            specs={"fixture": self.spec},
                            downloader=self.fake_downloader(archive_bytes("fixture-abc123", members=members)),
                            workspace_parent=parent,
                        )
            self.assertEqual(list(parent.iterdir()), [])

    def test_archive_expansion_ratio_limit_cleans_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-ratio-limit-") as parent_name:
            parent = Path(parent_name)
            with mock.patch("prepare_sources.MAX_ARCHIVE_EXPANSION_RATIO", 0.01):
                with self.assertRaisesRegex(SourcePreparationError, "expansion ratio"):
                    prepare_sources(
                        specs={"fixture": self.spec},
                        downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                        workspace_parent=parent,
                    )
            self.assertEqual(list(parent.iterdir()), [])

    def test_archive_limits_allow_boundary_valid_archive(self) -> None:
        payload = archive_bytes("fixture-abc123", members=[("fixture-abc123/one", "x")])
        with tempfile.TemporaryDirectory(prefix="source-helper-boundary-") as parent_name:
            parent = Path(parent_name)
            archive_path = parent / "boundary.tar.gz"
            archive_path.write_bytes(payload)
            with tarfile.open(archive_path, mode="r:gz") as bundle:
                members = list(bundle)
                expanded = sum(member.size for member in members)
                count = len(members)
                maximum_member = max(member.size for member in members)
            with mock.patch("prepare_sources.MAX_ARCHIVE_MEMBERS", count):
                with mock.patch("prepare_sources.MAX_ARCHIVE_UNCOMPRESSED_BYTES", expanded):
                    with mock.patch(
                        "prepare_sources.MAX_MEMBER_UNCOMPRESSED_BYTES",
                        maximum_member,
                    ):
                        with mock.patch("prepare_sources.MAX_ARCHIVE_EXPANSION_RATIO", expanded / archive_path.stat().st_size):
                            manifest = prepare_sources(
                                specs={"fixture": self.spec},
                                downloader=self.fake_downloader(payload),
                                workspace_parent=parent,
                            )
            cleanup_manifest(Path(str(manifest["manifest"])))

    def test_download_size_limit_removes_partial_archive(self) -> None:
        response = mock.MagicMock()
        response.headers = {}
        response.read.side_effect = [b"too-large", b""]
        response.geturl.return_value = "https://" + "codeload.github.com/Talus-Network/fixture/tar.gz/abc123"
        response.__enter__.return_value = response
        with tempfile.TemporaryDirectory(prefix="source-helper-download-limit-") as directory:
            destination = Path(directory) / "archive.tar.gz"
            with mock.patch("prepare_sources.urlopen", return_value=response):
                with mock.patch("prepare_sources.MAX_ARCHIVE_BYTES", 1):
                    with self.assertRaisesRegex(SourcePreparationError, "larger"):
                        _download_archive("https://" + "codeload.github.com/Talus-Network/fixture/tar.gz/abc123", destination)
            self.assertFalse(destination.exists())

    def test_archive_transport_validates_final_redirect_before_reading(self) -> None:
        approved = "https://" + "codeload.github.com/Talus-Network/fixture/tar.gz/abc123"
        for final_url in (
            "https://" + "codeload.github.com" + ":443/Talus-Network/fixture/tar.gz/abc123",
            "https://" + "codeload.github.com/Talus-Network/fixture/tar.gz/other",
            "https://" + "evil.invalid/Talus-Network/fixture/tar.gz/abc123",
        ):
            response = mock.MagicMock()
            response.geturl.return_value = final_url
            response.headers = {}
            response.__enter__.return_value = response
            with tempfile.TemporaryDirectory(prefix="source-helper-redirect-") as directory:
                destination = Path(directory) / "archive.tar.gz"
                with self.subTest(final_url=final_url), mock.patch(
                    "prepare_sources.urlopen", return_value=response
                ), self.assertRaises(SourcePreparationError):
                    _download_archive(approved, destination)
                response.read.assert_not_called()

        response = mock.MagicMock()
        response.geturl.return_value = approved
        response.headers = {}
        response.read.side_effect = [b"archive", b""]
        response.__enter__.return_value = response
        with tempfile.TemporaryDirectory(prefix="source-helper-safe-redirect-") as directory:
            destination = Path(directory) / "archive.tar.gz"
            with mock.patch("prepare_sources.urlopen", return_value=response):
                _download_archive(approved, destination)
            self.assertEqual(destination.read_bytes(), b"archive")

        for url in (
            "https://" + "codeload.github.com" + ":" + "443/Talus-Network/fixture/tar.gz/abc123",
            "https://" + "codeload.github.com/Talus-Network/fixture/tar.gz/../abc123",
        ):
            with self.subTest(url=url), self.assertRaises(SourcePreparationError):
                _validate_archive_url(url)

    def test_download_failure_does_not_leave_workspace(self) -> None:
        def failed_download(_url: str, _destination: Path) -> None:
            raise SourcePreparationError("simulated download failure")

        with tempfile.TemporaryDirectory(prefix="source-helper-download-") as parent_name:
            parent = Path(parent_name)
            with self.assertRaisesRegex(SourcePreparationError, "simulated download"):
                prepare_sources(specs={"fixture": self.spec}, downloader=failed_download, workspace_parent=parent)
            self.assertEqual(list(parent.iterdir()), [])

    def test_preparation_failure_cleanup_success_preserves_primary_without_note(self) -> None:
        def failed_download(_url: str, _destination: Path) -> None:
            raise ValueError("primary-prepare")

        with tempfile.TemporaryDirectory(prefix="source-helper-prepare-primary-") as parent_name:
            parent = Path(parent_name)
            with mock.patch.object(
                prepare_sources_module,
                "cleanup_workspace",
                wraps=prepare_sources_module.cleanup_workspace,
            ) as cleanup:
                with self.assertRaises(ValueError) as raised:
                    with prepared_sources(
                        specs={"fixture": self.spec},
                        downloader=failed_download,
                        workspace_parent=parent,
                    ):
                        self.fail("preparation should have failed")
                self.assertEqual(type(raised.exception), ValueError)
                self.assertEqual(str(raised.exception), "primary-prepare")
                self.assertEqual(getattr(raised.exception, "__notes__", []), [])
                self.assertEqual(cleanup.call_count, 1)
            self.assertEqual(list(parent.iterdir()), [])

    def test_preparation_primary_and_cleanup_failure_report_once_and_preserve_primary(self) -> None:
        def failed_download(_url: str, _destination: Path) -> None:
            raise ValueError("primary-prepare")

        seen_workspaces: list[Path] = []
        original_cleanup = prepare_sources_module.cleanup_workspace

        def cleanup_then_fail(workspace: Path) -> None:
            seen_workspaces.append(Path(workspace))
            original_cleanup(workspace)
            raise RuntimeError("cleanup-prepare")

        with tempfile.TemporaryDirectory(prefix="source-helper-prepare-cleanup-failure-") as parent_name:
            parent = Path(parent_name)
            with mock.patch.object(
                prepare_sources_module,
                "cleanup_workspace",
                side_effect=cleanup_then_fail,
            ) as cleanup:
                with self.assertRaises(ValueError) as raised:
                    with prepared_sources(
                        specs={"fixture": self.spec},
                        downloader=failed_download,
                        workspace_parent=parent,
                    ):
                        self.fail("preparation should have failed")
                self.assertEqual(type(raised.exception), ValueError)
                self.assertEqual(str(raised.exception), "primary-prepare")
                notes = getattr(raised.exception, "__notes__", [])
                self.assertEqual(
                    [note for note in notes if "cleanup-prepare" in note],
                    ["source workspace cleanup failed: cleanup-prepare"],
                )
                self.assertEqual(cleanup.call_count, 1)
                self.assertEqual(len(seen_workspaces), 1)
                self.assertEqual(seen_workspaces[0].parent, parent)
            self.assertEqual(list(parent.iterdir()), [])

    def test_cleanup_only_failure_is_raised_after_successful_preparation(self) -> None:
        seen_workspaces: list[Path] = []
        original_cleanup = prepare_sources_module.cleanup_workspace

        def cleanup_then_fail(workspace: Path) -> None:
            seen_workspaces.append(Path(workspace))
            original_cleanup(workspace)
            raise RuntimeError("cleanup-only")

        with tempfile.TemporaryDirectory(prefix="source-helper-cleanup-only-prepared-") as parent_name:
            parent = Path(parent_name)
            with mock.patch.object(
                prepare_sources_module,
                "cleanup_workspace",
                side_effect=cleanup_then_fail,
            ) as cleanup:
                with self.assertRaisesRegex(RuntimeError, "cleanup-only"):
                    with prepared_sources(
                        specs={"fixture": self.spec},
                        downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                        workspace_parent=parent,
                    ):
                        pass
                self.assertEqual(cleanup.call_count, 1)
                self.assertEqual(len(seen_workspaces), 1)
            self.assertEqual(list(parent.iterdir()), [])

    def test_signal_during_download_cleans_once_and_stops_continuation(self) -> None:
        child_script = """
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, os.environ["HELPER_DIR"])
from prepare_sources import RepositorySpec, _prepare_sources_for_specs

parent = Path(os.environ["WORKSPACE_PARENT"])
marker = parent / "post-signal"
spec = RepositorySpec("fixture", "Talus-Network/fixture", "abc123", ("README.md",))

def slow_download(_url, _destination):
    (parent / "download-started").write_text("started", encoding="utf-8")
    time.sleep(30)
    marker.write_text("continued", encoding="utf-8")

_prepare_sources_for_specs(specs={"fixture": spec}, downloader=slow_download, workspace_parent=parent)
"""
        for signum in (signal.SIGINT, signal.SIGTERM):
            with tempfile.TemporaryDirectory(prefix="source-helper-signal-") as directory:
                parent = Path(directory)
                environment = dict(os.environ)
                environment["HELPER_DIR"] = str(Path(__file__).resolve().parent)
                environment["WORKSPACE_PARENT"] = str(parent)
                process = subprocess.Popen(
                    [sys.executable, "-c", child_script],
                    cwd=parent,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                started = parent / "download-started"
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists())
                os.killpg(process.pid, signum)
                stdout, stderr = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertFalse((parent / "post-signal").exists())
                self.assertEqual([path.name for path in parent.iterdir()], ["download-started"])

    def test_signal_after_preparation_cleans_context_once_and_stops_consumer(self) -> None:
        child_script = """
import io
import os
import signal
import sys
import tarfile
import time
from pathlib import Path
sys.path.insert(0, os.environ["HELPER_DIR"])
import prepare_sources as prepare

parent = Path(os.environ["WORKSPACE_PARENT"])
counter = parent / "cleanup-count"
original_cleanup = prepare.cleanup_workspace

def counted_cleanup(workspace):
    current = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(current + 1), encoding="utf-8")
    return original_cleanup(workspace)

prepare.cleanup_workspace = counted_cleanup
spec = prepare.RepositorySpec("fixture", "Talus-Network/fixture", "abc123", ("README.md",))

def download(_url, destination):
    with tarfile.open(destination, mode="w:gz") as archive:
        info = tarfile.TarInfo("fixture-abc123")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        content = b"fixture source\\n"
        info = tarfile.TarInfo("fixture-abc123/README.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

with prepare._prepared_sources_for_specs(
    specs={"fixture": spec}, downloader=download, workspace_parent=parent
) as manifest:
    workspace = Path(str(manifest["workspace"]))
    (parent / "prepared").write_text(str(workspace), encoding="utf-8")
    time.sleep(30)
    (parent / "post-signal").write_text("continued", encoding="utf-8")
"""
        for signum in (signal.SIGINT, signal.SIGTERM):
            with tempfile.TemporaryDirectory(prefix="source-helper-context-signal-") as directory:
                parent = Path(directory)
                environment = dict(os.environ)
                environment["HELPER_DIR"] = str(Path(__file__).resolve().parent)
                environment["WORKSPACE_PARENT"] = str(parent)
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", child_script],
                    cwd=parent,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                prepared = parent / "prepared"
                deadline = time.monotonic() + 5
                while not prepared.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(prepared.exists())
                workspace = Path(prepared.read_text(encoding="utf-8"))
                os.killpg(process.pid, signum)
                stdout, stderr = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual((parent / "cleanup-count").read_text(encoding="utf-8"), "1")
                self.assertFalse(workspace.exists())
                self.assertFalse((parent / "post-signal").exists())

    def test_signal_during_preparation_to_consumer_transition_is_guarded(self) -> None:
        child_script = """
import io
import os
import signal
import sys
import tarfile
import time
from pathlib import Path
sys.path.insert(0, os.environ["HELPER_DIR"])
import prepare_sources as prepare

parent = Path(os.environ["WORKSPACE_PARENT"])
counter = parent / "cleanup-count"
original_cleanup = prepare.cleanup_workspace

def counted_cleanup(workspace):
    current = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(current + 1), encoding="utf-8")
    return original_cleanup(workspace)

prepare.cleanup_workspace = counted_cleanup
original_prepare = prepare._prepare_sources_for_specs_unprotected

def wrapped_prepare(*args, **kwargs):
    manifest = original_prepare(*args, **kwargs)
    (parent / "prepared").write_text(str(manifest["workspace"]), encoding="utf-8")
    time.sleep(30)
    return manifest

prepare._prepare_sources_for_specs_unprotected = wrapped_prepare
spec = prepare.RepositorySpec("fixture", "Talus-Network/fixture", "abc123", ("README.md",))

def download(_url, destination):
    with tarfile.open(destination, mode="w:gz") as archive:
        info = tarfile.TarInfo("fixture-abc123")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        content = b"fixture source\\n"
        info = tarfile.TarInfo("fixture-abc123/README.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

with prepare._prepared_sources_for_specs(
    specs={"fixture": spec}, downloader=download, workspace_parent=parent
) as _manifest:
    (parent / "post-signal").write_text("continued", encoding="utf-8")
"""
        for signum in (signal.SIGINT, signal.SIGTERM):
            with tempfile.TemporaryDirectory(prefix="source-helper-transition-") as directory:
                parent = Path(directory)
                environment = dict(os.environ)
                environment["HELPER_DIR"] = str(Path(__file__).resolve().parent)
                environment["WORKSPACE_PARENT"] = str(parent)
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", child_script],
                    cwd=parent,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                prepared = parent / "prepared"
                deadline = time.monotonic() + 5
                while not prepared.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(prepared.exists())
                workspace = Path(prepared.read_text(encoding="utf-8"))
                os.killpg(process.pid, signum)
                stdout, stderr = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual((parent / "cleanup-count").read_text(encoding="utf-8"), "1")
                self.assertFalse(workspace.exists())
                self.assertFalse((parent / "post-signal").exists())

    def test_cleanup_failure_does_not_replace_primary_exception(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-cleanup-primary-") as parent_name:
            workspace: Path | None = None

            def remember(manifest):
                nonlocal workspace
                workspace = Path(str(manifest["workspace"]))

            with mock.patch.object(
                prepare_sources_module, "cleanup_workspace", side_effect=RuntimeError("cleanup boom")
            ) as cleanup:
                with self.assertRaisesRegex(ValueError, "primary failure") as raised:
                    with prepared_sources(
                        specs={"fixture": self.spec},
                        downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                        workspace_parent=Path(parent_name),
                    ) as manifest:
                        remember(manifest)
                        raise ValueError("primary failure")
                self.assertEqual(cleanup.call_count, 1)
                self.assertTrue(any("cleanup boom" in note for note in raised.exception.__notes__))
            if workspace is not None:
                import shutil

                shutil.rmtree(workspace, ignore_errors=True)

    def test_cleanup_failure_is_raised_after_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-cleanup-only-") as parent_name:
            workspace: Path | None = None
            with mock.patch.object(
                prepare_sources_module, "cleanup_workspace", side_effect=RuntimeError("cleanup only")
            ) as cleanup:
                with self.assertRaisesRegex(RuntimeError, "cleanup only"):
                    with prepared_sources(
                        specs={"fixture": self.spec},
                        downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                        workspace_parent=Path(parent_name),
                    ) as manifest:
                        workspace = Path(str(manifest["workspace"]))
                self.assertEqual(cleanup.call_count, 1)
            if workspace is not None:
                import shutil

                shutil.rmtree(workspace, ignore_errors=True)

    def test_cleanup_manifest_ignores_corrupt_source_records(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-cleanup-record-") as parent_name:
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=Path(parent_name),
            )
            manifest_path = Path(str(manifest["manifest"]))
            workspace = Path(str(manifest["workspace"]))
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["repositories"]["fixture"]["source_tree_sha256"] = "broken"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            cleanup_manifest(manifest_path)
            self.assertFalse(workspace.exists())

    def test_cleanup_manifest_ignores_missing_archive_and_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-cleanup-missing-") as parent_name:
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=Path(parent_name),
            )
            manifest_path = Path(str(manifest["manifest"]))
            workspace = Path(str(manifest["workspace"]))
            record = manifest["repositories"]["fixture"]
            (workspace / str(record["archive_relative"])).unlink(missing_ok=True)
            (workspace / str(record["source_tree_manifest_relative"])).unlink(missing_ok=True)
            cleanup_manifest(manifest_path)
            self.assertFalse(workspace.exists())

    def test_cleanup_workspace_is_fallback_for_malformed_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-cleanup-fallback-") as parent_name:
            manifest = prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=Path(parent_name),
            )
            manifest_path = Path(str(manifest["manifest"]))
            workspace = Path(str(manifest["workspace"]))
            manifest_path.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(SourcePreparationError, "--workspace"):
                cleanup_manifest(manifest_path)
            cleanup_workspace(workspace)
            self.assertFalse(workspace.exists())

    def test_cleanup_refuses_an_unmarked_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-helper-cleanup-refusal-") as parent_name:
            workspace = Path(parent_name) / "skills-sources-unmarked"
            workspace.mkdir()
            with self.assertRaisesRegex(SourcePreparationError, "marker|unrecognised"):
                cleanup_workspace(workspace)
            self.assertTrue(workspace.exists())

    def test_custom_workspace_parent_cannot_escape_runtime_temp_root(self) -> None:
        with self.assertRaisesRegex(SourcePreparationError, "inside the runtime temporary directory"):
            prepare_sources(
                specs={"fixture": self.spec},
                downloader=self.fake_downloader(archive_bytes("fixture-abc123")),
                workspace_parent=Path.cwd(),
            )


if __name__ == "__main__":
    unittest.main()
