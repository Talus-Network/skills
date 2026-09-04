# TAP fixture reconstruction

Use this reference when a complete direct-plus-delayed fixture is requested. Reconstruct the package, both Tool modules, both schemas, the DAG/skill artifacts, and pure tests from repository-owned content. The canonical concrete assets are [direct](../fixtures/direct/README.md) and [delayed](../fixtures/delayed/README.md). The reconstruction must not import source files or deployment IDs from another checkout; consumer dependencies come from the public `nexus-move-packages` archive.

## Required checks

- `Move.toml` has repository-relative public Sui framework template dependencies; `dependency-template.json` records the pinned public source and the deterministic rewrite performed before the isolated build/test.
- Each fixture package has a nontrivial `execute` path with typed state/output behavior; native authorization/finalization is covered separately by the generated public-ABI consumer.
- DAG and skill artifacts agree on FQNs, ports, paths, commitments, policies, and fixed Tools.
- Pure tests cover direct and delayed application behavior and every documented invalid-input branch as an expected Move failure.
- If the reconstructed TAP includes tests that call published Nexus functions, run the beta `"$NEXUS_BETA_CLI" tap test --path <tap-package> --build-env testnet` gate after the package-owned tests and preserve its output as published-bytecode evidence.
- A read-only testnet report is used only for optional deployed package/module/object observation.

## Failure behavior

Malformed manifests, parent-directory escapes, missing dependency metadata, unknown output tags, stale semantic digests, unavailable source archives, and unavailable testnet responses fail closed. Keep the earliest concrete error and do not retry with a state-changing command.
