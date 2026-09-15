# v2.0.0 walks and evidence limitations

## Check the selected CLI and network

Use `nexus --version`, `nexus dag publish --help`, `nexus task schedule --help`, and the selected inspection command's `--help`. Check the selected deployment and wallet/network configuration against the published [Developer Setup](https://docs.talus.network/guides/getting-started/setup). The examples below describe the released v2.0.0 CLI; do not substitute beta VM tests for a live walk.

## Inspect Tools by FQN

Use the exact FQN from the deployment receipt, DAG, or supplied skill artifact to inspect each Tool. Do not run `nexus tool list` as a discovery or registration-verification step: a successful inventory command can return an empty list or rows with unavailable Tool details and unknown registration.

```bash
nexus tool inspect --tool-fqn "$TOOL_FQN" --json
```

Confirm the returned FQN, package, network, and registration state against the expected deployment. Keep the exact result and timestamp. Exit status 0 alone is not registration proof: if inspection cannot read the Tool or confirm its state, record the returned error as unavailable evidence. If no FQN is known, recover it from the supplied artifacts or request the deployment record; do not invent an FQN or register a duplicate Tool to repair discovery.

## Run a walk on v2.0.0

`nexus dag execute` is gone. Publish the DAG, then create and schedule a Task. These commands change chain state and spend funds: run them only within an already authorized live execution scope. Diagnosis and payment reconciliation remain read-only unless that scope is explicitly extended.

First validate the local DAG artifact and publish it:

```bash
nexus dag validate --path "$DAG_PATH"
nexus dag publish --path "$DAG_PATH"
```

Save the returned DAG object ID as `DAG_ID`; publishing alone does not start an Execution. Inspect its entry groups and required inputs:

```bash
nexus dag inspect --dag-id "$DAG_ID"
```

Set `ENTRY_GROUP` and `INPUT_JSON` to match that DAG. Set `PREPAY_MIST` to the approved Task reserve amount and `OCCURRENCE_BUDGET_MIST` to the approved maximum for this occurrence, both integer MIST values. Both funding flags are mandatory, including for an immediate one-off walk. The reserve must cover the intended execution budget; transaction gas is separate. Do not choose a spending amount on the user's behalf.

```bash
nexus task schedule --dag-id "$DAG_ID" \
  --entry-group "$ENTRY_GROUP" --input-json "$INPUT_JSON" \
  --prepay-amount-mist "$PREPAY_MIST" \
  --occurrence-budget-mist "$OCCURRENCE_BUDGET_MIST" --now
```

Save the returned Task ID as `TASK_ID` and transaction digest. Scheduling is not proof of execution or settlement. Discover the occurrence ID from the Task rather than assuming it is zero:

```bash
nexus task inspect --task-id "$TASK_ID"
nexus task occurrence list --task-id "$TASK_ID" --json
```

Set `OCCURRENCE_ID` to the returned occurrence being investigated, then inspect it:

```bash
nexus task occurrence inspect --task-id "$TASK_ID" --occurrence-id "$OCCURRENCE_ID"
nexus execution inspect --task-id "$TASK_ID" --occurrence-id "$OCCURRENCE_ID"
```

Record the Execution ID and inspect payment/Tool outcomes using the selected skill's evidence procedure. If execution is still pending, preserve that state and use read-only inspection; do not schedule a duplicate merely because completion is not yet visible.

## Testnet history is short-lived

Testnet prunes transaction history. Execution reconstruction can stop working after only a few days; there is no guaranteed retention window. `execution inspect` may fail with `history is incomplete: missing transaction …` even for an Execution that previously completed. A durable Task or occurrence record does not guarantee its supporting transaction history remains queryable.

Capture evidence immediately after the walk: collection time, CLI version, network/endpoint, DAG and Task IDs, occurrence and Execution IDs, transaction digests, raw inspection output, and relevant effects/events and payment results exposed by the tools. Save errors and exit statuses too. Historical output remains a timestamped observation, not a fresh chain verification.

When history is incomplete, classify the missing history as unavailable evidence. Do not infer execution failure, missing registration, or nonpayment from it, and do not repeatedly retry the same pruned transaction. Continue with available durable records and previously saved evidence, clearly stating what cannot be reconstructed. If fresh live proof is required, perform a new authorized walk with new IDs and capture it immediately; it does not recover or prove the old Execution. Rerunning a walk spends funds and needs authorization when it is outside the current task.
