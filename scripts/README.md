# Skills source and evidence helpers

## Public source preparation

`prepare_sources.py` creates a disposable manifest-backed workspace and downloads only three reviewed anonymous public GitHub archives over HTTPS: `Talus-Network/nexus-sdk`, `Talus-Network/nexus-move-packages`, and `MystenLabs/sui` at `d8459684b41eb09ab23fe16a9dd84173270bbaba`, the source revision for installed `sui 1.78.0-d8459684b41e`. Archive identity, exact refs/checksums, required paths, extracted-tree digests, and cleanup ownership are recorded in the manifest. The helper never searches the host filesystem for a source tree.

```bash
SOURCE_MANIFEST=""
cleanup_sources() {
  status="${1:-$?}"
  trap - EXIT INT TERM
  cleanup_status=0
  if [ -n "${SOURCE_MANIFEST:-}" ]; then
    python3 <skills-bundle>/scripts/prepare_sources.py cleanup --manifest "$SOURCE_MANIFEST" || cleanup_status=$?
  fi
  if [ "$cleanup_status" -ne 0 ]; then
    printf 'source cleanup failed (status %s)\n' "$cleanup_status" >&2
    if [ "$status" -eq 0 ]; then
      status="$cleanup_status"
    fi
  fi
  exit "$status"
}
trap cleanup_sources EXIT
trap 'cleanup_sources 130' INT
trap 'cleanup_sources 143' TERM
source_prepare_status=0
SOURCE_MANIFEST=""
SOURCE_MANIFEST="$(python3 <skills-bundle>/scripts/prepare_sources.py prepare \
  --only nexus-sdk --only nexus-move-packages --only sui --print-manifest-path)" || source_prepare_status=$?
if [ "$source_prepare_status" -ne 0 ] || [ -z "$SOURCE_MANIFEST" ]; then
  SOURCE_MANIFEST=""
  if [ "$source_prepare_status" -eq 0 ]; then source_prepare_status=1; fi
  printf 'source preparation failed (status %s)\n' "$source_prepare_status" >&2
  exit "${source_prepare_status:-1}"
fi
SDK_ROOT=""
sdk_root_status=0
SDK_ROOT="$(python3 <skills-bundle>/scripts/prepare_sources.py root \
  --manifest "$SOURCE_MANIFEST" --repo nexus-sdk)" || sdk_root_status=$?
if [ "$sdk_root_status" -ne 0 ] || [ -z "$SDK_ROOT" ]; then
  SDK_ROOT=""
  if [ "$sdk_root_status" -eq 0 ]; then sdk_root_status=1; fi
  printf 'nexus-sdk root resolution failed (status %s)\n' "$sdk_root_status" >&2
  exit "${sdk_root_status:-1}"
fi
MOVE_PACKAGES_ROOT=""
move_packages_root_status=0
MOVE_PACKAGES_ROOT="$(python3 <skills-bundle>/scripts/prepare_sources.py root \
  --manifest "$SOURCE_MANIFEST" --repo nexus-move-packages)" || move_packages_root_status=$?
if [ "$move_packages_root_status" -ne 0 ] || [ -z "$MOVE_PACKAGES_ROOT" ]; then
  MOVE_PACKAGES_ROOT=""
  if [ "$move_packages_root_status" -eq 0 ]; then move_packages_root_status=1; fi
  printf 'nexus-move-packages root resolution failed (status %s)\n' "$move_packages_root_status" >&2
  exit "${move_packages_root_status:-1}"
fi
SUI_ROOT=""
sui_root_status=0
SUI_ROOT="$(python3 <skills-bundle>/scripts/prepare_sources.py root \
  --manifest "$SOURCE_MANIFEST" --repo sui)" || sui_root_status=$?
if [ "$sui_root_status" -ne 0 ] || [ -z "$SUI_ROOT" ]; then
  SUI_ROOT=""
  if [ "$sui_root_status" -eq 0 ]; then sui_root_status=1; fi
  printf 'sui root resolution failed (status %s)\n' "$sui_root_status" >&2
  exit "${sui_root_status:-1}"
fi
```

Production preparation is pinned to the three reviewed refs and checksums; synthetic archive injection is confined to private test seams. The helper rejects unknown selectors, changed refs/checksums, non-HTTPS source archives, repository identity mismatches, unsafe archive members, and changed authenticated trees. Append only repository-relative paths to a verified root. Native Move consumers may copy only the two Sui framework package directories below `SUI_ROOT`; never copy the broader Sui repository into a generated consumer. Copy a verified root into a separate disposable build directory before running compilers.

The Move package archive is the public interface/dependency authority. Its direct package paths are `packages/primitives`, `packages/interface`, `packages/tool`, `packages/registry`, `packages/workflow`, and `packages/scheduler`; `packages/kernel` is retained only when required by that closure and is not a direct application dependency. Native declarations are compile-time interfaces, not executable protocol implementations.

## Public Move Registry package map

`mvr_packages.json` is the reviewed installation map for the six published public packages: `@talus/nexus-interface`, `@talus/nexus-primitives`, `@talus/nexus-registry`, `@talus/nexus-tool`, `@talus/nexus-scheduler`, and `@talus/nexus-workflow`. Their public package pages use the `www.moveregistry.com/package/@talus/<name>` pattern; network-facing `Move.toml` files should use the corresponding `r.mvr` names and commit `Move.lock`. The map records Testnet/Mainnet version/address and source-tag evidence; `@talus/nexus-policy` and `@talus/nexus-kernel` are explicitly not registered records.

Validate the map without network access, or collect fresh anonymous page/API evidence for both networks:

```bash
python3 -B <skills-bundle>/scripts/mvr_registry.py
python3 -B <skills-bundle>/scripts/mvr_registry.py --live
```

The live command only reads public MVR HTTPS endpoints and rejects a generic successful page unless the matching API response proves the package name, version, address, public source path, and network tag. Cross-check Testnet package addresses with the read-only Sui GraphQL helper below; neither helper installs, publishes, registers, signs, or mutates.

## Published Setup authority

The canonical live authority for version-sensitive setup is the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup). Check it anonymously with `python3 <skills-bundle>/scripts/docs_website.py --expected-version v2.0.0` before following a release-specific command. The checker accepts only the canonical HTTPS destination, validates the final URL, parses the release sentence, and never installs packages, switches a Sui environment, or mutates state.

## Read-only Sui testnet evidence

`testnet_evidence.py` is the only online evidence helper shipped by this bundle. It requires the explicit official HTTPS Sui testnet GraphQL endpoint and permits only the bounded read set: chain identity, latest checkpoint, package/object reads, and normalized Move module/function/struct reads. It does not read environment configuration or wallet files and has no transaction, signing, publish, registration, scheduling, or balance-mutation path.

```bash
python3 <skills-bundle>/scripts/testnet_evidence.py \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

The JSON report includes the explicit endpoint, `testnet` network label, the returned official testnet chain identifier, observations, allowlisted queries, response digests, collection time, and a canonical report digest. A wrong chain identifier, null/missing/empty/incomplete requested observation, timeout, HTTP/GraphQL error, malformed JSON, unsupported query, invalid identifier, non-testnet endpoint, or oversized response is a failure. Use the unit tests with an injected transport for deterministic offline coverage, and run the CLI command separately when a live public testnet read is required.

Normalized GraphQL module responses must include nonblank package/module identities and complete function/struct connections. GraphQL object responses require owner plus typed Move content or package module content; GraphQL function responses retain complete type-parameter, parameter, and return signatures; GraphQL struct responses retain abilities, type parameters, and typed fields. GraphQL package/module/object connections require nonblank names, fully qualified names, pageInfo, and cursor pagination to exhaustion. Empty collections remain valid only where the protocol permits them; missing shape fields, repeated/missing cursors, and empty entries fail closed.

## Offline validators

The compiled Move and cross-artifact validators prove only caller-bound structural consistency, source/tree integrity, and schema/identity alignment. The TAP verifier requires each `*.skill.tap.json` to point to its matching validated `*.dag.json` artifact. They do not prove native execution or deployed registration. Keep their receipts and manifests outside the hashed package/build roots, and retain the explicit distinction between offline evidence and read-only testnet observations.
