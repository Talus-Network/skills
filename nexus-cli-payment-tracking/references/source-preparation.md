# Public source and evidence preparation

Use this reference when a payment investigation needs version-sensitive CLI fields or Move object definitions. It is a read-only evidence step after the payment route has been selected. The canonical bundle contract is maintained in the commands and guidance below.

Set `SKILLS_BUNDLE_ROOT` to the installed bundle root and use `scripts/prepare_sources.py` from that root. Prepare only `nexus-sdk`, `nexus-move-packages`, and `sui`; resolve `SDK_ROOT`, `MOVE_PACKAGES_ROOT`, and `SUI_ROOT` from the returned manifest with the helper's `root --manifest ... --repo ...` command. Keep every path under the repository-relative verified roots. Never search the host checkout or copy private source paths into a report.

The normal public bootstrap is:

```bash
SOURCE_MANIFEST="$(python3 "$SKILLS_BUNDLE_ROOT/scripts/prepare_sources.py" prepare --only nexus-sdk --only nexus-move-packages --only sui --print-manifest-path)"
```

Install the cleanup trap before preparing the manifest. On normal exit or `INT`/`TERM`, call `python3 "$SOURCE_HELPER" cleanup --manifest "$SOURCE_MANIFEST"` exactly once when a manifest exists. Preserve the main command status; if cleanup fails after a successful operation, return the cleanup status and write the failure to stderr. The executable compatibility block in the entrypoint is deliberately small; this reference owns the surrounding explanation and evidence boundary.

For deployed package, module, object, or network facts, use the bundle-root `scripts/testnet_evidence.py` helper with an explicit official Sui Testnet GraphQL endpoint:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x<package-id> --module <module-name>
```

Record the endpoint, network label, allowlisted methods, response digests, and collection time. An unavailable, malformed, or incomplete response is an evidence gap, never proof of payment settlement.

## Executable source preparation contract

```bash
SKILLS_BUNDLE_ROOT="${SKILLS_BUNDLE_ROOT:?Set SKILLS_BUNDLE_ROOT to this installed Skills bundle root}"
SOURCE_HELPER="$SKILLS_BUNDLE_ROOT/scripts/prepare_sources.py"
test -f "$SOURCE_HELPER"
SOURCE_MANIFEST=""
cleanup_sources() {
  status="${1:-$?}"
  trap - EXIT INT TERM
  cleanup_status=0
  if [ -n "${SOURCE_MANIFEST:-}" ]; then
    python3 "$SOURCE_HELPER" cleanup --manifest "$SOURCE_MANIFEST" || cleanup_status=$?
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
SOURCE_MANIFEST="$(python3 "$SOURCE_HELPER" prepare --only nexus-sdk --only nexus-move-packages --only sui --print-manifest-path)" || source_prepare_status=$?
if [ "$source_prepare_status" -ne 0 ] || [ -z "$SOURCE_MANIFEST" ]; then
  SOURCE_MANIFEST=""
  if [ "$source_prepare_status" -eq 0 ]; then source_prepare_status=1; fi
  printf 'source preparation failed (status %s)\n' "$source_prepare_status" >&2
  exit "${source_prepare_status:-1}"
fi
SDK_ROOT=""
sdk_root_status=0
SDK_ROOT="$(python3 "$SOURCE_HELPER" root --manifest "$SOURCE_MANIFEST" --repo nexus-sdk)" || sdk_root_status=$?
if [ "$sdk_root_status" -ne 0 ] || [ -z "$SDK_ROOT" ]; then
  SDK_ROOT=""
  if [ "$sdk_root_status" -eq 0 ]; then sdk_root_status=1; fi
  printf 'nexus-sdk root resolution failed (status %s)\n' "$sdk_root_status" >&2
  exit "${sdk_root_status:-1}"
fi
MOVE_PACKAGES_ROOT=""
move_packages_root_status=0
MOVE_PACKAGES_ROOT="$(python3 "$SOURCE_HELPER" root --manifest "$SOURCE_MANIFEST" --repo nexus-move-packages)" || move_packages_root_status=$?
if [ "$move_packages_root_status" -ne 0 ] || [ -z "$MOVE_PACKAGES_ROOT" ]; then
  MOVE_PACKAGES_ROOT=""
  if [ "$move_packages_root_status" -eq 0 ]; then move_packages_root_status=1; fi
  printf 'nexus-move-packages root resolution failed (status %s)\n' "$move_packages_root_status" >&2
  exit "${move_packages_root_status:-1}"
fi
SUI_ROOT=""
sui_root_status=0
SUI_ROOT="$(python3 "$SOURCE_HELPER" root --manifest "$SOURCE_MANIFEST" --repo sui)" || sui_root_status=$?
if [ "$sui_root_status" -ne 0 ] || [ -z "$SUI_ROOT" ]; then
  SUI_ROOT=""
  if [ "$sui_root_status" -eq 0 ]; then sui_root_status=1; fi
  printf 'sui root resolution failed (status %s)\n' "$sui_root_status" >&2
  exit "${sui_root_status:-1}"
fi
```
