---
name: nexus-onchain-task-debugging
description: Diagnose pending, failed, or inconsistent Nexus Executions by inspecting exact read-only on-chain Task, DAG/configuration, input, Tool/result, authorization, and payment records and exhaustively classifying causes without guessing runtime behavior.
---

# Nexus on-chain Task debugging

Use this skill to trace a Task through its Occurrences, Executions, historical DAG/configuration, supplied and effective inputs, Tool/Invocation results, authorization, timeouts, and payment records. For every Execution, inspect all of those on-chain surfaces and return an exhaustive evidence ledger, not an improvised retry or a behavioral guess.

For version-sensitive setup or CLI fields, use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) and verify it with `scripts/docs_website.py`; do not search for a private or local source.

## Evidence sources

Read [the diagnosis worksheet](references/diagnosis.md) and [the public source map](references/source-map.md). Use the bundle helper for the three approved public repositories when version-sensitive source is needed; stable diagnostic procedures remain repository-owned:

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

For deployed state, use the repository-owned `scripts/testnet_evidence.py` boundary with an explicit official Sui testnet endpoint. It reads only chain identity, checkpoint, package, module, function, struct, and object data. It has no wallet, environment, signing, or transaction fallback:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Treat missing, stale, malformed, or conflicting evidence as a blocker and record the exact query failure.

## Diagnosis procedure

1. Pin the selected CLI/SDK version and verify the command-family help before interpreting a field.
2. Capture the explicit testnet endpoint and network identity without printing keys or capability material.
3. Resolve the Task, Occurrence, and Execution through exact IDs. Preserve owner, version, type, digest, and transaction/checkpoint provenance for every record.
4. Read the exact historical DAG and Execution configuration from on-chain object, event, or effect records. Inspect entry group, vertices, edges, defaults, Tool bindings, port schemas, scheduling/policy fields, timeouts, and lifecycle counters.
5. Read every supplied input and effective input/evaluation. For every Tool input port, establish its exact entry value, DAG default, or incoming edge and compare its port, cardinality, type/value kind, value or commitment, and Invocation link.
6. Read the Tool/Invocation, result/receipt, Leader authorization, and every payment surface: Task funding/reserve, `ExecutionPayment`, locks, Tool price/charge, priority fee, refund, recipient, settlement, and native SUI gas.
7. Compare every identity and relationship to the exact on-chain record that establishes it. CLI help and prepared public source may explain a returned field, but they are not evidence that a deployed object contains a value or that an actor behaved in a particular way.
8. Build the worksheet's cause ledger. Include every applicable invariant-failure cause across identity/configuration, DAG, inputs, lifecycle, Tool/result, authorization, and payment. Classify each cause only as `confirmed`, `ruled out`, or `unresolved`, cite the exact evidence, and name the missing read for every unresolved cause.
9. Do not stop at the first plausible outcome or evidence gap while another independent required read remains possible. Never use `likely`, `probably`, inferred intent, expected off-chain behavior, or a terminal event as a substitute for exact cause evidence.
10. Return the bounded cause ledger and safe next reads. Any refill, settlement, abort, close, retry, or scheduling action belongs behind a separate explicit authorization gate.

## Completion boundary

Offline package/build/schema checks prove only structure. Read-only testnet evidence proves only the returned public state at collection time. Neither is native execution, registration, actor intent, off-chain behavior, or payment settlement proof; state those limits and leave any unsupported cause `unresolved`.
