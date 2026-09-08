# Scaffold and finish an on-chain Tool

Use this reference to turn a Tool description into a Sui Move package, its declared output schema, tests, and registration evidence. Follow the published [on-chain Tool guide](https://docs.talus.network/guides/tool-development/build-onchain-tool) and [CLI reference](https://docs.talus.network/reference/cli/tool). Use the [public source map](source-map.md) for version-sensitive interface questions.

## Place and scaffold the package

Choose a package directory inside the user's authorized project. Follow an existing on-chain package layout when present; otherwise use a standalone directory. Do not nest a new package inside another Move package or add Rust HTTP deployment files to a Move Tool. Inspect existing files before adapting them.

Resolve the FQN namespace and action from the user/project contract. FQNs have at least three dot-separated parts matching `[a-z][a-z0-9_-]+` and a positive integer version, for example `com.example.counter.increment@1`. Treat `com.example` as an example namespace requiring replacement before registration. Keep the package/module identifier valid Move syntax; the FQN and filesystem name need not be identical.

After the parent skill's contract and Setup checks, use the released public scaffold:

```bash
nexus tool new --name counter_tool --description "Increment and report a shared counter" --template move --target ./
```

Adapt the generated package instead of maintaining a second frozen template. Use `nexus_primitives = { r.mvr = "@talus/nexus-primitives" }` and `nexus_interface = { r.mvr = "@talus/nexus-interface" }` as needed, retain the Tool's own unpublished address, and commit the resolved `Move.lock`. Match edition and any environment configuration to the selected compiler and documented test-extension support. A general Tool scaffold and a TAP scaffold have different manifest contracts; use the TAP workflow for a TAP package. Never search for sibling implementation checkouts or copy environment-specific dependency paths.

## Choose the execution admission model

`execute` must be public, return no Move value, take owned framework values before all application inputs, and end with `&mut TxContext`. Bare `entry fun` visibility is insufficient. The standard prefix is `UIDRequirements, OnchainToolResult`; the workflow-authorization prefix prepends `ProvenValue<AgentVertexAuthorization>`. The CLI derives registration mode from that signature; there is no `--workflow-authorization-cap-first` registration flag. `Clock` and shared application objects are ordinary application inputs after the prefix.

Choose the admission model from who may change the protected state, not from a keyword heuristic or the scaffold's default. When workflow authorization is required, consume and verify it against the worksheet recipient, state UID, and `onchain_tool_result::input_commitment(&result)` before mutation. Use the documented `consume_verified_for_worksheet_as_recipient` path and enforce the application's custody, recipient, amount, and state rules. Merely dropping `ProvenValue` or declaring a fixed Tool does not validate this authority. Preserve the supplied user design or resolve a material missing admission decision before implementing it.

## Implement witness, state, and outputs

- Keep the Tool witness identity distinct from the shared application state identity. The scaffold stores a witness object in a state-owned `Bag`; `tool_witness_id` returns that witness UID. Shared state lets workflows supply the object without tying execution to one submitting address; owned inputs need an explicit custody design.
- Satisfy `UIDRequirements` with the registered Tool witness on every successful finalization path. Check authorization and invariants before changing state or releasing assets.
- The public `Output` enum is registration schema. It must describe every emitted tag and named port even though runtime code builds `TaggedOutput`, not an `Output` value. Use matching snake_case tags/ports, flat values, and `err_*` branches for expected business outcomes. Reserve aborts/assertions for genuine invalid authority or invariant violations.
- Build inline values with `data::inline_data_value` using valid JSON bytes: strings/addresses include their JSON quotes, numbers and booleans do not. Use the documented `with_named_payload_many` form for a multi-valued port. Do not use obsolete `inline_one` or type-hint helpers.
- Produce exactly one declared `TaggedOutput` on each returning branch and finish through `nexus_interface::onchain_tool_result::finalize_and_share(result, requirements, output, ctx)`. Application events may aid observation but do not replace the result consumed by DAG edges.

## Test the implemented behavior

Factor the application decision/state transition so tests exercise the same logic used by `execute`. Put necessary test-only construction, inspection, invocation, and cleanup helpers in documented module extensions; do not invent public constructors for framework-owned values. Test every output branch, state delta, witness identity, and relevant authorization/recipient/commitment rejection.

Use `sui move build`, and plain `sui move test` for Nexus-free logic tests. For tests calling published Nexus functions, use the parent's explicit `NEXUS_BETA_CLI` published-bytecode harness, including the unfiltered final run. The harness makes local Nexus-call tests possible; do not claim `execute` can never be tested merely because an older scaffold lacked constructors. Keep structural checks, local VM evidence, and live execution evidence separate.

Verify schema/ABI, fixed prefix order, required witness, every output tag/port, and authorization-before-mutation behavior. A witness test must prove the state object ID differs from the witness ID expected by registration. Remove placeholder inputs, TODO business logic, and untested fake outputs before marking the implementation ready.

## Prepare registration evidence

Publication and registration are separately authorized transactions. Prepare the package/module, FQN, description, timeout, invocation cost, target network, exact witness ID, collateral/gas inputs, and expected readbacks before any signature.

For the scaffold's nested `Bag` witness, the published guide requires a reviewed package-specific dynamic-field decoder and a fixture proving it extracts the actual nested witness UID. A state read alone or a generic list of fields does not establish this ID. If that decoder/evidence is unavailable, report registration blocked; do not substitute the state ID, a dynamic-field wrapper ID, a guessed getter invocation, or an invented decoder.

When explicitly authorized and evidence is complete, use the CLI's documented registration flow, then `nexus tool validate onchain --ident <FQN>` and `nexus tool inspect --tool-fqn <FQN>`. Validation re-derives schema and admission mode from the deployed package and compares them to registration. Retain transaction, package lineage, Tool/state/witness IDs, FQN, network, and separate Tool/ToolCashier capability custody in the project's existing deployment record. Confirm collateral and SUI gas separately; neither an indexer row nor a local build proves registration.

Version a changed input/output or admission contract deliberately; keep existing DAGs compatible with their pinned FQN. Revalidate a changed package and its recorded registration rather than assuming an implementation upgrade is automatically reflected in registry state.

## Completion checklist

- [ ] Package placement, public dependencies, lockfile, edition, and Tool manifest match the selected workflow and network.
- [ ] `execute` has the supported public ABI, a verified admission design, and no mutation before required authorization checks.
- [ ] Witness identity is distinct from state identity, output schema matches all tags/ports, and each returning path finalizes one result.
- [ ] The real business logic and rejection paths are tested; build, structural, and applicable published-bytecode gates are recorded separately.
- [ ] README documents FQN, input/output ports, state/witness meaning, test commands, and unresolved deployment requirements.
- [ ] Nested-witness decoding and authorized registration/readback are either proven or explicitly pending; no capability or deployment ID is fabricated.
