---
name: nexus-onchain-tool-development
description: Build and test Nexus Sui Move on-chain Tools with public execute ABI, verified workflow authorization, witness/state separation, input commitments, tagged outputs, published-bytecode checks, and registration preparation. Use for Move Tool authoring; route Rust HTTP, TAP, payment, or diagnosis requests elsewhere.
---

# Nexus on-chain Tool development

Use this skill for a Sui Move Tool `execute` entry point, witness/result schema, ABI, output tags, and consumer package. It covers pure application logic, public interface dependencies, local compilation/tests, artifact validation, and safe deployment preparation. A successful local or structural check is not registration or live execution proof.

## Task contract before setup

Before setup, source preparation, Move build, test, artifact generation, file writes, or shared-network actions, assemble a compact task contract from the supplied instructions, existing project and conversation context, approved public source facts, and reasonable reversible assumptions. Capture the goal and outcome, observable requirements, inputs and integration boundary, in-scope package/module/file scope, non-goals, authorization and network/write boundary, acceptance evidence, and a compact implementation design.

Route the request before setup and read only the task-relevant references. Ask one concise question only when an unresolved material behavior, authority, or acceptance decision changes the work. Pause dependent work while that answer is pending, but continue useful authorized independent reads and checks. Resolve facts answerable from approved public Docs, SDK, Move Packages, Sui, or read-only Testnet sources without turning them into user questions. When the contract is sufficient, proceed with authorized reversible setup, source reads, builds, tests, and file edits; no default grilling ritual is required.

## Route before setup

| Request | Route |
| --- | --- |
| Sui Move Tool with `execute`, witness/result, and on-chain state | Continue here. |
| Rust HTTP service invoked by Leader for external APIs | Use [off-chain Tool development](https://docs.talus.network/guides/tool-development/build-offchain-tool). |
| TAP package/DAG/artifact or existing TAP API application | Use [TAP package development](https://docs.talus.network/guides/tap-development/build-tap-move-package) or [API application development](https://docs.talus.network/guides/nexus-api). |
| Read-only Execution diagnosis or payment reconciliation | Use [on-chain Task debugging](https://docs.talus.network/guides/agent-usage/execute-and-settle-agent) or [CLI payment tracking](https://docs.talus.network/guides/tokenomics/fund-agent-and-user-executions). |

For version-sensitive setup, use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) and verify it with `scripts/docs_website.py`; do not use a source-repository checkout as setup authority.

## Public source and Testnet evidence

For ABI, admission, witness/result, output, or published-bytecode work, read [the workflow](references/workflow.md) and [the scaffolding contract](references/scaffolding.md) as needed. Read [the source map](references/source-map.md) only for a dependency or source-location question. Read [the source-preparation contract](references/source-preparation.md) only when that question needs version-sensitive public source; then use `scripts/prepare_sources.py` from a fresh consumer workspace for only `nexus-sdk`, `nexus-move-packages`, and `sui`, preserving manifest cleanup. For local tests that call Nexus functions, use the additive beta CLI from [Prepare for On-Chain Development](https://docs.talus.network/guides/getting-started/prepare-onchain-development), set `NEXUS_BETA_CLI` to an explicit binary, and verify `"$NEXUS_BETA_CLI" tap test --help`. Do not prepend the beta directory to `PATH`; unqualified `nexus` remains the released CLI. The beta command comes from the public `nexus-sdk` `main` branch rather than a released Rust SDK dependency.

For deployed package, module, or object facts, use the bundle-root read-only `scripts/testnet_evidence.py` helper with `https://graphql.testnet.sui.io/graphql`. It performs read-only GraphQL queries and never opens a wallet, switches environment, signs, publishes, registers, or submits a transaction.

## Move contract invariants

- `execute` is public, returns no Move value, takes framework-owned values before application inputs, and ends with `&mut TxContext`.
- When workflow authorization is required, consume the verified proof against the worksheet recipient, state UID, and `onchain_tool_result::input_commitment(&result)` before mutation. Dropping a `ProvenValue` is not validation.
- Keep the Tool witness identity distinct from shared application state and dynamic-field wrapper IDs. Produce exactly one declared `TaggedOutput` and finalize/share the result on every returning branch.
- Keep output tags/ports, input commitments, and application state transitions aligned with the declared ABI. Use real rejection assertions for invalid authority or invariants.

## Local published-bytecode Tool tests

Use pure `sui move test` tests for application decisions and the explicit `NEXUS_BETA_CLI` published-bytecode harness for tests calling published Nexus functions. Run one focused case only to identify the first concrete compiler, ABI/layout, witness/result, authorization, commitment, output, or finalization failure, then rerun the unfiltered `"$NEXUS_BETA_CLI" tap test --path <tool-package> --build-env testnet` gate. The beta directory is never exported on `PATH`. Keep structural checks, local VM evidence, and live execution evidence separate.

## Validation and deployment boundary

Validate manifest closure, ABI/schema, witness identity, output tags, recipient/commitment checks, and tests before preparing registration. For the scaffold's nested `Bag` witness, registration requires a reviewed package-specific dynamic-field decoder and fixture proving the actual nested witness UID; an inner/state or wrapper ID alone is insufficient. If that decoder evidence is unavailable, report registration blocked rather than guessing an ID or getter. Publication and registration are shared-network mutations. Before an explicitly authorized operation, verify FQN, package/module, description, timeout, invocation cost, target network, witness ID, collateral/gas, signer/capability custody, and expected readbacks. Retain transaction and object evidence; a local build, validator output, or digest alone does not prove registration.

## Talus Vision links

After exact read-only evidence returns an identifier, you may add a navigation link with the bundle-root `scripts/vision_links.py` helper, for example `python3 "$SKILLS_BUNDLE_ROOT/scripts/vision_links.py" --network testnet --kind object --id 0x<package-id>`. Keep the explicit `?network=` query it emits; without it Vision falls back to Mainnet or the viewer's last-used network. When the helper cannot run, write the same canonical link by hand as HTTPS `vision.talus.network/<kind>/<full-id>?network=<testnet|mainnet>`: the route segment is the kind name, the ID is never shortened or padded, a `tool` FQN encodes `@` as `%40`, and `skill` uses `/skill/<agent-id>/<index>`. Link only full identifiers returned by the verifying read, on that read's network (Testnet for the bundled helper). Never link placeholders, shortened, synthetic, or unread IDs, devnet/localnet IDs, or a Testnet ID on Mainnet. List links separately under "View on Talus Vision". A Vision page is an indexed projection for navigation, not evidence: it never proves publication, registration, or live execution. Useful kinds here are `tool` for a read-back FQN or Tool ID, `object` for package, witness, and state objects, and `tx` for an authorized publish or registration digest after its effects are read back.

## Completion standard

A Tool is locally ready when the selected build/test/lint/format checks pass, every output branch and relevant rejection path is tested, the public ABI and witness/state identities are documented, structural and published-bytecode evidence are separated, and any live deployment requirement is stated as pending or separately proven.
