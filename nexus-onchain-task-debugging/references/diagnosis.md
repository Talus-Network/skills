# Task diagnosis worksheet

## Identity table

| Entity               | Record                                                                                                                                  |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Network/config       | Endpoint, chain ID, Nexus network ID, CLI/SDK version, package identities                                                               |
| Agent/Skill          | Agent ID, Skill ID, owner/controller, network binding, package/interface version, current status                                        |
| Task/config          | ID, owner, package/skill identity, policy, schedule configuration, status, version                                                      |
| Occurrence/config    | ID, schedule, start/end, timeout and lifecycle configuration, status, Task link                                                         |
| DAG/config           | ID, owner, historical version, entry group, vertices, edges, defaults, Tool bindings, port schemas                                     |
| Inputs/evaluations   | Supplied inputs, effective inputs, source, port/cardinality/type, commitment/hash, DAG and Invocation links                             |
| Execution/walk       | ID, Task/Occurrence/DAG links, walk index, vertex, authorization, timing, status and counters                                           |
| Tool/Invocation      | Tool ID/FQN, registry/cashier identity, registration state, endpoint/package, timeout, policy, cost, Invocation ID, beneficiary         |
| Result               | Invocation link, commitment, receipt, output variant/tag, output fields, schema match, commit and settlement state                      |
| Leader/authorization | Leader and capability IDs, network binding, assignment order, request and authorization evidence, deadline, submission outcome          |
| Task funding         | Funding source, reserve/vault ID, balance before/after, policy and ownership                                                            |
| Execution payment    | `ExecutionPayment` ID, source, amount, lock, consumption, refund and settlement state                                                   |
| Tool/fee payment     | Tool price/charge, cashier/beneficiary, priority fee, native SUI gas, recipients and effects                                            |
| Provenance           | endpoint, query method, checkpoint/transaction digest, response digest                                                                  |

Label every value `observed`, `derived from exact event/effect`, `user-supplied`, `unavailable`, or `conflicting`. Give each component an overall `correct`, `conflicting`, or `unavailable` verdict. Preserve the raw identifier and type; do not normalize an unknown value into a guessed one, and do not treat an object that merely exists as correct. A CLI-rendered value counts only when it is traceable to the exact on-chain object, event, or effect; help text and source code explain semantics but do not prove deployed state.

## Component completeness matrix

Before stating a cause, include one row for every related component reached through exact IDs, events, or effects. For payment components, use every row in the shared table below; do not replace those rows with an aggregate payment row.

| Component | Identity evidence | State/config evidence | Cross-link or invariant | Verdict |
| --- | --- | --- | --- | --- |
| Agent/Skill, Task/Occurrence configuration, DAG/configuration, supplied/effective inputs, Execution/walk, Tool/Invocation, result, Leader/authorization, provenance | Exact on-chain read/event/effect | Owner, type, historical version, status, schema, timing, or amount as applicable | Exact relationship and expected value | `correct`, `conflicting`, or `unavailable` |
| Payment components, one row per shared table row below | Exact payment object, event, or effect | Source, owner, lock, subtotal, charge, fee, gas, refund, recipient, or collection state as applicable | Exact payment ID, Execution/Invocation link, transfer, or custody invariant | `correct`, `conflicting`, or `unavailable` |

Do not omit a component because another event appears to explain the symptom. A timeout, abort, or payment-shortfall event is a protocol outcome; the completeness matrix determines whether an upstream identity, DAG, input, Tool, authorization, result, or payment mismatch caused or contributed to it.

## Shared payment component/invariant table

Use this same table to complete payment rows in the component completeness matrix and to populate the payment portion of the exhaustive cause ledger. Keep every applicable row even when one object or effect contains several fields. For completeness use `correct`, `conflicting`, or `unavailable`; for causes use `confirmed`, `ruled out`, or `unresolved`.

| Component | Required record and invariant | Evidence / completeness verdict | Cause status / missing or conflicting read |
| --- | --- | --- | --- |
| Task reserve/funding | Funding source, `TaskPaymentReserve` ID, owner, balance before/after, and policy; the reserve must belong to this Task/Occurrence and reconcile where exact amounts exist |  |  |
| `ExecutionPayment` total and lock | `ExecutionPayment` ID, Execution link, total amount, source, and lock; the payment must belong to this Execution and its locked total must be exact |  |  |
| `ExecutionPayment` gas subtotal | Gas budget, lock, consumption, and associated effect; the gas subtotal must reconcile to the payment and exact gas effect where exposed |  |  |
| `ExecutionPayment` Tool subtotal | Tool budget, lock, consumption, and exact Invocation link; the Tool subtotal must reconcile to the linked Invocation |  |  |
| `ExecutionPayment` priority subtotal | Priority budget, lock or consumption, and exact fee link; the priority subtotal must reconcile to the fee effect where exposed |  |  |
| Tool charge/beneficiary | Tool price, policy amount, charge, cashier, beneficiary, and Invocation receipt; charge and beneficiary must be established by exact records |  |  |
| Priority fee/recipient | Fee amount, vault or transfer, recipient, and transaction effect; recipient and amount must be established by the exact transfer/effect |  |  |
| Native SUI gas | Signer-owned gas coin, owner, amount, and gas effect; the gas source must be distinguished from protocol payment custody |  |  |
| Agent vault | Exact vault ID, owner, source, amount, and recipient; vault custody and transfer identity must be explicit |  |  |
| Refund amount/state | Refund source, amount, terminal state, and reserve closure; a balance difference alone does not establish a refund |  |  |
| Refund recipient | Exact refund destination or entitlement beneficiary and its object/effect; the recipient identity must be recorded |  |  |
| Collection authority/deposit | Cashier capability, policy, deposit, finalized Invocation, and collection action status; collectibility requires exact authority and deposit evidence |  |  |

Do not infer a transfer, charge, refund, recipient, or entitlement from an amount or object ID alone. A missing row remains `unavailable`; a contradictory source remains `conflicting`.

## Exhaustive cause ledger

After completing every independent safe read, list every applicable cause exposed by correctness invariants. This is an exhaustive protocol-record ledger, not a list of imagined actor or service behaviors. The shared payment table supplies one independent cause row for each payment component; copy its invariant, cite the exact evidence, and record the missing or conflicting read.

| Cause class | Exact invariant being tested | On-chain evidence | Status | Conflict or missing read |
| --- | --- | --- | --- | --- |
| Network/package/interface identity; Task/Occurrence configuration; DAG topology, reachability, defaults, binding, or schema; supplied/effective input source, cardinality, type, value, or commitment; Execution/walk lifecycle; Tool registration, Invocation, receipt, or result; Leader capability, authorization, deadline, submission, or takeover; provenance/version consistency | One exact invariant per row | Exact object/event/effect identifier, version, checkpoint, or transaction | `confirmed`, `ruled out`, or `unresolved` | Exact contradiction, or existing read that is missing or does not expose the value |
| Payment component, one row per shared payment table row | Exact invariant in the corresponding shared payment row | Exact payment object, event, effect, transfer, Invocation, or checkpoint | `confirmed`, `ruled out`, or `unresolved` | Exact contradiction, or missing/non-exposing payment read |

- `confirmed` requires an exact on-chain contradiction or invariant violation.
- `ruled out` requires exact on-chain evidence satisfying the whole invariant for the historical Execution; object presence or later configuration is insufficient.
- `unresolved` means the cause remains possible only because required on-chain evidence is missing, conflicting, stale, or not exposed by existing tools. State the missing read; do not invent a behavior narrative.
- Do not use `likely`, `probably`, confidence percentages, actor intent, expected service behavior, or temporal correlation as cause status.
- Keep one cause row per applicable payment component, including all shared-table rows, even when one cause is confirmed.

An amount with no matching source, effect, recipient, or final-state record is not enough to mark a cause `confirmed` or `ruled out`.

## Conservative inference rules

- A `RequestWalkExecutionEvent` proves that a walk was requested; it does not prove that the effective Tool payload contained every declared input or that the Tool ran.
- A Leader timeout is a downstream outcome until exact evidence identifies why no valid submission arrived. Check input completeness, authorization, Tool receipt/result, and payment before assigning the cause to a Leader or Tool.
- For every Tool input port, require one exact source: selected entry-group input, DAG default, or incoming edge. Matching DAG and Tool schemas are insufficient. A port with no source is a configuration conflict.
- Do not infer that defaults or edges exist because execution advanced. Do not infer that they are absent merely because a compact CLI view omits them; require an existing command/object read whose contract exposes absence, or mark the source unavailable.
- A published, finalized, or immutable object can still contain an incorrect configuration.
- Compare historical Execution/Event revisions with historical configuration. A later Agent Skill revision or replacement DAG may be the fix, but current state alone cannot be projected backward.
- Keep protocol outcome, root configuration cause, and later corrective change as three separate claims with separate evidence.
- Never infer what a Leader, Tool, scheduler, SDK, or external service attempted, received, rejected, or executed unless an exact on-chain record states that fact. If the chain records only a request, timeout, result, or payment effect, report only that recorded transition.

## Existing-tool boundary

Prefer existing `nexus` read-only inspection commands and the bundled `testnet_evidence.py` helper. Prepared public source may explain what a returned field means, but diagnosis must not patch or compile public archives, add temporary tests, write one-off decoders, or create new scripts to recover hidden state. When the installed tools cannot expose a material value, record the exact limitation and return `unavailable`. Propose new tooling only as a separate user-requested development task.

## Ordered read path

1. Verify the selected CLI/SDK version and read-only command help.
2. Run `scripts/testnet_evidence.py --graphql-url https://graphql.testnet.sui.io/graphql` for explicit network identity and latest checkpoint.
3. Resolve the supplied anchor ID and read Task, then its Occurrence, preserving their exact links and lifecycle counters.
4. Read the referenced Agent and Skill and validate owner/controller, network, package/interface version, and current status against the Task and Execution events.
5. Read the exact historical DAG with an existing inspection command and validate entry group, vertices, defaults, edges, Tool bindings, and input/output port schemas. Account for each Tool input port through an entry value, default, or edge; if the existing output omits a material source, mark it unavailable instead of inventing a decoder.
6. Read supplied inputs and effective evaluations; validate source, vertex port, cardinality, type, commitment, and the exact values or privacy-preserving hashes against the DAG and Invocation. Treat a walk-request event as dispatch evidence only.
7. Read the Execution and every referenced walk, then the exact Tool/cashier, Invocation, committed result or receipt, and declared output schema.
8. Read Leader selection, capability ownership/network binding, authorization, request deadline, submission, timeout, and takeover evidence.
9. Read Task reserve, `ExecutionPayment`, payment locks, Tool charges, refunds, recipients, priority fees, and native SUI gas as distinct surfaces.
10. Reconcile the ordered events/effects with all object versions and lifecycle counters, re-read any object whose version or ownership changed, and record transaction/checkpoint and response-digest provenance.
11. Complete every cause-ledger row supported by these surfaces. Continue independent reads after a conflict or unavailable field; mark only dependent causes unresolved when their evidence chain cannot be completed.

## Correctness invariants

- Agent, Skill, Leader capability, Tool, Task, and Execution network/package/interface identities must agree with exact on-chain bindings.
- Task, Occurrence, Execution, DAG, walk, vertex, Invocation, result, and payment IDs must cross-link through exact reads or events; matching names, FQNs, timestamps, or amounts are insufficient.
- The DAG entry group and edges must make the invoked vertex reachable, and its Tool binding must match the observed Tool ID/FQN and interface version.
- Every required effective input must have the expected port, cardinality, value kind/type, source, and commitment; extra, missing, stale, or mismatched inputs are conflicts even if dispatch succeeded.
- Every Tool input must resolve from exactly one entry value, DAG default, or incoming edge. A smaller entry-group input set is legitimate only when exact default/edge evidence accounts for the remaining required ports.
- A committed result must match the exact Invocation, input commitment, declared output variant/tag, port schema, receipt, authorization, and settlement record.
- Authorization and Leader evidence must match the selected network, vertex/walk, deadline, capability ownership, and submission outcome.
- Task/Occurrence/Execution/walk counters and terminal states must agree with the ordered event history.
- Task reserve, `ExecutionPayment`, Tool lock/charge/refund, priority fee, and native SUI gas must remain separately identified and arithmetically reconcilable where exact amounts are available.

## Failure branches

| Symptom         | First distinction                                              | Safe next read                                        |
| --------------- | -------------------------------------------------------------- | ----------------------------------------------------- |
| Pending         | committed result versus authorization versus payment shortfall | Execution, result, payment, and latest checkpoint     |
| Failed          | Tool error versus malformed result versus timeout              | Invocation receipt, result schema, and timeout fields |
| Timeout         | occurrence schedule versus executor progress                   | Occurrence, Execution, and event provenance           |
| Charge mismatch | Tool price versus policy amount versus gas                     | `ExecutionPayment`, Invocation, and policy objects    |
| Refund question | refund destination and exact amount versus reserve closure     | payment effects and recipient object                  |
| Unknown object  | identifier/type/version mismatch                               | explicit object read and network identity             |

## Read and mutation boundaries

Do not stop at the first plausible cause, terminal event, missing field, or contradiction while another independent required on-chain read is available. Continue through DAG/configuration, inputs, Execution/Tool/result/authorization, and every payment surface, then list all cause rows. When a missing or contradictory anchor prevents dependent reads, mark those dependent causes `unresolved` and identify the exact blocker rather than guessing their state. Do not develop ad hoc tooling to cross this boundary, and do not refill, settle, abort, close, reschedule, or retry from this worksheet. New tooling and chain mutations require separate explicit user authorization.

## Evidence limits

The public archive helper authenticates source trees, and the local validators authenticate caller-bound structure. The testnet helper authenticates the GraphQL response and records a digest. These checks prove only the returned public fields at collection time. They do not establish a Leader submission, native or off-chain Tool/provider execution, actor intent, finality, or payment settlement unless an exact execution/result/effect/receipt record states that fact.
