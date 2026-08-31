"""Validate the local runtime and test dependency closure of Move packages."""

from __future__ import annotations

import re
from pathlib import Path
import tomllib
from collections.abc import Mapping
from typing import Any


class MoveManifestClosureError(ValueError):
    """Raised when a reachable Move source is not a safe local closure."""


_PACKAGE_ALIAS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def validate_move_manifest_closure(project_root: Path, root_manifest: Path) -> dict[str, Any]:
    """Traverse runtime/dev Move manifests and locks below ``project_root``.

    Every dependency is required to use a local path whose resolved manifest is
    inside the supplied project. Both dependency tables are followed at every
    depth, lockfile-local sources are followed as well, and recursive cycles are
    rejected instead of silently treated as already validated.
    """

    project_root = Path(project_root).resolve()
    root_manifest = Path(root_manifest)
    if not project_root.is_dir():
        raise MoveManifestClosureError(f"Move closure root is not a directory: {project_root}")

    def contained(candidate: Path, label: str) -> Path:
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(project_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise MoveManifestClosureError(f"Move closure path escapes project root: {label}") from exc
        return resolved

    root_manifest = contained(root_manifest, str(root_manifest))
    if root_manifest.name != "Move.toml":
        raise MoveManifestClosureError(f"Move closure root is not Move.toml: {root_manifest}")
    if root_manifest.is_symlink() or not root_manifest.is_file():
        raise MoveManifestClosureError(f"Move closure root manifest is missing: {root_manifest}")

    visited: set[Path] = set()
    visiting: list[Path] = []
    manifests: set[Path] = set()
    dependency_counts = {"dependencies": 0, "dev-dependencies": 0}

    def read_manifest(path: Path, label: str) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise MoveManifestClosureError(f"{label} manifest is missing: {path}")
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise MoveManifestClosureError(f"{label} manifest is invalid: {path}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("package"), dict):
            raise MoveManifestClosureError(f"{label} manifest has no package table: {path}")
        package_name = document["package"].get("name")
        if not isinstance(package_name, str) or not package_name.strip():
            raise MoveManifestClosureError(f"{label} manifest has no package name: {path}")
        return document

    def local_dependency(manifest_path: Path, name: str, specification: Mapping[str, object], kind: str) -> Path:
        if set(specification) - {"local", "package"} or "local" not in specification:
            detail = ", ".join(sorted(str(key) for key in specification if key != "local"))
            raise MoveManifestClosureError(
                f"unapproved external dependency {name!r}: Move {kind} dependency is remote or malformed: {detail or 'missing local path'}"
            )
        local = specification["local"]
        if not isinstance(local, str) or not local.strip() or "\x00" in local:
            raise MoveManifestClosureError(f"Move {kind} dependency {name!r} has a malformed local path")
        target_root = contained(manifest_path.parent / local, f"{manifest_path}:{name}")
        if target_root.is_symlink() or not target_root.is_dir():
            raise MoveManifestClosureError(f"Move {kind} dependency {name!r} directory is missing")
        target = target_root / "Move.toml"
        if target.is_symlink() or not target.is_file():
            raise MoveManifestClosureError(f"Move {kind} dependency {name!r} has no Move.toml")
        target = contained(target, f"{manifest_path}:{name}/Move.toml")
        target_document = read_manifest(target, f"Move {kind} dependency {name!r}")
        alias = specification.get("package")
        if alias is not None:
            if not isinstance(alias, str) or not _PACKAGE_ALIAS_RE.fullmatch(alias):
                raise MoveManifestClosureError(f"Move {kind} dependency {name!r} has an invalid package alias")
            if target_document["package"].get("name") != alias:
                raise MoveManifestClosureError(
                    f"Move {kind} dependency {name!r} package alias does not match its local package"
                )
        return target

    def visit_lock(manifest_path: Path) -> None:
        lock_path = manifest_path.parent / "Move.lock"
        if not lock_path.exists():
            return
        if lock_path.is_symlink() or not lock_path.is_file():
            raise MoveManifestClosureError(f"Move.lock is not a regular file: {lock_path}")
        try:
            lock_document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise MoveManifestClosureError(f"Move.lock is invalid: {lock_path}") from exc
        pinned = lock_document.get("pinned", {})
        if not isinstance(pinned, Mapping):
            raise MoveManifestClosureError(f"Move.lock pins are malformed: {lock_path}")
        for scope, pins in pinned.items():
            if not isinstance(pins, Mapping):
                raise MoveManifestClosureError(f"Move.lock pin scope is malformed: {lock_path}:{scope}")
            for name, pin in pins.items():
                if not isinstance(pin, Mapping):
                    raise MoveManifestClosureError(f"Move.lock pin is malformed: {lock_path}:{name}")
                source = pin.get("source")
                if (pin.get("root") is True and source is None) or (
                    isinstance(source, Mapping) and dict(source) == {"root": True}
                ):
                    continue
                if not isinstance(source, Mapping) or "local" not in source:
                    detail = (
                        ", ".join(sorted(str(key) for key in source))
                        if isinstance(source, Mapping)
                        else "missing local source"
                    )
                    raise MoveManifestClosureError(
                        f"unapproved external dependency {name!r}: Move.lock source is remote or malformed: {lock_path}: {detail}"
                    )
                if set(source) != {"local"} or not isinstance(source["local"], str):
                    raise MoveManifestClosureError(f"Move.lock local source is malformed: {lock_path}:{name}")
                target = contained(lock_path.parent / source["local"], f"{lock_path}:{name}")
                if target.is_symlink() or not target.is_dir():
                    raise MoveManifestClosureError(f"Move.lock local source directory is missing: {lock_path}:{name}")
                target_manifest = target / "Move.toml"
                if target_manifest.is_symlink() or not target_manifest.is_file():
                    raise MoveManifestClosureError(f"Move.lock local source has no Move.toml: {lock_path}:{name}")
                visit(contained(target_manifest, f"{lock_path}:{name}/Move.toml"))

    def visit(path: Path) -> None:
        path = contained(path, str(path))
        if path in visiting:
            cycle = " -> ".join(str(item) for item in [*visiting, path])
            raise MoveManifestClosureError(f"Move manifest dependency cycle: {cycle}")
        if path in visited:
            return
        document = read_manifest(path, "Move")
        visiting.append(path)
        try:
            manifests.add(path)
            for kind in ("dependencies", "dev-dependencies"):
                dependencies = document.get(kind, {})
                if dependencies is None:
                    dependencies = {}
                if not isinstance(dependencies, Mapping):
                    raise MoveManifestClosureError(f"Move {kind} table is malformed: {path}")
                for name, specification in dependencies.items():
                    if not isinstance(name, str) or not isinstance(specification, Mapping):
                        raise MoveManifestClosureError(f"Move {kind} dependency is malformed: {path}")
                    dependency_counts[kind] += 1
                    visit(local_dependency(path, name, specification, kind))
            visit_lock(path)
        finally:
            visiting.pop()
        visited.add(path)

    visit(root_manifest)
    return {
        "root": str(root_manifest),
        "manifests": sorted(str(path) for path in manifests),
        "dependency_counts": {
            **dependency_counts,
            "runtime": dependency_counts["dependencies"],
            "dev": dependency_counts["dev-dependencies"],
            "total": sum(dependency_counts.values()),
        },
    }
