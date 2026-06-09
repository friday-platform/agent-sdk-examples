# hubspot-search

The first example in this repo, and a good template for a **deterministic** Friday
agent: a paginated REST search, no LLM, no tool loop.

It searches HubSpot for new support tickets and returns their IDs via
`ctx.http`, with the pipeline stage(s), time window, and limit all configurable
from the prompt.

What it demonstrates:

- The `@agent(...)` decorator: id, version, description, and declared `environment`.
- Reading config from the prompt and structured input (`ctx.input.config`).
- Making outbound HTTP calls with `ctx.http.fetch(...)` and handling `HttpError`.
- **Paginating** an API by following a cursor up to a bounded total.
- **Retrying** transient failures (429 / 5xx) with backoff that honors `Retry-After`.
- **Actionable errors** — surfacing the API's error `category` / `correlationId`.
- Emitting progress with `ctx.stream.intent(...)`.
- Returning a structured result with `ok(...)` / `err(...)`.

## Run / Register

Each agent is a standalone Python program ending in `run()`. Register it with a
local Friday daemon (it connects over NATS and handles one execute call):

```bash
curl -X POST http://localhost:8080/api/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"path": "'"$(pwd)"'/hubspot-search/agent.py"}'
```

## Input / Output

Input: an optional JSON config envelope in the prompt. Every field has a
default (pipeline stage `"1"` = "New" in HubSpot's default ticket pipeline,
last 60 minutes, limit 100), so an empty prompt also works.

- `pipelineStages` — stage ID(s) to match. The default `["1"]` is "New" in
  HubSpot's **default** ticket pipeline (id `"0"`); accounts with customized
  pipelines use different IDs (look them up via `GET /crm/v3/pipelines/tickets`).
- `withinMinutes` — how far back to look at `createdate` (clamped to ≤ 7 days).
- `limit` — the **maximum number of tickets to return in total** (the agent
  paginates to reach it), clamped to HubSpot's 10,000-record search window.

```json
{
  "task": "fetch-new-tickets",
  "config": {
    "pipelineStages": ["1"],
    "withinMinutes": 60,
    "limit": 100
  }
}
```

Output: a structured result whose `response` field holds the bare JSON array of
IDs, with `ticketIds` / `tickets` as structured equivalents (the IDs and
timestamp below are illustrative). `createdAt` is passed through verbatim from
HubSpot, which returns `createdate` as an ISO-8601 string in v3 search responses
(epoch-ms is only the filter *input* format):

```json
{
  "response": "[\"1000000001\",\"1000000002\"]",
  "ticketIds": ["1000000001", "1000000002"],
  "count": 2,
  "tickets": [
    {"id": "1000000001", "createdAt": "2024-06-04T15:20:00.000Z", "pipelineStage": "1"}
  ]
}
```

No tickets → `{"response": "[]", "ticketIds": [], "count": 0, "tickets": []}`.

## HubSpot API

`POST /crm/v3/objects/tickets/search` with filters
`hs_pipeline_stage IN <stages>` and `createdate GTE <epoch-ms>`, sorted by
`createdate DESCENDING`. Datetime properties are filtered by epoch
milliseconds per the HubSpot v3 search API.

- **Pagination** — pages are capped at 200 records; the agent follows
  `paging.next.after` until it has `limit` tickets or runs out of pages.
- **Rate limits** — the search endpoint allows only ~5 requests/second per
  token. On `429` the agent waits for the `Retry-After` header (falling back to
  exponential backoff with jitter) and retries; `5xx` is retried too. Config
  errors (`400`/`401`/`403`) fail fast without retrying.
- **Errors** — failures surface HubSpot's `category` (e.g. `RATE_LIMITS`,
  `MISSING_SCOPES`, `INVALID_AUTHENTICATION`) and `correlationId`, which is what
  HubSpot Support needs to investigate.

> **Freshness caveat:** HubSpot search is eventually consistent — a just-created
> ticket may not be indexed yet. A poller keyed on `createdate GTE now-window`
> should use an overlap window (or track a high-water mark) so it doesn't skip
> tickets created in the gap.

## Env

`HUBSPOT_ACCESS_TOKEN` — a HubSpot private app token with the
`crm.objects.tickets.read` scope. The `@agent` decorator declares it under
`environment.required`; the daemon resolves it from the environment and exposes
it as `ctx.env["HUBSPOT_ACCESS_TOKEN"]`. A token missing the scope yields a
`403 MISSING_SCOPES` error.

## Tests

```bash
uv run pytest
```

The suite ([`tests/`](tests/)) needs no HubSpot account — it calls `execute()`
directly with a hand-built `AgentContext` whose `ctx.http` is a mock, the same
injection seam the SDK's own tests use (`Http(http_fetch=...)`). It covers the
config parsing, the request body, the response parsing, and the handler's
success and error paths.
