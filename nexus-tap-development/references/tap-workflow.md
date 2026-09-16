# TAP workflow

This workflow applies to package, DAG, and skill-artifact changes. For an application over an existing TAP, use [Nexus API application development](nexus-api-application.md); app-only work does not require package setup or fixture gates.

Use the released `nexus` binary for scaffold, validation, publication, registration, binding, and scheduling. Set `NEXUS_BETA_CLI` to the explicit beta binary from Developer Setup and use it only for `tap test`; do not put its directory on `PATH`.

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

The public Move Packages archive includes a complete embedded TAP example at `examples/local_testing`. After resolving `MOVE_PACKAGES_ROOT` from the source manifest, use it as a fresh-agent creation/test reference:

```bash
"$NEXUS_BETA_CLI" tap test --path "$MOVE_PACKAGES_ROOT/examples/local_testing" --build-env testnet
```

This command is local/read-only with respect to Nexus: it loads published bytecode and runs the example's test extensions in memory. Do not treat its result as package publication, Tool registration, Agent binding, scheduling, settlement, or live execution proof.

## DAG and skill design

Bind every vertex to a declared Tool FQN and schema. The repository-owned DAG schema requires unique lowercase vertex names, `on_chain` or `off_chain` kinds, non-empty `entry_ports` and per-variant `output_ports`, and edges whose source/target vertices and ports are declared. Every output binds a declared vertex, variant, and port; the TAP skill points to the exact validated DAG and carries `requirements.input_commitment`, payment/schedule policies, fixed Tool byte bindings, shared objects, and `interface_revision.inner`. Validate these fields against caller-held intent and keep each path's required inputs and outputs explicit.

## Local published-bytecode verification

After package-owned `sui move build`, run plain `sui move test` only for a Nexus-free test package. For a TAP package or test package that calls published Nexus functions, set `NEXUS_BETA_CLI` to the explicit binary path from the published Developer Setup and run:

```bash
"$NEXUS_BETA_CLI" tap test --path <tap-package> --build-env testnet
```

The harness reads public Nexus bytecode for the selected environment, overlays only `#[test_only]` extensions in memory, and runs a local Sui VM. It needs network read access but no wallet, signer, gas, publication, registration, binding, scheduling, settlement, or asset movement. Use `--list` or one named filter to isolate the first failure, then rerun the unfiltered gate.

Repair from the earliest concrete compiler, linker, ABI/layout, witness/result, authorization, input-commitment, output, or finalization message. Change only the implicated package source or test arrangement and rerun the same VM gate. A green result proves the exercised published calls locally, not live registry state or Leader execution.

## Structural DAG and skill-artifact verification

Run `python3 "$SKILLS_BUNDLE_ROOT/nexus-tap-development/scripts/verify_tap_artifacts.py" "$TAP_PROJECT" --require-artifacts --json` as a separate gate. It owns manifest closure, DAG vertices/edges/ports, skill `dag_path`, fixed-Tool FQNs, payment/schedule policies, input commitments, shared objects, interface revision, and artifact paths. If this command reports a mismatch, repair the JSON/artifact input and rerun this validator; do not send the failure to the beta VM. Its report is `repository-owned-structural` evidence with `runtime_proof` equal to `not-proven`.

## Local structural verification

Run `sui move build`; run `sui move test` only for a Nexus-free test package or filter; then run the SDK's structural DAG/TAP validators and the bundled compiled/artifact validators in disposable directories. Capture the selected toolchain and generated digests. A structural pass proves syntax and consistency only.

## Testnet read boundary

Use `scripts/testnet_evidence.py --graphql-url https://graphql.testnet.sui.io/graphql` with optional package/module/object arguments for public deployed observations. Retain the endpoint, network label, methods, response digests, and timestamp. An observation does not prove execution, binding, or settlement.

## Authorized live walk on v2.0.0

Use this path only when the requested work includes live DAG execution; API-only development and local VM tests do not need it. `nexus dag execute` is gone in v2.0.0: publish a DAG, then schedule a Task. Check `nexus --version`, binary provenance, `nexus dag publish --help`, and `nexus task schedule --help` against the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup). A local build reporting `2.0.0` may expose later commands. The released CLI does not include `tap test` or `--authorization-binding`; protected Agent-skill bindings need the supported SDK/Move path, not guessed release flags.

Before signing, apply the mutation gate below. Verify the target network, signer, configured executor, exact DAG entry group/inputs, and approved funding amounts. SUI address balance is not proof of usable signer-owned gas coins; Task reserve funding and transaction gas are separate. Set `PREPAY_MIST` to the approved reserve and `OCCURRENCE_BUDGET_MIST` to the approved occurrence budget, both integer MIST; the reserve must cover the intended budget. Both flags are mandatory even for an immediate one-off walk. Do not invent amounts or change executors to bypass a preflight failure.

Inspect each bound Tool using its exact versioned FQN from the DAG or deployment receipt:

```bash
nexus tool inspect --tool-fqn "$TOOL_FQN" --json
```

Check returned identity, network, and registration, not just exit status. Do not use `nexus tool list` for discovery or registration proof: a successful result can be empty or contain unavailable details. Recover unknown FQNs from artifacts or request deployment records; unavailable inspection does not authorize duplicate registration.

For a new DAG, validate and publish; for an already published DAG, use its verified ID without republishing:

```bash
nexus dag validate --path "$DAG_PATH"
nexus dag publish --path "$DAG_PATH"
```

Save the actual returned DAG ID as `DAG_ID`. Inspect its entry groups and required inputs before setting `ENTRY_GROUP` and `INPUT_JSON`, then schedule within the authorized spending scope:

```bash
nexus dag inspect --dag-id "$DAG_ID"
nexus task schedule --dag-id "$DAG_ID" \
  --entry-group "$ENTRY_GROUP" --input-json "$INPUT_JSON" \
  --prepay-amount-mist "$PREPAY_MIST" \
  --occurrence-budget-mist "$OCCURRENCE_BUDGET_MIST" --now
```

Save the returned Task ID and transaction digest. Discover the actual Occurrence ID; never assume it is zero. These readbacks do not submit another walk:

```bash
nexus task inspect --task-id "$TASK_ID"
nexus task occurrence list --task-id "$TASK_ID" --json
nexus task occurrence inspect --task-id "$TASK_ID" --occurrence-id "$OCCURRENCE_ID"
nexus execution inspect --task-id "$TASK_ID" --occurrence-id "$OCCURRENCE_ID"
```

Publication or scheduling success is not Execution completion or settlement. Follow the exact Task → Occurrence → Execution links and result/payment effects; pending state is not a reason to schedule again.

Capture raw readbacks, errors and exit status, transaction effects/events, result/payment evidence, IDs, CLI version/revision, network/endpoint, and collection time immediately. Testnet execution history may be pruned after only a few days; there is no guaranteed retention interval. `history is incomplete: missing transaction …` means the historical trace is unavailable, not that the walk failed. Keep independent durable reads and timestamped saved evidence, without presenting them as fresh historical verification. A newly authorized run produces new evidence; it cannot recover the old execution.

## Mutation gate

Publishing a package, registering a Tool, binding a skill, scheduling a Task, upgrading a package, depositing funds, or submitting any transaction is a shared-network mutation. Stop before it unless the user authorizes the exact operation and the preflight proves network identity, package lineage, object IDs, ownership/capability, recipient, amount, gas, and expected post-state reads.
