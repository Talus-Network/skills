# Talus Nexus Agent Skills

Reusable engineering skills for building and diagnosing Talus Nexus applications. The bundle is self-contained: stable examples and test fixtures live here, while version-sensitive source is limited to anonymous public repositories and read-only Sui testnet evidence.

## Install from the public repository

Install the complete Skills bundle or select individual skills with the public `skills` CLI:

```bash
npx skills add Talus-Network/skills
```

This canonical command is release/install UX only. It selects content from the public Skills repository and is separate from source preparation, compiler inputs, and the observed offline build/test proof below.

## Use from a local checkout

```bash
SKILLS_BUNDLE_ROOT="${SKILLS_BUNDLE_ROOT:-$(pwd)}"
test -f "$SKILLS_BUNDLE_ROOT/scripts/validate_skills.py"
python3 -B "$SKILLS_BUNDLE_ROOT/scripts/validate_skills.py"
```

Run the validator from the checkout or installed bundle before loading a skill. Each skill directory can then be supplied directly to an Agent Skills-compatible runtime; install only the capabilities needed for the active project so unrelated instructions do not compete for context.

## Skills

| Skill                                                               | Use it for                                                                                 |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [nexus-onchain-tool-development](nexus-onchain-tool-development/)   | Build and validate an on-chain Tool in Sui Move.                                           |
| [nexus-offchain-tool-development](nexus-offchain-tool-development/) | Build, test, and validate an off-chain HTTP Tool service.                                  |
| [nexus-tap-development](nexus-tap-development/)                     | Build a TAP and its artifacts, and a Nexus API dashboard/dApp/backend for an existing TAP. |
| [nexus-onchain-task-debugging](nexus-onchain-task-debugging/)       | Trace Task, Occurrence, Execution, Tool, and payment evidence.                             |
| [nexus-cli-payment-tracking](nexus-cli-payment-tracking/)           | Reconcile funding, reserves, charges, refunds, and Tool revenue.                           |

## TAP applications and Tool scaffolding

For an application over an existing TAP, use [Nexus API application development](nexus-tap-development/references/nexus-api-application.md): hosted REST projections, paginated execution history, replayable SSE, a server-only provider key, and deterministic frontend/relay tests. App-only work does not require a Move package or CLI/source setup. The public [Nexus API guide](https://docs.talus.network/guides/nexus-api) and provider reference define the selected deployment's consumer contract.

The Tool skills include the full scaffold-to-implementation workflow and completion checks for [Rust HTTP Tools](nexus-offchain-tool-development/references/scaffolding.md) and [Sui Move Tools](nexus-onchain-tool-development/references/scaffolding.md). Start with the released public CLI scaffold, then implement real behavior and verify it using the matching skill. No separate plugin runtime is required.

## Safety model

The skills default to repository-owned examples, isolated build/test work, dry-run validation, and read-only inspection. Publishing, registration, deposits, refills, settlement, scheduling, upgrades, and asset movement require explicit authorization and a fresh network/signer/configuration preflight. Examples use placeholders rather than credentials or deployment-specific capability IDs.

## Portable source evidence

The source helper downloads only three reviewed anonymous public archives when bundled material is insufficient: [nexus-sdk](https://github.com/Talus-Network/nexus-sdk), [nexus-move-packages](https://github.com/Talus-Network/nexus-move-packages), and the pinned [Sui framework source](https://github.com/MystenLabs/sui/tree/d8459684b41eb09ab23fe16a9dd84173270bbaba) matching `sui 1.78.0-d8459684b41e`. It rejects local paths, SSH URLs, unknown repository selectors, and unapproved source roots. The Move package archive supplies the six public interface packages and their supporting closure; the Sui archive supplies only the framework packages needed for local Move compilation, and neither declaration set is an executable mock.

From a fresh consumer workspace, resolve the helper from the installed bundle and keep its manifest for cleanup:

```bash
SKILLS_BUNDLE_ROOT="${SKILLS_BUNDLE_ROOT:?Set SKILLS_BUNDLE_ROOT to this installed Skills bundle root}"
SOURCE_HELPER="$SKILLS_BUNDLE_ROOT/scripts/prepare_sources.py"
test -f "$SOURCE_HELPER"
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

Use only repository-relative paths below those verified roots. For native Move builds, use only the two framework package directories below `SUI_ROOT`. If a public archive is unavailable or fails its pinned checksum/tree checks, report the source-evidence gap and stop rather than looking for another source.

## Public Move Registry dependencies

For a network-facing Move consumer, declare the six reviewed Nexus packages through the public Move Registry and let Sui resolve the deployment-specific package address. The package pages are [nexus-interface](https://www.moveregistry.com/package/@talus/nexus-interface), [nexus-primitives](https://www.moveregistry.com/package/@talus/nexus-primitives), [nexus-registry](https://www.moveregistry.com/package/@talus/nexus-registry), [nexus-tool](https://www.moveregistry.com/package/@talus/nexus-tool), [nexus-scheduler](https://www.moveregistry.com/package/@talus/nexus-scheduler), and [nexus-workflow](https://www.moveregistry.com/package/@talus/nexus-workflow).

```toml
[dependencies]
nexus_interface  = { r.mvr = "@talus/nexus-interface" }
nexus_primitives = { r.mvr = "@talus/nexus-primitives" }
nexus_registry   = { r.mvr = "@talus/nexus-registry" }
nexus_tool       = { r.mvr = "@talus/nexus-tool" }
nexus_scheduler  = { r.mvr = "@talus/nexus-scheduler" }
nexus_workflow   = { r.mvr = "@talus/nexus-workflow" }
```

Commit the generated `Move.lock` and keep the package revision, network, and published dependency addresses matched. The public Move Packages repository remains useful for source inspection, provenance, contribution, and explicitly offline copied closure; it is not the preferred installation source. Do not invent a registry name for `nexus_policy` or `nexus_kernel`: the reviewed registry map records both as unregistered, and `nexus_kernel` remains supporting source closure rather than a direct application dependency.

## Published Setup authority

Version-sensitive instructions use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup), not a source-repository checkout. Check the page anonymously before relying on a release-specific command; the expected release for this bundle is `v2.0.0`.

```bash
python3 scripts/docs_website.py --url https://docs.talus.network/guides/getting-started/setup --expected-version v2.0.0
```

The check is read-only and fails closed on an unavailable page, an unsafe redirect, malformed content, or a version mismatch. It never installs a dependency or changes a Sui environment; show any resulting setup command to the user as a separate, explicitly authorized workflow.

## Read-only Sui testnet evidence

When a question needs deployed package, module, object, or network evidence, use the bundled boundary with the explicit official Sui testnet GraphQL endpoint. It performs only allowlisted read-only GraphQL queries and never uses a wallet, active CLI environment, or state-changing command:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x... --module module_name
```

The endpoint, network label, query methods, response digest, and timestamp are retained in the JSON report. An unavailable endpoint, malformed response, wrong network, or missing identifier is a concrete evidence failure; structural fixtures and validators must not be presented as runtime execution or registration proof.

## Validation

Run the bundle validator before proposing changes:

```bash
python3 scripts/validate_skills.py
python3 -B -m unittest discover -s scripts -p 'test_*.py'
python3 -B -m unittest discover -s nexus-tap-development/scripts -p 'test_*.py'
```

See [the source helper reference](scripts/README.md) and [the testnet evidence reference](scripts/README.md#read-only-sui-testnet-evidence) for the complete evidence and cleanup contract.
