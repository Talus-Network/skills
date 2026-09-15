# Talus Nexus Agent Skills

Reusable engineering skills for building and diagnosing Talus Nexus applications. Stable examples, companion scripts, and test fixtures live in this repository, while version-sensitive source is limited to anonymous public repositories and read-only Sui testnet evidence. A skill-only installation does not include the repository-level companion scripts.

## Install from the public repository

Install all five skill directories or select individual skills with the public `skills` CLI:

```bash
npx skills add Talus-Network/skills
```

Choose your coding assistant and the skills needed for your project in the installer. Once installed, ask your assistant to use the relevant skill by name.

## Skills

Before following any helper or fixture command from an installed skill, use its `references/consumer-setup.md` to fetch the pinned public companion checkout and set `SKILLS_BUNDLE_ROOT`. The installer does not set that variable or supply the repository-level scripts. Repository-maintainer commands below run from a full checkout. Live v2.0.0 execution is covered in the TAP workflow; Tool verification, payment reconciliation, and Task diagnosis include their own exact-FQN and Testnet evidence guidance.

| Skill                                                               | Use it for                                                                                 |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [nexus-onchain-tool-development](nexus-onchain-tool-development/)   | Build and validate an on-chain Tool in Sui Move.                                           |
| [nexus-offchain-tool-development](nexus-offchain-tool-development/) | Build, test, and validate an off-chain HTTP Tool service.                                  |
| [nexus-tap-development](nexus-tap-development/)                     | Build a TAP and its artifacts, and a Nexus API dashboard/dApp/backend for an existing TAP. |
| [nexus-onchain-task-debugging](nexus-onchain-task-debugging/)       | Trace Task, Occurrence, Execution, Tool, and payment evidence.                             |
| [nexus-cli-payment-tracking](nexus-cli-payment-tracking/)           | Reconcile funding, reserves, charges, refunds, and Tool revenue.                           |

## TAP applications and Tool scaffolding

For an application over an existing TAP, use [Nexus API application development](nexus-tap-development/references/nexus-api-application.md): hosted REST projections, paginated execution history, replayable SSE, a server-only provider key, and deterministic frontend/relay tests. App-only work does not require a Move package or CLI/source setup. The public [Nexus API guide](https://docs.talus.network/guides/nexus-api) and provider reference define the selected deployment's consumer contract.

The Tool skills include the full scaffold-to-implementation workflow and completion checks for [Rust HTTP Tools](nexus-offchain-tool-development/references/scaffolding.md) and [Sui Move Tools](nexus-onchain-tool-development/references/scaffolding.md). Start with the released public CLI scaffold, then implement real behavior and verify it using the matching skill. No separate plugin runtime is required.

## Safety model

The skills default to repository-owned examples, isolated build/test work, dry-run validation, and read-only inspection. Publishing, registration, deposits, refills, settlement, scheduling, upgrades, and asset movement require explicit authorization and a fresh network/signer/configuration preflight. Examples use placeholders rather than credentials or deployment-specific capability IDs.

## Portable source evidence

The source helper downloads only three reviewed anonymous public archives when bundled material is insufficient: [nexus-sdk](https://github.com/Talus-Network/nexus-sdk), [nexus-move-packages](https://github.com/Talus-Network/nexus-move-packages), and the pinned [Sui framework source](https://github.com/MystenLabs/sui/tree/d8459684b41eb09ab23fe16a9dd84173270bbaba) matching `sui 1.78.0-d8459684b41e`. It rejects local paths, SSH URLs, unknown repository selectors, and unapproved source roots. The Move package archive supplies the six public interface packages and their supporting closure; the Sui archive supplies only the framework packages needed for local Move compilation, and neither declaration set is an executable mock.

## Public Move Registry dependencies

For a network-facing Move consumer, declare the six reviewed Nexus packages through the public Move Registry and let Sui resolve the deployment-specific package address. The package pages are [nexus-interface](https://www.moveregistry.com/package/@talus/nexus-interface), [nexus-primitives](https://www.moveregistry.com/package/@talus/nexus-primitives), [nexus-registry](https://www.moveregistry.com/package/@talus/nexus-registry), [nexus-tool](https://www.moveregistry.com/package/@talus/nexus-tool), [nexus-scheduler](https://www.moveregistry.com/package/@talus/nexus-scheduler), and [nexus-workflow](https://www.moveregistry.com/package/@talus/nexus-workflow).

```toml
[dependencies]
nexus_interface  = { r.mvr = "@talus/nexus-interface" }
nexus_primitives = { r.mvr = "@talus/nexus-primitives" }
nexus_registry   = { r.mvr = "@talus/nexus-registry" }
nexus_tool       = { r.mvr = "@talus/nexus-tool" }
nexus_scheduler  = { r.mvr = "@talus/nexus-scheduler" }
nexus_workflow   = { r.mvr = "@talus/nexus-workflow" }
```

Commit the generated `Move.lock` and keep the package revision, network, and published dependency addresses matched. The public Move Packages repository remains useful for source inspection, provenance, contribution, and explicitly offline copied closure; it is not the preferred installation source. Do not invent a registry name for `nexus_policy` or `nexus_kernel`: the reviewed registry map records both as unregistered, and `nexus_kernel` remains supporting source closure rather than a direct application dependency.

## Published Setup authority

Version-sensitive instructions use the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup), not a source-repository checkout. Check the page anonymously before relying on a release-specific command; the expected release for this bundle is `v2.0.0`.

```bash
python3 scripts/docs_website.py --url https://docs.talus.network/guides/getting-started/setup --expected-version v2.0.0
```

The check is read-only and fails closed on an unavailable page, an unsafe redirect, malformed content, or a version mismatch. It never installs a dependency or changes a Sui environment; show any resulting setup command to the user as a separate, explicitly authorized workflow.

## Read-only Sui testnet evidence

When a question needs deployed package, module, object, or network evidence, use the bundled boundary with the explicit official Sui testnet GraphQL endpoint. It performs only allowlisted read-only GraphQL queries and never uses a wallet, active CLI environment, or state-changing command:

```bash
python3 "$SKILLS_BUNDLE_ROOT/scripts/testnet_evidence.py" \
  --graphql-url https://graphql.testnet.sui.io/graphql \
  --package-id 0x... --module module_name
```

The endpoint, network label, query methods, response digest, and timestamp are retained in the JSON report. An unavailable endpoint, malformed response, wrong network, or missing identifier is a concrete evidence failure; structural fixtures and validators must not be presented as runtime execution or registration proof.

## Validation

Run the bundle validator before proposing changes:

```bash
python3 scripts/validate_skills.py
python3 -B -m unittest discover -s scripts -p 'test_*.py'
python3 -B -m unittest discover -s nexus-tap-development/scripts -p 'test_*.py'
```

The standard-library `scripts/run_skill_evals.py` validates the five catalogs and can plan, run an explicitly supplied command, or replay saved traces. Each run records a selected skill snapshot, source digests, effective argv, raw JSONL stdout, stderr, and declared artifacts in a fresh case directory. A temporary cwd/snapshot organizes files but does not change the explicit command's OS permissions. Offline traces cannot prove Codex automatic selection, live provider or chain state, published-bytecode execution, registration, or settlement; missing activation is unknown for positive and negative cases, and ungraded behavior rubrics keep overall status `unknown` with `manual_status: pending`. Use `--plan` to inspect cases without execution and `--replay <saved-run> --output-dir <new-dir>` to rescore saved evidence without rerunning a command. The documented Codex command includes `--skip-git-repo-check` for its temporary non-Git cwd.

See [the source helper reference](scripts/README.md) and [the testnet evidence reference](scripts/README.md#read-only-sui-testnet-evidence) for the complete evidence and cleanup contract.
