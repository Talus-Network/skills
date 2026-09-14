#!/usr/bin/env python3
"""Plan, replay, and run small evidence-oriented Skill evaluation catalogs.

The runner is deliberately offline and standard-library only. It executes only
an explicit command supplied by the caller and cannot reproduce Codex's live
automatic selection. Structured invocation events are the only activation
evidence; prompt or assistant prose is never treated as proof.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = (
    "nexus-cli-payment-tracking",
    "nexus-onchain-task-debugging",
    "nexus-onchain-tool-development",
    "nexus-offchain-tool-development",
    "nexus-tap-development",
)
TRIGGERS = frozenset({"explicit", "implicit", "contextual", "negative"})
TERMINAL_TYPES = frozenset(
    {"response.completed", "run.completed", "task.completed", "turn.completed"}
)
TERMINAL_SUCCESS_STATUSES = frozenset(
    {"complete", "completed", "success", "succeeded"}
)
INVOCATION_TYPES = frozenset(
    {"skill_call", "skill_invocation", "skill_invoked", "skill_use", "skill_used"}
)
RUBRIC_PREFIX = "rubric:"

BUNDLE_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "answers",
        "evals",
        "expected",
        "expected_answers",
        "golden",
        "node_modules",
        "target",
    }
)
BUNDLE_IGNORED_FILES = frozenset({"run_skill_evals.py"})
BUNDLE_TEXT_SUFFIXES = frozenset(
    {".md", ".json", ".move", ".py", ".rs", ".toml", ".txt"}
)
HOSTNAME_LABEL_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


class CatalogError(ValueError):
    """Raised when a catalog or evaluation layout is unsafe."""


@dataclass(frozen=True)
class Case:
    skill: str
    case_id: str
    prompt: str
    trigger: str
    should_trigger: bool
    sources: tuple[str, ...]
    expected_output: str
    expectations: tuple[str, ...]
    artifacts: tuple[dict[str, Any], ...]
    expected_skill: str | None = None


SAFE_CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{path}: invalid JSON: {exc}") from exc


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _within(path: Path, parent: Path) -> bool:
    try:
        _resolved(path).relative_to(_resolved(parent))
    except ValueError:
        return False
    return True


def _overlaps(left: Path, right: Path) -> bool:
    return _within(left, right) or _within(right, left)


def _relative_path(raw: str, where: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise CatalogError(f"{where}: path must be relative and stay in its workspace")
    return relative



def _validate_case_id(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or SAFE_CASE_ID_RE.fullmatch(raw) is None:
        raise CatalogError(
            f"{where}: case id must be a single safe path component"
        )
    return raw


def _validate_expected_skill(
    raw: Any,
    skill: str,
    should_trigger: bool,
    where: str,
) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in EXPECTED_SKILLS:
        raise CatalogError(f"{where}: expected_skill must name a known sibling skill")
    if raw == skill:
        raise CatalogError(f"{where}: expected_skill must differ from owning skill")
    if should_trigger:
        raise CatalogError(
            f"{where}: expected_skill requires should_trigger=false for sibling routing"
        )
    return raw

def _validate_https_source(source: str, where: str) -> None:
    if not isinstance(source, str):
        raise CatalogError(f"{where}: HTTPS source must be a string")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in source
    ):
        raise CatalogError(
            f"{where}: HTTPS source contains whitespace or control characters"
        )
    try:
        parsed = urlsplit(source)
    except ValueError as exc:
        raise CatalogError(
            f"{where}: HTTPS source has malformed authority: {source}"
        ) from exc
    if parsed.scheme != "https" or not parsed.netloc:
        raise CatalogError(
            f"{where}: HTTPS source must have an HTTPS scheme and host: {source}"
        )
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise CatalogError(
            f"{where}: HTTPS source must not contain credentials: {source}"
        )
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise CatalogError(
            f"{where}: HTTPS source has malformed authority: {source}"
        ) from exc
    if not hostname:
        raise CatalogError(f"{where}: HTTPS source must have a non-empty host: {source}")
    if parsed.netloc.endswith(":"):
        raise CatalogError(f"{where}: HTTPS source has an empty port: {source}")
    normalized_hostname = hostname[:-1] if hostname.endswith(".") else hostname
    try:
        ipaddress.ip_address(normalized_hostname)
    except ValueError:
        labels = normalized_hostname.split(".")
        if (
            not normalized_hostname
            or len(normalized_hostname) > 253
            or not all(HOSTNAME_LABEL_RE.fullmatch(label) for label in labels)
        ):
            raise CatalogError(
                f"{where}: HTTPS source has an invalid host: {source}"
            )


def _source_path(root: Path, skill: str, source: str) -> Path | None:
    if not isinstance(source, str):
        raise CatalogError(f"{skill}: source must be bundled: or HTTPS: {source}")
    if not source.startswith("bundled:"):
        if not source.startswith("https://"):
            raise CatalogError(f"{skill}: source must be bundled: or HTTPS: {source}")
        _validate_https_source(source, f"{skill}: source")
        return None
    relative = _relative_path(
        source.removeprefix("bundled:"), f"{skill}: bundled source"
    )
    candidate = root / relative
    if not _within(candidate, root) or not candidate.is_file():
        raise CatalogError(f"{skill}: bundled source does not exist: {source}")
    for part in relative.parents:
        if (root / part).is_symlink():
            raise CatalogError(f"{skill}: bundled source symlink parent: {source}")
    if candidate.is_symlink():
        raise CatalogError(f"{skill}: bundled source symlink: {source}")
    return candidate


def _case(root: Path, skill: str, raw: Any, index: int) -> Case:
    where = f"{skill}/evals/evals.json[{index}]"
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: case must be an object")
    required = {
        "id",
        "prompt",
        "sources",
        "expected_output",
        "expectations",
        "trigger",
        "should_trigger",
    }
    missing = required - raw.keys()
    if missing:
        raise CatalogError(f"{where}: missing fields: {', '.join(sorted(missing))}")
    case_id = _validate_case_id(raw["id"], where)
    prompt = raw["prompt"]
    trigger = raw["trigger"]
    should_trigger = raw["should_trigger"]
    expected_output = raw["expected_output"]
    expected_skill = _validate_expected_skill(
        raw.get("expected_skill"), skill, should_trigger, where
    )
    if not isinstance(case_id, str) or not case_id.strip():
        raise CatalogError(f"{where}: id must be a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise CatalogError(f"{where}: prompt must be a non-empty string")
    if trigger not in TRIGGERS:
        raise CatalogError(f"{where}: unknown trigger {trigger!r}")
    if not isinstance(should_trigger, bool):
        raise CatalogError(f"{where}: should_trigger must be boolean")
    if trigger == "negative" and should_trigger:
        raise CatalogError(f"{where}: negative cases must have should_trigger=false")
    if not isinstance(expected_output, str) or not expected_output.strip():
        raise CatalogError(f"{where}: expected_output must be a non-empty string")
    sources = raw["sources"]
    if (
        not isinstance(sources, list)
        or not sources
        or not all(isinstance(item, str) and item.strip() for item in sources)
    ):
        raise CatalogError(f"{where}: sources must be a non-empty string list")
    for source in sources:
        _source_path(root, skill, source)
    expectations = raw["expectations"]
    if (
        not isinstance(expectations, list)
        or not expectations
        or not all(isinstance(item, str) and item.strip() for item in expectations)
    ):
        raise CatalogError(f"{where}: expectations must be a non-empty string list")
    artifacts = raw.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise CatalogError(f"{where}: artifacts must be a list")
    normalized_artifacts: list[dict[str, Any]] = []
    for artifact_index, artifact in enumerate(artifacts):
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("path"), str)
            or not artifact["path"].strip()
        ):
            raise CatalogError(
                f"{where}.artifacts[{artifact_index}]: expected an object with path"
            )
        normalized = dict(artifact)
        normalized["path"] = _relative_path(
            artifact["path"], f"{where}.artifacts[{artifact_index}]"
        ).as_posix()
        normalized_artifacts.append(normalized)
    return Case(
        skill=skill,
        case_id=case_id,
        prompt=prompt,
        trigger=trigger,
        should_trigger=should_trigger,
        sources=tuple(sources),
        expected_output=expected_output,
        expectations=tuple(expectations),
        artifacts=tuple(normalized_artifacts),
        expected_skill=expected_skill,
    )


def load_cases(root: Path = ROOT, skills: Iterable[str] | None = None) -> list[Case]:
    root = _resolved(root)
    selected = tuple(dict.fromkeys(EXPECTED_SKILLS if skills is None else skills))
    unknown = sorted(set(selected) - set(EXPECTED_SKILLS))
    if unknown:
        raise CatalogError(f"unknown skill(s): {', '.join(unknown)}")
    cases: list[Case] = []
    seen: set[tuple[str, str]] = set()
    for skill in selected:
        catalog_path = root / skill / "evals" / "evals.json"
        document = _json(catalog_path)
        if not isinstance(document, dict):
            raise CatalogError(
                f"{catalog_path}: catalog root must be a JSON object"
            )
        if document.get("skill_name") != skill:
            raise CatalogError(f"{catalog_path}: skill_name does not match {skill}")
        raw_cases = document.get("evals")
        if not isinstance(raw_cases, list) or len(raw_cases) < 2:
            raise CatalogError(f"{catalog_path}: expected at least two eval cases")
        skill_cases = [
            _case(root, skill, raw, index) for index, raw in enumerate(raw_cases)
        ]
        observed = {(item.trigger, item.should_trigger) for item in skill_cases}
        for trigger in TRIGGERS:
            expected = trigger != "negative"
            if (trigger, expected) not in observed:
                raise CatalogError(f"{catalog_path}: missing {trigger} trigger case")
        for item in skill_cases:
            key = (item.skill, item.case_id)
            if key in seen:
                raise CatalogError(f"duplicate case: {item.skill}/{item.case_id}")
            seen.add(key)
        cases.extend(skill_cases)
    return cases


def _event_value(event: dict[str, Any], key: str) -> Any:
    if key in event:
        return event[key]
    item = event.get("item")
    if isinstance(item, dict):
        return item.get(key)
    return None


def _structured_invocation(events: Iterable[dict[str, Any]], skill: str) -> bool:
    for event in events:
        if _event_value(event, "type") not in INVOCATION_TYPES:
            continue
        name = (
            _event_value(event, "skill")
            or _event_value(event, "name")
            or _event_value(event, "skill_name")
        )
        if name == skill:
            return True
    return False


def _recognized_invocations(events: Iterable[dict[str, Any]]) -> set[str]:
    recognized: set[str] = set()
    for event in events:
        if _event_value(event, "type") not in INVOCATION_TYPES:
            continue
        name = (
            _event_value(event, "skill")
            or _event_value(event, "name")
            or _event_value(event, "skill_name")
        )
        if name in EXPECTED_SKILLS:
            recognized.add(name)
    return recognized


def _terminal_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") in TERMINAL_TYPES]


def _terminal_event(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    terminal_events = _terminal_events(events)
    return terminal_events[-1] if terminal_events else None


def _terminal_failures(events: Iterable[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for event in _terminal_events(events):
        failure = _terminal_failure(event)
        if failure is not None:
            failures.append(failure)
    return failures


def _terminal_failure(event: dict[str, Any] | None) -> str | None:
    if event is None:
        return None
    if "status" in event:
        status = event["status"]
        if not isinstance(status, str) or status not in TERMINAL_SUCCESS_STATUSES:
            return f"terminal event reports unsupported or failure status: {status!r}"
    if event.get("success") is False:
        return "terminal event reports failure: success=false"
    if event.get("error"):
        return "terminal event contains an error"
    return None


def _decode_stream(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _read_trace(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"cannot read trace: {exc}"
    if not text:
        return [], "trace is empty"
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            return [], f"trace line {line_number} is empty"
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return [], f"trace line {line_number} is not JSON: {exc}"
        if not isinstance(event, dict):
            return [], f"trace line {line_number} is not an object"
        events.append(event)
    if not events:
        return [], "trace is empty"
    return events, None


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _activation_check(case: Case, events: list[dict[str, Any]]) -> dict[str, str]:
    recognized = _recognized_invocations(events)
    if not recognized:
        return _check(
            "skill_invocation",
            "unknown",
            "no recognized structured invocation event was observed; absence is not proof of non-invocation",
        )

    expected = (
        {case.skill}
        if case.should_trigger
        else {case.expected_skill}
        if case.expected_skill is not None
        else set()
    )
    if recognized == expected:
        label = "skill" if case.should_trigger else "expected sibling skill"
        return _check(
            "skill_invocation",
            "pass",
            f"structured invocation event names the expected {label}",
        )

    if case.should_trigger:
        detail = f"recognized skill selection {sorted(recognized)} does not exactly match owning skill {case.skill}"
    elif case.expected_skill is not None:
        detail = f"recognized skill selection {sorted(recognized)} does not exactly match expected sibling {case.expected_skill}"
    else:
        detail = f"negative control contains recognized skill selection {sorted(recognized)}"
    return _check("skill_invocation", "fail", detail)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_digests(root: Path, case: Case) -> dict[str, str | None]:
    digests: dict[str, str | None] = {}
    skill_path = root / case.skill / "SKILL.md"
    if not skill_path.is_file() or skill_path.is_symlink():
        raise CatalogError(f"selected skill entrypoint missing or symlinked: {skill_path}")
    digests[f"bundled:{case.skill}/SKILL.md"] = _sha256(skill_path)
    for source in case.sources:
        source_path = _source_path(root, case.skill, source)
        digests[source] = _sha256(source_path) if source_path is not None else None
    return digests


def _bundle_manifest(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise CatalogError(f"bundle root is missing, not a directory, or symlinked: {root}")
    manifest: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CatalogError(f"bundle contains symlink: {path}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise CatalogError(f"bundle entry cannot be resolved: {path}") from exc
        if not _within(resolved, root):
            raise CatalogError(f"bundle entry escapes its root: {path}")
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = _sha256(path)
        elif not path.is_dir():
            raise CatalogError(f"bundle entry is not a regular file or directory: {path}")
    return dict(sorted(manifest.items()))


def _bundle_integrity_check(
    root: Path, expected: dict[str, str]
) -> dict[str, str]:
    try:
        actual = _bundle_manifest(root)
    except (CatalogError, OSError) as exc:
        return _check(
            "bundle_integrity",
            "fail",
            f"command-visible bundle could not be validated after command: {exc}",
        )
    if actual == expected:
        return _check(
            "bundle_integrity",
            "pass",
            "command-visible bundle matches the pre-command manifest",
        )
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    modified = sorted(
        path for path in set(actual) & set(expected) if actual[path] != expected[path]
    )
    details = []
    if added:
        details.append(f"added: {', '.join(added)}")
    if removed:
        details.append(f"removed: {', '.join(removed)}")
    if modified:
        details.append(f"modified: {', '.join(modified)}")
    return _check(
        "bundle_integrity",
        "fail",
        "command-visible bundle changed after command ("
        + "; ".join(details)
        + ")",
    )

def _bundle_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.casefold()
        if (
            lowered in BUNDLE_IGNORED_DIRS
            or lowered in BUNDLE_IGNORED_FILES
            or lowered.startswith("expected")
            or lowered.startswith("answer")
            or lowered.startswith("test_")
            or lowered.endswith("_test.py")
            or lowered.endswith(".pyc")
        ):
            ignored.add(name)
    return ignored


def _copy_tree_without_symlinks(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise CatalogError(f"snapshot source is a symlink: {source}")
    if not source.is_dir():
        raise CatalogError(f"snapshot source is not a directory: {source}")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise CatalogError(f"snapshot source contains symlink: {item}")
    shutil.copytree(source, destination, symlinks=False, ignore=_bundle_ignore)


def _copy_file_without_symlinks(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise CatalogError(f"snapshot source is a symlink: {source}")
    if not source.is_file():
        raise CatalogError(f"snapshot source is not a file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise CatalogError(f"refusing overwrite in snapshot: {destination}")
    shutil.copy2(source, destination, follow_symlinks=False)


def _copy_skill_snapshot(source: Path, destination: Path) -> None:
    """Copy bundle guidance while keeping evaluator answers out of command view."""
    _copy_tree_without_symlinks(source, destination)


def _bundle_components(root: Path, case: Case) -> tuple[Path, ...]:
    components: set[Path] = {Path(case.skill)}
    for source in case.sources:
        if not source.startswith("bundled:"):
            continue
        relative = _relative_path(
            source.removeprefix("bundled:"), f"{case.skill}: bundled source"
        )
        if relative.parts:
            components.add(Path(relative.parts[0]))

    # Skill entrypoints explicitly name the shared runtime scripts they expose.
    # Copy the shared production tree, but exclude evaluator/test data.
    for component in tuple(components):
        component_root = root / component
        if not component_root.is_dir():
            continue
        for source_path in component_root.rglob("*"):
            if (
                not source_path.is_file()
                or source_path.is_symlink()
                or source_path.suffix.casefold() not in BUNDLE_TEXT_SUFFIXES
            ):
                continue
            try:
                source_text = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "scripts/" in source_text:
                components.add(Path("scripts"))
                break
    return tuple(sorted(components, key=lambda item: item.as_posix()))


def _copy_bundle_component(
    root: Path, component: Path, destination_root: Path
) -> None:
    source = root / component
    destination = destination_root / component
    if source.is_dir():
        _copy_tree_without_symlinks(source, destination)
    else:
        _copy_file_without_symlinks(source, destination)


def _prepare_snapshot(
    root: Path,
    case: Case,
    case_root: Path,
    workspace: Path | None = None,
) -> tuple[Path, dict[str, str | None]]:
    snapshot_root = case_root / "snapshot"
    snapshot_root.mkdir()
    if workspace is None:
        workspace = case_root / "workspace"
        workspace.mkdir()
    bundle_root = workspace / ".agents" / "skills"
    bundle_root.mkdir(parents=True)
    components = _bundle_components(root, case)
    digests = _source_digests(root, case)
    for component in components:
        _copy_bundle_component(root, component, snapshot_root)
        _copy_bundle_component(root, component, bundle_root)
    return snapshot_root, digests


def _path_has_symlink(path: Path, parent: Path) -> bool:
    try:
        relative = path.relative_to(parent)
    except ValueError:
        return True
    current = parent
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _safe_file(path: Path, parent: Path) -> tuple[bool, str]:
    parent_resolved = _resolved(parent)
    if (
        parent.is_symlink()
        or not parent.is_dir()
        or not _within(path, parent)
        or _path_has_symlink(path, parent)
    ):
        return False, "path escapes its workspace or contains symlink"
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return False, f"cannot resolve path: {exc}"
    if not _within(resolved, parent_resolved):
        return False, "resolved path escapes its workspace"
    if not path.is_file() or path.is_symlink():
        return False, "declared artifact missing or not regular file"
    return True, ""


def _safe_directory(path: Path, parent: Path) -> tuple[bool, str]:
    if (
        parent.is_symlink()
        or not parent.is_dir()
        or path.is_symlink()
        or not _within(path, parent)
        or _path_has_symlink(path, parent)
    ):
        return False, "directory escapes its case or contains symlink"
    if not path.exists():
        return False, "directory is missing"
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return False, f"cannot resolve directory: {exc}"
    if not _within(resolved, _resolved(parent)):
        return False, "resolved directory escapes its case"
    if not path.is_dir():
        return False, "path is not a directory"
    try:
        for child in path.rglob("*"):
            if child.is_symlink():
                return False, "directory contains symlink"
            if not child.is_file() and not child.is_dir():
                return False, "directory contains unsupported special entry"
            child_resolved = child.resolve(strict=True)
            if not _within(child_resolved, resolved):
                return False, "directory entry escapes its case"
    except OSError as exc:
        return False, f"cannot inspect directory: {exc}"
    return True, ""


def _artifact_checks(
    case: Case,
    source_root: Path,
    destination_root: Path,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    source_safe = True
    source_detail = ""
    if source_root.exists() or source_root.is_symlink():
        source_safe, source_detail = _safe_directory(source_root, source_root.parent)
    elif case.artifacts:
        source_safe = False
        source_detail = "artifact source directory is missing"

    if not source_safe:
        checks.append(_check("artifact_root", "fail", source_detail))

    destination_safe = True
    destination_detail = ""
    destination_parent = destination_root.parent
    if (
        destination_parent.is_symlink()
        or not destination_parent.is_dir()
        or not _within(destination_root, destination_parent)
        or _path_has_symlink(destination_root, destination_parent)
    ):
        destination_safe = False
        destination_detail = "artifact destination escapes workspace or contains symlink"
    else:
        if not destination_root.exists():
            try:
                destination_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                destination_safe = False
                destination_detail = f"artifact destination cannot be created: {exc}"
        if destination_safe:
            destination_safe, destination_detail = _safe_directory(
                destination_root, destination_parent
            )
    if not destination_safe:
        checks.append(_check("artifact_destination_root", "fail", destination_detail))

    for artifact in case.artifacts:
        relative = _relative_path(
            str(artifact["path"]), f"{case.skill}/{case.case_id}.artifacts"
        )
        source = source_root / relative
        destination = destination_root / relative
        name = f"artifact:{relative.as_posix()}"
        if not source_safe:
            checks.append(_check(name, "fail", source_detail))
            continue
        if not destination_safe:
            checks.append(_check(name, "fail", destination_detail))
            continue
        safe, detail = _safe_file(source, source_root)
        if not safe:
            checks.append(_check(name, "fail", detail))
            continue
        if not _within(destination, destination_root) or _path_has_symlink(
            destination, destination_root
        ):
            checks.append(
                _check(
                    name,
                    "fail",
                    "artifact destination escapes workspace or contains symlink",
                )
            )
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
        except OSError as exc:
            checks.append(_check(name, "fail", f"artifact copy failed: {exc}"))
        else:
            checks.append(
                _check(name, "pass", "declared artifact exists in isolated workspace")
            )
    return checks


def _status_from_checks(
    checks: list[dict[str, str]],
) -> tuple[str, str, str]:
    failures = [check for check in checks if check["status"] == "fail"]
    unknowns = [check for check in checks if check["status"] == "unknown"]
    rubric_unknowns = [
        check
        for check in checks
        if check["name"].startswith(RUBRIC_PREFIX) and check["status"] == "unknown"
    ]
    deterministic_fail = any(
        check["status"] == "fail"
        and not check["name"].startswith(RUBRIC_PREFIX)
        for check in checks
    )
    deterministic_unknown = any(
        check["status"] == "unknown"
        and not check["name"].startswith(RUBRIC_PREFIX)
        for check in checks
    )
    deterministic = "fail" if deterministic_fail else "unknown" if deterministic_unknown else "pass"
    manual = "pending" if rubric_unknowns else "complete"
    overall = "fail" if failures else "unknown" if unknowns else "pass"
    return overall, deterministic, manual


def _result(
    case: Case, checks: list[dict[str, str]], **extra: Any
) -> dict[str, Any]:
    status, deterministic_status, manual_status = _status_from_checks(checks)
    result: dict[str, Any] = {
        "schema_version": 2,
        "skill": case.skill,
        "case_id": case.case_id,
        "trigger": case.trigger,
        "should_trigger": case.should_trigger,
        "expected_skill": case.expected_skill,
        "checks": checks,
        "status": status,
        "deterministic_status": deterministic_status,
        "manual_status": manual_status,
        "manual_pending": manual_status == "pending",
    }
    result.update(extra)
    return result


def _grade_trace(
    case: Case,
    trace_path: Path,
    artifact_source_root: Path | None,
    artifact_destination_root: Path | None,
    returncode: int | None,
    bundle_root: Path | None = None,
    expected_bundle_manifest: dict[str, str] | None = None,
) -> dict[str, Any]:
    events, trace_error = _read_trace(trace_path)
    checks: list[dict[str, str]] = []
    if returncode == 0:
        checks.append(_check("command", "pass", "command exited successfully"))
    elif returncode is None:
        checks.append(_check("command", "fail", "command did not produce an exit status"))
    else:
        checks.append(_check("command", "fail", f"command exited with status {returncode}"))
    if trace_error is None:
        checks.append(_check("trace_jsonl", "pass", "stdout is valid JSONL objects"))
        terminal = _terminal_event(events)
        if terminal is None:
            checks.append(
            _check(
                "completion",
                "fail",
                "trace has no allowlisted terminal event; command start is not success",
            )
        )
        else:
            terminal_failures = _terminal_failures(events)
            if terminal_failures:
                checks.append(
                    _check(
                        "completion",
                        "fail",
                        "; ".join(terminal_failures),
                    )
                )
            else:
                checks.append(
                    _check(
                        "completion",
                        "pass",
                        "trace contains an allowlisted terminal completion event",
                    )
                )
        checks.append(_activation_check(case, events))
    else:
        checks.append(_check("trace_jsonl", "fail", trace_error))
        checks.append(
            _check(
                "completion",
                "fail",
                "trace could not establish an allowlisted terminal completion event",
            )
        )
        checks.append(
            _check("skill_invocation", "unknown", "activation cannot be validated from an invalid trace")
        )
    if artifact_source_root is not None and artifact_destination_root is not None:
        checks.extend(
            _artifact_checks(case, artifact_source_root, artifact_destination_root)
        )
    if bundle_root is not None and expected_bundle_manifest is not None:
        checks.append(_bundle_integrity_check(bundle_root, expected_bundle_manifest))

    for index, expectation in enumerate(case.expectations, 1):
        checks.append(_check(f"{RUBRIC_PREFIX}{index}", "unknown", expectation))
    return _result(case, checks, returncode=returncode)


def _effective_argv(command: list[str], case: Case) -> list[str]:
    replacements = {"{prompt}": case.prompt, "{case_id}": case.case_id}
    argv = []
    for item in command:
        for marker, replacement in replacements.items():
            item = item.replace(marker, replacement)
        argv.append(item)
    if "{prompt}" not in " ".join(command):
        argv.append(case.prompt)
    return argv


def _base_metadata(
    case: Case, source_digests: dict[str, str | None] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "mode": "run",
        "skill": case.skill,
        "case_id": case.case_id,
        "trigger": case.trigger,
        "should_trigger": case.should_trigger,
        "expected_skill": case.expected_skill,
        "prompt": case.prompt,
        "sources": list(case.sources),
        "expected_output": case.expected_output,
        "expectations": list(case.expectations),
        "artifacts": list(case.artifacts),
        "source_digests": source_digests or {},
        "bundle_manifest": {},
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_case(
    case: Case,
    command: list[str],
    output_root: Path,
    timeout: float | None,
    root: Path = ROOT,
    timeout_error: str | None = None,
) -> dict[str, Any]:
    case_root = output_root / case.skill / case.case_id
    if case_root.exists():
        raise CatalogError(f"refusing reuse existing case output: {case_root}")
    case_root.mkdir(parents=True)
    trace_path = case_root / "trace.jsonl"
    stderr_path = case_root / "stderr.log"
    metadata_path = case_root / "metadata.json"
    result_path = case_root / "result.json"
    workspace = case_root / "workspace"
    workspace.mkdir()
    artifact_dir = case_root / "artifacts"
    metadata = _base_metadata(case)
    metadata.update(
        {
            "case_root": ".",
            "workspace": "workspace",
            "snapshot_root": "snapshot",
            "skill_snapshot": f"snapshot/{case.skill}",
            "skill_discovery_root": "workspace/.agents/skills",
            "bundle_root": "workspace/.agents/skills",
            "bundle_components": [
                component.as_posix() for component in _bundle_components(root, case)
            ],
        }
    )
    stdout_text = ""
    stderr_text = ""
    returncode: int | None = None
    timed_out = False
    argv = _effective_argv(command, case)
    metadata["effective_argv"] = argv
    launch_error: str | None = None
    bundle_root = workspace / ".agents" / "skills"

    try:
        snapshot_root, source_digests = _prepare_snapshot(
            root, case, case_root, workspace
        )
        metadata["source_digests"] = source_digests
        metadata["bundle_manifest"] = _bundle_manifest(
            workspace / ".agents" / "skills"
        )
        metadata["snapshot_root"] = "snapshot"
    except (CatalogError, OSError) as exc:
        stderr_text = f"snapshot preparation failed: {exc}\n"
        trace_path.write_text("", encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
        metadata.update({"command_returncode": None, "snapshot_error": str(exc)})
        result = _grade_trace(case, trace_path, workspace, artifact_dir, None)
        result["mode"] = "run"
        _write_json(metadata_path, metadata)
        _write_json(result_path, result)
        return result

    environment = os.environ.copy()
    environment.update(
        {
            "SKILL_EVAL_CASE_ROOT": str(case_root),
            "SKILL_EVAL_WORKSPACE": str(workspace),
            "SKILL_EVAL_SNAPSHOT_ROOT": str(snapshot_root),
            "SKILL_EVAL_SKILL_ROOT": str(snapshot_root / case.skill),
            "SKILL_EVAL_DISCOVERY_ROOT": str(workspace / ".agents" / "skills"),
            "SKILLS_BUNDLE_ROOT": str(workspace / ".agents" / "skills"),
        }
    )
    if timeout is None or not math.isfinite(timeout) or timeout <= 0:
        launch_error = timeout_error or (
            f"invalid timeout: {timeout!r}; timeout must be finite greater than zero"
        )
    else:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
                returncode = process.returncode
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                partial_stdout = _decode_stream(
                    getattr(exc, "stdout", None) or getattr(exc, "output", None)
                )
                partial_stderr = _decode_stream(getattr(exc, "stderr", None))
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout_after, stderr_after = process.communicate()
                stdout_text = _decode_stream(stdout_after) or partial_stdout
                stderr_text = _decode_stream(stderr_after) or partial_stderr
                if not stderr_text.endswith("\n"):
                    stderr_text += "\n"
                stderr_text += "command timed out\n"
                returncode = 124
            else:
                stdout_text = _decode_stream(stdout_bytes)
                stderr_text = _decode_stream(stderr_bytes)
        except OSError as exc:
            launch_error = f"command launch failed: {exc}"
            returncode = 127
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()

    if launch_error is not None:
        stderr_text = f"{launch_error}\n"
        trace_path.write_text("", encoding="utf-8")
    else:
        trace_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    metadata.update(
        {
            "command_returncode": returncode,
            "timed_out": timed_out,
            "launch_error": launch_error,
        }
    )
    result = _grade_trace(
        case, trace_path, workspace, artifact_dir, returncode,
        bundle_root=bundle_root,
        expected_bundle_manifest=metadata["bundle_manifest"],
    )
    result.update({"mode": "run", "timed_out": timed_out})
    _write_json(metadata_path, metadata)
    _write_json(result_path, result)
    return result


def _plan_case(case: Case, root: Path, output_root: Path) -> dict[str, Any]:
    case_root = output_root / case.skill / case.case_id
    if case_root.exists():
        raise CatalogError(f"refusing reuse existing case output: {case_root}")
    case_root.mkdir(parents=True)
    result = {
        "schema_version": 2,
        "mode": "plan",
        "skill": case.skill,
        "case_id": case.case_id,
            "trigger": case.trigger,
            "should_trigger": case.should_trigger,
            "expected_skill": case.expected_skill,
            "prompt": case.prompt,
        "sources": list(case.sources),
        "expected_output": case.expected_output,
        "expectations": list(case.expectations),
        "artifacts": list(case.artifacts),
        "source_digests": _source_digests(root, case),
        "status": "planned",
        "execution": "no command executed",
    }
    _write_json(case_root / "plan.json", result)
    return result


def _safe_saved_component(path: Path, parent: Path, where: str) -> Path:
    if (
        path.is_symlink()
        or not _within(path, parent)
        or _path_has_symlink(path, parent)
    ):
        raise CatalogError(
            f"{where}: saved snapshot component escapes or contains a symlink"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CatalogError(
            f"{where}: saved snapshot component cannot be resolved: {exc}"
        ) from exc
    if not _within(resolved, _resolved(parent)):
        raise CatalogError(f"{where}: saved snapshot component escapes its snapshot")
    if not path.is_file() and not path.is_dir():
        raise CatalogError(f"{where}: saved snapshot component is missing or not regular")
    return path


def _safe_saved_directory(path: Path, parent: Path, where: str) -> Path:
    if (
        path.is_symlink()
        or not _within(path, parent)
        or _path_has_symlink(path, parent)
    ):
        raise CatalogError(f"{where}: saved snapshot path escapes or contains a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CatalogError(f"{where}: saved snapshot path cannot be resolved: {exc}") from exc
    if not _within(resolved, _resolved(parent)):
        raise CatalogError(f"{where}: saved snapshot path escapes its case")
    if not path.is_dir():
        raise CatalogError(f"{where}: saved snapshot path is missing or not a directory")
    try:
        for child in path.rglob("*"):
            if child.is_symlink():
                raise CatalogError(f"{where}: saved snapshot contains a symlink: {child}")
            if not child.is_file() and not child.is_dir():
                raise CatalogError(
                    f"{where}: saved snapshot contains unsupported special entry: {child}"
                )
    except OSError as exc:
        raise CatalogError(f"{where}: cannot inspect saved snapshot: {exc}") from exc
    return path


def _validate_bundle_manifest(
    snapshot_root: Path, raw_manifest: Any, where: str
) -> None:
    if not isinstance(raw_manifest, dict) or not raw_manifest:
        raise CatalogError(f"{where}: saved bundle_manifest is invalid")
    expected: dict[str, str] = {}
    for raw_path, digest in raw_manifest.items():
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise CatalogError(f"{where}: saved bundle_manifest path is invalid")
        relative = _relative_path(raw_path, f"{where}: bundle_manifest path")
        if relative.as_posix() != raw_path:
            raise CatalogError(f"{where}: saved bundle_manifest path is not canonical")
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", digest
        ):
            raise CatalogError(f"{where}: saved bundle_manifest digest is invalid")
        candidate = _safe_saved_component(
            snapshot_root / relative,
            snapshot_root,
            f"{where}: bundle_manifest[{raw_path}]",
        )
        if not candidate.is_file() or candidate.is_symlink():
            raise CatalogError(
                f"{where}: saved bundle_manifest path is not a regular file: {raw_path}"
            )
        expected[raw_path] = digest.lower()

    actual = _bundle_manifest(snapshot_root)
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        added = sorted(set(actual) - set(expected))
        details: list[str] = []
        if missing:
            details.append(f"missing files: {', '.join(missing)}")
        if added:
            details.append(f"added files: {', '.join(added)}")
        raise CatalogError(f"{where}: saved bundle_manifest file set changed ({'; '.join(details)})")
    for relative, digest in expected.items():
        if actual[relative].lower() != digest:
            raise CatalogError(
                f"{where}: saved bundle_manifest digest does not match snapshot: {relative}"
            )


def _saved_relative_path(raw: Any, where: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError(f"{where}: saved path must be a non-empty string")
    return _relative_path(raw, where)


def _validate_replay_workspace(
    path: Path, saved_case_root: Path, metadata: dict[str, Any]
) -> Path:
    where = str(path)
    workspace_relative = _saved_relative_path(
        metadata["workspace"], f"{where}: workspace"
    )
    return _safe_saved_directory(
        saved_case_root / workspace_relative,
        saved_case_root,
        f"{where}: workspace",
    )


def _validate_replay_provenance(
    path: Path, saved_case_root: Path, metadata: dict[str, Any], case: Case
) -> Path:
    where = str(path)
    expected_paths = {
        "case_root": ".",
        "workspace": "workspace",
        "skill_discovery_root": "workspace/.agents/skills",
        "bundle_root": "workspace/.agents/skills",
    }
    for field, expected in expected_paths.items():
        if metadata.get(field) != expected:
            raise CatalogError(f"{where}: saved {field} provenance is invalid")

    _validate_replay_workspace(path, saved_case_root, metadata)

    snapshot_relative = _saved_relative_path(
        metadata["snapshot_root"], f"{where}: snapshot_root"
    )
    snapshot_root = _safe_saved_directory(
        saved_case_root / snapshot_relative,
        saved_case_root,
        f"{where}: snapshot_root",
    )
    _validate_bundle_manifest(snapshot_root, metadata.get("bundle_manifest"), where)
    bundle_relative = _saved_relative_path(
        metadata["bundle_root"], f"{where}: bundle_root"
    )
    bundle_root = _safe_saved_directory(
        saved_case_root / bundle_relative,
        saved_case_root,
        f"{where}: bundle_root",
    )
    _validate_bundle_manifest(bundle_root, metadata.get("bundle_manifest"), where)
    skill_relative = _saved_relative_path(
        metadata["skill_snapshot"], f"{where}: skill_snapshot"
    )
    expected_skill_relative = snapshot_relative / case.skill
    if skill_relative != expected_skill_relative:
        raise CatalogError(
            f"{where}: saved skill_snapshot does not match snapshot_root and skill"
        )
    _safe_saved_directory(
        saved_case_root / skill_relative,
        saved_case_root,
        f"{where}: skill_snapshot",
    )

    components = metadata["bundle_components"]
    if not isinstance(components, list) or not components:
        raise CatalogError(f"{where}: saved bundle_components are invalid")
    component_paths: set[Path] = set()
    for index, component in enumerate(components):
        if not isinstance(component, str) or not component.strip():
            raise CatalogError(f"{where}: saved bundle_components[{index}] is invalid")
        relative = _relative_path(component, f"{where}: bundle_components[{index}]")
        component_paths.add(relative)
        _safe_saved_component(
            snapshot_root / relative,
            snapshot_root,
            f"{where}: bundle_components[{index}]",
        )

    required_components = {Path(case.skill)}
    source_references = [f"bundled:{case.skill}/SKILL.md", *case.sources]
    source_paths: dict[str, Path | None] = {}
    for source in source_references:
        if source.startswith("bundled:"):
            relative = _relative_path(
                source.removeprefix("bundled:"), f"{where}: source"
            )
            if relative.parts:
                required_components.add(Path(relative.parts[0]))
        source_paths[source] = _source_path(snapshot_root, case.skill, source)
    if not required_components.issubset(component_paths):
        missing = sorted(
            component.as_posix()
            for component in required_components - component_paths
        )
        raise CatalogError(
            f"{where}: saved bundle_components omit source components: {', '.join(missing)}"
        )

    digests = metadata["source_digests"]
    expected_digest_keys = set(source_references)
    if not isinstance(digests, dict) or set(digests) != expected_digest_keys:
        raise CatalogError(f"{where}: saved source_digests do not match declared sources")
    for source, source_path in source_paths.items():
        digest = digests[source]
        if source_path is None:
            if digest is not None:
                raise CatalogError(f"{where}: HTTPS source digest must be null: {source}")
            continue
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", digest
        ):
            raise CatalogError(f"{where}: saved digest is invalid: {source}")
        actual = _sha256(source_path)
        if digest.lower() != actual:
            raise CatalogError(f"{where}: saved digest does not match snapshot: {source}")
    return snapshot_root


REPLAY_MODES = frozenset({"run", "replay"})


def _validate_replay_metadata_envelope(raw: Any, path: Path) -> None:
    where = str(path)
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: saved metadata must be an object")

    required = {"schema_version", "mode", "command_returncode", "effective_argv"}
    missing = required - raw.keys()
    if missing:
        raise CatalogError(
            f"{where}: saved metadata is missing envelope fields: {', '.join(sorted(missing))}"
        )

    schema_version = raw["schema_version"]
    if type(schema_version) is not int or schema_version != 2:
        raise CatalogError(f"{where}: saved metadata schema_version must be 2")

    mode = raw["mode"]
    if not isinstance(mode, str) or mode not in REPLAY_MODES:
        raise CatalogError(
            f"{where}: saved metadata mode must be one of {sorted(REPLAY_MODES)}"
        )

    returncode = raw["command_returncode"]
    if returncode is not None and type(returncode) is not int:
        raise CatalogError(
            f"{where}: saved metadata command_returncode must be an integer or null"
        )

    timed_out_present = "timed_out" in raw
    timed_out = raw.get("timed_out")
    if timed_out_present and type(timed_out) is not bool:
        raise CatalogError(f"{where}: saved metadata timed_out must be boolean")

    snapshot_error_present = "snapshot_error" in raw
    snapshot_error = raw.get("snapshot_error")
    if snapshot_error_present and (
        not isinstance(snapshot_error, str) or not snapshot_error.strip()
    ):
        raise CatalogError(f"{where}: saved metadata snapshot_error must be non-empty string")

    launch_error_present = "launch_error" in raw
    launch_error = raw.get("launch_error")
    if launch_error_present and launch_error is not None and (
        not isinstance(launch_error, str) or not launch_error.strip()
    ):
        raise CatalogError(
            f"{where}: saved metadata launch_error must be null or non-empty string"
        )

    if snapshot_error_present:
        if returncode is not None:
            raise CatalogError(
                f"{where}: snapshot-error metadata must have null command_returncode"
            )
        if timed_out_present:
            raise CatalogError(
                f"{where}: snapshot-error metadata must omit timed_out"
            )
        if "launch_error" in raw:
            raise CatalogError(
                f"{where}: snapshot-error metadata must omit launch_error"
            )
    elif returncode is None and not (
        isinstance(launch_error, str) and launch_error.strip()
    ):
        raise CatalogError(
            f"{where}: null command_returncode requires snapshot_error or launch_error"
        )
    elif not timed_out_present:
        raise CatalogError(f"{where}: command metadata is missing timed_out")

    if launch_error_present:
        if launch_error is not None and (
            returncode not in (None, 127) or timed_out_present and timed_out
        ):
            raise CatalogError(
                f"{where}: launch_error is inconsistent with command outcome"
            )
    elif returncode is not None and not snapshot_error_present:
        raise CatalogError(f"{where}: command metadata is missing launch_error")

    if timed_out_present and timed_out and returncode != 124:
        raise CatalogError(
            f"{where}: timed-out metadata must have command_returncode 124"
        )

    effective_argv = raw["effective_argv"]
    if not isinstance(effective_argv, list) or not effective_argv or not all(
        isinstance(argument, str) for argument in effective_argv
    ):
        raise CatalogError(f"{where}: saved metadata effective_argv is invalid")

    if mode == "replay":
        replayed_from = raw.get("replayed_from")
        if not isinstance(replayed_from, str) or not replayed_from.strip():
            raise CatalogError(
                f"{where}: replay metadata requires non-empty replayed_from"
            )
    elif "replayed_from" in raw:
        raise CatalogError(f"{where}: run metadata must omit replayed_from")


def _metadata_identity(path: Path) -> tuple[str, str]:
    safe, detail = _safe_file(path, path.parent)
    if not safe:
        raise CatalogError(f"{path}: saved metadata is unsafe: {detail}")
    raw = _json(path)
    if not isinstance(raw, dict):
        raise CatalogError(f"{path}: saved metadata must be an object")
    if "skill" not in raw or "case_id" not in raw:
        raise CatalogError(f"{path}: saved metadata missing replay identity fields")
    skill = raw["skill"]
    case_id = _validate_case_id(raw["case_id"], str(path))
    if not isinstance(skill, str) or skill not in EXPECTED_SKILLS:
        raise CatalogError(f"{path}: saved metadata has invalid skill or case_id")
    return skill, case_id

def _metadata_case(path: Path) -> Case:
    safe, detail = _safe_file(path, path.parent)
    if not safe:
        raise CatalogError(f"{path}: saved metadata is unsafe: {detail}")
    raw = _json(path)
    _validate_replay_metadata_envelope(raw, path)
    required = {
        "skill",
        "case_id",
        "prompt",
        "trigger",
        "should_trigger",
        "sources",
        "expected_output",
        "expectations",
        "artifacts",
        "case_root",
        "workspace",
        "snapshot_root",
        "skill_snapshot",
        "skill_discovery_root",
        "bundle_root",
        "bundle_components",
        "source_digests",
        "bundle_manifest",
    }
    if not isinstance(raw, dict) or not required.issubset(raw):
        raise CatalogError(f"{path}: saved metadata is incomplete for replay")
    skill = raw["skill"]
    case_id = _validate_case_id(raw["case_id"], str(path))
    if skill not in EXPECTED_SKILLS:
        raise CatalogError(f"{path}: saved metadata has invalid skill or case_id")
    if not isinstance(raw["prompt"], str) or not raw["prompt"].strip():
        raise CatalogError(f"{path}: saved prompt is invalid")
    trigger = raw["trigger"]
    if (
        not isinstance(trigger, str)
        or trigger not in TRIGGERS
        or not isinstance(raw["should_trigger"], bool)
        or (trigger == "negative" and raw["should_trigger"])
    ):
        raise CatalogError(f"{path}: saved metadata has invalid case fields")
    expected_skill = _validate_expected_skill(
        raw.get("expected_skill"), skill, raw["should_trigger"], str(path)
    )
    sources = raw["sources"]
    expected_output = raw["expected_output"]
    expectations = raw["expectations"]
    artifacts = raw["artifacts"]
    if not isinstance(expected_output, str) or not expected_output.strip():
        raise CatalogError(f"{path}: saved expected_output is invalid")
    if not isinstance(sources, list) or not sources or not all(
        isinstance(item, str) and item.strip() for item in sources
    ):
        raise CatalogError(f"{path}: saved sources are invalid")
    if not isinstance(expectations, list) or not expectations or not all(
        isinstance(item, str) and item.strip() for item in expectations
    ):
        raise CatalogError(f"{path}: saved expectations are invalid")
    if not isinstance(artifacts, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item["path"].strip()
        for item in artifacts
    ):
        raise CatalogError(f"{path}: saved artifacts are invalid")
    normalized = []
    for artifact in artifacts:
        item = dict(artifact)
        item["path"] = _relative_path(item["path"], str(path)).as_posix()
        normalized.append(item)
    case = Case(
        skill=skill,
        case_id=case_id,
        prompt=raw["prompt"],
        trigger=trigger,
        should_trigger=raw["should_trigger"],
        sources=tuple(sources),
        expected_output=expected_output,
        expectations=tuple(expectations),
        artifacts=tuple(normalized),
        expected_skill=expected_skill,
    )
    return case


def _copy_saved_file(source: Path, destination: Path, parent: Path) -> None:
    safe, detail = _safe_file(source, parent)
    if not safe:
        raise CatalogError(f"{source}: {detail}")
    if destination.exists() or destination.is_symlink():
        raise CatalogError(f"refusing overwrite replay output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _copy_saved_directory(source: Path, destination: Path, parent: Path) -> None:
    safe, detail = _safe_directory(source, parent)
    if not safe:
        raise CatalogError(f"{source}: {detail}")
    if destination.exists() or destination.is_symlink():
        raise CatalogError(f"refusing overwrite replay output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, destination, symlinks=False)
    except (OSError, shutil.Error) as exc:
        raise CatalogError(f"cannot copy saved directory {source}: {exc}") from exc


def _preflight_replay_cases(case_roots: list[Path]) -> None:
    identities: set[tuple[str, str]] = set()
    for saved_case_root in case_roots:
        if saved_case_root.is_symlink() or not saved_case_root.is_dir():
            raise CatalogError(f"{saved_case_root}: saved case root must be a real directory")
        metadata_path = saved_case_root / "metadata.json"
        safe, detail = _safe_file(metadata_path, saved_case_root)
        if not safe:
            raise CatalogError(f"{metadata_path}: {detail}")
        case = _metadata_case(metadata_path)
        metadata = _json(metadata_path)
        if not isinstance(metadata, dict):
            raise CatalogError(f"{metadata_path}: metadata must be an object")
        _validate_replay_provenance(metadata_path, saved_case_root, metadata, case)
        _validate_replay_workspace(metadata_path, saved_case_root, metadata)
        for filename in ("trace.jsonl", "stderr.log"):
            evidence_path = saved_case_root / filename
            safe, detail = _safe_file(evidence_path, saved_case_root)
            if not safe:
                raise CatalogError(f"{evidence_path}: {detail}")
        identity = (case.skill, case.case_id)
        if identity in identities:
            raise CatalogError(
                f"duplicate saved case identity: {case.skill}/{case.case_id}"
            )
        identities.add(identity)

def _replay_case(saved_case_root: Path, output_root: Path) -> dict[str, Any]:
    if saved_case_root.is_symlink() or not saved_case_root.is_dir():
        raise CatalogError(f"{saved_case_root}: saved case root must be a real directory")
    metadata_path = saved_case_root / "metadata.json"
    safe, detail = _safe_file(metadata_path, saved_case_root)
    if not safe:
        raise CatalogError(f"{metadata_path}: {detail}")
    case = _metadata_case(metadata_path)
    metadata = _json(metadata_path)
    if not isinstance(metadata, dict):
        raise CatalogError(f"{metadata_path}: metadata must be an object")
    snapshot_source = _validate_replay_provenance(
        metadata_path, saved_case_root, metadata, case
    )
    workspace_source = _validate_replay_workspace(
        metadata_path, saved_case_root, metadata
    )
    case_root = output_root / case.skill / case.case_id
    if case_root.exists():
        raise CatalogError(f"refusing reuse existing replay output: {case_root}")
    case_root.mkdir(parents=True)
    trace_path = case_root / "trace.jsonl"
    stderr_path = case_root / "stderr.log"
    _copy_saved_file(saved_case_root / "trace.jsonl", trace_path, saved_case_root)
    _copy_saved_file(saved_case_root / "stderr.log", stderr_path, saved_case_root)
    _copy_skill_snapshot(snapshot_source, case_root / "snapshot")
    _copy_saved_directory(workspace_source, case_root / "workspace", saved_case_root)
    artifact_source = saved_case_root / "artifacts"
    artifact_destination = case_root / "artifacts"
    returncode = metadata["command_returncode"]
    result = _grade_trace(
        case, trace_path, artifact_source, artifact_destination, returncode,
        bundle_root=case_root / "workspace" / ".agents" / "skills",
        expected_bundle_manifest=metadata["bundle_manifest"],
    )
    result["mode"] = "replay"
    result["replayed_from"] = str(_resolved(saved_case_root))
    metadata["mode"] = "replay"
    metadata["replayed_from"] = str(_resolved(saved_case_root))
    _write_json(case_root / "metadata.json", metadata)
    _write_json(case_root / "result.json", result)
    return result


def _prepare_output_dir(root: Path, output_dir: Path) -> Path:
    output_dir = _resolved(output_dir)
    if _overlaps(root, output_dir):
        raise CatalogError(
            f"output directory overlaps evaluated Skills source tree: {output_dir}"
        )
    if output_dir.exists():
        if not output_dir.is_dir():
            raise CatalogError(f"output directory is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise CatalogError(
                f"output directory must be new or empty; refusing stale results: {output_dir}"
            )
    else:
        output_dir.mkdir(parents=True)
    return output_dir


def _replay_inputs(replay_root: Path) -> list[Path]:
    replay_root = _resolved(replay_root)
    if not replay_root.is_dir():
        raise CatalogError(f"replay root is not a directory: {replay_root}")
    case_roots: list[Path] = []
    for metadata_path in sorted(replay_root.rglob("metadata.json")):
        case_root = metadata_path.parent
        if (
            metadata_path.is_symlink()
            or not (case_root / "trace.jsonl").is_file()
            or not (case_root / "stderr.log").is_file()
        ):
            continue
        case_roots.append(case_root)
    if not case_roots:
        raise CatalogError(f"replay root contains no complete saved cases: {replay_root}")
    return case_roots


def _select_replay_cases(
    case_roots: list[Path],
    wanted_skills: list[str] | None,
    wanted_ids: list[str] | None,
) -> list[Path]:
    selected = case_roots
    wanted_skill_set = set(wanted_skills or ())
    if wanted_skills:
        unknown = sorted(wanted_skill_set - set(EXPECTED_SKILLS))
        if unknown:
            raise CatalogError(f"unknown skill(s): {', '.join(unknown)}")

    saved_cases = (
        [
            (path, _metadata_identity(path / "metadata.json"))
            for path in case_roots
        ]
        if wanted_skills or wanted_ids
        else []
    )
    if wanted_skills:
        available = {skill for _, (skill, _) in saved_cases}
        missing = sorted(wanted_skill_set - available)
        if missing:
            raise CatalogError(
                f"no saved cases match skill(s): {', '.join(missing)}"
            )
        selected = [
            path for path, (skill, _) in saved_cases if skill in wanted_skill_set
        ]
    if wanted_ids:
        wanted = set(wanted_ids)
        selected_cases = [
            (path, identity) for path, identity in saved_cases if path in selected
        ]
        selected = [
            path for path, (_, case_id) in selected_cases if case_id in wanted
        ]
        missing = wanted - {case_id for _, (_, case_id) in selected_cases}
        if missing:
            raise CatalogError(
                f"unknown saved case(s): {', '.join(sorted(missing))}"
            )
    return selected
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="plan without executing a command")
    mode.add_argument("--list", action="store_true", help="list validated cases and exit")
    mode.add_argument("--replay", help="re-grade saved raw traces without launching a command")
    parser.add_argument("--root", default=ROOT, type=Path, help="Skills bundle root")
    parser.add_argument("--skill", action="append", dest="skills", help="limit skill (repeatable)")
    parser.add_argument("--case", action="append", dest="case_ids", help="limit case id (repeatable)")
    parser.add_argument("--output-dir", type=Path, required=True, help="new or empty output directory")
    parser.add_argument("--command", help="explicit command parsed with shlex")
    parser.add_argument("--timeout", default="120.0", help="positive finite command timeout in seconds")
    return parser


def _select_cases(cases: list[Case], wanted_ids: list[str] | None) -> list[Case]:
    if not wanted_ids:
        return cases
    wanted = set(wanted_ids)
    selected = [case for case in cases if case.case_id in wanted]
    missing = wanted - {case.case_id for case in selected}
    if missing:
        raise CatalogError(f"unknown case(s): {', '.join(sorted(missing))}")
    return selected


def _parse_timeout(raw: str) -> tuple[float | None, str | None]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f"invalid timeout {raw!r}; timeout must be finite number greater than zero"
    if not math.isfinite(value) or value <= 0:
        return None, f"invalid timeout {raw!r}; timeout must be finite number greater than zero"
    return value, None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = _resolved(args.root)
        if args.replay is not None:
            if args.plan or args.list:
                raise CatalogError("--replay cannot be combined with --plan or --list")
            if args.command is not None:
                raise CatalogError("--command cannot be combined with --replay")
            replay_root = _resolved(Path(args.replay))
            output_dir = _resolved(args.output_dir)
            if _overlaps(replay_root, output_dir):
                raise CatalogError("replay input and output directories overlap")
            case_roots = _replay_inputs(replay_root)
            case_roots = _select_replay_cases(case_roots, args.skills, args.case_ids)
            _preflight_replay_cases(case_roots)
            output_dir = _prepare_output_dir(root, output_dir)
            results = [_replay_case(path, output_dir) for path in case_roots]
        else:
            cases = _select_cases(load_cases(root, args.skills), args.case_ids)
            if args.list:
                if args.command is not None:
                    raise CatalogError("--command cannot be combined with --list")
                for case in cases:
                    print(
                        json.dumps(
                            {
                                "skill": case.skill,
                                "case_id": case.case_id,
                                "trigger": case.trigger,
                                "should_trigger": case.should_trigger,
                            },
                            sort_keys=True,
                        )
                    )
                return 0
            output_dir = _prepare_output_dir(root, args.output_dir)
            if args.plan:
                if args.command is not None:
                    raise CatalogError("--command cannot be combined with --plan")
                results = [_plan_case(case, root, output_dir) for case in cases]
                for result in results:
                    print(json.dumps(result, sort_keys=True))
                print(
                    f"planned {len(results)} case(s); no command executed",
                    file=sys.stderr,
                )
                return 0
            if not args.command:
                raise CatalogError("--command is required unless --plan or --list is used")
            try:
                command = shlex.split(args.command)
            except ValueError as exc:
                raise CatalogError(f"--command: {exc}") from exc
            if not command:
                raise CatalogError("--command must contain an executable")
            timeout, timeout_error = _parse_timeout(args.timeout)
            results = [
                _run_case(
                    case,
                    command,
                    output_dir,
                    timeout,
                    root=root,
                    timeout_error=timeout_error,
                )
                for case in cases
            ]
        summary = {
            "schema_version": 2,
            "cases": len(results),
            "pass": sum(result["status"] == "pass" for result in results),
            "unknown": sum(result["status"] == "unknown" for result in results),
            "fail": sum(result["status"] == "fail" for result in results),
            "deterministic_pass": sum(
                result["deterministic_status"] == "pass" for result in results
            ),
            "deterministic_unknown": sum(
                result["deterministic_status"] == "unknown" for result in results
            ),
            "deterministic_fail": sum(
                result["deterministic_status"] == "fail" for result in results
            ),
            "manual_pending": sum(result["manual_pending"] for result in results),
            "results": [
                {
                    "case_id": result["case_id"],
                    "skill": result["skill"],
                    "status": result["status"],
                    "deterministic_status": result["deterministic_status"],
                    "manual_status": result["manual_status"],
                }
                for result in results
            ],
        }
        _write_json(output_dir / "summary.json", summary)
        for result in results:
            print(json.dumps(result, sort_keys=True))
        print(f"processed {len(results)} case(s); artifacts in {output_dir}", file=sys.stderr)
        return 1 if summary["fail"] else 0
    except (CatalogError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
