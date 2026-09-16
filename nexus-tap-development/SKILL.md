---
name: nexus-tap-development
description: "Build and verify Talus Agent Packages and their applications: Move packages, DAG/skill artifacts, structural validators, published-bytecode tests, or existing-TAP REST/SSE dashboards through a server-side relay. Use for TAP workflows; route standalone Tools, payment reconciliation, or Execution diagnosis elsewhere."
---

# Nexus TAP development

Before using helper commands or fixtures, read [skill-only installation and companion setup](references/consumer-setup.md); the skill installer does not supply the repository-level companion tools.

Use this skill for a DAG-backed TAP package, its Move package and skill artifact, or an application that reads an existing TAP through Nexus API REST/SSE. Keep structural/local tests, read-only API evidence, beta published-bytecode tests, and shared-network asset movement as separate evidence categories.

## Task contract before setup

Before setup, source preparation, package/DAG/fixture build, test, artifact generation, file writes, or shared-network actions, assemble a compact task contract from the supplied instructions, existing project and conversation context, approved public source facts, and reasonable reversible assumptions. Capture the goal and outcome, observable requirements, application/TAP/package/DAG/Tool inputs and integration boundary, in-scope deliverable/file scope, non-goals, authorization and network/write boundary, acceptance evidence, and a compact implementation design.

Route the request before setup and read only the task-relevant references. Ask one concise question only when an unresolved material behavior, authority, or acceptance decision changes the work. Pause dependent work while that answer is pending, but continue useful authorized independent reads and checks. Resolve facts answerable from approved public Docs, SDK, Move Packages, Sui, or read-only Testnet sources without turning them into user questions. When the contract is sufficient, proceed with authorized reversible setup, source reads, builds, tests, and file edits; no default grilling ritual is required.

## Route before setup

| Request | Workflow |
| --- | --- |
| Dashboard, React dApp, bot, or backend for an existing TAP | Read [Nexus API application development](references/nexus-api-application.md). Use mocked REST/SSE and a server-side relay; no real key is needed for local implementation. |
| New or changed TAP Move package, DAG, or skill artifact | Follow the package setup, public-source, fixture, and structural-validation workflow below. |
| TAP application plus package/DAG changes | Apply both the API application and package/artifact workflows. |
| Standalone Move or Rust HTTP Tool, payment reconciliation, or exhaustive Execution diagnosis | Route to [on-chain Tool](https://docs.talus.network/guides/tool-development/build-onchain-tool), [off-chain Tool](https://docs.talus.network/guides/tool-development/build-offchain-tool), [CLI payment tracking](https://docs.talus.network/guides/tokenomics/fund-agent-and-user-executions), or [on-chain Task debugging](https://docs.talus.network/guides/agent-usage/execute-and-settle-agent). |

For an app-only request, skip Move/CLI installation, source downloads, package scaffolding, fixtures, and published-bytecode gates. Hosted API projections and provider keys do not replace wallet/SDK transaction authority or chain evidence.

## Public source and Testnet evidence

For an API-only request, continue directly with [Nexus API application development](references/nexus-api-application.md) from the route above and skip package references, source downloads, fixtures, and published-bytecode gates. For a TAP package, DAG, or skill artifact, read [the TAP workflow](references/tap-workflow.md) and [the package patterns](references/demo-tap.md); read the [direct](fixtures/direct/README.md) or [delayed](fixtures/delayed/README.md) fixture guide only when that fixture path is in scope. Read the [mixed-Tool workflow](references/mixed-tool-workflow.md) only when the TAP combines Tools. Read [the source-preparation contract](references/source-preparation.md) only for a remaining version-sensitive public-source question; then use `scripts/prepare_sources.py` from a fresh consumer workspace for only `nexus-sdk`, `nexus-move-packages`, and `sui`. Use repository-relative paths below verified roots, preserve manifest cleanup, and stop at a source-evidence gap.

For version-sensitive setup, use public [Developer Setup](https://docs.talus.network/guides/getting-started/prepare-onchain-development) and verify commands with `scripts/docs_website.py`. Network-facing Move packages use reviewed public Move Registry names committed in `Move.lock`; repository source remains offline inspection provenance.

## Local published-bytecode TAP tests

Pure local tests cover application logic. When tests call published Nexus functions, set `NEXUS_BETA_CLI` to an explicit public beta binary and run `"$NEXUS_BETA_CLI" tap test --path <tap-package> --build-env testnet`. Use `--list` or one named case only to diagnose the first concrete compiler, linker, ABI/layout, witness/result, authorization, input-commitment, output, or finalization error, then rerun the same command unfiltered. The beta directory is never exported on `PATH`; the harness uses no wallet, signer, gas, publication, registration, binding, scheduling, settlement, or asset movement. A green VM case proves only the exercised published calls.

## Structural DAG and skill-artifact validation

Run the structural gate separately:

```bash
TAP_PROJECT=<repository-relative-tap-project>
python3 "$SKILLS_BUNDLE_ROOT/nexus-tap-development/scripts/verify_tap_artifacts.py" \
  "$TAP_PROJECT" --require-artifacts --json
```

This gate owns `Move.toml` closure, DAG vertices/edges/ports, fixed-Tool FQNs, policies, commitments, `dag_path`, and skill-artifact consistency. A structural failure belongs to this validator; do not retry it through `"$NEXUS_BETA_CLI" tap test`. Its report is structural evidence with `runtime_proof` set to `not-proven`.

## Read-only Testnet evidence

For deployed package, module, object, or network facts, use bundle-root `scripts/testnet_evidence.py` with the explicit official Sui Testnet GraphQL endpoint:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Retain endpoint, network label, allowlisted methods, response digests, and timestamp. GraphQL errors, wrong endpoints, malformed identifiers, and incomplete responses fail closed. Never publish, register, bind, schedule, settle, or submit a transaction from this read-only path.

## API application boundaries

Keep the provider key in server-only runtime configuration and use a same-origin relay. Validate deployment/network identity instead of inferring it from a shared example URL. Execution history uses the execution-scoped paginated route; SSE uses `events:read` separately from `executions:read`. Validate nonnegative safe event IDs, transport/payload agreement, strict monotonic replay, opaque cursors including zero, bounded retry/`Retry-After`, and explicit 401/403 recovery. Treat SSE and projection rows as read evidence, not transaction finality, deletion, settlement, or permission to resubmit.

## Operating contract

1. Record package, DAG, skill artifact, source references, and verification target before editing.
2. Keep Tool/TAP interfaces tied to prepared public Move-package and SDK references; use repository-owned fixtures and validators.
3. Classify each command as read-only inspection, local build/test, beta published-bytecode VM test, structural validation, or shared-network mutation before execution.
4. State explicitly whether evidence is structural, repository-owned, read-only Testnet, API projection, or live transaction evidence. Do not present one category as another.

## Talus Vision links

After exact read-only evidence returns an identifier, you may add a navigation link with the bundle-root `scripts/vision_links.py` helper, for example `python3 "$SKILLS_BUNDLE_ROOT/scripts/vision_links.py" --network testnet --kind agent --id 0x<agent-id>`. Keep the explicit `?network=` query it emits; without it Vision falls back to Mainnet or the viewer's last-used network. Link only full identifiers returned by the verifying read, on that read's network (Testnet for the bundled helper). Never link placeholders, shortened, synthetic, or unread IDs, devnet/localnet IDs, or a Testnet ID on Mainnet. List links separately under "View on Talus Vision". A Vision page is an indexed projection for navigation, not evidence: it never proves registration, execution, or settlement. Useful kinds here are `agent` for the TAP, `skill` with `--skill-index`, `workflow` for the DAG, and `execution`; an API application may render the same network-explicit routes.

## Completion standard

A TAP application is locally ready when relay tests, relevant frontend typecheck/build, replay/pagination cases, and synthetic-key checks pass. A TAP package is locally ready when package tests, direct/delayed fixture paths, dependency closure, artifact validator, and applicable beta VM gate pass. Report live API, wallet, registration, execution, and settlement evidence separately.
