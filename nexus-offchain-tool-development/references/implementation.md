# Off-chain Tool implementation

For a new crate or workspace member, start with [scaffolding and its completion checklist](scaffolding.md), then use this reference for the implementation boundary.

## Contract first

Define the Tool FQN, version, description, input schema, output schema, error variants, timeout, and beneficiary before writing the HTTP handler. Keep the response deterministic for the same validated request and make provider failures explicit in the schema.

Use the public SDK repository prepared by the parent skill for version-sensitive names and rely on the bundled guidance for procedures. Keep generated metadata and source paths repository-relative. The bundle's `scripts/testnet_evidence.py` is for read-only deployed-state observations; it is not a substitute for local Rust tests.

## Rust boundary

Keep provider I/O behind a small trait or adapter so unit tests can use a deterministic mock. Validate input before I/O, bound request and response sizes, classify transport/status/JSON/schema failures, and return one documented error shape. Do not log credentials or full provider payloads when they may contain sensitive data.

## Signed HTTP

Use the v3 contract in [verification](verification.md) and the published [Tool communication guide](https://docs.talus.network/guides/tool-development/tool-communication). The Leader signs the canonical input commitment; the Tool response signature binds that Leader signature, deterministic invocation nonce, and SHA-256 digest of canonical response BCS. Do not substitute generic method/path/timestamp signing. Cover altered commitments/signatures, unknown Leader keys, in-flight duplicates, and completed-response replay using the selected SDK's tests. Use synthetic local test keys and keep real signing material out of source and logs.

## Tests

Cover valid input, provider success, provider timeout, provider HTTP failure, malformed JSON, wrong output type, oversized response, signature tampering, replay, and an unavailable testnet evidence endpoint. Keep tests hermetic and leave any live testnet read as an explicit separately-run evidence command.

## Explicit outputs and wallet-funded Walrus storage

SDK/Toolkit 2.1.1 adds `NexusTool::encode_output` for returning explicit protocol ports with `OffchainToolOutput::from_ports`. Ordinary outputs keep their inline JSON encoding. A Tool that needs large outputs must upload them itself before returning; the Toolkit and Leader do not perform or fund that upload automatically. Use `nexus_walrus::WalrusStorage` with `client.wallet()?.clone()`, then place `StoredBlob::nexus_data()` in the declared output port. Match metadata port order and describe the decoded value in the schema, for example `#[schemars(with = "String")]` for a string.

The `nexus-walrus` adapter is distributed from the public SDK Git tag `v2.1.1`, not crates.io, and requires a C++ compiler and libclang. SDK and Toolkit 2.1.1 are published on crates.io; use those registry packages with the Git-distributed adapter. No SDK Git override or `[patch.crates-io]` is needed. Follow the published Setup dependency instructions and review the consumer lockfile after changes.

The shared wallet pays WAL for storage and SUI for gas on its connected Sui network and owns the Blob. Review `max_storage_cost_frost` as a per-blob estimate ceiling, not a guaranteed final charge or batch limit, and `gas_budget_mist` as a per-transaction SUI budget. Persist `UploadRegistration` before submission and `PendingUpload` before completing upload; retry the saved signed registration after uncertain submission rather than buying storage again. Choose retention and the Tool timeout to cover upload, certification, verified readback, and every later use. Extend owned storage before expiry; deletion requires explicitly deletable storage. Walrus data is public, not encrypted.

Use the SDK's `execution_limits` constants: `MAX_RESOLVED_DATA_BYTES` is 8 MiB for the whole input set and separately for the whole output set of one invocation; `MAX_INVOKE_BODY_BYTES` is the 12 MiB default HTTP request limit. Every inline/downloaded byte, every `Many` item, and 32 bytes per object ID count. Explicit HTTP limits remain effective but cannot raise the resolved budget. Move inline and encoded-port limits remain unchanged. Large or formatting-sensitive JSON travels as exact base64 `bytes` so the signed digest survives decoding; upgrade receiving runtimes before using that form.

Test reference integrity, explicit output order/schema validation, budget boundaries, and interrupted upload recovery separately from live payment. With unwind panics, the runtime contains Tool construction, authorization, invocation, decoding, and encoding panics as sanitized unsigned local errors. Invocation timeout returns an unsigned HTTP 504. Neither is a successful Tool result; `panic = "abort"` still terminates the process.
