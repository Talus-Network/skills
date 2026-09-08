# Scaffold and finish a Rust HTTP Tool

Use this reference when creating a Tool from a description or adding one to an existing Cargo project. It carries the complete scaffold-to-implementation workflow; a placeholder service is not a finished Tool. Follow the published [off-chain Tool guide](https://docs.talus.network/guides/tool-development/build-offchain-tool), [Toolkit reference](https://docs.talus.network/reference/toolkit/rust), and [CLI reference](https://docs.talus.network/reference/cli/tool) for the selected release.

## Resolve placement and names

Inspect only the user's chosen project. Parse Cargo manifests and enumerate actual crate directories; a Just recipe list is not a reliable crate inventory. If an existing Cargo workspace contains the requested Tool directory, follow its member layout and inherited dependencies. Otherwise create a standalone crate under the authorized destination. Do not nest an accidental workspace, overwrite existing files, or acquire another repository to infer conventions.

Infer names and schemas from the supplied behavior and existing project conventions. Reuse a supplied namespace. For an isolated example with no namespace, use a clearly marked `com.example` placeholder and require a real namespace before registration. Ask only when unresolved placement, namespace, provider behavior, or admission policy changes the contract; an automatic mode never authorizes overwriting or network mutations.

An FQN has at least three dot-separated parts, every part matching `[a-z][a-z0-9_-]+`, followed by a positive integer version such as `com.example.weather.forecast@1`. Each part has at least two characters. Use kebab-case crate names, Rust snake_case module names, and the project's existing FQN action convention. A named provider/action should be recognizable; keep the HTTP `path()` suffix and metadata URL consistent. A multi-Tool crate may share a namespace, so check whether the request belongs in that crate before creating a competing sibling.

## Generate from the released CLI

After the parent skill's contract and published Setup checks, use the selected released CLI scaffold and inspect its output:

```bash
nexus tool new --name weather-forecast --description "Return a weather forecast" --template rust --target ./
```

Adapt `Cargo.toml` and `src/main.rs`; split the implementation into a module when that helps maintain it. Add a README containing the FQN, purpose, input ports/types, every output variant/port, configuration variable names, and local validation instructions. Use the dependency versions prescribed by the published Setup/Tool guide, align SDK and Toolkit versions, and preserve or generate the reviewed `Cargo.lock` after dependency edits. In a workspace inherit fields only when the parent declares them. Do not substitute an unpinned upstream template for the selected release.

Use existing project automation only when it is part of the requested integration. If the project has a Tool manifest/build script/version convention, verify that its command, binary target, crate name, and FQN version agree; add the required workspace build/check/test/format entries. Do not invent `tools.json`, `build.rs`, `TOOL_FQN_VERSION`, or deployment wiring for a standalone crate. Source acquisition remains the parent skill's approved public-source flow.

## Replace the scaffold with the real contract

- Derive input ports from documented provider operations. Check the provider's public request/response contract instead of guessing from its name. Keep independently defaultable DAG inputs separate, reject unsupported input fields where appropriate, and validate input before I/O.
- Keep credentials in server runtime configuration. Input ports can become permanently visible on-chain; never accept provider keys or tokens as Tool inputs. Validate required configuration at startup, retain it in application state or an appropriate cache, and avoid repeated environment reads inside `invoke`.
- `--meta` must work without provider secrets, network calls, or a server listener. Perform runtime-only configuration validation after distinguishing the metadata path. Initialize logging before reporting startup errors, and record variable names without values.
- Match the selected `NexusTool` trait: `new` constructs the service, `fqn` identifies it, `path` determines its mount point, `description` is nonempty, `health` returns readiness, and `invoke` returns the associated Output rather than a `Result`. Override timeout deliberately and keep provider I/O deadlines below the Tool timeout. Use an injected client/base URL for hermetic tests.
- Use the optional `authorize(AuthContext)` hook only for a deliberate admission policy. It runs after signed HTTP authentication; its rejection is a local HTTP error, not signed result evidence. An allowlist or rate limit should be explicit and testable.
- Output uses an externally tagged enum whose variants all carry named fields, including an empty struct variant for zero ports. Unit/tuple variants and internal/untagged/flattened serde layouts can compile yet fail canonical output encoding. Keep ports flat and use explicit `err_*` variants for expected failures rather than optional crucial fields or success-shaped error payloads.
- Return one schema-declared output per invocation. Distinguish invalid input, timeout, transport/status, malformed response, and missing data when those are part of the contract. Keep the SDK-generated output schema consistent with the actual variants and ports.

Signed HTTP follows the v3 input commitment/canonical BCS contract in [verification](verification.md). Do not implement a separate generic method/path/timestamp signing scheme.

## Prove the running Tool

Run the project's locked check/test/lint/format commands from its actual Cargo workspace. Cover the real construction path, health readiness, every reachable output variant, provider failures, timeouts, malformed/oversized responses, and missing configuration with deterministic local mocks. A startup test should provide synthetic values for exactly the configuration variables that startup requires and bind to an available loopback port.

Run `--meta` without provider credentials and validate the JSON array and generated schemas. Start the server on loopback, then run `nexus tool validate offchain --url <loopback-tool-base-url>` with the Tool's `path()` suffix included. A CLI validation pass proves health/metadata; it does not by itself execute and decode every output branch. Separately invoke representative success/error cases and decode successful response bytes using the selected SDK's canonical BCS output decoder. `POST /invoke` accepts JSON but a successful response is canonical BCS; do not pipe it into `jq`. Local HTTP error bodies are a separate shape.

If a smoke runner is useful, implement it in the consumer project: start only its own process, retain that process handle, wait for bounded readiness, report early exit, and clean up its own listener/log resources. Do not kill unrelated listeners or treat a health-only fallback as full CLI validation. No runner is required for a simple test suite.

## Completion checklist

- [ ] The real Input, Output, provider behavior, FQN, description, timeout, and path are implemented; scaffold TODOs and fake success paths are gone.
- [ ] SDK/Toolkit dependencies and lockfile match the selected release, and conditional workspace integration preserves the project's conventions.
- [ ] Metadata extraction needs no runtime secret; application inputs, metadata, fixtures, and logs contain none.
- [ ] Every reachable output has a local test; HTTP invocation proves canonical output encoding, not just metadata schema generation.
- [ ] Local build/test/lint/format, startup, metadata, and CLI validation results are recorded with their limits.
- [ ] Signed HTTP mode and admission policy are deliberate and tested where in scope. Registration and live provider access are reported separately.

Continue to [verification and registration](verification.md) for exact metadata normalization, signed HTTP configuration, collateral/capability handling, and authorized registration. A schema-breaking change needs a new FQN version; leave existing consumers pinned to their original contract.
