# Task diagnosis worksheet

## Identity table

| Entity | Record |
| --- | --- |
| Task | ID, owner, package/skill identity, policy, status, version |
| Occurrence | ID, schedule, start/end, status, Task link |
| Execution | ID, occurrence link, vertex, authorization, status |
| Tool/Invocation | Tool FQN, registry identity, input commitment, output tag, receipt, beneficiary |
| Payment | source, reserve/payment ID, gas/priority/Tool split, charge/refund, settlement state |
| Provenance | endpoint, query method, checkpoint/transaction digest, response digest |

Label every value `observed`, `derived from exact event/effect`, `unavailable`, or `conflicting`. Preserve the raw identifier and type; do not normalize an unknown value into a guessed one.

## Ordered read path

1. Verify the selected CLI/SDK version and read-only command help.
2. Run `scripts/testnet_evidence.py --graphql-url https://graphql.testnet.sui.io/graphql` for explicit network identity and latest checkpoint.
3. Read Task, then its Occurrence, then Execution and the exact vertex/Invocation/result objects.
4. Read the related payment object and events; distinguish reserve, `ExecutionPayment`, Tool charge, refund, and gas.
5. Re-read any object whose version or ownership changed between responses and record the checkpoint or transaction digest.

## Failure branches

| Symptom | First distinction | Safe next read |
| --- | --- | --- |
| Pending | committed result versus authorization versus payment shortfall | Execution, result, payment, and latest checkpoint |
| Failed | Tool error versus malformed result versus timeout | Invocation receipt, result schema, and timeout fields |
| Timeout | occurrence schedule versus executor progress | Occurrence, Execution, and event provenance |
| Charge mismatch | Tool price versus policy amount versus gas | `ExecutionPayment`, Invocation, and policy objects |
| Refund question | refund destination and exact amount versus reserve closure | payment effects and recipient object |
| Unknown object | identifier/type/version mismatch | explicit object read and network identity |

## Stop conditions

Stop at the earliest missing or contradictory identity, owner, version, type origin, endpoint, or relationship. Do not refill, settle, abort, close, reschedule, or retry from this worksheet. Those are mutations and require a separate user-authorized plan with pre-state, exact IDs, gas, signer custody, and post-state readback.

## Evidence limits

The public archive helper authenticates source trees, and the local validators authenticate caller-bound structure. The testnet helper authenticates the GraphQL response and records a digest. These checks do not prove native execution, registration, liveness, or payment settlement beyond the state explicitly returned by the read.
