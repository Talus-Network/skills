---
name: nexus-offchain-tool-development
description: Scaffold, implement, test, and validate Nexus off-chain HTTP Tools in Rust, including external-provider integrations, Toolkit metadata, canonical output encoding, and signed HTTP. Use for standalone Tools or existing Cargo workspaces.
---

# Nexus off-chain Tool development

Use this skill for a Rust HTTP service that a Nexus Leader invokes during a workflow. It covers the Tool contract, schema and FQN design, external dependency seams, deterministic tests, Toolkit bootstrap, metadata validation, and signed HTTP. An off-chain Tool is appropriate for external APIs or computation unavailable on chain; on-chain state or asset mutation belongs in the on-chain Tool skill.

## Embedded `$grill-me` requirements/design phase

Before any setup check, scaffolding, source preparation or archive acquisition, build, test, metadata generation, server launch, file write, publication, registration, scheduling, signing, settlement, or network mutation, complete this self-contained phase inside this Skill:

1. Record a compact shared contract covering the goal/outcome, requirements and observable behavior, inputs and integration boundary, in-scope deliverable/file scope, non-goals, authorization plus network/write boundary, acceptance evidence/tests, and a compact implementation design.
2. Find the earliest unresolved material decision. Ask exactly one question at a time, include a recommended answer and why, wait for the answer, and update the contract. Do not ask about facts answerable from approved public Docs, SDK, Move Packages, Sui, or read-only Testnet sources; resolve those facts read-only and record the authority instead.
3. If the request already supplies every field, record the contract and design, state that no material question remains, and continue without an unnecessary confirmation question.
4. Until the contract and compact design are explicit and shared, stop: do not run development commands, create or edit project files, acquire source archives, build, test, generate metadata, launch a service, or perform any shared-network action. This phase is embedded here; do not install or invoke another skill for it.
5. After stating `shared understanding complete`, continue with the existing public-source, secret-safe, deterministic local-test, and explicitly authorized registration boundaries below.

For version-sensitive setup, use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) and verify it with `scripts/docs_website.py`; public repositories are the only source authorities.

## Public source and test evidence

Use the bundle-owned `scripts/prepare_sources.py` only from a fresh consumer workspace. Its normal public selection is the three pinned public archives: Nexus SDK, Nexus Move Packages, and the matching Sui framework source:

```bash
SKILLS_BUNDLE_ROOT="${SKILLS_BUNDLE_ROOT:?Set SKILLS_BUNDLE_ROOT to this installed Skills bundle root}"
SOURCE_HELPER="$SKILLS_BUNDLE_ROOT/scripts/prepare_sources.py"
SOURCE_MANIFEST=""
cleanup_sources() {
  status="${1:-$?}"
  trap - EXIT INT TERM
  cleanup_status=0
  if [ -n "${SOURCE_MANIFEST:-}" ]; then
    python3 "$SOURCE_HELPER" cleanup --manifest "$SOURCE_MANIFEST" || cleanup_status=$?
  fi
  if [ "$cleanup_status" -ne 0 ]; then
    printf 'source cleanup failed (status %s)\n' "$cleanup_status" >&2
    if [ "$status" -eq 0 ]; then
      status="$cleanup_status"
    fi
  fi
  exit "$status"
}
trap cleanup_sources EXIT
trap 'cleanup_sources 130' INT
trap 'cleanup_sources 143' TERM
source_prepare_status=0
SOURCE_MANIFEST=""
SOURCE_MANIFEST="$(python3 "$SOURCE_HELPER" prepare --only nexus-sdk --only nexus-move-packages --only sui --print-manifest-path)" || source_prepare_status=$?
if [ "$source_prepare_status" -ne 0 ] || [ -z "$SOURCE_MANIFEST" ]; then
  SOURCE_MANIFEST=""
  if [ "$source_prepare_status" -eq 0 ]; then source_prepare_status=1; fi
  printf 'source preparation failed (status %s)\n' "$source_prepare_status" >&2
  exit "${source_prepare_status:-1}"
fi
SDK_ROOT=""
sdk_root_status=0
SDK_ROOT="$(python3 "$SOURCE_HELPER" root --manifest "$SOURCE_MANIFEST" --repo nexus-sdk)" || sdk_root_status=$?
if [ "$sdk_root_status" -ne 0 ] || [ -z "$SDK_ROOT" ]; then
  SDK_ROOT=""
  if [ "$sdk_root_status" -eq 0 ]; then sdk_root_status=1; fi
  printf 'nexus-sdk root resolution failed (status %s)\n' "$sdk_root_status" >&2
  exit "${sdk_root_status:-1}"
fi
MOVE_PACKAGES_ROOT=""
move_packages_root_status=0
MOVE_PACKAGES_ROOT="$(python3 "$SOURCE_HELPER" root --manifest "$SOURCE_MANIFEST" --repo nexus-move-packages)" || move_packages_root_status=$?
if [ "$move_packages_root_status" -ne 0 ] || [ -z "$MOVE_PACKAGES_ROOT" ]; then
  MOVE_PACKAGES_ROOT=""
  if [ "$move_packages_root_status" -eq 0 ]; then move_packages_root_status=1; fi
  printf 'nexus-move-packages root resolution failed (status %s)\n' "$move_packages_root_status" >&2
  exit "${move_packages_root_status:-1}"
fi
SUI_ROOT=""
sui_root_status=0
SUI_ROOT="$(python3 "$SOURCE_HELPER" root --manifest "$SOURCE_MANIFEST" --repo sui)" || sui_root_status=$?
if [ "$sui_root_status" -ne 0 ] || [ -z "$SUI_ROOT" ]; then
  SUI_ROOT=""
  if [ "$sui_root_status" -eq 0 ]; then sui_root_status=1; fi
  printf 'sui root resolution failed (status %s)\n' "$sui_root_status" >&2
  exit "${sui_root_status:-1}"
fi
```

Use repository-relative paths below verified roots. The public references are [Nexus SDK](https://github.com/Talus-Network/nexus-sdk), [Nexus Move Packages](https://github.com/Talus-Network/nexus-move-packages), and the matching [Sui framework source](https://github.com/MystenLabs/sui); stable workflow guidance is bundled in this repository. If an archive or required marker is unavailable, report the source-evidence gap instead of locating another source.

For deployed package/module/object facts, call the bundled helper with an explicit testnet URL:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

This boundary performs only allowlisted read-only GraphQL queries. It does not use a wallet, active environment, credentials, or transaction path. Offline Rust tests and local HTTP mocks remain the default; report unavailable testnet evidence explicitly.

## Route by task

| Task | Read |
| --- | --- |
| Scaffold a new Tool or add one to a Cargo workspace | [Scaffolding and completion checklist](references/scaffolding.md), then the implementation and verification references. |
| Implement or adapt a Rust Tool, schema, FQN, external call, or tests | [implementation reference](references/implementation.md) |
| Validate metadata, configure signed HTTP, or diagnose a boundary | [verification reference](references/verification.md) |
| Build and verify a complete Tool | Read both references in order. |
| Feed this Tool into a TAP-owned on-chain state transition | Route the combined DAG/state workflow through `nexus-tap-development` and its mixed-tool reference. |

## Safety contract

- Classify every command as read-only inspection, local build/test, local server state, or shared-network mutation.
- Default to an isolated project, deterministic mocks, bounded timeouts, and dry-run/read-only inspection.
- Keep secrets in runtime configuration or a secret manager; never put private keys, API tokens, or signing material in source, fixtures, logs, or metadata.
- Validate URL scheme/host, timeout, response size, status, content type, JSON shape, required output fields, and error behavior before accepting an external response.
- Test success, provider failure, malformed payload, timeout, signature/header tampering, replay, and schema mismatch.
- Local `http://` is for development validation only. Production requires an operator-supplied reachable HTTPS endpoint; TLS and signed HTTP are separate protections.

## Registration boundary

Registration is a shared-network mutation. Before any authorized operation, verify the exact public FQN, schema, endpoint, beneficiary, collateral, gas budget, signer/capability custody, and returned effects/readback. A successful command or digest is not proof without authoritative post-state. If the selected SDK/public source or testnet evidence is unavailable, stop with the earliest concrete gap.
