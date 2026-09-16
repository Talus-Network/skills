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

Record exact object IDs, owners, types, versions, transaction/checkpoint provenance, amounts, and states. Use one row for each applicable surface below; split meters exposed by one object into separate rows. Classify every row as `observed`, `derived`, `unavailable`, or `conflicting`.

| Surface | Question | Do not conflate it with |
| --- | --- | --- |
| Signer SUI and owned gas coins | What funded transaction gas, and which owner/effect records it? | Tool collateral, Task reserves, Agent vaults, Tool earnings, or an ExecutionPayment budget |
| `TaskPaymentReserve` | What reserve funds future occurrences, with before/after amounts and owner? | Dispatched Execution budget, Invocation charge, or refund |
| `ExecutionPayment` budget and lock | What total budget and lock belong to this Execution? | Task reserve balance, native transaction gas, or another Execution |
| `ExecutionPayment` gas subtotal | What gas budget, lock, and consumption are recorded? | Signer gas coins or Tool charge |
| `ExecutionPayment` Tool subtotal | What Tool budget or lock is recorded? | Tool price/charge actually recorded by the Invocation |
| `ExecutionPayment` priority subtotal | What priority budget or lock is recorded? | Priority fee transfer/vault or Tool revenue |
| Tool/Invocation charge and receipt | What Tool price, policy amount, charge, result, deposit, and settlement are recorded? | ExecutionPayment budget, native gas, or ToolCashier collection without finalized evidence |
| Priority fee transfer/vault | What priority amount, recipient, and effect are recorded? | ExecutionPayment priority budget, Tool revenue, or signer gas |
| Agent payment vault | What Agent-funded custody, owner, and recipient are recorded? | Task reserve or ExecutionPayment custody |
| Refund amount and final state | What refund amount, source, status, and reserve closure are recorded? | Unspent budget inferred from subtraction or a balance snapshot |
| Refund recipient and beneficiary identity | Which exact object/address receives the refund or Tool entitlement? | Payment source, Tool price, or an inferred owner |
| Collection authority and deposit | Which exact capability, policy, beneficiary, and deposit establish collectability? | An unperformed collection action or a zero balance |

## v2 funding and evidence retention

Released v2.0.0 starts walks through DAG publication and Task scheduling, not `dag execute`. Scheduling requires both `--prepay-amount-mist` (Task reserve funding) and `--occurrence-budget-mist` (occurrence budget), including immediate one-off walks. Neither value proves an actual Invocation charge or settlement. Reconcile them against the linked reserve and Execution payment records; transaction gas is separate, and SUI address balance alone does not prove usable signer-owned gas coins. Payment investigation does not authorize publishing, scheduling, refilling, or collecting funds.

Save raw readbacks/errors and exit status, collection time, CLI version/revision, network/endpoint, exact IDs, and transaction effects/events and payment receipts immediately. Testnet may prune execution history after only a few days, without a guaranteed retention interval. `history is incomplete: missing transaction …` makes the historical trace unavailable; it is not zero payment, failed execution, or proof that no settlement occurred. Continue independent durable reads and mark dependent ledger rows unavailable. Saved evidence remains timestamped historical evidence, not a fresh read. A separately authorized rerun creates a different payment trace and cannot recover the old one.

## Collectability boundary

Treat a Tool amount as `unavailable` until the exact Invocation, policy/beneficiary, deposit, and settlement records establish that it is collectible. Missing collection authority or an unperformed collection action does not establish a zero entitlement. Compute a collectible amount only after verifying field semantics, payment scope, and non-overlap; do not subtract or add balances whose relationship is only inferred.

## Safe conclusion

An exact Task → Occurrence → Execution → `ExecutionPayment` relationship and matching Tool/Invocation/payment locks are required before declaring a trace complete. If an ID, owner, version, type origin, network identity, or relationship is absent or inconsistent, mark the row unavailable/conflicting and return the earliest missing read. Do not repair a payment trace by guessing a command or submitting a transaction.
