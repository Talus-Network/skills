# Payment ledger

Use this worksheet to keep payment custody and accounting surfaces separate. It supplements the entrypoint's route and read-first procedure; it never authorizes a transaction.

## Evidence sources

For version-sensitive CLI fields or Move payment objects, use the installed bundle's `scripts/prepare_sources.py` through [the source-preparation contract](source-preparation.md). For deployed package, module, object, or chain observations, use the bundle-root helper with the explicit official Sui Testnet GraphQL endpoint:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Retain `network`, `endpoint`, allowlisted `calls`, response digests, and report digest. A successful read proves only that returned public state existed at collection time; it does not prove execution, registration, payment settlement, or authorization.

## Ledger rows

Record exact object IDs, owners, types, versions, transaction/checkpoint provenance, amounts, and states. Use one row for each relationship and classify its evidence as `observed`, `derived`, `unavailable`, or `conflicting`.

| Surface | Question | Do not conflate it with |
| --- | --- | --- |
| Signer SUI and owned gas coins | What funded transaction gas? | Tool collateral, Task reserves, Agent vaults, or Tool earnings |
| `TaskPaymentReserve` | What funds future occurrences? | A dispatched Execution's budget or a settled Invocation |
| `ExecutionPayment` | What budget, locks, charges, priority fee, refund, and final state belong to this Execution? | Address balance, native gas, or another Execution |
| Tool/Invocation/receipt | What Tool price, policy amount, result, settlement, or deposit is recorded? | ToolCashier collection without finalized Invocation/deposit evidence |
| Agent payment vault | What Agent-funded custody and recipient are recorded? | Task/Execution payment custody |
| Priority fee/vault path | What priority accounting is recorded? | Tool revenue or execution funding |

## Collectability boundary

Treat a Tool amount as `unavailable` until the exact Invocation, policy/beneficiary, deposit, and settlement records establish that it is collectible. Missing collection authority or an unperformed collection action does not establish a zero entitlement. Compute a collectible amount only after verifying field semantics, payment scope, and non-overlap; do not subtract or add balances whose relationship is only inferred.

## Safe conclusion

An exact Task → Occurrence → Execution → `ExecutionPayment` relationship and matching Tool/Invocation/payment locks are required before declaring a trace complete. If an ID, owner, version, type origin, network identity, or relationship is absent or inconsistent, mark the row unavailable/conflicting and return the earliest missing read. Do not repair a payment trace by guessing a command or submitting a transaction.
