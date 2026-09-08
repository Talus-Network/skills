---
name: nexus-tap-development
description: Build and verify Talus Agent Packages (TAPs) and their applications, including Move packages, DAG/skill artifacts, dashboards, React dApps, bots, and backends using Nexus API REST and SSE through a server-side relay.
---

# Nexus TAP development

Use this skill to build a TAP package and DAG-backed Agent skill, or an application that reads existing TAP activity through Nexus API. It covers package and Tool composition, structural/local tests, read-only deployment checks, and application REST/SSE integration. It does not authorize publication, registration, deposits, scheduling, settlement, upgrades, or live-network asset movement.

## Embedded `$grill-me` requirements/design phase

Before any setup check, scaffolding, source preparation or archive acquisition, package/DAG/fixture build, test, artifact generation, file write, publication, registration, scheduling, signing, settlement, or network mutation, complete this self-contained phase inside this Skill:

1. Record a compact shared contract covering the goal/outcome, requirements and observable behavior, application/TAP/package/DAG/Tool inputs and integration boundary, in-scope deliverable/file scope, non-goals, authorization plus network/write boundary, acceptance evidence/tests, and a compact implementation design.
2. Find the earliest unresolved material decision. Ask exactly one question at a time, include a recommended answer and why, wait for the answer, and update the contract. Do not ask about facts answerable from approved public Docs, SDK, Move Packages, Sui, or read-only Testnet sources; resolve those facts read-only and record the authority instead.
3. If the request already supplies every field, record the contract and design, state that no material question remains, and continue without an unnecessary confirmation question.
4. Until the contract and compact design are explicit and shared, stop: do not run development commands, create or edit project files, acquire source archives, build, test, generate artifacts, or perform any shared-network action. This phase is embedded here; do not install or invoke another skill for it.
5. After stating `shared understanding complete`, continue with the existing public-source, fixture, artifact-consistency, and explicitly authorized deployment boundaries below.

## Choose the requested surface

| Request | Workflow |
| --- | --- |
| Dashboard, React dApp, bot, or backend for an existing TAP | Read [Nexus API application development](references/nexus-api-application.md). Use mocked REST/SSE and a server-side relay; no real key is needed for local implementation. |
| New or changed TAP Move package, DAG, or skill artifact | Follow the package setup, public-source, fixture, and validation sections below. |
| Application plus package/DAG changes | Apply the API reference to application files and the package gates only to the changed package/artifacts. |

For an application-only request, record existing identifiers and the app contract, then follow the API reference and its completion checks. Skip the remaining package setup, source downloads, Move/CLI installation, fixtures, and published-bytecode gates. Hosted API projections and provider keys do not replace wallet/SDK transaction authority or chain evidence.

For version-sensitive setup, use the published [Prepare for On-Chain Development](https://docs.talus.network/guides/getting-started/prepare-onchain-development) and verify it with `scripts/docs_website.py`; public repositories and read-only Testnet evidence are the only external authorities. For local tests that call published Nexus functions, follow its additive beta CLI block, set `NEXUS_BETA_CLI` to the explicit binary path, and verify `"$NEXUS_BETA_CLI" tap test --help`; do not prepend the beta directory to `PATH`. Unqualified `nexus` commands remain the released CLI. The beta command comes from the public `nexus-sdk` `main` branch and is not enabled by adding a released Rust SDK dependency.

## Public source preparation

Read [the TAP workflow](references/tap-workflow.md), [the package patterns](references/demo-tap.md), and [the mixed-tool workflow](references/mixed-tool-workflow.md). Prepare only the three approved public archives from a fresh consumer workspace:

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

The public Move package archive supplies direct interface packages and the supporting closure; the pinned Sui archive supplies only the two framework packages required by native Move consumers. The bundle includes a [direct fixture](fixtures/direct/README.md) and a [delayed fixture](fixtures/delayed/README.md); each checked-in `Move.toml` declares a repository-relative public Sui framework template described by `dependency-template.json`, and the forward flow replaces that template with the authenticated pinned source before isolated build/test. Validate and test each fixture contract, including success and expected-failure branches, as a repository-owned artifact. Never publish native interface declarations or use host-specific paths in `Move.toml`.

For a network-facing TAP, use the six reviewed Move Registry packages in `Move.toml` and commit the resulting `Move.lock`:

```toml
[dependencies]
nexus_interface  = { r.mvr = "@talus/nexus-interface" }
nexus_primitives = { r.mvr = "@talus/nexus-primitives" }
nexus_registry   = { r.mvr = "@talus/nexus-registry" }
nexus_tool       = { r.mvr = "@talus/nexus-tool" }
nexus_scheduler  = { r.mvr = "@talus/nexus-scheduler" }
nexus_workflow   = { r.mvr = "@talus/nexus-workflow" }
```

Use the matching public package pages for [interface](https://www.moveregistry.com/package/@talus/nexus-interface), [primitives](https://www.moveregistry.com/package/@talus/nexus-primitives), [registry](https://www.moveregistry.com/package/@talus/nexus-registry), [Tool](https://www.moveregistry.com/package/@talus/nexus-tool), [scheduler](https://www.moveregistry.com/package/@talus/nexus-scheduler), and [workflow](https://www.moveregistry.com/package/@talus/nexus-workflow). The copied package closure above remains an explicitly offline/source-inspection path; `nexus_policy` and `nexus_kernel` are not registered MVR names.

## Local published-bytecode TAP tests

After package-owned `sui move build`, run plain `sui move test` only for tests that do not call Nexus functions. For tests that call published Nexus functions, set `NEXUS_BETA_CLI` to the explicit binary path from the published Developer Setup and run:

```bash
"$NEXUS_BETA_CLI" tap test --path <tap-package> --build-env testnet
```

Use `"$NEXUS_BETA_CLI" tap test --path <tap-package> --list --build-env testnet` or one named test filter to diagnose a failure, then rerun the unfiltered command. The harness reads public Nexus bytecode for the selected environment, overlays only `#[test_only]` extensions in memory, and runs a local Sui VM. It needs network read access but no wallet, signer, gas, publication, registration, binding, scheduling, settlement, or asset movement.

Read the first concrete compiler, linker, ABI/layout, witness/result, authorization, input-commitment, output, or finalization error. Repair only the named package source or test arrangement, rerun the same VM gate, and preserve the successful output. A green local TAP test proves only the exercised published calls; it does not prove live Tool registration, Agent binding, Leader admission, scheduling, settlement, or execution.

## Structural DAG and skill-artifact validation

Set `TAP_PROJECT` to the repository-relative project being checked before running the structural validator. Its command is `python3 "$SKILLS_BUNDLE_ROOT/nexus-tap-development/scripts/verify_tap_artifacts.py" "$TAP_PROJECT" --require-artifacts --json`.

Run `python3 "$SKILLS_BUNDLE_ROOT/nexus-tap-development/scripts/verify_tap_artifacts.py" "$TAP_PROJECT" --require-artifacts --json` after the package build for repository-owned JSON checks. This gate owns `Move.toml` closure, DAG vertices/edges/ports, skill `dag_path`, fixed-Tool FQNs, payment/schedule policies, input commitments, shared objects, interface revision, and artifact paths. A manifest, DAG, or skill-artifact failure belongs to this validator; do not retry it through `"$NEXUS_BETA_CLI" tap test`. Its report is structural evidence with `runtime_proof` set to `not-proven`.

## Read-only testnet evidence

For an already deployed package, module, object, or network identity, run the repository-owned helper with an explicit official testnet endpoint:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

The helper performs only allowlisted read-only GraphQL queries and records response digests. It does not use a wallet or active environment and cannot publish, register, bind, schedule, or submit a transaction. Missing or unavailable evidence remains a blocker.

## Authoring modes

- **TAP application:** Use the [API application reference](references/nexus-api-application.md) for read views, pagination, replay, relay security, and frontend tests.

- **New TAP:** Start from the released public CLI scaffold, keep the generated package boundary, and adapt the DAG/config to the selected public schema; reserve the explicit `NEXUS_BETA_CLI` binary for `tap test` only.
- **Repository-owned fixture:** Use the bundled `demo-tap.md` patterns for direct and delayed paths without importing deployment IDs, capability IDs, or external source files.
- **Mixed Tool workflow:** Keep off-chain provider output schema-bound, pass it through a DAG edge, and validate the on-chain Tool's authorization, witness, commitment, output, and state mutation order.

## Operating contract

1. Record the package, DAG, skill artifact, source refs, and verification target before editing.
2. Keep Tool/TAP interfaces tied to the prepared public Move-package repository and current SDK repository references; use bundled procedures for all documentation guidance.
3. Use pure local tests for application logic, `"$NEXUS_BETA_CLI" tap test` for exercised published Nexus calls, and the bundle validators for artifact consistency and compiled call-graph checks.
4. Use placeholders for package IDs, object IDs, FQNs, witnesses, capabilities, and recipients until a target deployment supplies them.
5. Classify commands as read-only inspection, local build/test, or shared-network mutation before execution.
6. State explicitly whether evidence is structural, repository-owned, or read-only testnet observation; do not present any of those as native execution proof.

## Completion standard

A TAP application is locally ready when its REST/SSE and relay tests, relevant frontend typecheck/build, and synthetic-key bundle check pass. Report live API/network and wallet evidence separately. Apply the following package criteria only when a package or its artifacts changed.

A TAP is locally ready when its Move package, DAG, skill artifact, public dependency provenance, pure logic tests, structural validators, and applicable `"$NEXUS_BETA_CLI" tap test` published-bytecode checks pass. A deployed-state check is an additional read-only testnet report. Publication, Tool registration, DAG binding, scheduling, and asset movement require a separate authorization gate with exact IDs, signer custody, gas, pre-state, and post-state readback.
