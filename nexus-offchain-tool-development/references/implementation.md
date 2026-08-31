# Off-chain Tool implementation

## Contract first

Define the Tool FQN, version, description, input schema, output schema, error variants, timeout, and beneficiary before writing the HTTP handler. Keep the response deterministic for the same validated request and make provider failures explicit in the schema.

Use the public SDK repository prepared by the parent skill for version-sensitive names and rely on the bundled guidance for procedures. Keep generated metadata and source paths repository-relative. The bundle's `scripts/testnet_evidence.py` is for read-only deployed-state observations; it is not a substitute for local Rust tests.

## Rust boundary

Keep provider I/O behind a small trait or adapter so unit tests can use a deterministic mock. Validate input before I/O, bound request and response sizes, classify transport/status/JSON/schema failures, and return one documented error shape. Do not log credentials or full provider payloads when they may contain sensitive data.

## Signed HTTP

Verify the exact current SDK contract for signed headers. Bind the signature to method, path, body digest, timestamp, nonce, and the configured key identity. Reject stale timestamps, duplicate nonces, altered bodies, wrong paths, missing headers, and unknown key IDs. Tests should use generated ephemeral test keys and never persist private material.

## Tests

Cover valid input, provider success, provider timeout, provider HTTP failure, malformed JSON, wrong output type, oversized response, signature tampering, replay, and an unavailable testnet evidence endpoint. Keep tests hermetic and leave any live testnet read as an explicit separately-run evidence command.
