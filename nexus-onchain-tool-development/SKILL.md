---
name: nexus-onchain-tool-development
description: Build, test, and safely integrate Nexus on-chain Tools in Sui Move; use when implementing an execute module, witness/result schema, registration inputs, or a read-only deployment check.
---

# Nexus on-chain Tool development

Use this skill for a Sui Move Tool with an `execute` entry point, witness/result schema, ABI, output tags, and consumer package. It covers pure application logic, public interface dependencies, local compilation/tests, artifact validation, and safe deployment preparation.

## Embedded `$grill-me` requirements/design phase

Before any setup check, scaffolding, source preparation or archive acquisition, Move build, test, artifact generation, file write, publication, registration, scheduling, signing, settlement, or network mutation, complete this self-contained phase inside this Skill:

1. Record a compact shared contract covering the goal/outcome, requirements and observable behavior, inputs and integration boundary, in-scope package/module/file scope, non-goals, authorization plus network/write boundary, acceptance evidence/tests, and a compact implementation design.
2. Find the earliest unresolved material decision. Ask exactly one question at a time, include a recommended answer and why, wait for the answer, and update the contract. Do not ask about facts answerable from approved public Docs, SDK, Move Packages, Sui, or read-only Testnet sources; resolve those facts read-only and record the authority instead.
3. If the request already supplies every field, record the contract and design, state that no material question remains, and continue without an unnecessary confirmation question.
4. Until the contract and compact design are explicit and shared, stop: do not run development commands, create or edit project files, acquire source archives, build, test, generate artifacts, or perform any shared-network action. This phase is embedded here; do not install or invoke another skill for it.
5. After stating `shared understanding complete`, continue with the existing public-source, authorization/order, artifact-validation, and explicitly authorized deployment boundaries below.

For version-sensitive setup, use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) and verify it with `scripts/docs_website.py`; do not use a source-repository checkout as the setup authority.

## Public source preparation

For local tests that call Nexus functions, install the additive beta CLI from the [Prepare for On-Chain Development](https://docs.talus.network/guides/getting-started/prepare-onchain-development), set `NEXUS_BETA_CLI` to its explicit binary path, and verify `"$NEXUS_BETA_CLI" tap test --help` before using it. Do not prepend the beta directory to `PATH`; unqualified `nexus` commands remain the released CLI. The beta command is supplied by the public `nexus-sdk` `main` branch, not by adding a released `nexus-sdk` library dependency.

Read [the public source map](references/source-map.md) and [the workflow](references/workflow.md). In a fresh consumer workspace, prepare only the three approved public repositories, including the matching Sui framework source:

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

Use only repository-relative paths below verified roots. The public Move package archive supplies `packages/primitives`, `packages/interface`, `packages/tool`, `packages/registry`, `packages/workflow`, `packages/scheduler`, and its supporting `packages/kernel` closure. The public Sui archive supplies only `crates/sui-framework/packages/move-stdlib` and `crates/sui-framework/packages/sui-framework` for local compilation. Native declarations are compile-time interfaces; do not publish them or use them as executable mocks.

## Read-only testnet evidence

For deployed package/module/object facts, use the repository-owned helper with an explicit official Sui testnet URL:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Only chain identity, checkpoint, package/object, and normalized Move reads are allowed. The helper does not switch environments, open wallets, sign, publish, register, schedule, or submit transactions. If the endpoint is unavailable or the response is incomplete, report missing evidence.

## Move contract invariants

- Keep `execute` public and end with `&mut TxContext`; place the framework-owned prefix before application inputs.
- Satisfy the registered witness from the supplied state object and consume any workflow authorization against the exact worksheet recipient, state UID, and input commitment before mutating state or releasing assets.
- Produce one declared `TaggedOutput` on every reachable branch and finalize it through the public result API.
- Keep SUI gas, registration collateral, Tool price, Task reserve, and Execution payment separate.
- Use placeholders for package IDs, object IDs, FQNs, witnesses, capabilities, and recipients until a target deployment supplies them.

## Local published-bytecode test

After the package-owned `sui move build`, run plain `sui move test` only for tests that do not call Nexus functions. When tests call published Nexus functions, set `NEXUS_BETA_CLI` to the explicit binary path from the published Developer Setup and run:

```bash
"$NEXUS_BETA_CLI" tap test --path . --build-env testnet
```

Use `"$NEXUS_BETA_CLI" tap test --path . --list --build-env testnet` to discover tests and one named filter to shorten diagnosis, then rerun the unfiltered command. The harness reads public Nexus bytecode for the selected environment, overlays only `#[test_only]` extensions in memory, and runs a local Sui VM. It requires network read access but no wallet, signer, gas, publication, registration, binding, scheduling, settlement, or asset movement.

Treat the first concrete compiler, linker, ABI/layout, witness/result, authorization, input-commitment, output, or finalization error as the diagnosis target. Make the smallest source or test-arrangement repair, rerun the same filtered gate, and finish with the unfiltered gate. A green result proves only the exercised published calls and local arrangement; it does not prove Tool registration or live workflow execution.

## Validation and deployment boundary

The published-bytecode harness above is the additional local check for tests that call Nexus functions; keep its result separate from the structural and package-owned evidence described below.

Run Move build/test, the bundle's compiled/artifact validators, and pure application tests in disposable directories. These prove syntax, ABI shape, call-graph order, and cross-file consistency only. Registration, package publication, DAG binding, scheduling, and any asset movement are shared-network mutations that require explicit authorization, current chain identity, exact IDs, signer/capability custody, gas, and authoritative post-state readback.

## Routing

| Request | Read |
| --- | --- |
| Scaffold or implement a Move Tool | [workflow](references/workflow.md) |
| Choose authorization or witness handling | [workflow](references/workflow.md) |
| Review public ABI/dependency provenance | [source map](references/source-map.md) |
| Connect a Tool to a TAP | `nexus-tap-development` and its mixed-tool reference |
| Diagnose build/schema/result failures | [workflow](references/workflow.md) and `nexus-onchain-task-debugging` |
