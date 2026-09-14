# Public source and evidence preparation

Use this reference after the embedded contract/design gate and after routing between TAP package/artifact work and an API application. The canonical pinned archive, manifest, and cleanup contract is maintained in the commands and guidance below.

For package work, set `SKILLS_BUNDLE_ROOT` to the installed bundle root and run `scripts/prepare_sources.py` for only `nexus-sdk`, `nexus-move-packages`, and `sui`. Resolve each root from the manifest using `root --manifest ... --repo ...`; copy only verified repository-relative paths into a disposable consumer workspace. Never search sibling checkouts or copy the full Sui archive.

Install cleanup before preparation. On normal exit or `INT`/`TERM`, call the bundle-root helper's cleanup operation once for an existing manifest, preserve the primary status, and report cleanup failure on stderr. Failed preparation or root resolution is a source-evidence gap, not permission to use another source.

For published Nexus calls, use an explicit `NEXUS_BETA_CLI` binary and keep its VM output separate from the `verify_tap_artifacts.py` structural gate. For deployed observations, use bundle-root `scripts/testnet_evidence.py` with the official read-only Testnet GraphQL endpoint. API-only work follows the application reference and does not need source archives, Move tooling, or beta CLI setup.

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
