# TAP workflow

## Package and artifact layout

Keep the package in a disposable consumer directory with `Move.toml`, `sources/`, and `tests/`. Keep the validated `artifacts/portable.dag.json` and `artifacts/portable.skill.tap.json` beside the package; the skill's `dag_path` must point to that exact `*.dag.json` artifact. Use repository-owned fixtures for stable examples.

## Public dependency provenance

Network-facing TAP manifests use the six reviewed public Move Registry package names and a committed `Move.lock`:

```toml
[dependencies]
nexus_interface  = { r.mvr = "@talus/nexus-interface" }
nexus_primitives = { r.mvr = "@talus/nexus-primitives" }
nexus_registry   = { r.mvr = "@talus/nexus-registry" }
nexus_tool       = { r.mvr = "@talus/nexus-tool" }
nexus_scheduler  = { r.mvr = "@talus/nexus-scheduler" }
nexus_workflow   = { r.mvr = "@talus/nexus-workflow" }
```

For offline source inspection and repository-owned fixture tests, prepare the three approved public archives with `scripts/prepare_sources.py --only nexus-sdk --only nexus-move-packages --only sui`; the Sui archive is the exact `d8459684b41eb09ab23fe16a9dd84173270bbaba` source for installed `sui 1.78.0-d8459684b41e`. Copy the required Move-package closure and only the pinned Sui framework packages into `deps/`, preserve `Move.toml` and `Published.toml`, and ensure every offline consumer dependency stays inside that copied closure. Do not publish native interface packages or invent MVR records for policy/kernel.

## DAG and skill design

Bind every vertex to a declared Tool FQN and schema. The repository-owned DAG schema requires unique lowercase vertex names, `on_chain` or `off_chain` kinds, non-empty `entry_ports` and per-variant `output_ports`, and edges whose source/target vertices and ports are declared. Every output binds a declared vertex, variant, and port; the TAP skill points to the exact validated DAG and carries `requirements.input_commitment`, payment/schedule policies, fixed Tool byte bindings, shared objects, and `interface_revision.inner`. Validate these fields against caller-held intent and keep each path's required inputs and outputs explicit.

## Local verification

Run `sui move build`, `sui move test`, the SDK's structural DAG/TAP validators, and the bundled compiled/artifact validators in disposable directories. Capture the selected toolchain and generated digests. A structural pass proves syntax and consistency only.

## Testnet read boundary

Use `scripts/testnet_evidence.py --graphql-url https://graphql.testnet.sui.io/graphql` with optional package/module/object arguments for public deployed observations. Retain the endpoint, network label, methods, response digests, and timestamp. An observation does not prove execution, binding, or settlement.

## Mutation gate

Publishing a package, registering a Tool, binding a skill, scheduling a Task, upgrading a package, depositing funds, or submitting any transaction is a shared-network mutation. Stop before it unless the user authorizes the exact operation and the preflight proves network identity, package lineage, object IDs, ownership/capability, recipient, amount, gas, and expected post-state reads.
