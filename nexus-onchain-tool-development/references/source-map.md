# On-chain Tool source map

Use the public source references below for version-sensitive questions:

| Question | Source |
| --- | --- |
| Tool/TAP lifecycle and mutation boundaries | Bundled workflow and mutation-gate references |
| CLI templates, schemas, and validators | [Nexus SDK](https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool) |
| Move interface source and offline dependency closure | [Nexus Move Packages](https://github.com/Talus-Network/nexus-move-packages/tree/main/packages) |
| Network-facing Move installation/dependencies | [nexus-interface](https://www.moveregistry.com/package/@talus/nexus-interface), [nexus-primitives](https://www.moveregistry.com/package/@talus/nexus-primitives), [nexus-registry](https://www.moveregistry.com/package/@talus/nexus-registry), [nexus-tool](https://www.moveregistry.com/package/@talus/nexus-tool), [nexus-scheduler](https://www.moveregistry.com/package/@talus/nexus-scheduler), and [nexus-workflow](https://www.moveregistry.com/package/@talus/nexus-workflow) |
| Sui framework packages for offline Move builds | [Sui](https://github.com/MystenLabs/sui/tree/d8459684b41eb09ab23fe16a9dd84173270bbaba/crates/sui-framework/packages) at the pinned installed-CLI revision |
| GraphQL read method shape | Bundled `scripts/testnet_evidence.py` read-only contract |

Prepare public archives through `scripts/prepare_sources.py` and append only repository-relative paths below verified roots. Use `scripts/testnet_evidence.py` for current testnet package/module/object observations. Do not treat public native declarations, a source archive, or a read-only response as proof of execution.

## Dependency rule

For a network-facing consumer, use the exact public Move Registry names in the manifest:

```toml
[dependencies]
nexus_primitives = { r.mvr = "@talus/nexus-primitives" }
nexus_interface  = { r.mvr = "@talus/nexus-interface" }
```

Commit `Move.lock` and match its resolved package addresses to the selected network. For offline source inspection or deterministic fixture builds only, a consumer may point at copied public package directories inside the disposable project; keep direct dependencies limited to the packages required by the consumer and preserve the supporting closure separately. Native offline consumers must use local `std` and `sui` aliases for the two copied Sui framework packages with implicit dependencies disabled. Reject absolute paths, parent-directory escapes, unknown local package roots, and any dependency that is not present in the verified public archive. The public registry currently has no `nexus_policy` or `nexus_kernel` package record; do not invent either name.
