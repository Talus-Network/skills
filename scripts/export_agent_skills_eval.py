#!/usr/bin/env python3
"""Export the canonical Skills eval catalogs for agent-skills-eval."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "nexus-cli-payment-tracking",
    "nexus-offchain-tool-development",
    "nexus-onchain-task-debugging",
    "nexus-onchain-tool-development",
    "nexus-tap-development",
)
CATALOG_FILES = {"execution": "evals.json", "response": "response-evals.json"}
CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
EXPECTED_SKILLS = frozenset(SKILLS)


class ExportError(ValueError):
    """Raised when a canonical catalog cannot be exported safely."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"{path}: cannot read JSON catalog: {exc}") from exc
    if not isinstance(document, dict):
        raise ExportError(f"{path}: catalog root must be a JSON object")
    return document


def _catalog_path(root: Path, skill: str, catalog: str) -> Path:
    try:
        filename = CATALOG_FILES[catalog]
    except KeyError as exc:
        raise ExportError(f"unsupported catalog {catalog!r}") from exc
    return root / skill / "evals" / filename


def _case_id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not CASE_ID_RE.fullmatch(value):
        raise ExportError(f"{where}: id must be one safe path component")
    return value


def _case_list(document: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    entries = document.get("evals")
    if not isinstance(entries, list):
        raise ExportError(f"{path}: evals must be an array")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{path}[{index}]"
        if not isinstance(entry, dict):
            raise ExportError(f"{where}: eval must be an object")
        case = dict(entry)
        case_id = _case_id(case.get("id"), where)
        if case_id in seen:
            raise ExportError(f"{where}: duplicate id {case_id!r}")
        seen.add(case_id)
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ExportError(f"{where}: prompt must be a non-empty string")
        if not isinstance(case.get("expected_output"), str) or not case["expected_output"].strip():
            raise ExportError(f"{where}: expected_output must be a non-empty string")
        expectations = case.get("expectations")
        if (
            not isinstance(expectations, list)
            or not expectations
            or not all(isinstance(item, str) and item.strip() for item in expectations)
        ):
            raise ExportError(f"{where}: expectations must be a non-empty string array")
        if "assertions" in case:
            raise ExportError(f"{where}: canonical catalog must use expectations, not assertions")
        cases.append(case)
    return cases


def _load_cases(root: Path, skill: str, catalog: str) -> tuple[Path, list[dict[str, Any]]]:
    path = _catalog_path(root, skill, catalog)
    if not path.is_file():
        raise ExportError(f"{path}: catalog is missing")
    document = _read_json(path)
    if document.get("skill_name") != skill:
        raise ExportError(f"{path}: skill_name must match {skill!r}")
    return path, _case_list(document, path)


def _selected_case(skill: str, case_id: str, wanted: set[str]) -> bool:
    return not wanted or case_id in wanted or f"{skill}/{case_id}" in wanted


def _exported_case(case: dict[str, Any]) -> dict[str, Any]:
    exported = dict(case)
    expectations = list(case["expectations"])
    output_assertion = f"The output satisfies this expected output: {case['expected_output']}"
    assertions: list[str] = []
    for assertion in [*expectations, output_assertion]:
        if assertion not in assertions:
            assertions.append(assertion)
    exported["assertions"] = assertions
    return exported


def _copy_skill(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ExportError(f"{source}: refusing to export symlink {path}")
    try:
        shutil.copytree(source, destination)
    except OSError as exc:
        raise ExportError(f"cannot copy {source} to {destination}: {exc}") from exc


def _ensure_empty_output(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ExportError(f"output path is not a directory: {path}")
        try:
            has_entries = any(path.iterdir())
        except OSError as exc:
            raise ExportError(f"cannot inspect output directory {path}: {exc}") from exc
        if has_entries:
            raise ExportError(f"refusing to overwrite non-empty output directory: {path}")
    else:
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            raise ExportError(f"cannot create output directory {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_bundle(
    *,
    root: Path = ROOT,
    output_dir: Path,
    skills: Iterable[str] = SKILLS,
    catalog: str = "execution",
    cases: Iterable[str] = (),
) -> dict[str, Any]:
    """Copy selected skills and adapt their canonical catalogs for the npm evaluator."""

    root = root.resolve()
    output_dir = output_dir.resolve()
    selected_skills = list(dict.fromkeys(skills))
    if not selected_skills:
        raise ExportError("at least one skill must be selected")
    unknown_skills = sorted(set(selected_skills) - EXPECTED_SKILLS)
    if unknown_skills:
        raise ExportError(f"unknown skill(s): {', '.join(unknown_skills)}")
    if catalog not in {"execution", "response", "all"}:
        raise ExportError("catalog must be execution, response, or all")
    wanted_cases = set(cases)
    for requested in wanted_cases:
        if "/" in requested:
            requested_skill, requested_id = requested.split("/", 1)
            if requested_skill not in selected_skills or not CASE_ID_RE.fullmatch(requested_id):
                raise ExportError(f"unknown selected case {requested!r}")
        elif not CASE_ID_RE.fullmatch(requested):
            raise ExportError(f"case selector must be a case id or skill/case: {requested!r}")

    if output_dir == root or root in output_dir.parents:
        raise ExportError("output directory must be outside the canonical Skills root")

    # Validate every input and selector before creating output so a bad catalog
    # cannot leave a partially exported bundle behind. A case selector narrows
    # the bundle to skills containing a matching case.
    catalog_names = ("execution", "response") if catalog == "all" else (catalog,)
    skills_to_export: list[str] = []
    available_cases: set[str] = set()
    for skill in selected_skills:
        skill_has_match = False
        for catalog_name in catalog_names:
            _, source_cases = _load_cases(root, skill, catalog_name)
            selected_source_cases = [
                case for case in source_cases if _selected_case(skill, case["id"], wanted_cases)
            ]
            if selected_source_cases:
                skill_has_match = True
                available_cases.update(f"{skill}/{case['id']}" for case in selected_source_cases)
        if skill_has_match:
            skills_to_export.append(skill)
    if not wanted_cases:
        skills_to_export = selected_skills
    missing_cases = {
        requested
        for requested in wanted_cases
        if requested not in available_cases
        and not any(case.endswith(f"/{requested}") for case in available_cases)
    }
    if missing_cases:
        raise ExportError(f"unknown selected case(s): {', '.join(sorted(missing_cases))}")
    if not skills_to_export:
        raise ExportError("selected catalog has no matching cases")

    _ensure_empty_output(output_dir)
    manifest_cases: list[dict[str, Any]] = []
    missing_cases = set(wanted_cases)
    for skill in skills_to_export:
        source_skill = root / skill
        if not source_skill.is_dir():
            raise ExportError(f"skill directory is missing: {source_skill}")
        destination_skill = output_dir / skill
        _copy_skill(source_skill, destination_skill)
        combined: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        source_catalogs: list[dict[str, Any]] = []
        for catalog_name in catalog_names:
            source_path, source_cases = _load_cases(root, skill, catalog_name)
            selected = [
                case
                for case in source_cases
                if _selected_case(skill, case["id"], wanted_cases)
            ]
            for case in selected:
                case_id = case["id"]
                if case_id in seen_ids:
                    raise ExportError(f"{skill}: duplicate selected case id across catalogs: {case_id!r}")
                seen_ids.add(case_id)
                combined.append(_exported_case(case))
                missing_cases.discard(case_id)
                missing_cases.discard(f"{skill}/{case_id}")
                manifest_cases.append(
                    {
                        "skill": skill,
                        "case_id": case_id,
                        "catalog": catalog_name,
                        "source": str(source_path.relative_to(root)),
                        "expectation_count": len(case["expectations"]),
                        "exported_assertion_count": len(combined[-1]["assertions"]),
                        "adaptations": [
                            "map expectations to assertions",
                            "preserve expected_output and grade it as an assertion",
                        ],
                    }
                )
            source_catalogs.append(
                {
                    "catalog": catalog_name,
                    "source": str(source_path.relative_to(root)),
                    "source_sha256": _sha256(source_path),
                    "selected_cases": [case["id"] for case in selected],
                }
            )
        if not combined:
            raise ExportError(f"{skill}: selected catalog has no matching cases")
        output_catalog = destination_skill / "evals" / "evals.json"
        output_document = {
            "skill_name": skill,
            "source_constraint": "Exported rubric adapter; canonical catalog remains in the source bundle.",
            "evals": combined,
        }
        output_catalog.write_text(
            json.dumps(output_document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        source_catalogs_path = destination_skill / "agent-skills-eval-source-catalogs.json"
        source_catalogs_path.write_text(
            json.dumps(source_catalogs, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    if missing_cases:
        raise ExportError(f"unknown selected case(s): {', '.join(sorted(missing_cases))}")
    manifest = {
        "schema_version": 1,
        "catalog": catalog,
        "skills": skills_to_export,
        "cases": manifest_cases,
        "limitations": [
            "This export adapts catalog fields for agent-skills-eval; it does not prove automatic skill routing.",
            "Unless tools and fixtures are supplied separately, chat grading does not prove command, compiler, chain, or provider execution.",
        ],
    }
    (output_dir / "agent-skills-eval-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="canonical Skills bundle root")
    parser.add_argument("--output-dir", type=Path, required=True, help="new empty evaluator bundle directory")
    parser.add_argument("--skill", action="append", choices=SKILLS, help="skill to include; repeatable")
    parser.add_argument(
        "--catalog",
        choices=("execution", "response", "all"),
        default="execution",
        help="canonical catalog to export (default: execution)",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="case id or skill/case selector; repeatable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = export_bundle(
            root=args.root,
            output_dir=args.output_dir,
            skills=args.skill or SKILLS,
            catalog=args.catalog,
            cases=args.case,
        )
    except (ExportError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), **manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
