# Public source map

Use these public sources only when the bundled guidance does not answer a version-sensitive question:

1. The bundled diagnosis and payment references for lifecycle, CLI concepts, and payment terminology.
2. [Nexus SDK](https://github.com/Talus-Network/nexus-sdk) for CLI/schema/type names and generated client behavior.
3. [Nexus Move Packages](https://github.com/Talus-Network/nexus-move-packages) for public Move interfaces and dependency manifests.
4. [Sui](https://github.com/MystenLabs/sui/tree/d8459684b41eb09ab23fe16a9dd84173270bbaba/crates/sui-framework/packages) at the pinned installed-CLI revision for framework package sources.
5. The repository-owned `scripts/testnet_evidence.py` contract for GraphQL query shapes and read-only query semantics.

Prepare the public archives through `scripts/prepare_sources.py`; never infer source roots from the host filesystem. For deployed facts, query the explicit testnet endpoint through `scripts/testnet_evidence.py` and retain its report digest.

## Source roles

| Question | Authority |
| --- | --- |
| CLI flag, output field, or SDK type | Prepared public SDK plus selected binary `--help` |
| Lifecycle or payment meaning | Bundled diagnosis/payment guidance plus exact object/event evidence |
| Move dependency/interface | Prepared public Move package archive and its `Published.toml` records |
| Current package/module/object/network state | Read-only Sui testnet GraphQL report |
| Application behavior | Repository-owned fixture and deterministic unit test |

Public interface declarations are not executable runtime implementations. A read-only state response is not proof that a transaction, Tool, or workflow executed successfully.
