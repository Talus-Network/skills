#!/usr/bin/env python3
"""Regression tests for SKILL.md frontmatter validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate_skills.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_skills_frontmatter", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


class SkillFrontmatterTests(unittest.TestCase):
    def test_tap_description_is_valid_standard_yaml(self) -> None:
        text = (ROOT / "nexus-tap-development/SKILL.md").read_text(encoding="utf-8")
        metadata = VALIDATOR.frontmatter(text, ROOT / "nexus-tap-development/SKILL.md")
        self.assertEqual(metadata["name"], "nexus-tap-development")
        self.assertIn("applications: Move packages", metadata["description"])

    def test_unquoted_colon_in_plain_scalar_is_rejected(self) -> None:
        text = "---\nname: example\ndescription: Build applications: Move packages\n---\n"
        with self.assertRaisesRegex(ValueError, "invalid YAML frontmatter|unquoted"):
            VALIDATOR.frontmatter(text, Path("example/SKILL.md"))

    def test_fallback_parser_accepts_the_quoted_tap_description(self) -> None:
        text = (ROOT / "nexus-tap-development/SKILL.md").read_text(encoding="utf-8")
        previous = VALIDATOR._yaml
        VALIDATOR._yaml = None
        try:
            metadata = VALIDATOR.frontmatter(text, ROOT / "nexus-tap-development/SKILL.md")
        finally:
            VALIDATOR._yaml = previous
        self.assertIn("applications: Move packages", metadata["description"])

    def test_fallback_parser_ignores_comment_only_lines(self) -> None:
        text = (
            "---\n"
            "# description follows\n"
            "name: sample\n"
            "description: \"Clear, valid description\"\n"
            "---\n"
            "Body\n"
        )
        previous = VALIDATOR._yaml
        VALIDATOR._yaml = None
        try:
            metadata = VALIDATOR.frontmatter(text, Path("sample/SKILL.md"))
        finally:
            VALIDATOR._yaml = previous
        self.assertEqual(metadata, {"name": "sample", "description": "Clear, valid description"})

    def test_fallback_parser_rejects_unclosed_quoted_scalar(self) -> None:
        text = (
            "---\n"
            "name: sample\n"
            "description: \"Unclosed description\n"
            "---\n"
            "Body\n"
        )
        previous = VALIDATOR._yaml
        VALIDATOR._yaml = None
        try:
            with self.assertRaisesRegex(ValueError, "unclosed quoted"):
                VALIDATOR.frontmatter(text, Path("sample/SKILL.md"))
        finally:
            VALIDATOR._yaml = previous

    def test_fallback_parser_preserves_hash_inside_quoted_scalar(self) -> None:
        text = (
            "---\n"
            "name: sample\n"
            "description: \"Clear # valid description\"\n"
            "---\n"
        )
        previous = VALIDATOR._yaml
        VALIDATOR._yaml = None
        try:
            metadata = VALIDATOR.frontmatter(text, Path("sample/SKILL.md"))
        finally:
            VALIDATOR._yaml = previous
        self.assertEqual(metadata["description"], "Clear # valid description")

    def test_quoted_colon_and_folded_description_are_accepted(self) -> None:
        text = (
            "---\n"
            "name: example\n"
            "description: \"Build applications: Move packages\"\n"
            "metadata:\n"
            "  short-description: >-\n"
            "    A short description: with punctuation\n"
            "---\n"
        )
        metadata = VALIDATOR.frontmatter(text, Path("example/SKILL.md"))
        self.assertEqual(metadata["description"], "Build applications: Move packages")
        self.assertEqual(metadata["metadata"]["short-description"], "A short description: with punctuation")

    def test_fallback_parser_rejects_same_invalid_plain_scalar(self) -> None:
        text = "---\nname: example\ndescription: Build applications: Move packages\n---\n"
        previous = VALIDATOR._yaml
        VALIDATOR._yaml = None
        try:
            with self.assertRaisesRegex(ValueError, "unquoted"):
                VALIDATOR.frontmatter(text, Path("example/SKILL.md"))
        finally:
            VALIDATOR._yaml = previous


if __name__ == "__main__":
    unittest.main()
