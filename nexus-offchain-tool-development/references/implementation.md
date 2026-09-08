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
