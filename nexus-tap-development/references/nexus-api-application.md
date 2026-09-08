# Build a TAP application with Nexus API

Use this mode for a dashboard, React dApp, bot, or backend that reads an existing Agent, its skills, Tasks, and Executions. A TAP application can use these projections without creating or rebuilding a Move package. If the request also changes the TAP package or DAG, apply the package workflow separately to those files.

## Select the consumer contract

Record the application stack, intended deployment/network, existing Agent/skill/DAG identifiers, required views, read filters, relay ownership, and test evidence. Determine whether wallet actions are requested. Reuse supplied decisions; resolve endpoint and schema facts from the public guides rather than asking the user to design the API.

Use the published [Nexus API guide](https://docs.talus.network/guides/nexus-api), [dApp connection guide](https://docs.talus.network/guides/nexus-api/connect-a-dapp), and [TypeScript consumer](https://docs.talus.network/guides/nexus-api/typescript-client). The provider's [API reference](https://api.taluslabs.dev/docs) and deployment's `GET /openapi.json` define exact paths, scopes, filters, status/event names, and replay policy. Do not invent a Nexus TypeScript client package: the documented consumer uses ordinary fetch and SSE interfaces.

The guides show `https://api.taluslabs.dev` for both network examples. Treat that as a provisional provider URL, not proof of Mainnet or Testnet identity. Confirm the intended deployment and its network before presenting indexed objects as belonging to a network. Keep API deployment and cursor storage associated; do not reuse another deployment's IDs or replay cursor.

## Keep reads and transactions separate

| Application need | Read surface | Interpretation |
| --- | --- | --- |
| Agent and skill selection | `/agents` and its documented skill/revision resources | Indexed discovery; inspect the selected skill revision and DAG identity. |
| DAG and Tool presentation | `/dags`, `/tools` | Describe the workflow and registered Tool metadata. |
| Schedule progress | `/tasks` and its documented occurrence/reserve resources | Task scheduling and funding projections. |
| Run list and detail | `/executions`, `/executions/{execution_object_id}` | Execution status and counters, scoped by supported Agent/skill/DAG/Task/invoker filters. |
| Run drill-down | `/executions/{execution_object_id}/walks`, `/executions/{execution_object_id}/events`, `/executions/{execution_object_id}/payment-ledger` | Walks, paginated event history, and charge/refund entries. |
| Live updates | `/events/stream` | SSE notification and replay; refresh the affected REST resource. |

The execution list and execution event-history endpoint require `executions:read`; the SSE stream separately requires `events:read`. Confirm other resource scopes in the provider reference and request only the resource families the app uses. `/executions/{execution_object_id}/events` is paginated history, distinct from `/events/stream` replay.

Nexus API does not publish, register, bind, fund, schedule, sign, settle, or move assets. Implement requested actions through the public SDK/CLI or wallet-approved transaction flow with its own authority checks. A provider API key grants read access, not wallet authority. After a successful transaction, retain its digest, show indexing progress, and refresh the projection; an SSE frame or an absent REST row does not prove finality, deletion, settlement, or authorization. For contract state evidence, use the parent skill's read-only Sui Testnet helper independently of the hosted projection.

## Put the provider key in the server process

- A full-stack app uses same-origin route handlers; a static SPA needs a reviewed server relay or sidecar. A bot/backend may call the service directly from its server process.
- The provider issues `NEXUS_API_KEY` through its secure channel. Keep it in server-only runtime configuration or a secret manager. Never put it in a `VITE_`/public variable, browser bundle, URL, local storage, source, fixture, log, or shell argument. Local implementation and mock tests do not need a real key.
- Validate `NEXUS_API_URL` before attaching `x-api-key`: the tutorial accepts an origin-root HTTPS URL without userinfo, path prefix, query, or fragment. Use a separately reviewed adapter if the provider publishes a path-prefixed service. Never let browser input choose the upstream origin or supply arbitrary proxy routes.
- Allow only the GET resources and query parameters the app needs. Apply the app's own authentication and authorization before exposing scoped provider reads to users. Handle redirects explicitly so a credential-bearing request cannot escape the reviewed destination.
- Preserve upstream status and body. Forward only `content-type`, `x-request-id`, `x-error-code`, `retry-after`, `ratelimit-limit`, `ratelimit-remaining`, and `ratelimit-reset`; generate local cache policy. Drop upstream cookies, redirects, authentication challenges, CSP, site-data, cache, framing, and encoding headers.
- For SSE, preserve event frames and keep-alives, use `text/event-stream`, disable buffering, set no-cache/no-transform, and abort the upstream request when the browser disconnects. Browser `EventSource` connects to the same-origin relay; it cannot add the provider header itself.

Follow [Configure Hosted API Access](https://docs.talus.network/guides/nexus-api/tutorial/01-get-a-key) for an explicitly authorized live read. Do not require credentials, a CLI install, public source archives, or Move tooling merely to build and test the application with mocks.

## Read complete, validated pages

List responses use `{ items, metadata: { total_items, next_token } }`. Validate content type, JSON, required item fields, a nonnegative safe integer total, and the documented cursor type before using a page. Page size defaults to 20 and caps at 100 in the guide; confirm the selected deployment's limits.

Treat `metadata.next_token` as opaque: pass it unchanged as the next `page_token` with the same filters and stop only at `null`. A numeric zero is not a stop condition. Reject malformed or repeated cursors; do not invent offsets or truncate a healthy list after an arbitrary page count. Retry only the failed page before yielding its items so recovery does not duplicate rows. Reset the retry budget after each successful page.

Use exact filter names and snake_case status values from the API contract, such as `dag_object_id`, `created_after`, and `pending_settlement`. Unknown values are HTTP 400 `INVALID_FILTER_VALUE`, not a successful empty list. Keep loading, empty, failed, syncing, and successfully loaded states distinct. Validate detail responses as well as lists, and cancel or ignore stale requests after a filter or selected execution changes.

## Resume an event stream safely

1. Load the initial REST view, then open the same-origin stream with supported `kinds`. Unknown event kinds are HTTP 400 `INVALID_FILTER_VALUE`.
2. Parse a complete event frame before storing its ID. The documented frame has a nonnegative safe integer `id`, string `kind`, string `sender_address`, and a present `payload`; `tx_digest` is optional and must be a nonempty string when present.
3. If `MessageEvent.lastEventId` is nonempty, require a canonical nonnegative integer string that exactly agrees with the validated payload ID. When it is empty, use the payload ID. Reject unsafe integers, malformed frames, mismatched IDs, duplicates, and backwards IDs without rendering them or advancing the cursor.
4. Persist only the agreed, strictly increasing validated cursor. A fresh stream omits it. An explicit reconnect carries it through `last_event_id` or `Last-Event-ID` as documented by the relay/provider; do not guess precedence when both are supplied.
5. Use valid frames as invalidation signals and re-read affected REST state. Coalesce bursts and preserve a syncing/degraded view during projection lag. Refresh REST on reconnect so a missed wake-up does not leave the app stale.
6. On unmount, changed subscription, or fatal error, close the stream and cancel obsolete reads. In React, an explicit retry creates a new subscription generation with the saved validated cursor and a fresh REST read; do not leave an uncontrolled automatic reconnect loop after a terminal error.

If a cursor is rejected because it belongs to another deployment or is outside retention, use the provider's published retention/reset policy. Clear it and restart at the current tip only when that recovery is documented. Do not claim gap-free replay or silently discard history. Render service strings as text, never as untrusted HTML.

## Bound error recovery

| Response | Consumer action |
| --- | --- |
| 401 or 403 | Stop requests/reconnects with that credential and show a recovery action; provider correction is required. |
| 400 invalid filter or malformed response | Correct the request/consumer; do not retry unchanged. |
| 404 or empty projection after an event | Check deployment/identifier and allow bounded projection catch-up; do not claim deletion. |
| 408, 429, transport failure, or 5xx | Retry only idempotent reads within an attempt budget. Honor `Retry-After` seconds or HTTP date, then documented `ratelimit-reset`, then capped exponential backoff. |
| 413 | Reduce the page/request size before retrying. |

Some 408/413/parser responses have no JSON error envelope. Branch on HTTP status and content type; retain bounded diagnostic text and `x-request-id` without assuming `error_code` exists. Preserve rate-limit metadata in typed errors. See [API troubleshooting](https://docs.talus.network/guides/nexus-api/troubleshooting).

## Implement and verify in the application

Adapt the public [Run Watcher tutorial](https://docs.talus.network/guides/nexus-api/tutorial) and [React port](https://docs.talus.network/guides/nexus-api/tutorial/05-port-to-react) to the chosen stack. Copy only the consumer and relay pieces needed into the user's application; maintain its tests there. Public guide URLs are documentation, not a requirement for a sibling checkout or private service source.

- Exercise at least seven healthy pages, a zero cursor, terminal null, repeated/malformed cursors, and a retry on a later page with no duplicated items.
- Mock 429 with `Retry-After`, 5xx fallback, retry exhaustion, terminal 401/403, malformed JSON/MIME, and envelope-free 408/413. Confirm each successful page gets a fresh retry budget.
- Test SSE framing, valid replay, empty transport ID fallback, mismatched/unsafe IDs, duplicates/backwards frames, unknown kinds, disconnect/reconnect, cancellation, and provider-documented stale-cursor recovery. Assert rejected frames never advance replay state.
- Test relay route/origin validation, query forwarding, status/body preservation, the positive response-header allowlist, terminal authorization handling, stream cancellation, and no credential forwarding across redirects.
- Typecheck and build the actual frontend, then scan its emitted bundle with a disposable synthetic key sentinel to prove the server key was excluded. Test loading/error/empty/syncing states and React cleanup/retry behavior; do not use a real key for this check.

Report application files, covered views, mock/typecheck/build results, and any separately authorized live-read evidence. State whether deployment identity, real provider access, wallet transactions, or chain state were unverified. An app-only change does not require TAP package, DAG, fixture, or published-bytecode gates; a coupled package change still does.
