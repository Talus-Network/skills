#!/usr/bin/env python3
"""Regression tests for the public Skills README source bootstrap."""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "prepare_sources.py"
README_PATH = ROOT / "README.md"


def load_helper():
    sys.path.insert(0, str(HELPER_PATH.parent))
    spec = importlib.util.spec_from_file_location("skills_readme_prepare_sources", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load source helper: {HELPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SkillsReadmeCommandTests(unittest.TestCase):
    def test_public_install_commands_are_release_only(self) -> None:
        text = README_PATH.read_text(encoding="utf-8")
        selector = "Talus-Network/" + "skills"
        block_match = re.search(
            r"## Install from the public repository\n\n.*?```bash\n(.*?)\n```",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(block_match)
        block = block_match.group(1)
        syntax = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        commands = [line for line in block.splitlines() if line.startswith("npx skills add ")]
        self.assertEqual(len(commands), 1)
        for command in commands:
            self.assertEqual(command, "npx skills add " + selector)
        self.assertIn("canonical command is release/install UX only", text)
        self.assertIn("SKILLS_BUNDLE_ROOT", text)
        self.assertNotIn(selector, (ROOT / "scripts/prepare_sources.py").read_text(encoding="utf-8"))
        self.assertNotIn(selector, (ROOT / "scripts/forward_portability.py").read_text(encoding="utf-8"))

    def test_repository_local_entrypoint_runs_without_network(self) -> None:
        text = README_PATH.read_text(encoding="utf-8")
        block_match = re.search(r"## Use from a local checkout\n\n```bash\n(.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(block_match)
        block = block_match.group(1)
        syntax = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertNotIn("npx " + "skills add", block)
        environment = dict(os.environ)
        environment["SKILLS_BUNDLE_ROOT"] = str(ROOT)
        result = subprocess.run(["bash", "-c", block], cwd=ROOT, env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_published_setup_authority_is_explicit_and_live_checked(self) -> None:
        text = README_PATH.read_text(encoding="utf-8")
        self.assertIn("https://" + "docs.talus.network/guides/getting-started/setup", text)
        self.assertIn("scripts/docs_website.py", text)
        self.assertIn("--expected-version v2.0.0", text)
        self.assertIn("fails closed", text)
        website_source = (ROOT / "scripts/docs_website.py").read_text(encoding="utf-8")
        self.assertIn("CANONICAL_SETUP_URL", website_source)
        self.assertIn("EXPECTED_SETUP_VERSION", website_source)
        self.assertNotIn("github.com", website_source)

    def test_public_bootstrap_extracts_and_preflights_against_helper(self) -> None:
        text = README_PATH.read_text(encoding="utf-8")
        block_match = re.search(r"## Portable source evidence\n\n.*?```bash\n(.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(block_match)
        block = block_match.group(1)
        syntax = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn('SKILLS_BUNDLE_ROOT="${SKILLS_BUNDLE_ROOT:?', block)
        self.assertIn('SOURCE_HELPER="$SKILLS_BUNDLE_ROOT/scripts/prepare_sources.py"', block)
        prepare_line = next(line for line in block.splitlines() if " prepare --only " in line and "--print-manifest-path" in line)
        command = prepare_line.split("python3 ", 1)[1]
        command = command[: command.rfind(")")]
        tokens = shlex.split(command)
        self.assertEqual(tokens[0], "$SOURCE_HELPER")
        helper = load_helper()
        args = helper._parser().parse_args(tokens[1:])
        self.assertEqual(args.command, "prepare")
        self.assertEqual(args.only, ["nexus-sdk", "nexus-move-packages", "sui"])
        self.assertEqual(args.repo, [])
        self.assertEqual(args.checksum, [])
        names = [spec.logical_name for spec in helper._normalise_specs(None, {}, {}, args.only)]
        self.assertEqual(names, ["nexus-move-packages", "nexus-sdk", "sui"])
        self.assertIn("--only nexus-sdk --only nexus-move-packages --only sui", prepare_line)
        self.assertNotIn("prepare --print-manifest-path", block)
        self.assertEqual(args.only, ["nexus-sdk", "nexus-move-packages", "sui"])
        self.assertLess(block.index("trap cleanup_sources"), block.index("SDK_ROOT=", block.index("SOURCE_MANIFEST=")))

    def test_changed_ref_without_checksum_is_rejected_by_real_helper(self) -> None:
        helper = load_helper()
        with self.assertRaises(helper.SourcePreparationError):
            helper._normalise_specs(None, {"nexus-sdk": "reviewed-feature"}, {}, ["nexus-sdk"])

    def test_public_source_bootstraps_clean_once_and_exit_on_signal(self) -> None:
        skill_paths = (
            ROOT / "scripts/README.md",
            ROOT / "nexus-offchain-tool-development/SKILL.md",
            ROOT / "nexus-tap-development/SKILL.md",
            ROOT / "nexus-onchain-task-debugging/SKILL.md",
            ROOT / "nexus-onchain-tool-development/SKILL.md",
            ROOT / "nexus-cli-payment-tracking/SKILL.md",
        )
        with tempfile.TemporaryDirectory(prefix="skills-trap-test-") as directory:
            directory_path = Path(directory)
            fake_python = directory_path / "python3"
            log_path = directory_path / "calls.log"
            marker_path = directory_path / "post-signal"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
                "if [ \"$1\" = \"-c\" ]; then printf '%s\\n' /tmp/fake-workspace; exit 0; fi\n"
                "case \"$2\" in\n"
                "  prepare) if [ \"${FAIL_PREPARE:-0}\" -ne 0 ]; then exit \"$FAIL_PREPARE\"; fi; printf '%s\\n' /tmp/fake-manifest ;;\n"
                "  root) printf '%s\\n' /tmp/fake-root ;;\n"
                "  -c) printf '%s\\n' /tmp/fake-workspace ;;\n"
                "  cleanup) if [ \"${FAIL_CLEANUP:-0}\" -ne 0 ]; then printf '%s\\n' cleanup-failed >&2; exit \"$FAIL_CLEANUP\"; fi; exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = f"{directory}:{environment['PATH']}"
            environment["FAKE_LOG"] = str(log_path)
            environment["SKILLS_BUNDLE_ROOT"] = str(ROOT)
            for skill_path in skill_paths:
                text = skill_path.read_text(encoding="utf-8")
                block_match = re.search(
                    r"```bash\n(.*?trap cleanup_sources EXIT.*?trap 'cleanup_sources 143' TERM.*?SOURCE_MANIFEST=.*?)\n```",
                    text,
                    re.DOTALL,
                )
                self.assertIsNotNone(block_match, skill_path)
                block = block_match.group(1)
                if skill_path == ROOT / "scripts/README.md":
                    block = block.replace("<skills-bundle>", str(ROOT))
                syntax = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True, check=False)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)
                self.assertLess(block.index("trap cleanup_sources EXIT"), block.index('SOURCE_MANIFEST="$(python3'))
                self.assertIn('if [ -n "${SOURCE_MANIFEST:-}" ]', block)
                log_path.unlink(missing_ok=True)
                normal = subprocess.run(
                    ["bash", "-c", block], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
                )
                self.assertEqual(normal.returncode, 0, normal.stderr)
                calls = log_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(sum(" cleanup " in f" {call} " for call in calls), 1, skill_path)
                log_path.unlink(missing_ok=True)
                marker_path.unlink(missing_ok=True)
                signalled = subprocess.run(
                    ["bash", "-c", block + f"\nkill -TERM $$\nprintf post > {shlex.quote(str(marker_path))}\n"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertIn(signalled.returncode, (0, 130, 143), skill_path)
                calls = log_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(sum(" cleanup " in f" {call} " for call in calls), 1, skill_path)
                self.assertFalse(marker_path.exists(), skill_path)

    def test_cleanup_failure_is_reported_without_masking_main_status(self) -> None:
        skill_paths = (
            ROOT / "README.md",
            ROOT / "scripts/README.md",
            ROOT / "nexus-offchain-tool-development/SKILL.md",
            ROOT / "nexus-tap-development/SKILL.md",
            ROOT / "nexus-onchain-task-debugging/SKILL.md",
            ROOT / "nexus-onchain-tool-development/SKILL.md",
            ROOT / "nexus-cli-payment-tracking/SKILL.md",
        )
        with tempfile.TemporaryDirectory(prefix="skills-cleanup-failure-test-") as directory:
            directory_path = Path(directory)
            fake_python = directory_path / "python3"
            log_path = directory_path / "calls.log"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
                "if [ \"$1\" = \"-c\" ]; then printf '%s\\n' /tmp/fake-workspace; exit 0; fi\n"
                "case \"$2\" in\n"
                "  prepare) printf '%s\\n' /tmp/fake-manifest ;;\n"
                "  root) printf '%s\\n' /tmp/fake-root ;;\n"
                "  -c) printf '%s\\n' /tmp/fake-workspace ;;\n"
                "  cleanup) printf '%s\\n' cleanup-failed >&2; exit 23 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = f"{directory}:{environment['PATH']}"
            environment["FAKE_LOG"] = str(log_path)
            environment["SKILLS_BUNDLE_ROOT"] = str(ROOT)
            for skill_path in skill_paths:
                if skill_path == ROOT / "README.md":
                    block_match = re.search(
                        r"## Portable source evidence\n\n.*?```bash\n(.*?)\n```",
                        skill_path.read_text(encoding="utf-8"),
                        re.DOTALL,
                    )
                else:
                    block_match = re.search(
                        r"```bash\n(.*?trap cleanup_sources EXIT.*?trap 'cleanup_sources 143' TERM.*?SOURCE_MANIFEST=.*?)\n```",
                        skill_path.read_text(encoding="utf-8"),
                        re.DOTALL,
                    )
                self.assertIsNotNone(block_match, skill_path)
                block = block_match.group(1)
                if skill_path == ROOT / "scripts/README.md":
                    block = block.replace("<skills-bundle>", str(ROOT))
                self.assertEqual(subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True).returncode, 0)

                environment["FAIL_CLEANUP"] = "23"
                normal = subprocess.run(
                    ["bash", "-c", block], cwd=ROOT, env=environment, capture_output=True, text=True, check=False
                )
                self.assertEqual(normal.returncode, 23, skill_path)
                self.assertIn("source cleanup failed", normal.stderr, skill_path)
                self.assertIn("status 23", normal.stderr, skill_path)

                failed = subprocess.run(
                    ["bash", "-c", block + "\nfalse\n"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(failed.returncode, 1, skill_path)
                self.assertIn("source cleanup failed", failed.stderr, skill_path)

                for signal_name, expected_status in (("INT", (130,)), ("TERM", (143,))):
                    signalled = subprocess.run(
                        ["bash", "-c", block + f"\nkill -{signal_name} $$\nprintf post-signal\n"],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertIn(signalled.returncode, expected_status, (skill_path, signal_name))
                    self.assertIn("source cleanup failed", signalled.stderr, (skill_path, signal_name))

    def test_prepare_and_each_root_failure_stop_before_dependent_commands(self) -> None:
        bootstrap_paths = (
            ROOT / "README.md",
            ROOT / "scripts/README.md",
            ROOT / "nexus-offchain-tool-development/SKILL.md",
            ROOT / "nexus-tap-development/SKILL.md",
            ROOT / "nexus-onchain-task-debugging/SKILL.md",
            ROOT / "nexus-onchain-tool-development/SKILL.md",
            ROOT / "nexus-cli-payment-tracking/SKILL.md",
        )
        with tempfile.TemporaryDirectory(prefix="skills-bootstrap-failure-test-") as directory:
            directory_path = Path(directory)
            fake_python = directory_path / "python3"
            log_path = directory_path / "calls.log"
            continuation_path = directory_path / "continued"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s\n' \"$*\" >> \"$FAKE_LOG\"\n"
                "if [ \"$1\" = \"-c\" ]; then printf '%s\n' /tmp/fake-workspace; exit 0; fi\n"
                "case \"$2\" in\n"
                "  prepare) if [ \"${FAIL_PREPARE:-0}\" -ne 0 ]; then exit \"$FAIL_PREPARE\"; fi; printf '%s\n' /tmp/fake-manifest ;;\n"
                "  root) if [ \"${FAIL_ROOT:-}\" = \"$6\" ]; then printf '%s\n' stale-root; exit \"${FAIL_ROOT_STATUS:-41}\"; fi; printf '%s\n' /tmp/fake-root ;;\n"
                "  -c) printf '%s\n' /tmp/fake-workspace ;;\n"
                "  cleanup) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = f"{directory}:{environment['PATH']}"
            environment["FAKE_LOG"] = str(log_path)
            environment["SKILLS_BUNDLE_ROOT"] = str(ROOT)
            for bootstrap_path in bootstrap_paths:
                if bootstrap_path == ROOT / "README.md":
                    block_match = re.search(
                        r"## Portable source evidence\n\n.*?```bash\n(.*?)\n```",
                        bootstrap_path.read_text(encoding="utf-8"),
                        re.DOTALL,
                    )
                else:
                    block_match = re.search(
                        r"```bash\n(.*?trap cleanup_sources EXIT.*?trap 'cleanup_sources 143' TERM.*?SOURCE_MANIFEST=.*?)\n```",
                        bootstrap_path.read_text(encoding="utf-8"),
                        re.DOTALL,
                    )
                self.assertIsNotNone(block_match, bootstrap_path)
                block = block_match.group(1)
                if bootstrap_path == ROOT / "scripts/README.md":
                    block = block.replace("<skills-bundle>", str(ROOT))
                self.assertEqual(
                    subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True).returncode,
                    0,
                )

                log_path.unlink(missing_ok=True)
                continuation_path.unlink(missing_ok=True)
                environment.pop("FAIL_ROOT", None)
                environment["FAIL_PREPARE"] = "37"
                prepared = subprocess.run(
                    ["bash", "-c", block + f"\nprintf continued > {shlex.quote(str(continuation_path))}\n"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(prepared.returncode, 37, bootstrap_path)
                self.assertFalse(continuation_path.exists(), bootstrap_path)
                prepare_calls = log_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(sum(" prepare " in f" {call} " for call in prepare_calls), 1, bootstrap_path)
                self.assertEqual(sum(" root " in f" {call} " for call in prepare_calls), 0, bootstrap_path)
                self.assertEqual(sum(" cleanup " in f" {call} " for call in prepare_calls), 0, bootstrap_path)

                environment.pop("FAIL_PREPARE", None)
                for root_name, expected_status in (
                    ("nexus-sdk", 41),
                    ("nexus-move-packages", 42),
                    ("sui", 43),
                ):
                    log_path.unlink(missing_ok=True)
                    continuation_path.unlink(missing_ok=True)
                    environment["FAIL_ROOT"] = root_name
                    environment["FAIL_ROOT_STATUS"] = str(expected_status)
                    failed_root = subprocess.run(
                        [
                            "bash",
                            "-c",
                            "set +e\n" + block + f"\nprintf continued > {shlex.quote(str(continuation_path))}\n",
                        ],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(failed_root.returncode, expected_status, (bootstrap_path, root_name))
                    self.assertFalse(continuation_path.exists(), (bootstrap_path, root_name))
                    root_calls = log_path.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(sum(" prepare " in f" {call} " for call in root_calls), 1, (bootstrap_path, root_name))
                    self.assertEqual(sum(f"--repo {root_name}" in call for call in root_calls), 1, (bootstrap_path, root_name))
                    self.assertEqual(sum(" cleanup " in f" {call} " for call in root_calls), 1, (bootstrap_path, root_name))
                    self.assertNotIn("stale-root", failed_root.stderr, (bootstrap_path, root_name))
                environment.pop("FAIL_ROOT", None)
                environment.pop("FAIL_ROOT_STATUS", None)


if __name__ == "__main__":
    unittest.main()
