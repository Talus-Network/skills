#!/usr/bin/env python3
"""Validate the portable structure and source/evidence policy of the Skills bundle."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = {
    "nexus-onchain-tool-development",
    "nexus-offchain-tool-development",
    "nexus-tap-development",
    "nexus-onchain-task-debugging",
    "nexus-cli-payment-tracking",
}
DEVELOPMENT_SKILLS = frozenset(
    {
        "nexus-offchain-tool-development",
        "nexus-onchain-tool-development",
        "nexus-tap-development",
    }
)
DEVELOPMENT_GRILL_HEADING = "## Embedded `$grill-me` requirements/design phase"
DEVELOPMENT_GRILL_MARKERS = (
    "goal/outcome",
    "requirements and observable behavior",
    "inputs and integration boundary",
    "non-goals",
    "authorization",
    "acceptance evidence/tests",
    "compact implementation design",
    "exactly one question at a time",
    "recommended answer",
    "approved public docs",
    "shared understanding complete",
    "do not install or invoke another skill",
    "do not run development commands",
)
DEVELOPMENT_ACTION_MARKERS = (
    "skills_bundle_root=",
    "source_helper=",
    "source_prepare_status=",
    "scripts/prepare_sources.py",
    "scripts/testnet_evidence.py",
)
PUBLIC_GITHUB_REPOSITORIES = {
    "Talus-Network/nexus-sdk",
    "Talus-Network/nexus-move-packages",
    "MystenLabs/sui",
}
APPROVED_PUBLIC_ARCHIVE_REFS = {
    "Talus-Network/nexus-sdk": "1f67d5b02b7caa5411e449eb171eda5c178f83fe",
    "Talus-Network/nexus-move-packages": "b070517238b83dd607e7ef6134d3ac413fdcc01f",
    "MystenLabs/sui": "d8459684b41eb09ab23fe16a9dd84173270bbaba",
}
APPROVED_PUBLIC_ARCHIVE_SHA256 = {
    "Talus-Network/nexus-sdk": "a6b25bb7d98bde41fe172afe673a28e7b7731ad51947a81d6cb615c144ed55b1",
    "Talus-Network/nexus-move-packages": "e88c6b977e87847f441564ecb9bcb75c269c5214d640721684139e93ddaa32f2",
    "MystenLabs/sui": "1b974c1b10413e873b98df2740a877a930a08b40c80af58b52f737385f8bdf44",
}
OFFICIAL_SUI_GRAPHQL_URL = "https://graphql.testnet.sui.io/graphql"
PUBLISHED_NEXUS_DOCS_HOST = "docs.talus.network"
PUBLISHED_NEXUS_DOCS_PATHS: frozenset[str] = frozenset(
    {
        "/guides/getting-started/setup",
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
        "/guides/tool-development/build-offchain-tool",
        "/guides/tool-development/build-onchain-tool",
        "/guides/tool-development/tool-communication",
        "/guides/tool-development/verify-offchain-tool-result",
        "/reference/toolkit/rust",
        "/reference/cli/tool",
        "/concepts/06-payment-vaults-reserves-and-settlement",
        "/reference/cli/task",
        "/reference/cli/tap",
    }
)
PUBLISHED_NEXUS_API_HOST = "api.taluslabs.dev"
PUBLISHED_NEXUS_API_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/docs",
        "/openapi.json",
    }
)
REVIEWED_OFFICIAL_SUI_DOCUMENTATION_URLS: frozenset[str] = frozenset(
    {
        "https://docs.sui.io/references/sui-api",
    }
)
PUBLIC_MVR_PACKAGE_NAMES: tuple[str, ...] = (
    "@talus/nexus-interface",
    "@talus/nexus-primitives",
    "@talus/nexus-registry",
    "@talus/nexus-tool",
    "@talus/nexus-scheduler",
    "@talus/nexus-workflow",
)
PUBLIC_MVR_PAGE_HOST = "www.moveregistry.com"
PUBLIC_MVR_API_HOSTS = {
    "testnet.mvr.mystenlabs.com": "testnet",
    "mainnet.mvr.mystenlabs.com": "mainnet",
}
LOCAL_DEVELOPMENT_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _token(*parts: str) -> str:
    return "".join(parts)


# Keep the policy implementation independent from any particular unapproved
# repository name. The URL and selector allowlists are the source-of-truth
# boundary for every public source.
PUBLIC_INSTALL_GITHUB_REPOSITORY = _token("Talus-Network/", "skills")
SOURCE_SELECTOR_RE = re.compile(r"--only\s+([A-Za-z0-9_-]+)", re.IGNORECASE)
ALLOWED_SOURCE_SELECTORS = frozenset({"nexus-sdk", "nexus-move-packages", "sui"})
PUBLIC_INSTALL_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    + re.escape(PUBLIC_INSTALL_GITHUB_REPOSITORY).replace("/", r"\s*/\s*")
    + r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
PUBLIC_INSTALL_COMMAND_RE = re.compile(
    r"^npx skills add " + re.escape(PUBLIC_INSTALL_GITHUB_REPOSITORY) + r"$",
    re.MULTILINE,
)
PUBLIC_INSTALL_COMMAND_VARIANT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])npx[ \t]+skills[ \t]+add[ \t]+talus[ \t-]*network[ \t]*/[ \t]*skills"
    r"(?:[ \t]+[^\r\n`]+)?"
)
PUBLIC_REPOSITORY_REFERENCE_RE = re.compile(r"(?i)(?<![A-Za-z0-9_.-])(?:Talus-Network|MystenLabs)\s*/\s*[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])")
URL_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9+.-]*://[^\s)>'\"<]+")
SCP_URL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+|[A-Za-z0-9.-]+\.[A-Za-z]{2,}):[^\s)>'\"<]+"
)
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
TEMP_ROOT_RE = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(_token("/", "tmp")) + r"(?=$|[\s\"'}):;,])")
DRIVE_ROOT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
FORBIDDEN_SEMANTIC_SOURCE_PHRASES = (
    _token("documentation", " repository"),
    _token("documentation", " archive"),
    _token("helper-resolved ", "documentation", " repository"),
    _token("public sdk/", "docs/move packages sources"),
    _token("documentation", " repository authority"),
)
FORBIDDEN_SEMANTIC_SOURCE_PATTERNS = (
    re.compile(r"\b(?:docs?|documentation)\s+(?:repository|archive|source|authority)\b"),
    re.compile(r"\b(?:sdk|move\s+packages?)\s*/\s*docs?\b"),
)
CHECKOUT_PHRASES = (
    _token("checked", "-out"),
    _token("active ", "checkout"),
    _token("pre-existing ", "checkout"),
    _token("adjacent ", "checkout"),
    _token("source ", "checkout"),
    _token("workspace ", "root"),
)


def frontmatter(text: str, path: Path) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    try:
        raw = text.split("---\n", 2)[1]
    except IndexError as exc:
        raise ValueError(f"{path}: unclosed YAML frontmatter") from exc
    fields: dict[str, str] = {}
    active_key: str | None = None
    for line in raw.splitlines():
        if line.startswith((" ", "\t")) and active_key:
            fields[active_key] = f"{fields[active_key]} {line.strip()}".strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        active_key = key.strip()
        fields[active_key] = value.strip().strip("'\"")
    return fields


def validate_links(path: Path, text: str, errors: list[str]) -> None:
    for target in LINK_RE.findall(text):
        if (
            target.startswith("#")
            or target.casefold().startswith(("http://", "https://", "mailto:"))
            or re.match(r"(?i)^[A-Za-z][A-Za-z0-9+.-]*://", target)
            or SCP_URL_RE.match(target)
        ):
            continue
        relative = target.split("#", 1)[0]
        if relative and not (path.parent / relative).resolve().exists():
            errors.append(f"{path.relative_to(ROOT)}: broken local link {target!r}")


def _path_label(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def validate_eval_file(skill: str, path: Path, errors: list[str]) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")
        return
    if not isinstance(document, dict):
        errors.append(f"{path.relative_to(ROOT)}: expected an eval object")
        return
    if document.get("skill_name") != skill:
        errors.append(f"{path.relative_to(ROOT)}: skill_name must match the skill directory")
    if not isinstance(document.get("source_constraint"), str) or not document["source_constraint"].strip():
        errors.append(f"{path.relative_to(ROOT)}: source_constraint must be a non-empty string")
    entries = document.get("evals")
    if not isinstance(entries, list) or len(entries) < 2:
        errors.append(f"{path.relative_to(ROOT)}: expected at least two distinct eval cases")
        return
    seen: set[str | int] = set()
    for index, entry in enumerate(entries):
        where = f"{path.relative_to(ROOT)}[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: eval must be an object")
            continue
        eval_id = entry.get("id")
        if not isinstance(eval_id, (str, int)) or isinstance(eval_id, bool):
            errors.append(f"{where}: id must be a string or integer")
        elif eval_id in seen:
            errors.append(f"{where}: duplicate id {eval_id!r}")
        else:
            seen.add(eval_id)
        for key in ("prompt", "expected_output"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                errors.append(f"{where}: {key} must be a non-empty string")
        sources = entry.get("sources")
        if not isinstance(sources, list) or not sources or not all(isinstance(item, str) and item for item in sources):
            errors.append(f"{where}: sources must be a non-empty string list")
        expectations = entry.get("expectations")
        if not isinstance(expectations, list) or len(expectations) < 2 or not all(isinstance(item, str) and item for item in expectations):
            errors.append(f"{where}: expectations must contain at least two observable strings")


def validate_development_grill(skill: str, text: str, errors: list[str]) -> None:
    """Require the embedded requirements/design gate before development actions."""

    if skill not in DEVELOPMENT_SKILLS:
        return
    lowered = text.casefold()
    heading = DEVELOPMENT_GRILL_HEADING.casefold()
    if heading not in lowered:
        errors.append(f"{skill}/SKILL.md: missing embedded development requirements gate")
        return
    gate_start = lowered.index(heading)
    gate_end = lowered.find("\nfor version-sensitive setup", gate_start)
    if gate_end == -1:
        gate_end = len(lowered)
    gate = lowered[gate_start:gate_end]
    setup_index = lowered.find("for version-sensitive setup")
    if setup_index != -1 and gate_start >= setup_index:
        errors.append(f"{skill}/SKILL.md: development gate must precede setup guidance")
    for marker in DEVELOPMENT_GRILL_MARKERS:
        if marker.casefold() not in gate:
            errors.append(f"{skill}/SKILL.md: development gate is missing {marker!r}")
    action_positions = [lowered.index(marker.casefold()) for marker in DEVELOPMENT_ACTION_MARKERS if marker.casefold() in lowered]
    if not action_positions or gate_start >= min(action_positions):
        errors.append(f"{skill}/SKILL.md: development gate must precede development actions")
    prefix = lowered[:gate_start]
    if any(marker.casefold() in prefix for marker in DEVELOPMENT_ACTION_MARKERS):
        errors.append(f"{skill}/SKILL.md: development actions appear before the requirements gate")
    for forbidden in ("skill://", "grill-me/", "pip install"):
        if forbidden in gate:
            errors.append(f"{skill}/SKILL.md: embedded gate must remain self-contained")


def _text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            sample = path.read_bytes()[:4096]
        except OSError:
            continue
        if b"\x00" not in sample:
            yield path


def _classify_public_url(url: str, *, allow_published_documentation: bool = False) -> tuple[bool, str]:
    """Classify an external URL against the anonymous public-source policy."""
    if not isinstance(url, str) or not url or any(character.isspace() for character in url):
        return False, "URL is malformed"
    if "{" in url or "}" in url:
        return False, "URL templates are not accepted as external references"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False, "URL is malformed"
    try:
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False, "invalid port"
    if parsed.username or parsed.password:
        return False, "credentials are not allowed"
    if parsed.query or parsed.fragment:
        return False, "query parameters and fragments are not allowed"
    scheme = parsed.scheme.casefold()
    host = hostname
    if scheme == "http":
        if host in LOCAL_DEVELOPMENT_HOSTS:
            return True, "local development"
        return False, "non-HTTPS external URL"
    if scheme != "https" or not host or port is not None or parsed.netloc.casefold() != host:
        return False, "URL must use the approved HTTPS destination"
    if host == urlsplit(OFFICIAL_SUI_GRAPHQL_URL).hostname and parsed.path == "/graphql":
        return True, "official GraphQL endpoint"
    if host == PUBLISHED_NEXUS_DOCS_HOST:
        if not allow_published_documentation:
            return False, "published documentation is allowed only in documentation contexts"
        normalized_path = parsed.path.rstrip("/") or "/"
        if normalized_path not in PUBLISHED_NEXUS_DOCS_PATHS:
            return False, "published documentation path is not in the reviewed allowlist"
        return True, "published Nexus documentation"
    if host == PUBLISHED_NEXUS_API_HOST:
        if not allow_published_documentation:
            return False, "published API documentation is allowed only in documentation contexts"
        normalized_path = parsed.path.rstrip("/") or "/"
        if normalized_path not in PUBLISHED_NEXUS_API_PATHS:
            return False, "published API documentation path is not in the reviewed allowlist"
        return True, "published Nexus API documentation"
    if host == PUBLIC_MVR_PAGE_HOST:
        if parsed.path.rstrip("/") == "/package":
            return True, "reviewed public Move Registry package base"
        expected_paths = {"/package/" + name for name in PUBLIC_MVR_PACKAGE_NAMES}
        if parsed.path not in expected_paths or parsed.query or parsed.fragment:
            return False, "MVR page is not an exact reviewed package destination"
        return True, "reviewed public Move Registry package"
    if host in PUBLIC_MVR_API_HOSTS:
        network = PUBLIC_MVR_API_HOSTS[host]
        if parsed.path.rstrip("/") == "/v1/names":
            return True, f"reviewed public Move Registry {network} API base"
        expected_paths = {
            "/v1/names/" + quote(name, safe="")
            for name in PUBLIC_MVR_PACKAGE_NAMES
        }
        if parsed.path not in expected_paths or parsed.query or parsed.fragment:
            return False, f"MVR {network} API path is not an exact reviewed package destination"
        return True, f"reviewed public Move Registry {network} API"
    reviewed_docs_prefix = "https://" + "docs.sui.io/"
    if host == "docs.sui.io" and parsed.path in {
        urlsplit(reviewed_url).path for reviewed_url in REVIEWED_OFFICIAL_SUI_DOCUMENTATION_URLS
    } and url.casefold().startswith(reviewed_docs_prefix):
        return True, "reviewed official Sui documentation"
    if host == "github.com":
        components = [part for part in parsed.path.split("/") if part]
        decoded_path = unquote(parsed.path)
        if ".." in decoded_path.split("/") or "\x00" in decoded_path:
            return False, "path traversal is not allowed"
        if len(components) < 2:
            return False, "GitHub URL must name an approved repository"
        repository = "/".join(components[:2])
        if repository not in PUBLIC_GITHUB_REPOSITORIES:
            return False, f"GitHub repository is not in the public allowlist: {repository}"
        return True, "approved public GitHub repository"
    if host == "codeload.github.com":
        components = [unquote(part) for part in parsed.path.split("/") if part]
        if len(components) != 4 or components[2] != "tar.gz" or not components[3]:
            return False, "codeload URL must name an approved repository archive and ref"
        repository = "/".join(components[:2])
        if repository not in PUBLIC_GITHUB_REPOSITORIES:
            return False, f"codeload repository is not in the public allowlist: {repository}"
        if any(part in {"", ".", ".."} for part in components):
            return False, "codeload path is not canonical"
        if components[3] != APPROVED_PUBLIC_ARCHIVE_REFS[repository]:
            return False, "codeload ref is not the reviewed archive revision"
        return True, "approved public codeload archive"
    return False, "external host is not in the public allowlist"


def _validate_public_urls(
    path: Path,
    content: str,
    errors: list[str],
    *,
    url_resolver: Callable[[str], str | None] | None = None,
    allow_published_documentation: bool = False,
) -> None:
    candidates = [*URL_RE.findall(content), *SCP_URL_RE.findall(content)]
    seen: set[str] = set()
    for url in candidates:
        clean_url = url.rstrip(".,;`")
        if clean_url in seen:
            continue
        seen.add(clean_url)
        if clean_url in {"http://", "https://"}:
            continue
        allowed, reason = _classify_public_url(
            clean_url,
            allow_published_documentation=allow_published_documentation,
        )
        if not allowed:
            errors.append(f"{_path_label(path)}: URL is not in the public allowlist ({reason}): {clean_url}")
            continue
        if url_resolver is None:
            continue
        try:
            final_url = url_resolver(clean_url)
        except Exception as exc:  # pragma: no cover - injected audit adapter
            errors.append(f"{_path_label(path)}: URL destination could not be verified: {exc}")
            continue
        if final_url is None:
            continue
        final_allowed, final_reason = _classify_public_url(
            final_url,
            allow_published_documentation=allow_published_documentation,
        )
        if not final_allowed:
            errors.append(
                f"{_path_label(path)}: URL redirects outside the public allowlist ({final_reason}): {clean_url} -> {final_url}"
            )


def _is_documentation_context(root: Path, path: Path) -> bool:
    """Allow published documentation URLs only in bundled documentation content."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return path.name in {"README.md", "docs_website.py"} or path.name == "SKILL.md" or "references" in relative.parts or "evals" in relative.parts


def _validate_public_repository_references(
    root: Path,
    path: Path,
    content: str,
    errors: list[str],
    install_context_spans: tuple[tuple[int, int], ...],
) -> None:
    """Reject bare repository selectors unless they are approved public sources."""

    if path.suffix.lower() not in {".md", ".markdown", ".json"}:
        return

    canonical_install_spans = tuple(
        match.span()
        for match in PUBLIC_INSTALL_COMMAND_RE.finditer(content)
        if _span_is_contained(match.span(), install_context_spans)
    )
    for reference in PUBLIC_REPOSITORY_REFERENCE_RE.finditer(content):
        normalized = re.sub(r"\s+", "", reference.group(0))
        if normalized in PUBLIC_GITHUB_REPOSITORIES:
            continue
        if normalized == PUBLIC_INSTALL_GITHUB_REPOSITORY and _span_is_contained(reference.span(), canonical_install_spans):
            continue
        errors.append(f"{_path_label(path)}: repository reference is not an approved public repository: {reference.group(0)}")


def _root_readme_install_context_spans(root: Path, path: Path, content: str) -> tuple[tuple[int, int], ...]:
    """Return the root README section where public installation references are permitted."""

    if path != root / "README.md":
        return ()
    heading = re.search(r"(?m)^##[ \t]+Install from the public repository[ \t]*$", content)
    if heading is None:
        return ()
    next_heading = re.search(r"(?m)^##[ \t]+", content[heading.end() :])
    section_end = heading.end() + next_heading.start() if next_heading is not None else len(content)
    return ((heading.end(), section_end),)


def _span_is_contained(span: tuple[int, int], containers: Iterable[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in containers)


def _validate_public_install_references(
    root: Path,
    path: Path,
    content: str,
    errors: list[str],
    install_context_spans: tuple[tuple[int, int], ...],
) -> None:
    """Permit the public Skills selector only in exact root-README install context."""

    references = list(PUBLIC_INSTALL_REFERENCE_RE.finditer(content))
    variants = list(PUBLIC_INSTALL_COMMAND_VARIANT_RE.finditer(content))
    if not references and not variants:
        return
    canonical_command_spans = tuple(
        match.span()
        for match in PUBLIC_INSTALL_COMMAND_RE.finditer(content)
        if _span_is_contained(match.span(), install_context_spans)
    )
    for match in variants:
        if _span_is_contained(match.span(), canonical_command_spans):
            continue
        errors.append(
            f"{_path_label(path)}: public Skills install command variant is not allowed; use the exact canonical "
            f"command only in the root README installation section"
        )
    for reference in references:
        if _span_is_contained(reference.span(), canonical_command_spans):
            continue
        errors.append(
            f"{_path_label(path)}: public Skills install selector/reference is allowed only as the exact canonical "
            f"command in the root README installation section: {PUBLIC_INSTALL_GITHUB_REPOSITORY}"
        )


def validate_portability(
    root: Path,
    errors: list[str],
    *,
    url_resolver: Callable[[str], str | None] | None = None,
) -> None:
    """Reject host paths, local source discovery, and forbidden source authorities."""

    for path in _text_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: cannot read text for portability audit: {exc}")
            continue
        install_context_spans = _root_readme_install_context_spans(root, path, content)
        _validate_public_urls(
            path,
            content,
            errors,
            url_resolver=url_resolver,
            allow_published_documentation=_is_documentation_context(root, path),
        )
        _validate_public_repository_references(root, path, content, errors, install_context_spans)
        _validate_public_install_references(root, path, content, errors, install_context_spans)
        lowered = content.lower()
        for phrase in FORBIDDEN_SEMANTIC_SOURCE_PHRASES:
            if phrase in lowered:
                errors.append(f"{path.relative_to(root)}: forbidden semantic source policy phrase")
        if any(pattern.search(lowered) for pattern in FORBIDDEN_SEMANTIC_SOURCE_PATTERNS):
            errors.append(f"{path.relative_to(root)}: forbidden semantic source policy pattern")
        for selector in SOURCE_SELECTOR_RE.findall(lowered):
            if selector.casefold() not in ALLOWED_SOURCE_SELECTORS:
                errors.append(
                    f"{path.relative_to(root)}: source selector is not an approved public repository: {selector}"
                )
        if TEMP_ROOT_RE.search(content) or DRIVE_ROOT_RE.search(content):
            errors.append(f"{path.relative_to(root)}: host-specific path is forbidden")
        for phrase in CHECKOUT_PHRASES:
            if phrase.lower() in lowered:
                errors.append(f"{path.relative_to(root)}: local source discovery phrase is forbidden")


def validate_interface_authority(skills_root: Path, errors: list[str]) -> None:
    """Require public Move-package authority for generated consumer guidance."""

    for path in _text_files(skills_root):
        if path.suffix not in {".md", ".json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if (
            "Move.toml" in text
            and "nexus-move-packages" not in text
            and "MystenLabs/sui" not in text
            and "testnet_evidence" not in text
        ):
            errors.append(f"{path.relative_to(skills_root)}: Move workflow lacks public package authority")
        for match in re.finditer(r"r\.mvr\s*=\s*[\"']([^\"']+)[\"']", text):
            if match.group(1) not in PUBLIC_MVR_PACKAGE_NAMES:
                errors.append(f"{path.relative_to(skills_root)}: unapproved MVR dependency {match.group(1)!r}")


def validate_helper(errors: list[str]) -> None:
    helper_path = ROOT / "scripts" / "prepare_sources.py"
    evidence_path = ROOT / "scripts" / "testnet_evidence.py"
    website_path = ROOT / "scripts" / "docs_website.py"
    mvr_path = ROOT / "scripts" / "mvr_registry.py"
    mvr_catalog_path = ROOT / "scripts" / "mvr_packages.json"
    helper_tests = ROOT / "scripts" / "test_prepare_sources.py"
    evidence_tests = ROOT / "scripts" / "test_testnet_evidence.py"
    website_tests = ROOT / "scripts" / "test_docs_website.py"
    for path, label in (
        (helper_path, "public source helper"),
        (evidence_path, "testnet evidence boundary"),
        (website_path, "published Docs website boundary"),
        (mvr_path, "public Move Registry boundary"),
        (mvr_catalog_path, "public Move Registry catalog"),
        (helper_tests, "source helper tests"),
        (evidence_tests, "testnet evidence tests"),
        (website_tests, "published Docs website tests"),
    ):
        if not path.is_file():
            errors.append(f"{path.relative_to(ROOT)}: missing {label}")
    if not all(path.is_file() for path in (helper_path, evidence_path, website_path, mvr_path, mvr_catalog_path, helper_tests, evidence_tests, website_tests)):
        return
    try:
        helper_spec = importlib.util.spec_from_file_location("skills_source_helper", helper_path)
        if helper_spec is None or helper_spec.loader is None:
            raise ImportError("cannot load source helper")
        helper = importlib.util.module_from_spec(helper_spec)
        sys.modules[helper_spec.name] = helper
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            helper_spec.loader.exec_module(helper)
        finally:
            sys.dont_write_bytecode = previous
        repositories = getattr(helper, "DEFAULT_REPOSITORIES")
        if set(repositories) != {"nexus-sdk", "nexus-move-packages", "sui"}:
            errors.append("scripts/prepare_sources.py: source catalog must contain only public repositories")
        for logical_name, repository in repositories.items():
            if not repository.ref or not repository.archive_url.startswith("https://" + "codeload.github.com/"):
                errors.append(f"scripts/prepare_sources.py: {logical_name} lacks an HTTPS archive")
            if not repository.required_paths:
                errors.append(f"scripts/prepare_sources.py: {logical_name} lacks identity markers")
            expected_repository = {
                "nexus-sdk": "Talus-Network/nexus-sdk",
                "nexus-move-packages": "Talus-Network/nexus-move-packages",
                "sui": "MystenLabs/sui",
            }.get(logical_name)
            if expected_repository != repository.repository:
                errors.append(f"scripts/prepare_sources.py: {logical_name} has an unapproved repository identity")
            if APPROVED_PUBLIC_ARCHIVE_REFS.get(repository.repository) != repository.ref:
                errors.append(f"scripts/prepare_sources.py: {logical_name} has an unapproved archive ref")
            if APPROVED_PUBLIC_ARCHIVE_SHA256.get(repository.repository) != repository.archive_sha256:
                errors.append(f"scripts/prepare_sources.py: {logical_name} has an unapproved archive checksum")
        move_spec = repositories.get("nexus-move-packages")
        public_paths = tuple(f"packages/{name}" for name in getattr(helper, "PUBLIC_MOVE_PACKAGE_CLOSURE_NAMES", ()))
        if move_spec is None or tuple(move_spec.required_paths) != ("README.md", *public_paths):
            errors.append("scripts/prepare_sources.py: public package closure is incomplete")
        sui_spec = repositories.get("sui")
        sui_paths = tuple(getattr(helper, "SUI_FRAMEWORK_REQUIRED_PATHS", ()))
        if sui_spec is None or tuple(sui_spec.required_paths) != sui_paths:
            errors.append("scripts/prepare_sources.py: public Sui framework markers are incomplete")
        expected_toolchain = f"sui 1.78.0-{sui_spec.ref[:12]}" if sui_spec is not None else None
        if getattr(helper, "SUI_TOOLCHAIN_VERSION", None) != expected_toolchain:
            errors.append("scripts/prepare_sources.py: Sui toolchain version is not bound to the pinned commit")
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        errors.append(f"scripts/prepare_sources.py: helper metadata is invalid: {exc}")
    evidence_text = evidence_path.read_text(encoding="utf-8")
    for marker in ("GRAPHQL_READ_OPERATIONS", "TESTNET_GRAPHQL_HOST", "OFFICIAL_TESTNET_CHAIN_IDENTIFIER", "SuiTestnetEvidenceClient", "--graphql-url"):
        if marker not in evidence_text:
            errors.append(f"scripts/testnet_evidence.py: missing read-only boundary marker {marker!r}")
    website_text = website_path.read_text(encoding="utf-8")
    for marker in ("CANONICAL_SETUP_URL", "EXPECTED_SETUP_VERSION", "fetch_setup_page", "PublishedDocsError"):
        if marker not in website_text:
            errors.append(f"scripts/docs_website.py: missing live website boundary marker {marker!r}")
    try:
        mvr_spec = importlib.util.spec_from_file_location("skills_mvr_registry", mvr_path)
        if mvr_spec is None or mvr_spec.loader is None:
            raise ImportError("cannot load MVR registry helper")
        mvr = importlib.util.module_from_spec(mvr_spec)
        sys.modules[mvr_spec.name] = mvr
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            mvr_spec.loader.exec_module(mvr)
        finally:
            sys.dont_write_bytecode = previous
        catalog = mvr.load_catalog(mvr_catalog_path)
        if tuple(record["name"] for record in mvr.records(catalog)) != PUBLIC_MVR_PACKAGE_NAMES:
            errors.append("scripts/mvr_packages.json: catalog package names do not match the reviewed six-package set")
    except (ImportError, AttributeError, OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"scripts/mvr_packages.json: MVR catalog is invalid: {exc}")


def main(argv: list[str] | None = None) -> int:
    del argv
    errors: list[str] = []
    readme_path = ROOT / "README.md"
    try:
        readme = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: README.md: {exc}")
        return 1
    for skill in sorted(EXPECTED_SKILLS):
        skill_dir = ROOT / skill
        skill_file = skill_dir / "SKILL.md"
        eval_file = skill_dir / "evals" / "evals.json"
        if not skill_file.is_file():
            errors.append(f"{skill}/SKILL.md: missing")
            continue
        if f"]({skill}/)" not in readme:
            errors.append(f"README.md: missing inventory link for {skill}")
        text = skill_file.read_text(encoding="utf-8")
        try:
            metadata = frontmatter(text, skill_file)
        except ValueError as exc:
            errors.append(str(exc))
            metadata = {}
        if metadata.get("name") != skill:
            errors.append(f"{skill}/SKILL.md: frontmatter name must match the directory")
        if not metadata.get("description"):
            errors.append(f"{skill}/SKILL.md: missing trigger description")
        if len(list(skill_dir.rglob("*.md"))) < 2:
            errors.append(f"{skill}: expected at least one focused reference")
        validate_development_grill(skill, text, errors)
        for path in skill_dir.rglob("*.md"):
            validate_links(path, path.read_text(encoding="utf-8"), errors)
        if eval_file.is_file():
            validate_eval_file(skill, eval_file, errors)
        else:
            errors.append(f"{skill}/evals/evals.json: missing")
        if "prepare_sources.py" not in text or "testnet_evidence.py" not in text:
            errors.append(f"{skill}/SKILL.md: public source and testnet evidence boundaries are required")
    validate_helper(errors)
    validate_interface_authority(ROOT, errors)
    validate_portability(ROOT, errors)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Validated {len(EXPECTED_SKILLS)} Nexus skills with public source and testnet policies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
