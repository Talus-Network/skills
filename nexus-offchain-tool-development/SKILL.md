---
name: nexus-offchain-tool-development
description: Build, test, and verify standalone Rust HTTP Tools for Nexus external-provider workflows, including bounded provider I/O, stable schemas, secret-free metadata, signed HTTP v3, replay protection, and registration preparation. Use for off-chain Tool services; route Move Tools, TAP applications, payment, or diagnosis elsewhere.
---

# Nexus off-chain Tool development

Use this skill for the Rust HTTP service that Nexus Leader invokes during a workflow. It covers Tool contract and schema/FQN design, external dependency seams, deterministic tests, Toolkit metadata, signed HTTP, and safe registration preparation. On-chain state or asset mutation belongs in [on-chain Tool development](https://docs.talus.network/guides/tool-development/build-onchain-tool); an existing TAP UI/API belongs in [API application development](https://docs.talus.network/guides/nexus-api).

## Task contract before setup

Before setup, source preparation, build, test, metadata generation, server launch, file writes, or shared-network actions, assemble a compact task contract from the supplied instructions, existing project and conversation context, approved public source facts, and reasonable reversible assumptions. Capture the goal and outcome, observable requirements, inputs and integration boundary, in-scope crate/module/file scope, non-goals, authorization and network/write boundary, acceptance evidence, and a compact implementation design.

Route the request before setup and read only the task-relevant references. Ask one concise question only when an unresolved material behavior, authority, or acceptance decision changes the work. Pause dependent work while that answer is pending, and continue useful authorized independent reads or checks. Resolve facts answerable from approved public Docs, SDK, Move Packages, Sui, or read-only Testnet sources without turning them into user questions. When the contract is sufficient, proceed; there is no default grilling ritual or required phrase.

## Route before setup

| Request | Route |
| --- | --- |
| Rust HTTP Tool with external provider I/O, metadata, or signed HTTP | Continue here. |
| Sui Move Tool with `execute`, witness/result, or on-chain state | Use [on-chain Tool development](https://docs.talus.network/guides/tool-development/build-onchain-tool). |
| TAP package/artifacts or an existing TAP REST/SSE application | Use [TAP package development](https://docs.talus.network/guides/tap-development/build-tap-move-package) or [API application development](https://docs.talus.network/guides/nexus-api). |
| Read-only Execution diagnosis or payment reconciliation | Use [on-chain Task debugging](https://docs.talus.network/guides/agent-usage/execute-and-settle-agent) or [CLI payment tracking](https://docs.talus.network/guides/tokenomics/fund-agent-and-user-executions). |

For version-sensitive setup, use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup) and `scripts/docs_website.py`; public repositories are source authorities, not private checkouts.

## Public source and Testnet evidence

Choose references by the requested work: read [implementation](references/implementation.md) for the Tool contract, provider seams, schemas, and output variants; read [verification](references/verification.md) for metadata, signed HTTP, endpoint, or registration checks; and read [scaffolding](references/scaffolding.md) for a new Tool skeleton or structural test harness. Read [the source-preparation contract](references/source-preparation.md) only when a version-sensitive SDK, Move, or Sui source question remains after approved public Docs and read-only evidence; then use `scripts/prepare_sources.py` from a fresh consumer workspace for only the reviewed public SDK, Move Packages, and matching Sui framework archives. Preserve manifest cleanup and stop at an evidence gap.

For deployed package, module, or object facts, use the bundle-root `scripts/testnet_evidence.py` helper with the explicit official Testnet GraphQL endpoint:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Public state does not prove provider invocation, signed-result acceptance, registration, or payment settlement.

## Safety contract

- Classify commands as read-only inspection, local build/test, local server operation, or shared-network mutation. Default to an isolated project, deterministic mocks, bounded timeouts, and dry-run/read-only inspection.
- Keep private keys, API tokens, signing material, and provider credentials in runtime configuration or a secret manager. Never put them in source, fixtures, logs, metadata, browser output, or shell arguments.
- Validate URL scheme/host, timeout, response size, status, content type, JSON shape, required output fields, and error behavior before accepting provider data.
- Test success, provider failure, malformed payload, timeout, schema mismatch, signature/header tampering, and replay. Local `http://` validation is for development; production requires an operator-supplied HTTPS endpoint.

## Metadata and invocation evidence

Read [the scaffolding reference](references/scaffolding.md) before reviewing an existing Tool or changing its output. Every output variant, including a zero-port error variant, uses externally tagged named fields; a unit variant may pass metadata `oneOf` while failing canonical BCS invocation encoding. Write `cargo run -- --meta` output to a temporary file outside the project, extract the single metadata object, and validate the selected Tool's full URL including its `path()` suffix. Metadata/schema validation is separate from actual `/invoke` execution and canonical BCS decoding; exercise representative success/error outputs in local tests. `--meta` must not require provider configuration, credentials, or network I/O, while service startup still validates required runtime configuration.

## Signed HTTP and registration boundary

Signed HTTP v3 authenticates the canonical schema-ordered input commitment and canonical response bytes with a deterministic invocation nonce; it does not replace HTTPS. Test missing/tampered/wrong signatures and replay locally with the current Toolkit contract. Registration is a shared-network mutation: verify exact FQN, schema, final endpoint/path, beneficiary, collateral, gas, signer/capability custody, authorization, and authoritative post-state before and after an explicitly authorized operation. A successful command, digest, or metadata object alone is not registration proof.

## Completion standard

A Tool is locally ready when locked build/test/lint/format checks pass, every output variant and health/metadata/invocation path has deterministic coverage, metadata is secret-free, the selected custom path validates, and any signed HTTP, registration, or staging evidence is stated separately as run, unavailable, or pending.
