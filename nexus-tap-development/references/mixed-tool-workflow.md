# Mixed off-chain/on-chain workflow

## Public interface authority

Use the public [Move Registry interface package](https://www.moveregistry.com/package/@talus/nexus-interface) and [primitives package](https://www.moveregistry.com/package/@talus/nexus-primitives) for a network-facing on-chain Tool manifest, and commit `Move.lock` for the selected network. The public [Nexus Move Packages](https://github.com/Talus-Network/nexus-move-packages/tree/main/packages) repository remains the source/provenance authority for offline closure inspection; keep the selected revision and published-address records together, and do not substitute a private implementation source or a guessed deployment.

## Contract

The off-chain Tool returns exactly one schema-valid choice or an explicit error. The DAG maps that value to the on-chain Tool input. The on-chain Tool validates the value before changing state, consumes the exact authorization/witness/commitment proof, and emits a declared output payload.

## Test matrix

Cover each legal provider choice, provider timeout/error, malformed JSON, invalid choice, commitment mismatch, wrong recipient, unsatisfied witness, and every reachable output branch. Use deterministic local mocks for provider I/O and pure Move tests for application logic.

## Published-bytecode check

After the pure application tests, run the beta `"$NEXUS_BETA_CLI" tap test --path <tap-package> --build-env testnet` command when the TAP tests call published Nexus functions. It reads public bytecode and overlays only test extensions in a local VM; it does not publish, register, bind, schedule, settle, or move assets. Diagnose the earliest ABI, authorization, commitment, output, or finalization error, make the smallest repair, and rerun the same VM gate before artifact checks.

## Artifact checks

Validate the DAG edge, input/output ports, fixed Tool identity, skill path, payment policy, and schedule policy against caller-held semantic intent with `verify_tap_artifacts.py --require-artifacts --json`. This structural validator owns JSON/artifact failures; negative mutations must fail closed and must not be routed to the beta VM.

## Deployed read

If a package is already deployed, query its package/module/object state through `scripts/testnet_evidence.py` at `https://graphql.testnet.sui.io/graphql`. Preserve the response digest and identify the query as read-only. Do not claim that it proves the mixed workflow executed.
