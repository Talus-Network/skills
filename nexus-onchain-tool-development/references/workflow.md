# On-chain Tool workflow

## 1. Define the contract

Choose the FQN, module/function name, input schema, output tags/payload types, witness identity, authorization mode, and exact state mutation. Preserve the public framework argument prefix and trailing mutable transaction context.

## 2. Prepare dependencies

For a network-facing consumer, use `r.mvr = "@talus/nexus-primitives"` and `r.mvr = "@talus/nexus-interface"` from the reviewed [Move Registry package pages](https://www.moveregistry.com/package/@talus/nexus-interface); commit `Move.lock` and match its resolution to the target network. For offline source inspection and deterministic fixture tests, run the public helper with repeated `--only nexus-sdk --only nexus-move-packages --only sui`, resolve roots from its manifest, copy the needed public Move package closure plus only the pinned Sui framework packages into a disposable consumer project, and retain `Published.toml` provenance. Never put a host path into a maintained network-facing manifest, and do not infer unregistered policy/kernel registry names.

## 3. Implement and test

Consume the state witness and any workflow authorization against the exact recipient, state UID, and input commitment before mutation. Satisfy requirements, construct a declared `TaggedOutput` on every branch, and finalize through the public result API. Test successful output, each error branch, malformed input, authorization mismatch, wrong recipient, commitment mismatch, witness mismatch, and mutation-order violations.

## 4. Validate artifacts

Run `sui move build` and `sui move test` in a disposable project. Capture the actual compiled module/build receipt and use the bundle validators for call-graph, tree, schema, DAG, and identity consistency. Treat those reports as offline structural evidence only.

## 5. Read deployed state

When a package or module has already been deployed, run `scripts/testnet_evidence.py --graphql-url https://graphql.testnet.sui.io/graphql --package-id 0x<package-id> --module <module-name>`. Retain the report and digest. The command is read-only and cannot establish a transaction or registration outcome by itself.

## 6. Gate mutations

Publication, Tool registration, DAG binding, scheduling, upgrades, deposits, and transactions require a separate user-authorized plan. Immediately before an authorized mutation, verify endpoint/network identity, package lineage, exact object IDs, signer/capability custody, recipient, amount, gas, and expected post-state reads. Stop on any mismatch.

## Failure recovery

If public source preparation, compilation, validator checks, or testnet reads fail, preserve the earliest concrete error and do not substitute a different source, guessed flag, fabricated ID, or state-changing retry.
