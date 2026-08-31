#!/usr/bin/env python3
"""Prepare public source evidence in a disposable, manifest-backed workspace.

The helper deliberately uses only the Python standard library.  It is useful to
skills, tests, and small consumers that need source evidence without relying on
the layout of the host workspace.  The CLI keeps a prepared workspace alive
until ``cleanup`` is called; the ``prepared_sources`` context manager cleans it
automatically for library callers. Only reviewed public repositories are in the
default catalog; callers cannot turn this helper into a filesystem source loader.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import sys
import tarfile
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import Callable, Iterator, Mapping, Sequence
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from tree_integrity import (
    TreeIntegrityError,
    copy_verified_tree,
    tree_digest,
    validate_root,
    validate_tree_manifest,
    write_tree_manifest,
)


SCHEMA_VERSION = 1
OWNER = "Talus-Network"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
# These limits are deliberately independent of the compressed download cap.
# Tar headers can describe very large expanded trees while the archive itself
# remains small, so every archive is preflighted before the first output file
# is created.
# The pinned public Sui source archive contains more than ten thousand safe
# regular-file/directory members. Keep the bound finite while allowing that
# reviewed archive to be authenticated as a whole.
MAX_ARCHIVE_MEMBERS = 50_000
MAX_MEMBER_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_EXPANSION_RATIO = 1_000.0
DEFAULT_ARCHIVE_DIRECTORY_MODE = 0o755
WORKSPACE_PREFIX = "skills-sources-"
MARKER_NAME = ".skills-source-workspace.json"
CHUNK_SIZE = 1024 * 1024
SCRIPT_PATH = Path(__file__).resolve()
PUBLIC_MOVE_PACKAGE_NAMES = (
    "primitives",
    "interface",
    "tool",
    "registry",
    "workflow",
    "scheduler",
)
PUBLIC_MOVE_PACKAGE_PATHS = tuple(f"packages/{name}" for name in PUBLIC_MOVE_PACKAGE_NAMES)
PUBLIC_MOVE_PACKAGE_TRANSITIVE_NAMES = ("kernel",)
PUBLIC_MOVE_PACKAGE_TRANSITIVE_PATHS = tuple(
    f"packages/{name}" for name in PUBLIC_MOVE_PACKAGE_TRANSITIVE_NAMES
)
PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES = PUBLIC_MOVE_PACKAGE_NAMES + PUBLIC_MOVE_PACKAGE_TRANSITIVE_NAMES
PUBLIC_MOVE_PACKAGE_CLOSURE_PATHS = PUBLIC_MOVE_PACKAGE_PATHS + PUBLIC_MOVE_PACKAGE_TRANSITIVE_PATHS
SUI_REPOSITORY = "MystenLabs/sui"
SUI_REVISION = "d8459684b41eb09ab23fe16a9dd84173270bbaba"
SUI_ARCHIVE_SHA256 = "1b974c1b10413e873b98df2740a877a930a08b40c80af58b52f737385f8bdf44"
SUI_TOOLCHAIN_VERSION = "sui 1.78.0-d8459684b41e"
SUI_FRAMEWORK_PACKAGE_NAMES = ("move-stdlib", "sui-framework")
SUI_FRAMEWORK_PACKAGE_PATHS = tuple(
    f"crates/sui-framework/packages/{name}" for name in SUI_FRAMEWORK_PACKAGE_NAMES
)
SUI_FRAMEWORK_REQUIRED_PATHS = (
    "crates/sui-framework/packages/move-stdlib/Move.toml",
    "crates/sui-framework/packages/move-stdlib/sources",
    "crates/sui-framework/packages/sui-framework/Move.toml",
    "crates/sui-framework/packages/sui-framework/sources",
)


@dataclass(frozen=True)
class RepositorySpec:
    """A repository that can be fetched as a GitHub archive."""

    logical_name: str
    repository: str
    ref: str
    required_paths: tuple[str, ...]
    archive_sha256: str | None = None
    allow_ignored_links: bool = False

    @property
    def archive_url(self) -> str:
        owner, name = self.repository.split("/", 1)
        return "https://" + f"codeload.github.com/{owner}/{name}/tar.gz/{quote(self.ref, safe='')}"

    @property
    def archive_prefix(self) -> str:
        return self.repository.rsplit("/", 1)[1] + "-"


DEFAULT_REPOSITORIES: Mapping[str, RepositorySpec] = {
    "nexus-sdk": RepositorySpec(
        logical_name="nexus-sdk",
        repository=f"{OWNER}/nexus-sdk",
        ref="1f67d5b02b7caa5411e449eb171eda5c178f83fe",
        required_paths=("Cargo.toml", "cli", "toolkit-rust"),
        archive_sha256="a6b25bb7d98bde41fe172afe673a28e7b7731ad51947a81d6cb615c144ed55b1",
    ),
    "nexus-move-packages": RepositorySpec(
        logical_name="nexus-move-packages",
        repository=f"{OWNER}/nexus-move-packages",
        ref="b070517238b83dd607e7ef6134d3ac413fdcc01f",
        required_paths=("README.md", *PUBLIC_MOVE_PACKAGE_CLOSURE_PATHS),
        archive_sha256="e88c6b977e87847f441564ecb9bcb75c269c5214d640721684139e93ddaa32f2",
    ),
    "sui": RepositorySpec(
        logical_name="sui",
        repository=SUI_REPOSITORY,
        ref=SUI_REVISION,
        required_paths=SUI_FRAMEWORK_REQUIRED_PATHS,
        archive_sha256=SUI_ARCHIVE_SHA256,
        allow_ignored_links=True,
    ),
}

APPROVED_PUBLIC_REPOSITORIES = frozenset(spec.repository for spec in DEFAULT_REPOSITORIES.values())


class SourcePreparationError(RuntimeError):
    """Raised when source preparation cannot prove a safe result."""


Downloader = Callable[[str, Path], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _source_preparation_signal_guard(cleanup: Callable[[], None] | None = None) -> Iterator[None]:
    """Abort preparation on INT/TERM and restore the caller's handlers afterwards."""

    previous: dict[int, object] = {}

    def abort(signum: int, _frame: object) -> None:
        failures: list[str] = []
        if cleanup is not None:
            try:
                with _ignore_source_preparation_signals():
                    cleanup()
            except BaseException as exc:
                failures.append(f"cleanup failed: {exc}")
        prior = previous.get(signum)
        if callable(prior) and prior is not abort:
            try:
                prior(signum, _frame)
            except BaseException as exc:
                failures.append(f"nested signal cleanup failed: {exc}")
        detail = "; ".join(failures)
        suffix = f"; {detail}" if detail else ""
        raise SourcePreparationError(f"source preparation interrupted by signal {signum}{suffix}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, abort)
        except (OSError, ValueError):
            continue
    try:
        yield
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass


@contextmanager
def _ignore_source_preparation_signals() -> Iterator[None]:
    """Prevent a second signal from interrupting best-effort workspace removal."""

    previous: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, signal.SIG_IGN)
        except (OSError, ValueError):
            continue
    try:
        yield
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass


def _validate_archive_url(url: object) -> str:
    if not isinstance(url, str) or not url or url != url.strip() or any(character.isspace() for character in url):
        raise SourcePreparationError("source archive URL must be canonical")
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise SourcePreparationError("source archive URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or hostname != "codeload.github.com"
        or parsed.netloc.lower() != "codeload.github.com"
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise SourcePreparationError("source archives must use the canonical HTTPS codeload destination")
    components = parsed.path.split("/")
    if len(components) != 5 or components[0] or components[3] != "tar.gz" or not components[1] or not components[2] or not components[4]:
        raise SourcePreparationError("source archive URL path is not canonical")
    decoded_components = [unquote(component) for component in components[1:]]
    if any(not component or component in {".", ".."} or "\x00" in component for component in decoded_components):
        raise SourcePreparationError("source archive URL contains an unsafe path")
    return "https://" + "codeload.github.com/" + "/".join(components[1:])


def _download_archive(url: str, destination: Path, timeout: float = 90.0) -> None:
    canonical_url = _validate_archive_url(url)
    request = Request(canonical_url, headers={"User-Agent": "talus-nexus-source-preparer/1"})
    try:
        response = urlopen(request, timeout=timeout)
    except OSError as exc:
        raise SourcePreparationError(f"source download failed: {canonical_url}: {exc}") from exc
    with response:
        geturl = getattr(response, "geturl", None)
        final_url = geturl() if callable(geturl) else geturl
        final_canonical_url = _validate_archive_url(final_url)
        if final_canonical_url != canonical_url:
            raise SourcePreparationError("source archive redirect changed the reviewed destination")
        length_header = response.headers.get("Content-Length")
        if length_header:
            try:
                if int(length_header) > MAX_ARCHIVE_BYTES:
                    raise SourcePreparationError(f"source archive is larger than {MAX_ARCHIVE_BYTES} bytes")
            except ValueError as exc:
                raise SourcePreparationError("source archive has an invalid Content-Length") from exc
        written = 0
        try:
            with destination.open("xb") as target:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise SourcePreparationError(f"source archive is larger than {MAX_ARCHIVE_BYTES} bytes")
                    target.write(chunk)
        except SourcePreparationError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise SourcePreparationError(f"could not write source archive: {destination}: {exc}") from exc


def _safe_member_parts(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name:
        raise SourcePreparationError("archive contains an empty or NUL-containing member name")
    path = PurePosixPath(name)
    if path.is_absolute() or name.startswith("/"):
        raise SourcePreparationError(f"archive contains an absolute member: {name!r}")
    parts = path.parts
    if not parts or any(
        part in ("", ".", "..") or "\\" in part or ":" in part for part in parts
    ):
        raise SourcePreparationError(f"archive contains an unsafe member path: {name!r}")
    return parts


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _temporary_parent() -> Path:
    return Path(tempfile.gettempdir()).resolve()


@dataclass(frozen=True)
class _ArchiveMember:
    name: str
    kind: str
    size: int
    mode: int
    typeflag: bytes
    linkname: str
    content_sha256: str | None = None


@dataclass(frozen=True)
class _ArchiveScan:
    top_level: str
    top_level_canonical: str
    members: tuple[_ArchiveMember, ...]
    expanded_bytes: int
    compressed_bytes: int
    expected_entries: tuple[dict[str, object], ...]
    expected_tree_sha256: str
    ignored_members: tuple[str, ...] = ()


def _archive_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class _StableArchive:
    """Keep one no-follow archive descriptor across preflight and extraction."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise SourcePreparationError("source archive must be an absolute non-symlink file")
        if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_CLOEXEC")):
            raise SourcePreparationError("stable archive validation is unsupported on this host")
        self.fd = -1
        try:
            path_metadata = os.lstat(self.path)
            if not stat.S_ISREG(path_metadata.st_mode):
                raise SourcePreparationError(f"source archive is not a regular file: {self.path}")
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            descriptor_metadata = os.fstat(self.fd)
        except OSError as exc:
            if self.fd >= 0:
                os.close(self.fd)
            raise SourcePreparationError(f"source archive cannot be opened safely: {self.path}") from exc
        try:
            if _archive_identity(path_metadata) != _archive_identity(descriptor_metadata):
                raise SourcePreparationError(f"source archive changed while opening: {self.path}")
            self.compressed_bytes = descriptor_metadata.st_size
            if self.compressed_bytes <= 0:
                raise SourcePreparationError(f"source archive is empty: {self.path}")
            if self.compressed_bytes > MAX_ARCHIVE_BYTES:
                raise SourcePreparationError(
                    f"source archive is larger than {MAX_ARCHIVE_BYTES} bytes: {self.path}"
                )
            self.archive_sha256 = self._digest_descriptor("source archive")
            self._path_metadata = path_metadata
            self._descriptor_metadata = descriptor_metadata
        except BaseException:
            os.close(self.fd)
            raise

    def _digest_descriptor(self, label: str) -> str:
        try:
            before = os.fstat(self.fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARCHIVE_BYTES:
                raise SourcePreparationError(f"{label} is no longer a bounded regular file: {self.path}")
            os.lseek(self.fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(self.fd, CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise SourcePreparationError(f"{label} grew beyond its size limit: {self.path}")
                digest.update(chunk)
            after = os.fstat(self.fd)
        except OSError as exc:
            raise SourcePreparationError(f"{label} cannot be read: {self.path}") from exc
        if _archive_identity(before) != _archive_identity(after) or total != before.st_size:
            raise SourcePreparationError(f"{label} changed while being read: {self.path}")
        return digest.hexdigest()

    def assert_stable(self, label: str) -> None:
        try:
            path_metadata = os.lstat(self.path)
            descriptor_metadata = os.fstat(self.fd)
        except OSError as exc:
            raise SourcePreparationError(f"{label} disappeared: {self.path}") from exc
        if _archive_identity(path_metadata) != _archive_identity(self._path_metadata):
            raise SourcePreparationError(f"{label} pathname now names a different archive: {self.path}")
        if _archive_identity(descriptor_metadata) != _archive_identity(self._descriptor_metadata):
            raise SourcePreparationError(f"{label} descriptor identity changed: {self.path}")
        if self._digest_descriptor(label) != self.archive_sha256:
            raise SourcePreparationError(f"{label} digest changed: {self.path}")

    @contextmanager
    def open_tar(self) -> Iterator[tarfile.TarFile]:
        self.assert_stable("source archive before tar pass")
        stream = None
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            duplicate = os.dup(self.fd)
            stream = os.fdopen(duplicate, "rb", closefd=True)
            with tarfile.open(fileobj=stream, mode="r:*") as bundle:
                yield bundle
        except (OSError, tarfile.TarError) as exc:
            raise SourcePreparationError(f"source archive cannot be read through its stable descriptor") from exc
        finally:
            if stream is not None:
                stream.close()

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self) -> _StableArchive:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _archive_member_spec(member: tarfile.TarInfo) -> _ArchiveMember:
    parts = _safe_member_parts(member.name)
    if member.issym() or member.islnk():
        raise SourcePreparationError(f"source archive contains links which are not allowed: {member.name!r}")
    if member.isdir():
        kind = "directory"
    elif member.isfile():
        kind = "file"
    else:
        raise SourcePreparationError(f"source archive contains an unsafe member: {member.name!r}")
    if member.size < 0 or member.size > MAX_MEMBER_UNCOMPRESSED_BYTES:
        raise SourcePreparationError(
            f"source archive member is larger than {MAX_MEMBER_UNCOMPRESSED_BYTES} bytes: {member.name!r}"
        )
    mode = stat.S_IMODE(member.mode)
    if kind == "directory":
        mode |= (mode & 0o444) >> 2
        mode |= 0o700
    return _ArchiveMember(member.name, kind, member.size, mode, member.type, member.linkname)


def _canonical_archive_member_path(parts: Sequence[str]) -> str:
    return "/".join(unicodedata.normalize("NFC", part) for part in parts)


def _read_archive_member(bundle: tarfile.TarFile, member: tarfile.TarInfo, expected_size: int) -> str:
    extracted = bundle.extractfile(member)
    if extracted is None:
        raise SourcePreparationError(f"source archive member cannot be read: {member.name!r}")
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = extracted.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size or total > MAX_MEMBER_UNCOMPRESSED_BYTES:
                raise SourcePreparationError(f"source archive member exceeds its declared size: {member.name!r}")
            digest.update(chunk)
    except OSError as exc:
        raise SourcePreparationError(f"source archive member cannot be read: {member.name!r}") from exc
    finally:
        extracted.close()
    if total != expected_size:
        raise SourcePreparationError(f"source archive member size changed: {member.name!r}")
    return digest.hexdigest()


def _archive_tree_projection(
    members: Sequence[_ArchiveMember],
) -> tuple[tuple[dict[str, object], ...], str]:
    """Derive the extracted-root tree evidence without creating files."""

    entries_by_path: dict[str, dict[str, object]] = {}
    path_keys: dict[str, str] = {}
    for member in members:
        parts = _safe_member_parts(member.name)
        relative_parts = tuple(parts[1:])
        for index in range(len(relative_parts) + 1):
            raw_prefix = relative_parts[:index]
            relative = _relative_archive_path(raw_prefix)
            key = unicodedata.normalize("NFC", relative).casefold()
            previous = path_keys.get(key)
            if previous is not None and previous != relative:
                raise SourcePreparationError(
                    f"source archive path normalization collision between {previous!r} and {relative!r}"
                )
            path_keys[key] = relative
            if relative in entries_by_path and entries_by_path[relative]["type"] == "file" and index < len(relative_parts):
                raise SourcePreparationError(f"source archive file is also a directory: {member.name!r}")
            if index < len(relative_parts) or member.kind == "directory":
                existing = entries_by_path.get(relative)
                if existing is not None and existing.get("type") == "file":
                    raise SourcePreparationError(f"source archive file is also a directory: {member.name!r}")
                if existing is None:
                    mode = member.mode if index == len(relative_parts) and member.kind == "directory" else DEFAULT_ARCHIVE_DIRECTORY_MODE
                    existing = {
                        "type": "directory",
                        "mode": mode,
                        "size": 0,
                        "path": relative,
                    }
                    entries_by_path[relative] = existing
                elif index == len(relative_parts) and member.kind == "directory":
                    existing["mode"] = member.mode
                continue
            existing = entries_by_path.get(relative)
            if existing is not None:
                raise SourcePreparationError(f"source archive contains a duplicate or conflicting member: {member.name!r}")
            if member.content_sha256 is None:
                raise SourcePreparationError(f"source archive file digest is unavailable: {member.name!r}")
            entries_by_path[relative] = {
                "type": "file",
                "mode": member.mode,
                "size": member.size,
                "sha256": member.content_sha256,
                "path": relative,
            }
    if entries_by_path.get(".", {}).get("type") != "directory":
        raise SourcePreparationError("source archive identity root is not a directory")
    entries = list(entries_by_path.values())
    directories = sorted(
        (entry for entry in entries if entry.get("type") == "directory"),
        key=lambda entry: 0 if entry["path"] == "." else str(entry["path"]).count("/") + 1,
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
        directory["sha256"] = tree_digest(sorted(descendants, key=lambda item: str(item["path"])))
    entries.sort(key=lambda entry: str(entry["path"]))
    return tuple(entries), tree_digest(entries)


def _relative_archive_path(parts: Sequence[str]) -> str:
    return "." if not parts else "/".join(unicodedata.normalize("NFC", part) for part in parts)


def _scan_archive_object(
    archive: _StableArchive,
    expected_prefix: str,
    *,
    allow_ignored_links: bool = False,
) -> _ArchiveScan:
    archive.assert_stable("source archive before preflight")
    members: list[_ArchiveMember] = []
    ignored_members: list[str] = []
    top_levels: dict[str, str] = {}
    seen: set[str] = set()
    expanded_bytes = 0
    archive_member_count = 0
    try:
        with archive.open_tar() as bundle:
            while True:
                member = bundle.next()
                if member is None:
                    break
                archive_member_count += 1
                if archive_member_count > MAX_ARCHIVE_MEMBERS:
                    raise SourcePreparationError(
                        f"source archive contains too many members (maximum {MAX_ARCHIVE_MEMBERS})"
                    )
                if member.issym() or member.islnk():
                    if not allow_ignored_links:
                        _archive_member_spec(member)
                    parts = _safe_member_parts(member.name)
                    canonical = _canonical_archive_member_path(parts).casefold()
                    if canonical in seen:
                        raise SourcePreparationError(f"source archive contains a duplicate member: {member.name!r}")
                    seen.add(canonical)
                    ignored_members.append(member.name)
                    continue
                spec = _archive_member_spec(member)
                parts = _safe_member_parts(spec.name)
                canonical = _canonical_archive_member_path(parts).casefold()
                if canonical in seen:
                    raise SourcePreparationError(f"source archive contains a duplicate member: {spec.name!r}")
                seen.add(canonical)
                if spec.kind == "file":
                    spec = replace(spec, content_sha256=_read_archive_member(bundle, member, spec.size))
                members.append(spec)
                top_levels.setdefault(unicodedata.normalize("NFC", parts[0]), parts[0])
                expanded_bytes += spec.size
                if expanded_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise SourcePreparationError(
                        f"source archive expands beyond {MAX_ARCHIVE_UNCOMPRESSED_BYTES} bytes"
                    )
    except (OSError, tarfile.TarError) as exc:
        raise SourcePreparationError("source archive preflight cannot read its stable descriptor") from exc
    if not members:
        raise SourcePreparationError(f"source archive is empty: {archive.path}")
    expansion_ratio = expanded_bytes / archive.compressed_bytes
    if expansion_ratio > MAX_ARCHIVE_EXPANSION_RATIO:
        raise SourcePreparationError(
            "source archive expansion ratio exceeds "
            f"{MAX_ARCHIVE_EXPANSION_RATIO:g}:1"
        )
    if len(top_levels) != 1:
        raise SourcePreparationError(f"source archive has ambiguous roots: {sorted(top_levels)!r}")
    top_level_canonical = next(iter(top_levels))
    top_level = top_levels[top_level_canonical]
    if not top_level_canonical.startswith(expected_prefix):
        raise SourcePreparationError(f"source archive identity mismatch: {top_level_canonical!r}")
    expected_entries, expected_tree_sha256 = _archive_tree_projection(members)
    archive.assert_stable("source archive after preflight")
    return _ArchiveScan(
        top_level,
        top_level_canonical,
        tuple(members),
        expanded_bytes,
        archive.compressed_bytes,
        expected_entries,
        expected_tree_sha256,
        tuple(ignored_members),
    )


def _extract_archive_object(
    archive: _StableArchive,
    scan: _ArchiveScan,
    destination: Path,
    *,
    allow_ignored_links: bool = False,
) -> Path:
    seen: set[str] = set()
    expanded_bytes = 0
    member_count = 0
    archive_member_count = 0
    ignored_seen: list[str] = []
    directory_targets: dict[tuple[str, ...], tuple[Path, int]] = {}
    try:
        with archive.open_tar() as bundle:
            expected_index = 0
            while True:
                member = bundle.next()
                if member is None:
                    break
                archive_member_count += 1
                if archive_member_count > MAX_ARCHIVE_MEMBERS:
                    raise SourcePreparationError(
                        f"source archive contains too many members (maximum {MAX_ARCHIVE_MEMBERS})"
                    )
                if member.issym() or member.islnk():
                    if not allow_ignored_links:
                        _archive_member_spec(member)
                    if expected_index > len(scan.members) or member.name not in scan.ignored_members:
                        raise SourcePreparationError(
                            f"source archive ignored link set changed during extraction: {member.name!r}"
                        )
                    ignored_seen.append(member.name)
                    continue
                if expected_index >= len(scan.members):
                    raise SourcePreparationError("source archive member set changed during extraction")
                expected = scan.members[expected_index]
                expected_index += 1
                actual = _archive_member_spec(member)
                if replace(actual, content_sha256=expected.content_sha256) != expected:
                    raise SourcePreparationError(
                        f"source archive member changed during extraction: {actual.name!r}"
                    )
                member_count += 1
                if member_count > MAX_ARCHIVE_MEMBERS:
                    raise SourcePreparationError(
                        f"source archive contains too many members (maximum {MAX_ARCHIVE_MEMBERS})"
                    )
                expanded_bytes += actual.size
                if expanded_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise SourcePreparationError(
                        f"source archive expands beyond {MAX_ARCHIVE_UNCOMPRESSED_BYTES} bytes"
                    )
                parts = _safe_member_parts(actual.name)
                if unicodedata.normalize("NFC", parts[0]) != scan.top_level_canonical:
                    raise SourcePreparationError(
                        f"archive top-level root changed during extraction: {actual.name!r}"
                    )
                target = destination.joinpath(*parts)
                if not _inside(destination, target):
                    raise SourcePreparationError(f"archive member escapes extraction root: {actual.name!r}")
                canonical = _canonical_archive_member_path(parts).casefold()
                if canonical in seen:
                    raise SourcePreparationError(f"source archive contains a duplicate member: {actual.name!r}")
                seen.add(canonical)
                if actual.kind == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                    directory_targets[tuple(parts)] = (target, actual.mode)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise SourcePreparationError(f"archive member cannot be read: {actual.name!r}")
                digest = hashlib.sha256()
                total = 0
                with extracted, target.open("xb") as output:
                    while True:
                        chunk = extracted.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > actual.size or total > MAX_MEMBER_UNCOMPRESSED_BYTES:
                            raise SourcePreparationError(
                                f"source archive member exceeds its declared size: {actual.name!r}"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fchmod(output.fileno(), actual.mode)
                if total != actual.size or digest.hexdigest() != expected.content_sha256:
                    raise SourcePreparationError(f"source archive member content changed: {actual.name!r}")
                for index in range(1, len(parts)):
                    parent_parts = tuple(parts[:index])
                    directory_targets.setdefault(
                        parent_parts,
                        (destination.joinpath(*parent_parts), DEFAULT_ARCHIVE_DIRECTORY_MODE),
                    )
            if expected_index != len(scan.members) or tuple(ignored_seen) != scan.ignored_members:
                raise SourcePreparationError("source archive member set changed during extraction")
    except (OSError, tarfile.TarError) as exc:
        raise SourcePreparationError(f"source archive extraction failed: {archive.path}") from exc
    if member_count != len(scan.members) or expanded_bytes != scan.expanded_bytes:
        raise SourcePreparationError("source archive bounds changed during extraction")
    if expanded_bytes / scan.compressed_bytes > MAX_ARCHIVE_EXPANSION_RATIO:
        raise SourcePreparationError("source archive expansion ratio changed during extraction")
    for parts, (target, mode) in sorted(directory_targets.items(), key=lambda item: len(item[0]), reverse=True):
        try:
            os.chmod(target, mode, follow_symlinks=False)
        except OSError as exc:
            raise SourcePreparationError(f"source archive directory cannot be finalized: {'/'.join(parts)}") from exc
    archive.assert_stable("source archive after extraction")
    return destination / scan.top_level


def safe_extract_archive(
    archive: Path,
    destination: Path,
    expected_prefix: str,
    *,
    allow_ignored_links: bool = False,
) -> Path:
    """Extract a preflighted archive through one stable descriptor."""

    destination.mkdir(parents=True, exist_ok=False)
    try:
        with _StableArchive(archive) as stable:
            scan = _scan_archive_object(stable, expected_prefix, allow_ignored_links=allow_ignored_links)
            root = _extract_archive_object(stable, scan, destination, allow_ignored_links=allow_ignored_links)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    try:
        return validate_root(root, "archive identity root")
    except TreeIntegrityError as exc:
        raise SourcePreparationError("archive identity root is not a safe directory") from exc


def _validate_required_paths(root: Path, required_paths: Sequence[str]) -> None:
    for relative in required_paths:
        parts = _safe_member_parts(relative)
        candidate = root.joinpath(*parts)
        if not _inside(root, candidate) or not candidate.exists():
            raise SourcePreparationError(f"source identity is missing required path: {relative}")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _marker_path(workspace: Path) -> Path:
    return workspace / MARKER_NAME


def _write_marker(workspace: Path, specs: Sequence[RepositorySpec]) -> None:
    marker = {
        "owner": "talus-nexus-source-preparer",
        "schema_version": SCHEMA_VERSION,
        "workspace": str(workspace.resolve()),
        "created_at": _utc_now(),
        "nonce": base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("="),
        "specifications": {
            spec.logical_name: {
                "repository": spec.repository,
                "ref": spec.ref,
                "required_paths": list(spec.required_paths),
                "reviewed_archive_sha256": spec.archive_sha256,
                "allow_ignored_links": spec.allow_ignored_links,
            }
            for spec in specs
        },
    }
    _write_json(_marker_path(workspace), marker)


def _read_marker(workspace: Path) -> dict[str, object]:
    marker_path = _marker_path(workspace)
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(f"workspace marker is unavailable or invalid: {workspace}") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("owner") != "talus-nexus-source-preparer"
        or marker.get("schema_version") != SCHEMA_VERSION
        or marker.get("workspace") != str(workspace.resolve())
        or not isinstance(marker.get("specifications"), dict)
    ):
        raise SourcePreparationError(f"workspace marker does not belong to the source preparer: {workspace}")
    return marker


def _normalise_specs(
    specs: Mapping[str, RepositorySpec] | None,
    overrides: Mapping[str, str] | None,
    checksums: Mapping[str, str] | None,
    only: Sequence[str] | None = None,
) -> list[RepositorySpec]:
    custom_catalog = specs is not None
    selected = dict(specs if specs is not None else DEFAULT_REPOSITORIES)
    supplied_checksums = dict(checksums or {})
    unknown_checksums = sorted(set(supplied_checksums) - set(selected))
    if unknown_checksums:
        raise SourcePreparationError(f"unknown logical repository checksum: {', '.join(unknown_checksums)}")
    for logical_name, checksum in supplied_checksums.items():
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise SourcePreparationError(f"source archive checksum is invalid: {logical_name}")
    if only:
        unknown = sorted(set(only) - set(selected))
        if unknown:
            raise SourcePreparationError(f"unknown logical repository: {', '.join(unknown)}")
        selected = {name: selected[name] for name in dict.fromkeys(only)}
    for logical_name, checksum in supplied_checksums.items():
        current = selected.get(logical_name)
        if current is not None:
            reviewed = DEFAULT_REPOSITORIES.get(logical_name)
            if not custom_catalog and reviewed is not None and checksum != reviewed.archive_sha256:
                raise SourcePreparationError(
                    f"source archive checksum is not the pinned reviewed checksum: {logical_name}"
                )
            selected[logical_name] = RepositorySpec(
                logical_name=current.logical_name,
                repository=current.repository,
                ref=current.ref,
                required_paths=current.required_paths,
                archive_sha256=checksum,
                allow_ignored_links=current.allow_ignored_links,
            )
    for logical_name, ref in (overrides or {}).items():
        if logical_name not in selected:
            raise SourcePreparationError(f"unknown logical repository: {logical_name}")
        if not ref.strip():
            raise SourcePreparationError(f"empty repository ref: {logical_name}")
        current = selected[logical_name]
        reviewed = DEFAULT_REPOSITORIES.get(logical_name)
        if not custom_catalog and reviewed is not None and ref != reviewed.ref:
            raise SourcePreparationError(f"repository ref is pinned to the reviewed revision: {logical_name}")
        selected[logical_name] = RepositorySpec(
            logical_name=current.logical_name,
            repository=current.repository,
            ref=ref,
            required_paths=current.required_paths,
            archive_sha256=supplied_checksums.get(
                logical_name,
                current.archive_sha256 if ref == current.ref else None,
            ),
            allow_ignored_links=current.allow_ignored_links,
        )
    for logical_name, ref in (overrides or {}).items():
        current = (specs if specs is not None else DEFAULT_REPOSITORIES).get(logical_name)
        if current is None or ref != current.ref:
            if logical_name not in supplied_checksums:
                raise SourcePreparationError(
                    f"configurable repository ref requires a caller-supplied archive checksum: {logical_name}"
                )
    for logical_name, spec in selected.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", logical_name):
            raise SourcePreparationError(f"invalid logical repository name: {logical_name}")
        owner, name = spec.repository.split("/", 1) if "/" in spec.repository else ("", "")
        if custom_catalog:
            if owner != OWNER or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise SourcePreparationError(f"repository is not an approved fixture source: {spec.repository}")
        else:
            reviewed = DEFAULT_REPOSITORIES.get(logical_name)
            if reviewed is None or spec.repository != reviewed.repository:
                raise SourcePreparationError(f"repository is not an approved public source: {spec.repository}")
            if spec.ref != reviewed.ref:
                raise SourcePreparationError(f"repository ref is pinned to the reviewed revision: {logical_name}")
            if spec.required_paths != reviewed.required_paths:
                raise SourcePreparationError(f"repository markers differ from the reviewed specification: {logical_name}")
            if spec.archive_sha256 != reviewed.archive_sha256:
                raise SourcePreparationError(f"source archive checksum is not the pinned reviewed checksum: {logical_name}")
        if not spec.ref or any(ord(char) < 0x20 for char in spec.ref):
            raise SourcePreparationError(f"repository ref is invalid: {logical_name}")
        if spec.archive_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", spec.archive_sha256):
            raise SourcePreparationError(f"source archive checksum is invalid: {logical_name}")
    return [selected[name] for name in sorted(selected)]


def _prepare_sources_for_specs_unprotected(
    *,
    specs: Mapping[str, RepositorySpec] | None = None,
    overrides: Mapping[str, str] | None = None,
    checksums: Mapping[str, str] | None = None,
    only: Sequence[str] | None = None,
    downloader: Downloader = _download_archive,
    workspace_parent: Path | None = None,
    workspace_created: Callable[[Path], None] | None = None,
    cleanup: Callable[[Path], None] | None = None,
) -> dict[str, object]:
    """Download and validate sources, returning a machine-readable manifest.

    This private seam is reserved for deterministic tests; production callers
    must use ``prepare_sources`` and cannot replace the HTTPS-only downloader.
    """

    parent = workspace_parent.resolve() if workspace_parent else None
    if parent is not None and not _inside(_temporary_parent(), parent):
        raise SourcePreparationError("custom workspace parent must be inside the runtime temporary directory")
    workspace: Path | None = None
    try:
        workspace = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=str(parent) if parent else None))
        if workspace_created is not None:
            workspace_created(workspace)
        selected_specs = _normalise_specs(specs, overrides, checksums, only)
        _write_marker(workspace, selected_specs)
        repositories: dict[str, object] = {}
        archive_evidence: dict[str, object] = {}
        for spec in selected_specs:
            archive = workspace / "archives" / f"{spec.logical_name}.tar.gz"
            archive.parent.mkdir(parents=True, exist_ok=True)
            extracted = workspace / "sources" / spec.logical_name
            extracted.parent.mkdir(parents=True, exist_ok=True)
            downloader(spec.archive_url, archive)
            if not archive.is_file() or archive.is_symlink():
                raise SourcePreparationError(f"downloaded archive is missing or unsafe: {spec.logical_name}")
            try:
                with _StableArchive(archive) as stable_archive:
                    archive_sha256 = stable_archive.archive_sha256
                    if spec.archive_sha256 is not None and archive_sha256 != spec.archive_sha256:
                        raise SourcePreparationError(
                            f"source archive checksum does not match the reviewed specification: {spec.logical_name}"
                        )
                    scan = _scan_archive_object(
                        stable_archive,
                        spec.archive_prefix,
                        allow_ignored_links=spec.allow_ignored_links,
                    )
                    root = _extract_archive_object(
                        stable_archive,
                        scan,
                        extracted,
                        allow_ignored_links=spec.allow_ignored_links,
                    )
            except SourcePreparationError:
                raise
            try:
                root = validate_root(root, f"extracted source {spec.logical_name}")
            except TreeIntegrityError as exc:
                raise SourcePreparationError(f"extracted source root is not a safe directory: {spec.logical_name}") from exc
            _validate_required_paths(root, spec.required_paths)
            tree_manifest_path = workspace / "trees" / f"{spec.logical_name}.json"
            try:
                source_tree = write_tree_manifest(root, tree_manifest_path)
            except TreeIntegrityError as exc:
                raise SourcePreparationError(
                    f"extracted source tree cannot be authenticated: {spec.logical_name}"
                ) from exc
            if source_tree["tree_sha256"] != scan.expected_tree_sha256:
                raise SourcePreparationError(
                    f"extracted source tree does not match the archive-derived digest: {spec.logical_name}"
                )
            archive_evidence[spec.logical_name] = {
                "archive_relative": str(archive.relative_to(workspace)),
                "archive_sha256": archive_sha256,
                "reviewed_archive_sha256": spec.archive_sha256,
                "root_relative": str(root.relative_to(workspace)),
                "source_tree_manifest_relative": str(tree_manifest_path.relative_to(workspace)),
                "source_tree_sha256": source_tree["tree_sha256"],
                "archive_tree_sha256": scan.expected_tree_sha256,
                "archive_tree_entries": len(scan.expected_entries),
                "ignored_archive_links": list(scan.ignored_members),
            }
            repositories[spec.logical_name] = {
                "logical_name": spec.logical_name,
                "repository": spec.repository,
                "ref": spec.ref,
                "archive_url": spec.archive_url,
                "archive_sha256": archive_sha256,
                "reviewed_archive_sha256": spec.archive_sha256,
                "archive_relative": str(archive.relative_to(workspace)),
                "root": str(source_tree["root"]),
                "root_relative": str(root.relative_to(workspace)),
                "source_tree_manifest": str(tree_manifest_path.resolve()),
                "source_tree_manifest_relative": str(tree_manifest_path.relative_to(workspace)),
                "source_tree_sha256": source_tree["tree_sha256"],
                "archive_tree_sha256": scan.expected_tree_sha256,
                "archive_tree_entries": len(scan.expected_entries),
                "ignored_archive_links": list(scan.ignored_members),
                "required_paths": list(spec.required_paths),
                "allow_ignored_links": spec.allow_ignored_links,
            }
        marker = _read_marker(workspace)
        marker["archives"] = archive_evidence
        _write_json(_marker_path(workspace), marker)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": _utc_now(),
            "workspace": str(workspace.resolve()),
            "manifest": str((workspace / "manifest.json").resolve()),
            "repositories": repositories,
            "cleanup": {
                "command": [str(SCRIPT_PATH), "cleanup", "--manifest", str((workspace / "manifest.json").resolve())],
            },
        }
        _write_json(workspace / "manifest.json", manifest)
        return manifest
    except BaseException as primary:
        if workspace is not None:
            try:
                with _ignore_source_preparation_signals():
                    if cleanup is not None:
                        cleanup(workspace)
                    else:
                        _cleanup_created_workspace(workspace)
            except BaseException as cleanup_error:
                _attach_cleanup_failure(primary, cleanup_error)
        raise


def _prepare_sources_for_specs(
    *,
    specs: Mapping[str, RepositorySpec] | None = None,
    overrides: Mapping[str, str] | None = None,
    checksums: Mapping[str, str] | None = None,
    only: Sequence[str] | None = None,
    downloader: Downloader = _download_archive,
    workspace_parent: Path | None = None,
) -> dict[str, object]:
    """Prepare sources under an INT/TERM guard that covers the full workspace lifecycle."""

    with _source_preparation_signal_guard():
        return _prepare_sources_for_specs_unprotected(
            specs=specs,
            overrides=overrides,
            checksums=checksums,
            only=only,
            downloader=downloader,
            workspace_parent=workspace_parent,
        )


def prepare_sources(
    *,
    overrides: Mapping[str, str] | None = None,
    checksums: Mapping[str, str] | None = None,
    only: Sequence[str] | None = None,
) -> dict[str, object]:
    """Prepare the reviewed public catalog; archive transport and workspace are not caller-selectable."""

    return _prepare_sources_for_specs(
        overrides=overrides,
        checksums=checksums,
        only=only,
    )


def _load_manifest(manifest_path: Path, *, allow_unapproved: bool = False) -> dict[str, object]:
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(f"manifest is unavailable or invalid: {manifest_path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise SourcePreparationError(f"unsupported source manifest: {manifest_path}")
    manifest_path = manifest_path.resolve()
    workspace_value = document.get("workspace")
    repositories = document.get("repositories")
    manifest_value = document.get("manifest")
    if not isinstance(workspace_value, str) or not isinstance(manifest_value, str) or not isinstance(repositories, dict):
        raise SourcePreparationError(f"source manifest is missing workspace/repositories: {manifest_path}")
    workspace = Path(workspace_value).resolve()
    if not workspace.name.startswith(WORKSPACE_PREFIX):
        raise SourcePreparationError(f"manifest workspace is not helper-owned: {workspace}")
    if not _inside(_temporary_parent(), workspace):
        raise SourcePreparationError(f"manifest workspace is outside the runtime temporary directory: {workspace}")
    internal_manifest = (workspace / "manifest.json").resolve()
    if manifest_value != str(internal_manifest) or not internal_manifest.is_file():
        raise SourcePreparationError(f"manifest path does not match its helper workspace: {manifest_path}")
    if manifest_path != internal_manifest:
        try:
            internal_document = json.loads(internal_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourcePreparationError(f"internal source manifest is unavailable or invalid: {internal_manifest}") from exc
        if internal_document != document:
            raise SourcePreparationError(f"copied source manifest does not match its helper-owned manifest: {manifest_path}")
    marker = _read_marker(workspace)
    specifications = marker["specifications"]
    assert isinstance(specifications, dict)
    archive_evidence = marker.get("archives")
    if not isinstance(archive_evidence, dict):
        raise SourcePreparationError("source workspace marker lacks archive evidence")
    if set(repositories) != set(specifications):
        raise SourcePreparationError("source manifest repository inventory does not match its workspace marker")
    if set(repositories) != set(archive_evidence):
        raise SourcePreparationError("source manifest archive inventory does not match its workspace marker")
    for logical_name, record in repositories.items():
        if not isinstance(logical_name, str) or not isinstance(record, dict):
            raise SourcePreparationError("source manifest contains an invalid repository record")
        if record.get("logical_name") != logical_name:
            raise SourcePreparationError(f"source manifest logical repository name is inconsistent: {logical_name}")
        root_value = record.get("root")
        root_relative = record.get("root_relative")
        source_tree_manifest = record.get("source_tree_manifest")
        source_tree_manifest_relative = record.get("source_tree_manifest_relative")
        source_tree_sha256 = record.get("source_tree_sha256")
        archive_tree_sha256 = record.get("archive_tree_sha256")
        archive_tree_entries = record.get("archive_tree_entries")
        ignored_archive_links = record.get("ignored_archive_links")
        allow_ignored_links = record.get("allow_ignored_links", False)
        archive_relative = record.get("archive_relative")
        repository = record.get("repository")
        ref = record.get("ref")
        archive_url = record.get("archive_url")
        checksum = record.get("archive_sha256")
        reviewed_checksum = record.get("reviewed_archive_sha256")
        required_paths = record.get("required_paths")
        if (
            not isinstance(root_value, str)
            or not isinstance(root_relative, str)
            or not isinstance(source_tree_manifest, str)
            or not isinstance(source_tree_manifest_relative, str)
            or not isinstance(source_tree_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source_tree_sha256)
            or not isinstance(archive_tree_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", archive_tree_sha256)
            or not isinstance(archive_tree_entries, int)
            or archive_tree_entries < 1
            or not isinstance(ignored_archive_links, list)
            or not all(isinstance(item, str) for item in ignored_archive_links)
            or not isinstance(allow_ignored_links, bool)
            or not isinstance(archive_relative, str)
            or not isinstance(repository, str)
            or not isinstance(ref, str)
            or not ref
            or not isinstance(archive_url, str)
            or not isinstance(checksum, str)
            or (
                reviewed_checksum is not None
                and (not isinstance(reviewed_checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", reviewed_checksum))
            )
            or not isinstance(required_paths, list)
            or not all(isinstance(item, str) for item in required_paths)
        ):
            raise SourcePreparationError(f"source manifest identity/paths are incomplete: {logical_name}")
        specification = specifications.get(logical_name)
        if not isinstance(specification, dict):
            raise SourcePreparationError(f"source workspace marker lacks repository specification: {logical_name}")
        if (
            specification.get("repository") != repository
            or specification.get("ref") != ref
            or specification.get("required_paths") != required_paths
            or specification.get("reviewed_archive_sha256") != reviewed_checksum
            or specification.get("allow_ignored_links", False) != allow_ignored_links
        ):
            raise SourcePreparationError(f"source manifest metadata differs from its workspace marker: {logical_name}")
        evidence = archive_evidence.get(logical_name)
        if not isinstance(evidence, dict):
            raise SourcePreparationError(f"source workspace marker lacks archive evidence: {logical_name}")
        catalog = DEFAULT_REPOSITORIES.get(logical_name)
        if catalog is None and not allow_unapproved:
            raise SourcePreparationError(f"source manifest contains an unapproved logical repository: {logical_name}")
        if catalog is not None and (
            catalog.repository != repository
            or catalog.ref != ref
            or list(catalog.required_paths) != required_paths
            or catalog.archive_sha256 != reviewed_checksum
            or catalog.allow_ignored_links != allow_ignored_links
        ):
            raise SourcePreparationError(f"source manifest metadata differs from the reviewed repository specification: {logical_name}")
        if catalog is not None and catalog.archive_sha256 is not None:
            if reviewed_checksum != catalog.archive_sha256 or checksum != catalog.archive_sha256:
                raise SourcePreparationError(
                    f"source archive checksum differs from the reviewed repository specification: {logical_name}"
                )
        try:
            expected_url = RepositorySpec(logical_name, repository, ref, tuple(required_paths)).archive_url
        except (ValueError, TypeError) as exc:
            raise SourcePreparationError(f"source manifest repository identity is malformed: {logical_name}") from exc
        if (
            (
                repository != catalog.repository
                if catalog is not None
                else not (allow_unapproved and repository.startswith(f"{OWNER}/"))
            )
            or archive_url != expected_url
            or not archive_url.startswith("https://" + "codeload.github.com/")
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        ):
            raise SourcePreparationError(f"source manifest identity/checksum is invalid: {logical_name}")
        expected_archive = workspace / "archives" / f"{logical_name}.tar.gz"
        expected_root_parent = workspace / "sources" / logical_name
        if archive_relative != str(expected_archive.relative_to(workspace)):
            raise SourcePreparationError(f"source archive path is not helper-owned: {logical_name}")
        if (
            evidence.get("archive_relative") != archive_relative
            or evidence.get("archive_sha256") != checksum
            or evidence.get("reviewed_archive_sha256") != reviewed_checksum
            or evidence.get("root_relative") != root_relative
            or evidence.get("source_tree_manifest_relative") != source_tree_manifest_relative
            or evidence.get("source_tree_sha256") != source_tree_sha256
            or evidence.get("archive_tree_sha256") != archive_tree_sha256
            or evidence.get("archive_tree_entries") != archive_tree_entries
            or evidence.get("ignored_archive_links") != ignored_archive_links
        ):
            raise SourcePreparationError(f"source manifest archive evidence differs from its workspace marker: {logical_name}")
        if not expected_archive.is_file() or expected_archive.is_symlink():
            raise SourcePreparationError(f"source archive is missing or unsafe: {logical_name}")
        try:
            with _StableArchive(expected_archive) as stable_archive:
                if stable_archive.archive_sha256 != checksum:
                    raise SourcePreparationError(
                        f"source archive checksum does not match the manifest: {logical_name}"
                    )
                scan = _scan_archive_object(
                    stable_archive,
                    repository.rsplit("/", 1)[1] + "-",
                    allow_ignored_links=allow_ignored_links,
                )
                if reviewed_checksum is not None and stable_archive.archive_sha256 != reviewed_checksum:
                    raise SourcePreparationError(
                        f"source archive checksum does not match the reviewed specification: {logical_name}"
                    )
        except SourcePreparationError:
            raise
        if source_tree_sha256 != scan.expected_tree_sha256 or archive_tree_sha256 != scan.expected_tree_sha256:
            raise SourcePreparationError(f"source tree digest is not derived from the retained archive: {logical_name}")
        if archive_tree_entries != len(scan.expected_entries):
            raise SourcePreparationError(f"source tree entry count is not derived from the retained archive: {logical_name}")
        if ignored_archive_links != list(scan.ignored_members):
            raise SourcePreparationError(f"ignored archive links are not derived from the retained archive: {logical_name}")
        archive_root_name = scan.top_level
        expected_root = expected_root_parent / archive_root_name
        expected_tree_manifest = workspace / source_tree_manifest_relative
        try:
            validated_root = validate_root(expected_root, f"extracted source {logical_name}")
        except TreeIntegrityError as exc:
            raise SourcePreparationError(f"extracted source root is not a safe directory: {logical_name}") from exc
        if (
            root_relative != str(expected_root.relative_to(workspace))
            or not _inside(workspace, expected_tree_manifest)
            or root_value != str(validated_root)
            or source_tree_manifest != str(expected_tree_manifest.resolve())
            or not expected_tree_manifest.is_file()
        ):
            raise SourcePreparationError(f"source root/workspace relationship is invalid: {logical_name}")
        _validate_required_paths(expected_root, required_paths)
        try:
            validate_tree_manifest(
                validated_root,
                expected_tree_manifest,
                source_tree_sha256,
                f"extracted source {logical_name}",
            )
        except TreeIntegrityError as exc:
            raise SourcePreparationError(
                f"extracted source tree digest does not match the manifest: {logical_name}"
            ) from exc
    return document


def load_manifest(manifest_path: Path) -> dict[str, object]:
    """Load a manifest and require every repository to match the public catalog."""

    return _load_manifest(manifest_path)


def _load_manifest_for_tests(manifest_path: Path) -> dict[str, object]:
    """Test-only manifest loader for synthetic archive identities."""

    return _load_manifest(manifest_path, allow_unapproved=True)


def _repository_root(manifest_path: Path, logical_name: str, *, allow_unapproved: bool = False) -> Path:
    manifest = _load_manifest(manifest_path, allow_unapproved=allow_unapproved)
    repositories = manifest["repositories"]
    assert isinstance(repositories, dict)
    record = repositories.get(logical_name)
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("root"), str)
        or not isinstance(record.get("source_tree_manifest"), str)
        or not isinstance(record.get("source_tree_sha256"), str)
    ):
        raise SourcePreparationError(f"logical repository is not in the manifest: {logical_name}")
    try:
        root = validate_root(Path(record["root"]), f"repository {logical_name}")
        validate_tree_manifest(
            root,
            Path(record["source_tree_manifest"]),
            record["source_tree_sha256"],
            f"repository {logical_name}",
        )
    except TreeIntegrityError as exc:
        raise SourcePreparationError(
            f"repository source tree changed before returning its root: {logical_name}"
        ) from exc
    return root


def repository_root(manifest_path: Path, logical_name: str) -> Path:
    """Resolve a repository root only from a public-catalog manifest."""

    return _repository_root(manifest_path, logical_name)


def _repository_root_for_tests(manifest_path: Path, logical_name: str) -> Path:
    """Test-only root resolver for synthetic archive identities."""

    return _repository_root(manifest_path, logical_name, allow_unapproved=True)


def cleanup_workspace(workspace: Path) -> None:
    """Remove only a workspace marked by this helper."""

    workspace = Path(workspace)
    if not workspace.is_absolute():
        raise SourcePreparationError(f"refusing to clean a relative workspace: {workspace}")
    try:
        path_metadata = os.lstat(workspace)
    except OSError as exc:
        raise SourcePreparationError(f"refusing to clean an unavailable workspace: {workspace}") from exc
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(path_metadata.st_mode):
        raise SourcePreparationError(f"refusing to clean a non-directory workspace: {workspace}")
    resolved = workspace.resolve(strict=True)
    if not resolved.name.startswith(WORKSPACE_PREFIX):
        raise SourcePreparationError(f"refusing to clean an unrecognised workspace: {resolved}")
    if not _inside(_temporary_parent(), resolved):
        raise SourcePreparationError(f"refusing to clean a workspace outside the runtime temporary directory: {resolved}")
    _read_marker(resolved)
    shutil.rmtree(resolved)


def _cleanup_created_workspace(workspace: Path) -> None:
    """Clean a workspace that this invocation created, including pre-marker failure paths."""

    workspace = Path(workspace)
    if not workspace.exists():
        return
    marker = _marker_path(workspace)
    if marker.is_file():
        cleanup_workspace(workspace)
        return
    try:
        resolved = workspace.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourcePreparationError(f"created source workspace is no longer safely addressable: {workspace}") from exc
    if (
        resolved.is_symlink()
        or not resolved.is_dir()
        or not resolved.name.startswith(WORKSPACE_PREFIX)
        or not _inside(_temporary_parent(), resolved)
    ):
        raise SourcePreparationError(f"refusing to clean an unmarked source workspace: {workspace}")
    shutil.rmtree(resolved)


def _attach_cleanup_failure(primary: BaseException, cleanup_error: BaseException) -> None:
    """Report cleanup failure without replacing the primary exception."""

    try:
        primary.add_note(f"source workspace cleanup failed: {cleanup_error}")
    except (AttributeError, RuntimeError):
        # Cleanup reporting must never mask the original failure.
        pass


def cleanup_manifest(manifest_path: Path) -> None:
    try:
        document = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(
            "cleanup manifest is unavailable or malformed; use --workspace with the saved workspace path"
        ) from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise SourcePreparationError(
            "cleanup manifest has an unsupported schema; use --workspace with the saved workspace path"
        )
    workspace_value = document.get("workspace")
    if not isinstance(workspace_value, str) or not workspace_value:
        raise SourcePreparationError(
            "cleanup manifest has no workspace; use --workspace with the saved workspace path"
        )
    cleanup_workspace(Path(workspace_value))


@contextmanager
def _prepared_sources_for_specs(
    *,
    specs: Mapping[str, RepositorySpec] | None = None,
    overrides: Mapping[str, str] | None = None,
    checksums: Mapping[str, str] | None = None,
    only: Sequence[str] | None = None,
    downloader: Downloader = _download_archive,
    workspace_parent: Path | None = None,
) -> Iterator[dict[str, object]]:
    """Private fixture seam whose signal guard spans preparation and consumption."""

    workspace: Path | None = None
    cleanup_attempted = False

    def workspace_created(created: Path) -> None:
        nonlocal workspace
        workspace = created

    def cleanup_once(created: Path | None = None) -> None:
        nonlocal cleanup_attempted
        if created is not None and (workspace is None or Path(created) != workspace):
            raise SourcePreparationError("source cleanup callback received an unexpected workspace")
        if cleanup_attempted:
            return
        cleanup_attempted = True
        if workspace is not None:
            _cleanup_created_workspace(workspace)

    primary: BaseException | None = None
    signal_guard = _source_preparation_signal_guard(cleanup_once)
    signal_guard.__enter__()
    try:
        manifest = _prepare_sources_for_specs_unprotected(
            specs=specs,
            overrides=overrides,
            checksums=checksums,
            only=only,
            downloader=downloader,
            workspace_parent=workspace_parent,
            workspace_created=workspace_created,
            cleanup=cleanup_once,
        )
        yield manifest
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            with _ignore_source_preparation_signals():
                cleanup_once()
        except BaseException as error:
            cleanup_error = error
        try:
            signal_guard.__exit__(None, None, None)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
            elif primary is not None:
                _attach_cleanup_failure(primary, error)
        if cleanup_error is not None:
            if primary is not None:
                _attach_cleanup_failure(primary, cleanup_error)
            else:
                raise cleanup_error


@contextmanager
def prepared_sources(
    *,
    overrides: Mapping[str, str] | None = None,
    checksums: Mapping[str, str] | None = None,
    only: Sequence[str] | None = None,
) -> Iterator[dict[str, object]]:
    """Prepare the reviewed public catalog and clean it on exit without caller-selected transport or workspace."""

    with _prepared_sources_for_specs(
        overrides=overrides,
        checksums=checksums,
        only=only,
    ) as manifest:
        yield manifest


def _parse_repo_overrides(values: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SourcePreparationError(f"repository override must be NAME=REF: {value!r}")
        logical_name, ref = value.split("=", 1)
        if logical_name in overrides:
            raise SourcePreparationError(f"repository override is repeated: {logical_name}")
        overrides[logical_name] = ref
    return overrides


def _parse_checksums(values: Sequence[str]) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SourcePreparationError(f"archive checksum must be NAME=64hex: {value!r}")
        logical_name, checksum = value.split("=", 1)
        if logical_name in checksums:
            raise SourcePreparationError(f"archive checksum is repeated: {logical_name}")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise SourcePreparationError(f"archive checksum is invalid: {logical_name}")
        checksums[logical_name] = checksum
    return checksums


def _prepare_command(args: argparse.Namespace) -> int:
    manifest = prepare_sources(
        overrides=_parse_repo_overrides(args.repo),
        checksums=_parse_checksums(args.checksum),
        only=args.only,
    )
    manifest_path = Path(str(manifest["manifest"]))
    if args.manifest_output:
        _write_json(Path(args.manifest_output), manifest)
    if args.print_manifest_path:
        print(manifest_path)
    else:
        json.dump(manifest, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="download and validate source archives")
    prepare.add_argument("--repo", action="append", default=[], metavar="NAME=REF", help="override a default repository ref")
    prepare.add_argument("--checksum", action="append", default=[], metavar="NAME=64HEX", help="caller-reviewed archive checksum")
    prepare.add_argument("--only", action="append", metavar="NAME", help="prepare only selected logical repositories")
    prepare.add_argument("--manifest-output", metavar="PATH", help="copy the manifest to a caller-selected path")
    prepare.add_argument("--print-manifest-path", action="store_true", help="print only the helper-owned manifest path")
    prepare.add_argument("--json", action="store_true", help="emit the manifest as JSON (the default output mode)")
    prepare.set_defaults(handler=_prepare_command)

    root = commands.add_parser("root", help="resolve a logical repository root")
    root.add_argument("--manifest", required=True, type=Path)
    root.add_argument("--repo", required=True, dest="logical_name")
    root.set_defaults(handler=lambda args: print(repository_root(args.manifest, args.logical_name)) or 0)

    cleanup = commands.add_parser("cleanup", help="remove a helper-owned disposable workspace")
    group = cleanup.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest", type=Path)
    group.add_argument("--workspace", type=Path)
    cleanup.set_defaults(
        handler=lambda args: (cleanup_manifest(args.manifest) if args.manifest else cleanup_workspace(args.workspace)) or 0
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return int(args.handler(args))
    except (SourcePreparationError, OSError, ValueError) as exc:
        print(f"source preparation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
