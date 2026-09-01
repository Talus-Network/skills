#!/usr/bin/env python3
"""Descriptor-safe canonical tree manifests for disposable build inputs.

The walker deliberately does not provide an atomic filesystem snapshot.  It
does provide a bounded validation operation: every directory and regular file
is opened relative to a no-follow directory descriptor, metadata is checked
before and after reads, roots are checked before and after traversal, and
symlink targets are resolved without following a path outside the root.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import unicodedata
from typing import Any, Mapping, Sequence


TREE_MANIFEST_SCHEMA_VERSION = 1
TREE_CHUNK_SIZE = 1024 * 1024
MAX_TREE_FILE_BYTES = 512 * 1024 * 1024
MAX_SYMLINK_RESOLUTION_DEPTH = 40


class TreeIntegrityError(ValueError):
    """Raised when a tree cannot be authenticated during one validation pass."""


def _canonical_json_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def tree_digest(entries: Sequence[Mapping[str, object]]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {"schema_version": TREE_MANIFEST_SCHEMA_VERSION, "entries": [dict(entry) for entry in entries]}
        )
    ).hexdigest()


def _require_descriptor_support() -> None:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise TreeIntegrityError("descriptor-safe tree hashing is unsupported on this host")
    if os.stat not in os.supports_dir_fd or os.open not in os.supports_dir_fd or os.readlink not in os.supports_dir_fd:
        raise TreeIntegrityError("descriptor-relative filesystem operations are unsupported on this host")
    if os.listdir not in os.supports_fd or not hasattr(os, "pread"):
        raise TreeIntegrityError("descriptor-relative directory listing is unsupported on this host")


def _flags(*, directory: bool = False) -> int:
    _require_descriptor_support()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _metadata_record(metadata: os.stat_result) -> dict[str, object]:
    # Directory byte sizes are filesystem-specific; canonical tree evidence
    # retains their mode while representing their logical size as zero.
    size = 0 if stat.S_ISDIR(metadata.st_mode) else metadata.st_size
    return {"mode": stat.S_IMODE(metadata.st_mode), "size": size}


def _normalised_component(name: str, label: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or "\\" in name
        or ":" in name
    ):
        raise TreeIntegrityError(f"{label} contains an unsupported path component")
    return unicodedata.normalize("NFC", name)


def _relative_key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative).casefold()


def _relative_path(parts: Sequence[str]) -> str:
    return "." if not parts else "/".join(parts)


def _relative_depth(relative: str) -> int:
    return 0 if relative == "." else relative.count("/") + 1


def _kind(metadata: os.stat_result) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    raise TreeIntegrityError("tree contains an unsupported special file")


def _ensure_same(label: str, before: os.stat_result, after: os.stat_result) -> None:
    if _metadata_identity(before) != _metadata_identity(after):
        raise TreeIntegrityError(f"{label} changed during tree validation")


def _ensure_same_location(label: str, before: os.stat_result, after: os.stat_result) -> None:
    if (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)) != (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
    ):
        raise TreeIntegrityError(f"{label} changed during tree validation")


@dataclass
class _RootDescriptor:
    path: Path
    resolved: Path
    fd: int
    metadata: os.stat_result
    path_metadata: os.stat_result

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self) -> "_RootDescriptor":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _open_root(root: Path, label: str = "tree root") -> _RootDescriptor:
    """Open an absolute root without following any path component."""

    _require_descriptor_support()
    root = Path(root)
    if not root.is_absolute():
        raise TreeIntegrityError(f"{label} must be an absolute directory")
    descriptors: list[int] = []
    safe_parts: list[str] = []
    try:
        descriptors.append(os.open(os.sep, _flags(directory=True)))
        for component in root.parts[1:]:
            if component in ("", "."):
                continue
            if component == "..":
                if len(descriptors) == 1:
                    raise TreeIntegrityError(f"{label} escapes the filesystem root")
                os.close(descriptors.pop())
                safe_parts.pop()
                continue
            try:
                path_metadata = os.stat(component, dir_fd=descriptors[-1], follow_symlinks=False)
                if stat.S_ISLNK(path_metadata.st_mode):
                    raise TreeIntegrityError(f"{label} must not contain symlink path components")
                if not stat.S_ISDIR(path_metadata.st_mode):
                    raise TreeIntegrityError(f"{label} path component is not a directory")
                child_fd = os.open(component, _flags(directory=True), dir_fd=descriptors[-1])
                child_metadata = os.fstat(child_fd)
            except OSError as exc:
                raise TreeIntegrityError(f"{label} cannot be opened without following links") from exc
            try:
                _ensure_same_location(f"{label} path component", path_metadata, child_metadata)
            except BaseException:
                os.close(child_fd)
                raise
            descriptors.append(child_fd)
            safe_parts.append(component)
        fd = descriptors[-1]
        metadata = os.fstat(fd)
        path_metadata = os.lstat(root)
        if stat.S_ISLNK(path_metadata.st_mode):
            raise TreeIntegrityError(f"{label} must be an absolute non-symlink directory")
        if not stat.S_ISDIR(metadata.st_mode):
            raise TreeIntegrityError(f"{label} is not a directory")
        _ensure_same(label, metadata, path_metadata)
        resolved = root.resolve(strict=True)
        safe_path = Path(os.sep, *safe_parts)
        if resolved != safe_path:
            raise TreeIntegrityError(f"{label} contains a symlink or path alias")
        resolved_metadata = os.lstat(resolved)
        _ensure_same(f"{label} resolved path", metadata, resolved_metadata)
        for descriptor in descriptors[:-1]:
            os.close(descriptor)
        return _RootDescriptor(root, resolved, fd, metadata, path_metadata)
    except TreeIntegrityError:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except (OSError, RuntimeError) as exc:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise TreeIntegrityError(f"{label} cannot be validated: {root}") from exc


def _verify_root_descriptor(root: _RootDescriptor, label: str = "tree root") -> None:
    try:
        descriptor_metadata = os.fstat(root.fd)
        path_metadata = os.lstat(root.path)
        resolved_metadata = os.lstat(root.resolved)
    except OSError as exc:
        raise TreeIntegrityError(f"{label} disappeared during validation") from exc
    if stat.S_ISLNK(path_metadata.st_mode):
        raise TreeIntegrityError(f"{label} was replaced by a symlink")
    _ensure_same(label, root.metadata, descriptor_metadata)
    _ensure_same(f"{label} path", root.metadata, path_metadata)
    _ensure_same(f"{label} resolved path", root.metadata, resolved_metadata)


def _read_regular_fd(fd: int, metadata: os.stat_result, label: str) -> str:
    if metadata.st_size < 0 or metadata.st_size > MAX_TREE_FILE_BYTES:
        raise TreeIntegrityError(f"{label} exceeds the bounded tree file size")
    before = os.fstat(fd)
    _ensure_same(label, metadata, before)
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, TREE_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_TREE_FILE_BYTES:
                raise TreeIntegrityError(f"{label} grew beyond the bounded tree file size")
            digest.update(chunk)
        after = os.fstat(fd)
    except OSError as exc:
        raise TreeIntegrityError(f"{label} cannot be read") from exc
    _ensure_same(label, before, after)
    if total != before.st_size:
        raise TreeIntegrityError(f"{label} changed while being read")
    first_digest = digest.hexdigest()
    second_digest = hashlib.sha256()
    second_total = 0
    try:
        offset = 0
        while True:
            chunk = os.pread(fd, TREE_CHUNK_SIZE, offset)
            if not chunk:
                break
            offset += len(chunk)
            second_total += len(chunk)
            if second_total > MAX_TREE_FILE_BYTES:
                raise TreeIntegrityError(f"{label} grew beyond the bounded tree file size")
            second_digest.update(chunk)
        second_after = os.fstat(fd)
    except OSError as exc:
        raise TreeIntegrityError(f"{label} cannot be reread safely") from exc
    _ensure_same(label, after, second_after)
    if second_total != after.st_size or second_digest.hexdigest() != first_digest:
        raise TreeIntegrityError(f"{label} content changed during tree validation")
    return first_digest


def _open_child_directory(parent_fd: int, name: str, metadata: os.stat_result, label: str) -> int:
    try:
        child_fd = os.open(name, _flags(directory=True), dir_fd=parent_fd)
        child_metadata = os.fstat(child_fd)
    except OSError as exc:
        raise TreeIntegrityError(f"{label} cannot be opened without following links") from exc
    try:
        _ensure_same(label, metadata, child_metadata)
    except BaseException:
        os.close(child_fd)
        raise
    if not stat.S_ISDIR(child_metadata.st_mode):
        os.close(child_fd)
        raise TreeIntegrityError(f"{label} is no longer a directory")
    return child_fd


@dataclass
class _ResolvedTarget:
    relative: str
    metadata: os.stat_result


def _resolve_symlink_target(
    root_fd: int,
    parent_parts: Sequence[str],
    target_text: str,
    root_device: int,
    label: str,
) -> _ResolvedTarget:
    """Resolve a link by descriptor-relative component walking.

    Absolute link targets are rejected deliberately.  Relative targets may
    use ``..`` only while remaining below the authenticated root.  Every
    intermediate symlink is resolved with the same no-follow descriptors and
    a bounded link count.
    """

    if not target_text or "\x00" in target_text:
        raise TreeIntegrityError(f"{label} has an invalid target")
    target_path = PurePosixPath(target_text)
    if target_path.is_absolute() or target_text.startswith("/"):
        raise TreeIntegrityError(f"{label} has an absolute target")
    pending: deque[str] = deque((*parent_parts, *target_path.parts))
    stack: list[tuple[int, list[str]]] = [(os.dup(root_fd), [])]
    links = 0
    try:
        while pending:
            raw_component = pending.popleft()
            if raw_component == ".":
                continue
            if raw_component == "..":
                if len(stack) == 1:
                    raise TreeIntegrityError(f"{label} escapes the authenticated tree root")
                fd, parts = stack.pop()
                os.close(fd)
                continue
            component = _normalised_component(raw_component, label)
            current_fd, current_parts = stack[-1]
            try:
                metadata = os.stat(raw_component, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                raise TreeIntegrityError(f"{label} target is unreadable") from exc
            if metadata.st_dev != root_device:
                raise TreeIntegrityError(f"{label} crosses a filesystem boundary")
            if stat.S_ISLNK(metadata.st_mode):
                links += 1
                if links > MAX_SYMLINK_RESOLUTION_DEPTH:
                    raise TreeIntegrityError(f"{label} has a cyclic symlink target")
                try:
                    nested = os.readlink(raw_component, dir_fd=current_fd)
                    nested_after = os.stat(raw_component, dir_fd=current_fd, follow_symlinks=False)
                except OSError as exc:
                    raise TreeIntegrityError(f"{label} target cannot be read") from exc
                _ensure_same(f"{label} target", metadata, nested_after)
                nested_path = PurePosixPath(nested)
                if nested_path.is_absolute() or nested.startswith("/"):
                    raise TreeIntegrityError(f"{label} escapes through an absolute symlink target")
                pending = deque((*nested_path.parts, *pending))
                continue
            if pending:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise TreeIntegrityError(f"{label} traverses a non-directory target")
                child_fd = _open_child_directory(current_fd, raw_component, metadata, f"{label} target")
                stack.append((child_fd, [*current_parts, component]))
                continue
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise TreeIntegrityError(f"{label} targets a special file")
            try:
                target_fd = os.open(raw_component, _flags(directory=stat.S_ISDIR(metadata.st_mode)), dir_fd=current_fd)
                target_after = os.fstat(target_fd)
            except OSError as exc:
                raise TreeIntegrityError(f"{label} target cannot be opened safely") from exc
            try:
                _ensure_same(f"{label} target", metadata, target_after)
            finally:
                os.close(target_fd)
            return _ResolvedTarget(_relative_path([*current_parts, component]), target_after)
        raise TreeIntegrityError(f"{label} has an empty target")
    finally:
        for fd, _parts in stack:
            try:
                os.close(fd)
            except OSError:
                pass


def _tree_entries_from_descriptor(root_descriptor: _RootDescriptor) -> list[dict[str, object]]:
    """Walk one already-open root descriptor without reopening its pathname."""

    root_fd = root_descriptor.fd
    root = root_descriptor.resolved
    entries: list[dict[str, object]] = []
    seen_paths: dict[str, str] = {}
    seen_inodes: dict[tuple[int, int], str] = {}
    symlinks: list[tuple[dict[str, object], _ResolvedTarget]] = []
    try:
        root_before = root_descriptor.metadata
        root_path_before = root_descriptor.path_metadata
        if not stat.S_ISDIR(root_before.st_mode):
            raise TreeIntegrityError("tree root is not a directory")
        root_device = root_before.st_dev

        def record_inode(relative: str, metadata: os.stat_result) -> None:
            if stat.S_ISLNK(metadata.st_mode):
                return
            identity = (metadata.st_dev, metadata.st_ino)
            previous = seen_inodes.get(identity)
            if previous is not None:
                raise TreeIntegrityError(f"hardlink/path collision between {previous!r} and {relative!r}")
            seen_inodes[identity] = relative

        def add_entry(relative: str, entry: dict[str, object], metadata: os.stat_result) -> dict[str, object]:
            key = _relative_key(relative)
            previous = seen_paths.get(key)
            if previous is not None:
                raise TreeIntegrityError(
                    f"tree path normalization collision between {previous!r} and {relative!r}"
                )
            seen_paths[key] = relative
            record_inode(relative, metadata)
            entry["path"] = relative
            entries.append(entry)
            return entry

        def walk(directory_fd: int, parent_parts: list[str], directory_metadata: os.stat_result) -> None:
            relative = _relative_path(parent_parts)
            directory_entry = add_entry(relative, {"type": "directory", **_metadata_record(directory_metadata)}, directory_metadata)
            child_start = len(entries)
            try:
                names = [os.fsdecode(name) for name in os.listdir(directory_fd)]
            except OSError as exc:
                raise TreeIntegrityError(f"tree directory cannot be listed: {relative}") from exc
            normalised_names: list[tuple[str, str]] = []
            child_keys: set[str] = set()
            for name in names:
                normalised = _normalised_component(name, relative)
                key = _relative_key(normalised)
                if key in child_keys:
                    raise TreeIntegrityError(f"tree directory has a path normalization collision: {relative}")
                child_keys.add(key)
                normalised_names.append((normalised, name))
            for component, actual_name in sorted(normalised_names, key=lambda item: item[0]):
                child_relative = _relative_path((*parent_parts, component))
                try:
                    metadata = os.stat(actual_name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise TreeIntegrityError(f"tree entry disappeared: {child_relative}") from exc
                if metadata.st_dev != root_device:
                    raise TreeIntegrityError(f"tree entry crosses a filesystem boundary: {child_relative}")
                kind = _kind(metadata)
                if kind == "directory":
                    child_fd = _open_child_directory(directory_fd, actual_name, metadata, child_relative)
                    try:
                        walk(child_fd, [*parent_parts, component], os.fstat(child_fd))
                    finally:
                        os.close(child_fd)
                elif kind == "file":
                    try:
                        child_fd = os.open(actual_name, _flags(), dir_fd=directory_fd)
                    except OSError as exc:
                        raise TreeIntegrityError(f"tree file cannot be opened safely: {child_relative}") from exc
                    try:
                        after_open = os.fstat(child_fd)
                        _ensure_same(child_relative, metadata, after_open)
                        digest = _read_regular_fd(child_fd, after_open, child_relative)
                        metadata_after_path = os.stat(
                            actual_name, dir_fd=directory_fd, follow_symlinks=False
                        )
                    finally:
                        os.close(child_fd)
                    _ensure_same(child_relative, metadata, metadata_after_path)
                    add_entry(
                        child_relative,
                        {"type": "file", **_metadata_record(after_open), "sha256": digest},
                        after_open,
                    )
                else:
                    try:
                        target_text = os.readlink(actual_name, dir_fd=directory_fd)
                    except OSError as exc:
                        raise TreeIntegrityError(f"tree symlink target cannot be read: {child_relative}") from exc
                    target = _resolve_symlink_target(
                        root_fd, parent_parts, target_text, root_device, f"symlink target {child_relative}"
                    )
                    try:
                        metadata_after = os.stat(actual_name, dir_fd=directory_fd, follow_symlinks=False)
                        target_after_text = os.readlink(actual_name, dir_fd=directory_fd)
                    except OSError as exc:
                        raise TreeIntegrityError(f"tree symlink changed during validation: {child_relative}") from exc
                    _ensure_same(child_relative, metadata, metadata_after)
                    if target_after_text != target_text:
                        raise TreeIntegrityError(f"tree symlink target changed during validation: {child_relative}")
                    entry = add_entry(
                        child_relative,
                        {
                            "type": "symlink",
                            **_metadata_record(metadata_after),
                            "target": target_text,
                            "target_path": target.relative,
                            "target_type": _kind(target.metadata),
                            "target_mode": stat.S_IMODE(target.metadata.st_mode),
                            "target_size": target.metadata.st_size,
                        },
                        metadata_after,
                    )
                    symlinks.append((entry, target))
            try:
                directory_after = os.fstat(directory_fd)
            except OSError as exc:
                raise TreeIntegrityError(f"tree directory disappeared: {relative}") from exc
            _ensure_same(relative, directory_metadata, directory_after)
            directory_entry["sha256"] = tree_digest(
                sorted(entries[child_start:], key=lambda item: str(item["path"]))
            )

        walk(root_fd, [], root_before)
        entry_by_path = {str(entry["path"]): entry for entry in entries}
        for entry, target in symlinks:
            target_entry = entry_by_path.get(target.relative)
            if target_entry is None or target_entry.get("type") != _kind(target.metadata):
                raise TreeIntegrityError(f"symlink target is absent from the authenticated tree: {entry['path']}")
            entry["target_sha256"] = target_entry.get("sha256")
            entry["sha256"] = hashlib.sha256(
                _canonical_json_bytes({key: value for key, value in entry.items() if key != "path"})
            ).hexdigest()
        directories = sorted(
            (entry for entry in entries if entry.get("type") == "directory"),
            key=lambda entry: _relative_depth(str(entry["path"])),
            reverse=True,
        )
        for directory in directories:
            directory_path = str(directory["path"])
            prefix = "" if directory_path == "." else f"{directory_path}/"
            descendants = [
                entry
                for entry in entries
                if str(entry["path"]) != directory_path
                and (not prefix or str(entry["path"]).startswith(prefix))
            ]
            directory["sha256"] = tree_digest(sorted(descendants, key=lambda entry: str(entry["path"])))
        entries.sort(key=lambda entry: str(entry["path"]))
        try:
            root_after = os.fstat(root_fd)
            root_path_after = os.lstat(root_descriptor.path)
        except OSError as exc:
            raise TreeIntegrityError("tree root disappeared during validation") from exc
        _ensure_same("tree root", root_before, root_after)
        _ensure_same("tree root path", root_path_before, root_path_after)
        _verify_root_descriptor(root_descriptor)
        return entries
    finally:
        pass


def tree_entries(root: Path) -> list[dict[str, object]]:
    """Return canonical entries after one descriptor-safe validation walk."""

    root_descriptor = _open_root(Path(root))
    try:
        return _tree_entries_from_descriptor(root_descriptor)
    finally:
        root_descriptor.close()


def validate_root(root: Path, label: str = "tree root") -> Path:
    """Check a root with a no-follow descriptor without walking its contents."""

    with _open_root(Path(root), label) as descriptor:
        _verify_root_descriptor(descriptor, label)
        return descriptor.resolved


def _manifest_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise TreeIntegrityError("tree manifest must be an absolute non-symlink path")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TreeIntegrityError(f"tree manifest is unavailable: {path}") from exc


def write_tree_manifest(root: Path, manifest_path: Path) -> dict[str, object]:
    descriptor = _open_root(Path(root))
    try:
        root = descriptor.resolved
        entries = _tree_entries_from_descriptor(descriptor)
        _verify_root_descriptor(descriptor)
    finally:
        descriptor.close()
    manifest_path = Path(manifest_path)
    if not manifest_path.is_absolute():
        raise TreeIntegrityError("tree manifest must be an absolute path")
    try:
        manifest_path.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise TreeIntegrityError("tree manifest must be outside the authenticated root")
    document: dict[str, object] = {
        "schema_version": TREE_MANIFEST_SCHEMA_VERSION,
        "root": str(root.resolve()),
        "entries": entries,
        "tree_sha256": tree_digest(entries),
    }
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.part")
    if manifest_path.is_symlink():
        raise TreeIntegrityError("tree manifest must be an absolute non-symlink path")
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.replace(manifest_path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise TreeIntegrityError(f"tree manifest cannot be written: {manifest_path}") from exc
    return {
        "path": str(manifest_path.resolve()),
        "root": str(root),
        "tree_sha256": str(document["tree_sha256"]),
        "entries": len(entries),
    }


def validate_tree_manifest(
    root: Path,
    manifest_path: Path,
    expected_tree_sha256: str,
    label: str = "tree",
) -> dict[str, object]:
    descriptor = _open_root(Path(root), label)
    try:
        root = descriptor.resolved
        manifest_path = _manifest_path(Path(manifest_path))
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TreeIntegrityError(f"{label} manifest is unreadable: {manifest_path}") from exc
        if not isinstance(document, dict) or document.get("schema_version") != TREE_MANIFEST_SCHEMA_VERSION:
            raise TreeIntegrityError(f"{label} manifest has an unsupported schema version")
        entries = document.get("entries")
        if document.get("root") != str(root) or not isinstance(entries, list) or any(
            not isinstance(entry, Mapping) for entry in entries
        ):
            raise TreeIntegrityError(f"{label} manifest root or entries are invalid")
        try:
            manifest_digest = tree_digest(entries)
        except (TypeError, ValueError) as exc:
            raise TreeIntegrityError(f"{label} manifest entries cannot be digested") from exc
        if document.get("tree_sha256") != manifest_digest:
            raise TreeIntegrityError(f"{label} manifest tree digest is invalid")
        actual_entries = _tree_entries_from_descriptor(descriptor)
        actual_digest = tree_digest(actual_entries)
        if entries != actual_entries:
            raise TreeIntegrityError(f"{label} tree entries do not match the recorded manifest")
        if actual_digest != expected_tree_sha256:
            raise TreeIntegrityError(f"{label} tree digest does not match the expected source/build digest")
        _verify_root_descriptor(descriptor, label)
        return {"manifest": str(manifest_path), "tree_sha256": actual_digest, "entries": len(actual_entries)}
    finally:
        descriptor.close()


def copy_verified_tree(
    source_root: Path,
    destination_root: Path,
    expected_tree_sha256: str,
    *,
    manifest_path: Path | None = None,
) -> dict[str, object]:
    """Copy an authenticated source tree and authenticate the destination."""

    source_descriptor = _open_root(Path(source_root), "authenticated source root")
    try:
        source_root = source_descriptor.resolved
        if tree_digest(_tree_entries_from_descriptor(source_descriptor)) != expected_tree_sha256:
            raise TreeIntegrityError("source tree does not match its expected digest before copy")
        _verify_root_descriptor(source_descriptor, "authenticated source root")
        destination_root = Path(destination_root)
        if not destination_root.is_absolute():
            raise TreeIntegrityError("authenticated source destination must be an absolute path")
        try:
            destination_metadata = os.lstat(destination_root)
        except FileNotFoundError:
            destination_metadata = None
        except OSError as exc:
            raise TreeIntegrityError(f"verified source destination cannot be inspected: {destination_root}") from exc
        if destination_metadata is not None:
            if stat.S_ISLNK(destination_metadata.st_mode):
                raise TreeIntegrityError(f"verified source destination must not be a symlink: {destination_root}")
            raise TreeIntegrityError(f"verified source destination already exists: {destination_root}")
        try:
            shutil.copytree(source_root, destination_root, symlinks=True)
            destination_descriptor = _open_root(destination_root, "verified source destination")
            try:
                copied_entries = _tree_entries_from_descriptor(destination_descriptor)
                _verify_root_descriptor(destination_descriptor, "verified source destination")
                destination_resolved = destination_descriptor.resolved
            finally:
                destination_descriptor.close()
            copied_digest = tree_digest(copied_entries)
            if copied_digest != expected_tree_sha256:
                raise TreeIntegrityError("authenticated source copy does not match the source tree digest")
            source_after_digest = tree_digest(_tree_entries_from_descriptor(source_descriptor))
            if source_after_digest != expected_tree_sha256:
                raise TreeIntegrityError("authenticated source root changed during the copy")
        except OSError as exc:
            shutil.rmtree(destination_root, ignore_errors=True)
            raise TreeIntegrityError(f"authenticated source copy failed: {destination_root}") from exc
        except BaseException:
            shutil.rmtree(destination_root, ignore_errors=True)
            raise
        evidence: dict[str, object] = {"root": str(destination_resolved), "tree_sha256": copied_digest}
        if manifest_path is not None:
            evidence.update(write_tree_manifest(destination_root, manifest_path))
        return evidence
    finally:
        source_descriptor.close()
