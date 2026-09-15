---
name: nexus-cli-payment-tracking
description: Reconcile Nexus Task funding, ExecutionPayment budgets, Tool charges, priority fees, refunds, and custody from source-verified read-only CLI and object evidence. Use for payment-state gaps; route full Execution-cause diagnosis or Tool/TAP authoring to the matching skill.
metadata:
  short-description: Track Nexus payment state safely from CLI evidence
---

# Nexus CLI payment tracking

Before using helper commands or fixtures, read [skill-only installation and companion setup](references/consumer-setup.md); the skill installer does not supply the repository-level companion tools.

Use this skill when the user needs to explain where value is held, what a Task or Execution may charge, whether a Tool Invocation settled or was refunded, why payment is short, or what a Tool/Leader can collect. Reconstruct the exact Task, Occurrence, Execution, `ExecutionPayment`, `Invocation`, Agent vault, ToolCashier, and priority-vault relationships. This skill diagnoses recorded state; it does not authorize payment or invent missing queries.

## Route before setup

| Request | Route |
| --- | --- |
| Reconcile reserves, charges, refunds, settlement, or custody from recorded payment state | Continue here. |
| Pending, failed, or inconsistent Execution with exhaustive DAG/input/Tool/authorization causes | Use [on-chain Task debugging](https://docs.talus.network/guides/agent-usage/execute-and-settle-agent). |
| Build or change a Sui Move Tool, Rust HTTP Tool, or TAP | Use [on-chain Tool](https://docs.talus.network/guides/tool-development/build-onchain-tool), [off-chain Tool](https://docs.talus.network/guides/tool-development/build-offchain-tool), [TAP package development](https://docs.talus.network/guides/tap-development/build-tap-move-package), or [API application development](https://docs.talus.network/guides/nexus-api). |

Do this routing before version-sensitive setup or source preparation. For terminology, use the published [payment concepts](https://docs.talus.network/concepts/06-payment-vaults-reserves-and-settlement) and [CLI task reference](https://docs.talus.network/reference/cli/task) as user-facing guidance only; they are not payment or source evidence.

## Public source and Testnet evidence

For payment reconciliation, start with [the payment ledger](references/payment-ledger.md). Read [the CLI map](references/cli-map.md) when an exact command family or field remains version-sensitive. Read [the source-preparation contract](references/source-preparation.md) only when that fact cannot be established from approved public Docs or read-only evidence; then use the installed bundle's `scripts/prepare_sources.py` from a fresh consumer workspace only for the reviewed public SDK, Move Packages, and Sui archives. Use repository-relative paths below verified roots and stop at an archive, checksum, tree, or cleanup failure.

For deployed package, module, object, or network facts, use the bundle-root `scripts/testnet_evidence.py` helper with the explicit official Sui Testnet GraphQL endpoint:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Record endpoint, network label, allowlisted query methods, response digests, and timestamp. An unavailable or incomplete response is missing evidence, not a successful payment observation.

## Custody boundaries

- Signer SUI and owned gas coins fund transaction gas; they do not prove Tool collateral, Task reserves, Agent vaults, or Tool earnings.
- A `TaskPaymentReserve` funds future occurrences. An `ExecutionPayment` budgets one dispatched Execution. Neither balance alone proves Invocation charge or occurrence settlement.
- An Agent payment vault holds Agent-funded SUI separately from Task/Execution payment custody. Preserve the exact source recipient.
- Tool price, policy-backed Invocation amount, Leader reimbursement, immediate transaction gas, and priority fees are distinct meters. Keep `ExecutionPayment` gas, priority, and Tool totals separate.
- ToolCashier collection requires the exact cashier capability plus finalized Invocation/deposit evidence; it does not withdraw Task or Execution funds.

## Read-first procedure

1. Pin the selected CLI version and verify exact command-family help before using a field or flag.
2. Record the selected Testnet endpoint and package/object bundle without printing keys, tokens, capabilities, or private material.
3. Start with read-only gas, Task, Occurrence, and cost inspection, then correlate the exact Execution payment ID.
4. Read the payment object and preserve `execution_id`, source/policy, budget split, locked vertices, Tool/priority charges, final state, and transaction provenance.
5. For Tool accounting, inspect the exact Tool, cashier, access policy, inbox, Invocation, and receipt before calling revenue collectable.
6. Write a compact ledger of IDs, owners, types, versions, digests, amounts, states, and evidence classes (`observed`, `derived`, `unavailable`, or `conflicting`). Follow the payment ledger's separate surface rows for every applicable component, retaining `unavailable` or `conflicting` when a field cannot be read.

## Mutation gate

Classify every command as read-only inspection, local build/test, or shared-network state change. A state-changing operation requires separate explicit authorization and a preflight that confirms signer, network, package/object bindings, ownership, amount, recipient, gas, and exact IDs. A transaction digest is not proof; inspect effects and re-read affected objects after an authorized operation.

## Completion standard

A trace is complete only when the Task → Occurrence → Execution → `ExecutionPayment` relationships are exact, Tool/Invocation/payment locks agree with settlement/refund evidence, independent gas/collateral/vault/priority surfaces are not conflated, and every conclusion is backed by selected-build output, object state, events, or effects. Otherwise return the earliest missing or conflicting evidence and the safest next read.
