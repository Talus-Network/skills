# Repository-owned TAP patterns

Use these patterns as local examples for a TAP with a direct path and a delayed path. The bundle ships concrete [direct](../fixtures/direct/README.md) and [delayed](../fixtures/delayed/README.md) fixtures with Move tests and bound artifacts; they intentionally use placeholder IDs and pure application state so they can be tested without a deployed service or network mutation.

For public lifecycle terminology, consult the published [TAP CLI reference](https://docs.talus.network/reference/cli/tap). This website link is documentation-only guidance and never a compiler, build, runtime, forward, or testnet source.

## Direct path

The DAG invokes one on-chain Tool, passes the required state/witness inputs, records a result tag, and ends at a declared output. Test the success branch, every error tag, input commitment, authorization recipient, witness satisfaction, and state transition.

## Delayed path

The DAG schedules a future occurrence on the same declared path. Test that the schedule policy, occurrence input, and follow-up output are distinct from the direct path. Do not infer follow-up execution from a successful schedule record.

## Mixed path

An off-chain Tool returns one schema-valid value; the DAG passes it to an on-chain Tool, which validates the value, consumes authorization and the state witness, mutates state, and emits a typed output. Keep provider failure and invalid input branches explicit.

## Evidence

Use the local validator for artifact consistency and `scripts/testnet_evidence.py` for read-only package/module/object observations. Keep the report's structural, repository-owned, and testnet evidence classes separate.
