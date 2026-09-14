---
name: nexus-onchain-task-debugging
description: Diagnose pending, failed, or inconsistent Nexus Executions by exhaustively reconciling exact on-chain Task, DAG, input, Tool/result, authorization, timeout, and payment records. Use for evidence-backed cause ledgers; route payment-only reconciliation or authoring work elsewhere.
---

# Nexus on-chain Task debugging

Use this skill to trace a Task through its Occurrences and Executions, historical DAG/configuration, supplied effective inputs, Tool/Invocation results, authorization, timeouts, and payment records. For every Execution, inspect every required on-chain surface and return an exhaustive evidence ledger rather than an improvised retry or behavioral guess.

## Route before setup

| Request | Route |
| --- | --- |
| Pending, failed, or inconsistent Execution and its complete on-chain cause ledger | Continue here. |
| Payment custody, reserve, charge, refund, or Tool revenue reconciliation without an Execution-wide diagnosis | Use [CLI payment tracking](https://docs.talus.network/guides/tokenomics/fund-agent-and-user-executions). |
| Build or change an on-chain Tool, off-chain Tool, or TAP/application | Use [on-chain Tool](https://docs.talus.network/guides/tool-development/build-onchain-tool), [off-chain Tool](https://docs.talus.network/guides/tool-development/build-offchain-tool), [TAP package development](https://docs.talus.network/guides/tap-development/build-tap-move-package), or [API application development](https://docs.talus.network/guides/nexus-api). |

Route before reading version-sensitive setup. Use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) and its read-only `scripts/docs_website.py` checker for command facts; do not search for private or local source.

## Evidence sources

Start with [the diagnosis worksheet](references/diagnosis.md) to classify every required read. Read [the public source map](references/source-map.md) only when an exact CLI, SDK, or Move implementation fact remains unresolved. Read [the source-preparation contract](references/source-preparation.md) only when that unresolved fact needs version-sensitive public source; then use the installed bundle's `scripts/prepare_sources.py` for only the three reviewed public archives and preserve the manifest cleanup boundary.

For deployed state, use the repository-owned read-only `scripts/testnet_evidence.py` boundary with an explicit official Testnet GraphQL endpoint:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Treat missing, stale, malformed, or conflicting evidence as a concrete gap and record the exact query failure. Read-only public source or Testnet state proves only the returned public fields at collection time; it does not establish a Leader submission, native or off-chain Tool/provider execution, actor intent, finality, or payment settlement unless an exact execution/result/effect/receipt record states that fact.

## Diagnosis procedure

1. Pin the selected CLI/SDK version and verify exact command-family help before interpreting a field.
2. Capture explicit Testnet endpoint and network identity without printing keys or capability material.
3. Resolve Task, Occurrence, and Execution through exact IDs. Preserve owner, version, type, digest, and transaction/checkpoint provenance for every record.
4. Read the exact historical DAG Execution configuration from on-chain objects, events, or effects. Inspect entry groups, vertices, edges, defaults, Tool bindings, port schemas, scheduling/policy fields, timeouts, and lifecycle counters.
5. Read every supplied and effective input. For every Tool input port, establish the exact entry value, DAG default, or incoming edge; compare port, cardinality, type/value kind, value or commitment, and Invocation link.
6. Read Tool/Invocation, result/receipt, Leader authorization, and every payment surface: Task funding/reserve, `ExecutionPayment`, locks, Tool price/charge, priority fee, refund, recipient, settlement, and native SUI gas.
7. Compare identity relationships only when an exact on-chain record establishes them. CLI help can explain a returned field; it cannot prove a deployed object contains a value or an actor behaved in a particular way.
8. Build the worksheet cause ledger across identity/configuration, DAG, inputs, lifecycle, Tool/result, authorization, and payment. Classify every applicable cause only as `confirmed`, `ruled out`, or `unresolved`, cite exact evidence, and name the missing read for each unresolved cause.
9. Continue independent required reads after a conflict or plausible outcome. Never use `likely`, `probably`, inferred intent, expected off-chain behavior, or a terminal event as a substitute for exact cause evidence.
10. Return a bounded cause ledger and safe next reads. Refill, settlement, abort, close, retry, scheduling, and other state changes require a separate explicit authorization gate.

## Completion boundary

Offline package/build/schema checks prove only structure. Read-only Testnet evidence proves only returned public state at a collection time. Neither proves native execution, registration, actor intent, off-chain behavior, or payment settlement; leave unsupported causes `unresolved`.
