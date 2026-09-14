# Skills source and evidence helpers

## External evaluator export

The canonical catalogs under each skill's `evals/` directory use `expectations`. The standard `scripts/export_agent_skills_eval.py` creates a new evaluator bundle and maps every expectation plus the canonical `expected_output` criterion to the `assertions` field understood by `agent-skills-eval`; it never overwrites the source catalog.

Export the execution/routing cases (the default) or the separate supplied-input response cases deliberately:

```bash
python3 -B scripts/export_agent_skills_eval.py --output-dir <disposable-eval-bundle> --catalog response
npx agent-skills-eval <disposable-eval-bundle> --target <model> --judge <model> --baseline --strict
```

Use `--skill <skill-name>` and repeatable `--case <case-id>` or `--case <skill-name>/<case-id>` selectors to keep a run focused. `--catalog all` combines both catalogs after checking for duplicate IDs. The generated `agent-skills-eval-manifest.json` records selected cases and the adaptation; each exported skill's `agent-skills-eval-source-catalogs.json` records the source catalog paths and SHA-256 digests. The external evaluator's chat responses still do not prove automatic skill routing or command, compiler, provider, chain, or transaction execution without separately supplied tools and fixtures.

## Skill evaluation runner

The repository includes `run_skill_evals.py`, a Python standard-library harness for the small scenario catalogs in each skill. It validates explicit, implicit, contextual, and negative trigger metadata, requires each catalog root to be a JSON object, and resolves `bundled:` references from the selected bundle root without network access. The harness cannot prove Codex's live automatic skill selection, but its explicit command runs with the selected local bundle discoverable at `$CWD/.agents/skills`.

Plan cases before execution:

`--plan`, `--list`, and `--replay` are mutually exclusive terminal modes; supplying more than one is a usage error.

```bash
python3 -B scripts/run_skill_evals.py --plan \
  --skill nexus-tap-development --output-dir /tmp/nexus-skill-eval-plan
```

Run only an explicitly supplied command. Every case gets a new output directory containing a copied evidence snapshot under `snapshot/`, a temporary command cwd under `workspace`, `workspace/.agents/skills` with the selected skill, explicitly routed sibling skills, shared production scripts/resources, and selected fixtures. `evals/` catalogs, expected-answer data, Git metadata, caches, and evaluator tests stay out of the command-visible bundle. `SKILLS_BUNDLE_ROOT` points to `workspace/.agents/skills`; `SKILL_EVAL_SNAPSHOT_ROOT` and `SKILL_EVAL_SKILL_ROOT` remain available for diagnostics. Each case records `metadata.json`, raw stdout `trace.jsonl`, `stderr.log`, declared artifacts, and `result.json`; metadata includes a SHA-256 `bundle_manifest` for every regular file visible under the bundle root. runner recomputes that regular-file manifest after producer exits; added, removed, or modified regular files, plus invalid symlink or special entries, fail the live case's deterministic grade. Empty-directory-only changes are outside this file-content guarantee. `summary.json` records aggregate states. Command cwd bundle filesystem isolation aids reproducibility; the explicit command keeps its normal process/runtime permissions. Codex example uses `--skip-git-repo-check` because the temporary cwd is not a Git checkout:

```bash
python3 -B scripts/run_skill_evals.py \
  --skill nexus-tap-development \
  --case tap-api-app-only \
  --output-dir /tmp/nexus-skill-eval-run \
  --command 'codex exec --json --full-auto --skip-git-repo-check {prompt}'
```

The command receives only the case prompt and case ID; expected outputs and rubric text stay in grading metadata and are never passed to the command. Structured skill-invocation events are the only activation evidence: assistant prose, prompt text, and `command_execution` starts do not count. Missing invocation is `unknown` for both positive and negative cases. Missing or malformed JSONL, missing allowlisted terminal event, any explicit terminal status outside the documented success set (`complete`, `completed`, `success`, `succeeded`), terminal failure/error, command failure, timeout, launch error, invalid artifact root, or missing/symlinked artifact fails closed. A successful command with ungraded behavior rubrics has `deterministic_status: pass`, overall `status: unknown`, and `manual_status: pending`. Re-grade saved traces without rerunning the command model:

```bash
python3 -B scripts/run_skill_evals.py \
  --replay /tmp/nexus-skill-eval-run \
  --output-dir /tmp/nexus-skill-eval-replay
```

Replay accepts repeatable `--skill` and `--case` selectors; repeated skills form a union and case IDs intersect that saved owning-skill scope. Case IDs come from each saved `metadata.json`, so a case directory can be renamed during relocation without changing its selector or emitted case ID. Unknown or no-match selections fail before case results are created. `--command` is rejected with `--replay`, and replay never launches a producer command.

Replay validates the saved regular-file `bundle_manifest` inventory and contents against the snapshot workspace before copying or grading; saved symlink or special entries fail closed, while empty-directory-only changes are outside this integrity guarantee; copies saved raw trace, stderr, workspace, metadata, bundle snapshot, and artifacts into a new output directory before writing deterministic/manual-pending results. Altered, added, removed, symlinked, or escaped bundle files fail closed. Catalog replay case IDs must be a single safe path component using letters, numbers, dot, underscore, and hyphen; traversal, absolute, dot, and separator-bearing IDs are rejected before output creation. Cross-routing cases set `should_trigger: false` and declare an `expected_skill` sibling skill: a structured invocation of the sibling passes activation evidence, an owning-skill invocation fails, and missing invocation remains `unknown`. All recognized terminal events are inspected, so any terminal failure or unsupported status fails completion even if another terminal event succeeds. Replay recomputes aggregate status and artifact checks. Real provider, chain, beta VM, registration, settlement, wallet, and automatic-selection evidence requires its separately authorized workflow.

Replay accepts only the recorded evidence boundary. Every `bundled:` source must be a safe relative path to a non-symlinked file in the saved snapshot, saved snapshot selected-skill provenance must match case metadata, and saved bundled digests and the complete bundle manifest must match snapshot content. HTTPS sources are syntax-checked without fetching and retain a recorded `null` digest; replay never consults the current Skills checkout. Recognized wrong or mixed skill selection fails activation grading; no recognized selection remains `unknown`.

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

The JSON report includes the explicit endpoint, `testnet` network label, the returned official testnet chain identifier, observations, navigation-only `vision_links` for returned object/package addresses, allowlisted queries, response digests, collection time, and a canonical report digest. A wrong chain identifier, null/missing/empty/incomplete requested observation, timeout, HTTP/GraphQL error, malformed JSON, unsupported query, invalid identifier, non-testnet endpoint, or oversized response is a failure. Use the unit tests with an injected transport for deterministic offline coverage, and run the CLI command separately when a live public testnet read is required.

Normalized GraphQL module responses must include nonblank package/module identities and complete function/struct connections. GraphQL object responses require owner plus typed Move content or package module content; GraphQL function responses retain complete type-parameter, parameter, and return signatures; GraphQL struct responses retain abilities, type parameters, and typed fields. GraphQL package/module/object connections require nonblank names, fully qualified names, pageInfo, and cursor pagination to exhaustion. Empty collections remain valid only where the protocol permits them; missing shape fields, repeated/missing cursors, and empty entries fail closed.

## Talus Vision navigation links

`vision_links.py` builds Talus Vision explorer links for identifiers that exact read-only evidence already returned. It performs no network access. Every link carries an explicit `?network=testnet` or `?network=mainnet` query, because Vision otherwise defaults to Mainnet or reuses the viewer's last-selected network, and a Testnet ID then opens as not found. Vision has no devnet or localnet view.

```bash
python3 <skills-bundle>/scripts/vision_links.py --network testnet --kind execution --id 0x<execution-id>
python3 <skills-bundle>/scripts/vision_links.py --network testnet --kind skill --id 0x<agent-id> --skill-index 0
```

| Kind | Route | Identifier |
| --- | --- | --- |
| `tx` | `/tx/<digest>` | Canonical base58 transaction digest (32 bytes) |
| `execution` | `/execution/<id>` | DAG Execution object ID |
| `payment` | `/payment/<id>` | The Execution ID; Vision resolves its `ExecutionPayment` |
| `task` | `/task/<id>` | Task object ID |
| `workflow` | `/workflow/<id>` | DAG object ID |
| `tool` | `/tool/<fqn>` | `domain.name@version` FQN, with `@` encoded as `%40`, or a Tool object ID |
| `agent` | `/agent/<id>` | Agent (TAP) object ID |
| `skill` | `/skill/<agent-id>/<index>` | Agent ID plus numeric skill index |
| `leader` | `/leader/<id>` | Leader capability ID |
| `profile` | `/profile/<address>` | Wallet, signer, or beneficiary address |
| `object` | `/object/<id>` | Any other object or package, including reserves, `ExecutionPayment`, vaults, Occurrences, and Invocations |

Object IDs must already be full, lowercase, non-zero `0x` plus 64 hex; the helper never pads a shortened ID. `testnet_evidence.py` emits `vision_links.object` and `vision_links.package` for returned addresses. `validate_skills.py` accepts Vision URLs only in documentation contexts and only in this canonical form. A Vision page is an indexed projection that can lag or differ from chain state; neither the page nor its link is evidence, so keep links in a separate navigation list beside the evidence ledger.

## Offline validators

The compiled Move and cross-artifact validators prove only caller-bound structural consistency, source/tree integrity, and schema/identity alignment. The TAP verifier requires each `*.skill.tap.json` to point to its matching validated `*.dag.json` artifact. They do not prove native execution or deployed registration. Keep their receipts and manifests outside the hashed package/build roots, and retain the explicit distinction between offline evidence and read-only testnet observations.
