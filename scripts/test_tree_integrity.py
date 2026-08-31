"""Adversarial tests for descriptor-safe tree authentication."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tree_integrity
from tree_integrity import (
    TreeIntegrityError,
    copy_verified_tree,
    tree_digest,
    tree_entries,
    validate_root,
    validate_tree_manifest,
    write_tree_manifest,
)


class TreeIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tree-integrity-")
        self.root = Path(self.directory.name) / "root"
        self.root.mkdir()
        self.payload = self.root / "a-payload.txt"
        self.payload.write_bytes(b"0123456789")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "child.txt").write_bytes(b"child")
        self.target = self.root / "z-target.txt"
        self.target.write_bytes(b"target")
        self.link = self.root / "link.txt"
        self.link.symlink_to(self.target.name)
        self.manifest = Path(self.directory.name) / "root-tree.json"
        write_tree_manifest(self.root, self.manifest)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_stable_tree_and_manifest_round_trip(self) -> None:
        entries = tree_entries(self.root)
        evidence = validate_tree_manifest(self.root, self.manifest, tree_digest(entries))
        self.assertEqual(evidence["entries"], len(entries))
        self.assertEqual(evidence["tree_sha256"], tree_digest(entries))
        symlink = next(entry for entry in entries if entry["path"] == "link.txt")
        self.assertEqual(symlink["target_path"], "z-target.txt")
        self.assertEqual(symlink["target_sha256"], next(entry for entry in entries if entry["path"] == "z-target.txt")["sha256"])

    def test_same_size_rewrite_during_read_is_rejected(self) -> None:
        original_read = os.read
        changed = False

        def racing_read(fd: int, size: int) -> bytes:
            nonlocal changed
            data = original_read(fd, size)
            if data and not changed:
                changed = True
                self.payload.write_bytes(b"abcdefghij")
            return data

        with mock.patch("tree_integrity.os.read", side_effect=racing_read):
            with self.assertRaisesRegex(TreeIntegrityError, "changed"):
                tree_entries(self.root)

    def test_regular_file_to_symlink_swap_during_read_is_rejected(self) -> None:
        original_read = os.read
        swapped = False

        def racing_read(fd: int, size: int) -> bytes:
            nonlocal swapped
            data = original_read(fd, size)
            if data and not swapped:
                swapped = True
                self.payload.unlink()
                self.payload.symlink_to(self.target.name)
            return data

        with mock.patch("tree_integrity.os.read", side_effect=racing_read):
            with self.assertRaisesRegex(TreeIntegrityError, "changed"):
                tree_entries(self.root)

    def test_hardlink_alias_is_rejected(self) -> None:
        os.link(self.target, self.root / "target-alias.txt")
        with self.assertRaisesRegex(TreeIntegrityError, "hardlink/path collision"):
            tree_entries(self.root)

    def test_special_file_is_rejected(self) -> None:
        fifo = self.root / "named-pipe"
        os.mkfifo(fifo)
        try:
            with self.assertRaisesRegex(TreeIntegrityError, "special file"):
                tree_entries(self.root)
        finally:
            fifo.unlink(missing_ok=True)

    def test_symlink_escape_and_cycle_are_rejected(self) -> None:
        outside = Path(self.directory.name) / "outside.txt"
        outside.write_bytes(b"outside")
        self.link.unlink()
        self.link.symlink_to(os.path.relpath(outside, self.root))
        with self.assertRaisesRegex(TreeIntegrityError, "escapes"):
            tree_entries(self.root)
        self.link.unlink()
        self.link.symlink_to(self.link.name)
        with self.assertRaisesRegex(TreeIntegrityError, "cyclic"):
            tree_entries(self.root)

    def test_symlink_root_alias_is_rejected_by_every_root_entry_point(self) -> None:
        alias = Path(self.directory.name) / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        expected = tree_digest(tree_entries(self.root))
        with self.assertRaisesRegex(TreeIntegrityError, "non-symlink|symlink"):
            validate_root(alias)
        with self.assertRaisesRegex(TreeIntegrityError, "non-symlink|symlink"):
            tree_entries(alias)
        with self.assertRaisesRegex(TreeIntegrityError, "non-symlink|symlink"):
            write_tree_manifest(alias, Path(self.directory.name) / "alias-tree.json")
        with self.assertRaisesRegex(TreeIntegrityError, "non-symlink|symlink"):
            validate_tree_manifest(alias, self.manifest, expected)
        with self.assertRaisesRegex(TreeIntegrityError, "non-symlink|symlink"):
            copy_verified_tree(alias, Path(self.directory.name) / "alias-copy", expected)

    def test_renamed_root_path_alias_is_rejected(self) -> None:
        moved = Path(self.directory.name) / "renamed-root"
        self.root.rename(moved)
        self.root.symlink_to(moved, target_is_directory=True)
        try:
            with self.assertRaisesRegex(TreeIntegrityError, "non-symlink|symlink|alias"):
                tree_entries(self.root)
        finally:
            self.root.unlink()
            moved.rename(self.root)

    def test_root_swap_during_walk_is_rejected(self) -> None:
        moved = Path(self.directory.name) / "swapped-root"
        swapped = False

        original_verify = tree_integrity._verify_root_descriptor

        def replace_root(descriptor, label: str = "tree root") -> None:
            nonlocal swapped
            if not swapped:
                swapped = True
                self.root.rename(moved)
                self.root.symlink_to(moved, target_is_directory=True)
            original_verify(descriptor, label)

        try:
            with mock.patch("tree_integrity._verify_root_descriptor", side_effect=replace_root):
                with self.assertRaisesRegex(TreeIntegrityError, "tree root"):
                    tree_entries(self.root)
        finally:
            if self.root.is_symlink():
                self.root.unlink()
            if moved.exists():
                moved.rename(self.root)


if __name__ == "__main__":
    unittest.main()
