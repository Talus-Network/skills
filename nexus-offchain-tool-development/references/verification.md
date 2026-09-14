# Validate and prepare an off-chain Tool

Read this reference after implementation checks pass or when the request involves metadata, signed HTTP, endpoint operation, registration, or a verification failure. Current source authority is the helper-resolved public SDK root's `cli/src/tool/{tool_validate,tool_register_offchain,tool_auth}.rs` and `toolkit-rust/src/{runtime,config,signed_http_warp}.rs`, together with the repository-owned procedures in this bundle.

## 1. Classify the side effect

| Operation | Default treatment |
| --- | --- |
| `cargo check/test/clippy/fmt`, `cargo run -- --meta`, local mock server | Local build or inspection; safe in an isolated project. |
| `nexus tool validate offchain --url ...` | Read/health request to the supplied endpoint; use loopback or explicitly authorized staging. |
| `nexus tool inspect`, `nexus tool list`, `nexus tool auth list-keys` | Read-only network inspection; verify configuration and network provenance first. |
| `nexus tool register offchain`, `tool auth register-key`, `tool configure-verifier`, deposits, claims, unregister, updates, or cashier collection | Shared-network mutation; stop for explicit authorization and run the mutation preflight immediately before it. |

Never use a localhost URL as a production registration target. A request to build, validate, or debug does not authorize registration, key publication, collateral lock, or payment movement.

## 2. Inspect metadata without mutating the network

The Toolkit's `--meta` branch prints a pretty JSON array, including for a single Tool, and its URL is a `http://localhost` placeholder derived from `path()`. The CLI's `--from-meta` parser currently reads one metadata object, not that array. For one Tool, normalize the output into a temporary reviewed directory outside the project. Keep this block in a subshell so its cleanup traps do not replace a parent source-preparation trap; set `TOOL_MANIFEST` to the Tool project's `Cargo.toml`:

```bash
(
  set -eu
  if [ -z "${TOOL_MANIFEST:-}" ]; then
    printf 'TOOL_MANIFEST must name the Tool project Cargo.toml\n' >&2
    exit 1
  fi
  manifest_dir="$(dirname -- "$TOOL_MANIFEST")"
  manifest_file="$(basename -- "$TOOL_MANIFEST")"
  if ! manifest_dir="$(cd -- "$manifest_dir" && pwd -P)"; then
    printf 'TOOL_MANIFEST directory cannot be resolved\n' >&2
    exit 1
  fi
  TOOL_MANIFEST="$manifest_dir/$manifest_file"
  if [ ! -f "$TOOL_MANIFEST" ]; then
    printf 'TOOL_MANIFEST does not name a file: %s\n' "$TOOL_MANIFEST" >&2
    exit 1
  fi
  metadata_dir=""
  if ! metadata_dir="$(mktemp -d "${TMPDIR:-/tmp}/nexus-tool-meta.XXXXXX")" \
      || [ -z "$metadata_dir" ] || [ ! -d "$metadata_dir" ]; then
    printf 'metadata temporary directory creation failed\n' >&2
    exit 1
  fi
  cleanup_metadata() {
    status="$?"
    trap - EXIT INT TERM
    cleanup_status=0
    rm -rf -- "$metadata_dir" || cleanup_status="$?"
    if [ "$cleanup_status" -ne 0 ]; then
      printf 'metadata cleanup failed (status %s)\n' "$cleanup_status" >&2
      [ "$status" -eq 0 ] && status="$cleanup_status"
    fi
    exit "$status"
  }
  trap cleanup_metadata EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  cd -- "$metadata_dir" || exit 1
  cargo run --manifest-path "$TOOL_MANIFEST" -- --meta > meta-array.json
  jq -e 'type == "array" and length == 1' meta-array.json >/dev/null
  jq -e '.[0]' meta-array.json > meta.json
  jq -e 'has("fqn") and has("url") and has("description") and has("timeout") and has("input_schema") and has("output_schema")' meta.json >/dev/null
)
```

The block resolves `TOOL_MANIFEST` against the project working directory and validates the file before `cd` changes into `metadata_dir`, so both relative and absolute manifest inputs remain valid. The `metadata_dir` owns both generated files for the block's lifetime and is removed on normal completion or interruption; it does not affect the parent source-cleanup trap. For multiple Tools, keep the array for discovery and use the current `--batch` live endpoint flow or split reviewed objects according to the selected CLI version; do not guess a batch file format. Check that each object has `fqn`, `url`, `description`, `timeout`, `input_schema`, and `output_schema`, and that the output schema has a non-null top-level `oneOf`. Never hand-edit JSON Schema to paper over an implementation mismatch; change the Rust types and regenerate metadata.

Assume the selected Tool's `path()` returns `/weather`. Start the server in the foreground from the project terminal, then run validation from a second terminal so the server remains available:

```bash
BIND_ADDR=127.0.0.1:8080 cargo run --manifest-path "$TOOL_MANIFEST"
# second terminal, while the foreground server is running
nexus tool validate offchain --url http://127.0.0.1:8080/weather
```

Current validation requests `/health` and requires `200 OK`, fetches `/meta`, parses the FQN/URL/description/timeout/schemas, and rejects metadata without top-level `oneOf`. Independently review the final URL syntax and non-empty description before registration because live endpoint validation and registration metadata checks are separate paths. The custom Tool `path()` is part of the URL, so validation must target `/weather` here and the Tool's actual suffix in other projects; the `/health` and `/meta` routes are served below that base path. A successful local validation proves health and metadata compatibility; it does not exercise `POST /invoke` or prove canonical output encoding. Invoke and decode representative success/error outputs in the local tests described in [scaffolding](scaffolding.md).

## 3. Configure signed HTTP v3 deliberately

Signed HTTP v3 authenticates the canonical schema-ordered input commitment and canonical response bytes; it does not replace HTTPS. The Leader signs the 32-byte input hash. The Tool signs a domain-separated message containing the Leader signature, deterministic invocation nonce, and the SHA-256 of canonical response BCS. The current request headers are `X-Nexus-Sig-V`, `X-Nexus-Leader-Id`, `X-Nexus-Leader-Key-Id`, `X-Nexus-Input-Hash`, `X-Nexus-Leader-Signature`, and `X-Nexus-Nonce`; the response carries `X-Nexus-Sig-V` and, when configured, `X-Nexus-Tool-Signature`.

The current Toolkit configuration is JSON with no top-level `version` field:

```json
{
  "invoke_max_body_bytes": 10485760,
  "signed_http": {
    "mode": "required",
    "allowed_leaders_path": "<runtime-config-dir>/allowed-leaders.json",
    "tools": {
      "xyz.taluslabs.exchanges.coinbase.spot-price@1": {
        "response_signing_key": "<32-byte-ed25519-private-key>",
        "replay_cache_ttl_ms": 300000
      }
    }
  }
}
```

Current `SignedHttpMode` values are only `disabled` and `required`; an omitted `signed_http` section disables signed HTTP, while a present section defaults to required. Required mode needs one inline `allowed_leaders` object or one `allowed_leaders_path`, a nonempty `tools` map, and a positive replay TTL. `response_signing_key` is optional for request-verification-only service but required for the RegisteredKey result path. Do not add a top-level version, `tool_signing_key`, Tool KID field, method/path/query/body/time-window/status claims, or an external replay-store field from stale examples.

Export the active Leader allowlist through the read-only CLI path, using a temporary or operator-managed destination:

```bash
nexus tool auth export-allowed-leaders --all --out <allowed-leaders.json>
nexus tool auth sync-allowed-leaders --out <allowed-leaders.json> --interval 30s --once
```

Use repeated `--leader <LEADER_CAP_ID>` instead of `--all` only when the operator has supplied exact capability IDs. The allowlist file is version `1` and is resolved locally by the Toolkit; refresh it after Leader key rotation. For a real Tool key, set a restrictive umask, generate and register only under explicit authorization, keep the private key in a protected secret manager, never print or commit it, and retain only public key/transaction evidence:

```bash
umask 077
nexus tool auth keygen --out <protected-temp-key.json>
chmod 600 <protected-temp-key.json>
nexus tool auth register-key --tool-fqn <FQN> --signing-key <protected-key-file> --skip-if-active
nexus tool auth list-keys --tool-fqn <FQN>
```

The final Tool URL must be HTTPS with a trusted certificate, and a gateway must preserve every v3 request and response header. Signed HTTP does not make a plaintext gateway-to-Tool hop safe. The replay cache is process-local and keyed by deterministic nonce plus canonical input hash; an in-flight duplicate returns `409`, an exact completed retry can return cached bytes, and completed entries expire by TTL. Multi-instance routing needs a separately reviewed shared-state or affinity design.

## 4. Prepare or perform registration

The live registration path validates the endpoint and then submits a transaction:

```bash
nexus tool register offchain --url <final-https-url> --invocation-cost <MIST>
```

The metadata path skips live HTTP validation and is appropriate only when the reviewed metadata object describes the exact final HTTPS endpoint. Since the extracted `--meta` object normally contains the localhost placeholder, pass `--url <final-https-url>` as an explicit override and review the resulting URL before signing:

```bash
nexus tool register offchain --from-meta <meta.json> --url <final-https-url> --invocation-cost <MIST>
```

Both commands mutate the Tool registry. Before running either, verify the selected Nexus environment/config, signer SUI address and gas, exact FQN and endpoint, an owned `Coin<US>` with sufficient registration collateral, and the user's authorization. `--collateral-coin <OBJECT_ID>` selects the explicit owned collateral; SUI gas is separate. `--no-save` avoids persisting returned owner capabilities in local CLI config. `--batch` discovers all tools from a live `/tools` endpoint and is incompatible with `--from-meta`.

Read back the result by FQN after an authorized transaction:

```bash
nexus tool inspect --tool-fqn <FQN> --json
nexus tool list
```

Registration returns a Tool plus separate `CloneableOwnerCap<OverTool>` and `CloneableOwnerCap<OverToolCashier>` capabilities. Preserve the transaction digest, Tool ID, FQN, endpoint, capability custody, collateral state, and any ToolCashier policy evidence. `nexus tool claim-collateral` is a delayed collateral recovery path after unregister/lock conditions; it is not invocation-earnings collection or SUI gas recovery.

## 5. Prove a signed-result path safely

When a Tool is registered with the appropriate active key and the Tool owner separately enables RegisteredKey verifier support, configure the DAG's off-chain vertex with `"verifier": "registered_key"` and run `nexus dag validate --path <dag.json>`. This parser check does not prove the Tool key, Leader key, or endpoint works.

In a local required-mode test, an unsigned `/invoke` must fail before input decoding, normally with `401` and an `auth_failed` JSON error. Exercise tampered/missing/wrong signatures with the current SDK v3 tests or a local fixture, never against production traffic. For an accepted staging workflow, retain Tool/key/Task/Occurrence/Execution readbacks, FQN/Tool ID, active key IDs, input hash, nonce context, canonical result evidence, transaction digest, and verifier decision. Local Leader logs are not an on-chain verdict.

## 6. Version and diagnose

Change the FQN version when adding/removing/renaming input ports, output variants or ports, or changing their types. Keep the old FQN for existing DAGs and register the new one after its metadata and tests pass. An implementation-only fix can retain the FQN only when the schema and observable contract remain compatible.

| Symptom | Safe diagnosis |
| --- | --- |
| Validation rejects metadata | Check `/health` is exactly `200`, `/meta` is valid JSON, URL/FQN/description are non-empty, and output schema has top-level `oneOf`. |
| `--from-meta` parse fails on `cargo run -- --meta` output | Extract one object from the Toolkit's array with `jq '.[0]'`; do not register the array as one Tool. |
| Metadata still points at localhost | `--meta` intentionally uses a placeholder URL; pass `--url <final-https-url>` with `--from-meta` and review the override before any authorized transaction. |
| FQN or Tool not found | Compare the exact `fqn!` value, version, registered record, and DAG reference; do not infer an ID from a different deployment. |
| `401 auth_failed` | Check v3 version/header preservation, canonical input hash, Leader allowlist, active Leader key ID, and HTTPS/proxy behavior. |
| Missing Tool signature | Check required config, exact FQN map entry, response signing key, gateway response headers, and canonical BCS response; a local JSON error is not signed result evidence. |
| `409 request_in_flight` | The deterministic nonce is still executing; let the first request finish and retry according to Leader policy. |
| Registration rejected | Re-check network/object bundle, valid final HTTPS URL, metadata, SUI gas, owned `Coin<US>` collateral, and authorization; do not substitute address-balance SUI for collateral. |
| Accepted result remains pending | Separate result verification from payment, ToolCashier, occurrence, and settlement lifecycle; use the payment/debugging capability for that diagnosis. |

## Completion evidence

A local implementation is ready for handoff when locked build/test/lint/format checks pass, deterministic tests cover every output variant and health behavior, `--meta` parses, `nexus tool validate offchain` succeeds against the intended local/staging endpoint, and the report states whether signed HTTP, registration, and staging workflow proof were intentionally not run. A production registration is complete only with explicit authorization, a successful transaction, and matching on-chain readback; never mark it complete from metadata validation alone.
