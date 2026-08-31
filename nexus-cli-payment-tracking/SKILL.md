---
name: nexus-cli-payment-tracking
description: Trace Nexus funding, reserves, execution payments, Tool revenue, priority fees, and refunds with source-verified CLI reads; use when reconciling payment state or a funding/settlement gap.
metadata:
  short-description: Track Nexus payment state safely with the CLI
---

# Nexus CLI payment tracking

Use this skill when a user needs to explain where value is held, what a Task or Execution may charge, whether a Tool Invocation settled or refunded, why a payment is short, or what a Tool/Leader can collect. Reconstruct the trace from exact Task, Occurrence, Execution, `ExecutionPayment`, `Invocation`, Agent vault, ToolCashier, and priority-vault evidence. This skill diagnoses state; it does not authorize a payment or invent missing queries.

For version-sensitive setup or CLI fields, use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) and verify it with `scripts/docs_website.py`; public repositories and read-only Testnet evidence are the only external authorities.

## Public terminology references

Use the published [payment concepts](https://docs.talus.network/concepts/06-payment-vaults-reserves-and-settlement) and [CLI task reference](https://docs.talus.network/reference/cli/task) for terminology and user-facing guidance only. These website links are explanatory references, not source-preparation, build, runtime, forward, or testnet inputs.

## Prepare public source evidence

Use the bundle helper from a fresh consumer workspace before interpreting version-sensitive CLI fields or Move payment objects. The normal path uses the three approved public authorities, including the matching Sui framework source:

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
SOURCE_WORKSPACE=""
source_workspace_status=0
SOURCE_WORKSPACE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["workspace"])' "$SOURCE_MANIFEST")" || source_workspace_status=$?
if [ "$source_workspace_status" -ne 0 ] || [ -z "$SOURCE_WORKSPACE" ]; then
  SOURCE_WORKSPACE=""
  if [ "$source_workspace_status" -eq 0 ]; then source_workspace_status=1; fi
  printf 'source workspace resolution failed (status %s)\n' "$source_workspace_status" >&2
  exit "${source_workspace_status:-1}"
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

Use only repository-relative paths below verified roots. If an archive is unavailable or fails checksum/tree validation, report the gap and remain read-only. See [the CLI map](references/cli-map.md) and [the payment ledger](references/payment-ledger.md) for source roles and evidence fields.

## Read-only testnet evidence

For deployed package, module, object, or network facts, use the bundled helper with the explicit official Sui testnet GraphQL endpoint. It performs only read-only GraphQL queries and has no wallet or environment fallback:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Record the endpoint, network label, query methods, response digests, and timestamp. An unavailable endpoint or incomplete response is missing evidence, not a successful payment observation.

## Custody boundaries

- Signer SUI and owned gas coins fund transaction gas; they do not prove Tool collateral, Task reserves, Agent vaults, or Tool earnings.
- A `TaskPaymentReserve` funds future occurrences. An `ExecutionPayment` budgets one dispatched Execution. Neither balance alone proves an Invocation charge or occurrence settlement.
- An Agent payment vault holds Agent-funded SUI and is separate from Task/Execution payment custody. Preserve the exact source and recipient.
- Tool price, policy-backed Invocation amount, Leader reimbursement, and immediate transaction gas are distinct meters. Keep `ExecutionPayment` gas, priority, and Tool totals separate.
- ToolCashier collection is Tool revenue and requires the exact cashier capability plus finalized Invocation/deposit evidence. It does not withdraw Task or Execution funds.
- Priority fees are a charge/accounting path in `ExecutionPayment` and a separate Leader-tagged vault path. Do not treat a vault share as execution funding.

## Read-first procedure

1. Pin the selected CLI version and verify the exact command-family help before using a field or flag.
2. Record the selected testnet endpoint and package/object bundle without printing keys, tokens, capabilities, or private material.
3. Start with read-only gas, Task, Occurrence, and cost inspection, then correlate the exact Execution and payment ID.
4. Read the payment object and preserve `execution_id`, source/policy, budget split, locked vertices, Tool/priority charges, final state, and transaction provenance.
5. For Tool accounting, inspect the exact Tool, cashier, access policy, inbox, Invocation, and receipt before calling revenue collectable.
6. Write a compact ledger with IDs, owners, types, versions, digests, amounts, states, and evidence classification (`observed`, `derived`, `unavailable`, or `conflicting`).

## Mutation gate

Classify every command as read-only inspection, local build/test, or shared-network mutation. Refill, deposit, collection, swap, withdrawal, collateral claim, settlement, abort, close, policy configuration, scheduling, and any transaction submission are state-changing. Stop before them unless the user authorizes that exact operation and the preflight confirms signer, network, package/object bindings, ownership, amount, recipient, gas, and exact IDs. A transaction digest is not proof; inspect effects and re-read the affected objects after any authorized operation.

## Completion standard

A trace is complete only when Task → Occurrence → Execution → `ExecutionPayment` relationships are exact, Tool/Invocation/payment locks and settlement/refund evidence agree, independent gas/collateral/vault/priority surfaces are not conflated, and every conclusion is backed by selected-build output, object state, events, or effects. Otherwise return the earliest missing or conflicting evidence and a safe next read.
