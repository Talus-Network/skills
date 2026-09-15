# On-chain Tool workflow

For a new package, begin with [scaffolding and its completion checklist](scaffolding.md), including the supported ABI and nested-witness registration boundary.

## 1. Define the contract

Choose the FQN, module/function name, input schema, output tags/payload types, witness identity, authorization mode, and exact state mutation. Preserve the public framework argument prefix and trailing mutable transaction context.

## 2. Prepare dependencies

For a network-facing consumer, use `r.mvr = "@talus/nexus-primitives"` and `r.mvr = "@talus/nexus-interface"` from the reviewed [Move Registry package pages](https://www.moveregistry.com/package/@talus/nexus-interface); commit `Move.lock` and match its resolution to the target network. For offline source inspection and deterministic fixture tests, run the public helper with repeated `--only nexus-sdk --only nexus-move-packages --only sui`, resolve roots from its manifest, copy the needed public Move package closure plus only the pinned Sui framework packages into a disposable consumer project, and retain `Published.toml` provenance. Never put a host path into a maintained network-facing manifest, and do not infer unregistered policy/kernel registry names.

## 3. Implement and test

Consume the state witness and any workflow authorization against the exact recipient, state UID, and input commitment before mutation. Satisfy requirements, construct a declared `TaggedOutput` on every branch, and finalize through the public result API. Test successful output, each error branch, malformed input, authorization mismatch, wrong recipient, commitment mismatch, witness mismatch, and mutation-order violations.

## 4. Run local published-bytecode tests

After package-owned `sui move build`, run plain `sui move test` only for tests that do not call Nexus functions. For tests that call published Nexus functions, set `NEXUS_BETA_CLI` to the explicit binary path from the published Developer Setup and run `"$NEXUS_BETA_CLI" tap test --path . --build-env testnet`. The harness reads public Nexus bytecode for the selected environment, overlays only `#[test_only]` extensions, and runs a local Sui VM; it does not publish, register, schedule, sign, or move assets. Use `--list` or one named filter for diagnosis, then rerun the unfiltered command.

Treat the first concrete ABI, type-layout, witness/result, authorization, input-commitment, output, or finalization error as the repair target. Fix only the Tool source or test arrangement that caused it, rerun the same gate, and report the command output. A green result proves the exercised published call locally, not live Tool registration or workflow execution.

For a complete public Tool/TAP starting point, use `examples/local_testing` from the prepared Nexus Move Packages source and run its local suite with `"$NEXUS_BETA_CLI" tap test --path "$MOVE_PACKAGES_ROOT/examples/local_testing" --build-env testnet`. The example demonstrates a package-owned state module, an on-chain Tool `execute` ABI, authorization and input-commitment checks, published result finalization, and rejection tests. It remains a local/read-only test fixture; it does not register or execute a Tool on a network.

## 5. Validate artifacts

Run `sui move build` in a disposable project; run `sui move test` only for a Nexus-free test package or filter. Capture the actual compiled module/build receipt and use the bundle validators for call-graph, tree, schema, DAG, and identity consistency. Treat those reports as offline structural evidence only.

## 6. Read deployed state

When a package or module has already been deployed, run `scripts/testnet_evidence.py --graphql-url https://graphql.testnet.sui.io/graphql --package-id 0x<package-id> --module <module-name>`. Retain the report and digest. The command is read-only and cannot establish a transaction or registration outcome by itself.

For registration verification, resolve the exact versioned FQN from the deployment receipt or DAG/skill artifact and run:

```bash
nexus tool inspect --tool-fqn "$TOOL_FQN" --json
```

Verify returned Tool/package identity, network, and registration against the intended deployment. Do not use `nexus tool list` for discovery or registration proof: a successful command can return an empty inventory or unavailable details. Exit 0 alone is insufficient; preserve failed or incomplete inspection as unavailable evidence. If the FQN is unknown, obtain the deployment artifact rather than inventing one or registering a duplicate.

Registration does not prove a live invocation. Only when the user requests a live walk, follow the published [execution guide](https://docs.talus.network/guides/agent-usage/execute-and-settle-agent) and verify the selected release's help: v2.0.0 uses DAG publication then Task scheduling, with mandatory `--prepay-amount-mist` and `--occurrence-budget-mist`; `dag execute` is gone. Keep approved Task funding separate from signer-owned SUI transaction gas. Do not make live scheduling a prerequisite for local Tool development.

For authorized live proof, save exact Task/Occurrence/Execution and Tool IDs, result/payment effects/events, transaction digests, raw output/errors and exit status, CLI version/revision, network/endpoint, and collection time immediately. Do not assume Occurrence zero or infer completion/settlement from scheduling. Testnet history may be pruned after a few days, with no guaranteed interval: `history is incomplete: missing transaction …` means unavailable historical evidence, not failed execution. Preserve independent durable reads and timestamped saved evidence without calling it a fresh verification; a newly authorized run is new evidence, not recovery of the old trace.

## 7. Gate mutations

Publication, Tool registration, DAG binding, scheduling, upgrades, deposits, and transactions require a separate user-authorized plan. Immediately before an authorized mutation, verify endpoint/network identity, package lineage, exact object IDs, signer/capability custody, recipient, amount, gas, and expected post-state reads. Stop on any mismatch.

## Failure recovery

If public source preparation, compilation, validator checks, or testnet reads fail, preserve the earliest concrete error and do not substitute a different source, guessed flag, fabricated ID, or state-changing retry.
