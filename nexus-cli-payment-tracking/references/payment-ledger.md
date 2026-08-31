# Payment ledger

## Trace worksheet

Record one row for each observed object or event:

| Field | Required value |
| --- | --- |
| Identity | object ID, type, version, digest, owner |
| Workflow link | Task ID, Occurrence ID, Execution ID, vertex, Invocation ID |
| Payment link | payment ID, source, policy, budget split, Tool/priority totals |
| Outcome | status, charge, refund, settlement evidence, transaction provenance |
| Evidence class | observed, exact-event derived, unavailable, or conflicting |

Never infer an Invocation from an FQN, Tool price, cashier ID, or payment ID alone. Correlate the exact Execution, runtime vertex, Tool, policy, amount, source, beneficiary, and refund destination.

## Distinct custody surfaces

- SUI gas is immediate transaction funding and is independent from payment reserves and Tool earnings.
- `TaskPaymentReserve` funds future occurrences; `ExecutionPayment` funds one dispatched Execution.
- Agent vault balance is Agent-funded custody and must match the exact Agent/source identity.
- Tool registration collateral is distinct from Tool invocation revenue.
- ToolCashier inbox/collection evidence applies only to finalized Invocation or deposit objects accepted by the selected policy.
- Priority fees in an ExecutionPayment are separate from any priority-vault balance or share.

## Testnet evidence

Use `../scripts/testnet_evidence.py` with an explicit official testnet endpoint for package, module, object, and chain observations. Retain its `network`, `endpoint`, `calls`, response digests, and report digest. A successful read proves only that the returned public state existed at collection time; it does not prove execution, registration, payment settlement, or authorization.

## Safe conclusion

If an ID, owner, version, type origin, network identity, or relationship is absent or inconsistent, mark the row unavailable/conflicting and stop at the earliest missing read. Do not repair a payment trace with a guessed command or by submitting a transaction.
