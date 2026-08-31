# Payment command map

Use the selected binary's `--help` output as the command contract. The public SDK links below are source references; they do not grant permission to run a state-changing command.

| Evidence | Public source |
| --- | --- |
| Gas balance and JSON output | [SDK gas commands](https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/gas) and the custody rules in [the payment ledger](payment-ledger.md) |
| Task, Occurrence, and cost fields | [SDK task commands](https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/task) and the identity rules in [the payment ledger](payment-ledger.md) |
| TAP payment and vault fields | [SDK TAP commands](https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tap) and the custody rules in [the payment ledger](payment-ledger.md) |
| ToolCashier policy, inbox, and collection | [SDK Tool commands](https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/tool) and the mutation gate in [the payment ledger](payment-ledger.md) |
| Execution and Invocation evidence | [SDK execution commands](https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/execution) and the trace worksheet in [the payment ledger](payment-ledger.md) |
| Priority-fee accounting | [SDK network commands](https://github.com/Talus-Network/nexus-sdk/tree/main/cli/src/network) and the separate custody rules in [the payment ledger](payment-ledger.md) |
| On-chain interface/dependency shape | [Public Move package interfaces](https://github.com/Talus-Network/nexus-move-packages/tree/main/packages) |
| Sui framework packages for local Move interpretation | [Pinned Sui framework source](https://github.com/MystenLabs/sui/tree/d8459684b41eb09ab23fe16a9dd84173270bbaba/crates/sui-framework/packages) |
| Deployed package/module/object observations | `scripts/testnet_evidence.py` with `https://graphql.testnet.sui.io/graphql` |

## Command classes

Read-only inspection includes version/help, gas balance, object inspection, Task/Occurrence/Execution/payment reads, and the explicit testnet evidence helper. Local build/test changes only disposable files. Deposits, refills, collection, settlement, scheduling, policy updates, and transaction submission are shared-network mutations and require a separate authorization gate.
