# hubspot

The first example in this repo, and a good template for a **deterministic** Friday
agent: a single REST call, no LLM, no tool loop.

It searches HubSpot for new support tickets and returns their IDs — a single
HubSpot CRM v3 search via `ctx.http`, with the pipeline stage(s), time window,
and limit all configurable from the prompt.

What it demonstrates:

- The `@agent(...)` decorator: id, version, description, and declared `environment`.
- Reading config from the prompt and structured input (`ctx.input.config`).
- Making an outbound HTTP call with `ctx.http.fetch(...)` and handling `HttpError`.
- Emitting progress with `ctx.stream.intent(...)`.
- Returning a structured result with `ok(...)` / `err(...)`.

## Run / Register

Each agent is a standalone Python program ending in `run()`. Register it with a
local Friday daemon (it connects over NATS and handles one execute call):

```bash
curl -X POST http://localhost:8080/api/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"path": "'"$(pwd)"'/hubspot/agent.py"}'
```

## Input / Output

Input: an optional JSON config envelope in the prompt. Every field has a
default (pipeline stage `"1"` = "New" in HubSpot's default ticket pipeline,
last 60 minutes, limit 100), so an empty prompt also works. Set
`pipelineStages` to the stage ID(s) from your own HubSpot portal.

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
IDs, with `ticketIds` / `tickets` as structured equivalents (the IDs below are
illustrative):

```json
{
  "response": "[\"1000000001\",\"1000000002\"]",
  "ticketIds": ["1000000001", "1000000002"],
  "count": 2,
  "tickets": [
    {"id": "1000000001", "createdAt": "1717500000000", "pipelineStage": "1"}
  ]
}
```

No tickets → `{"response": "[]", "ticketIds": [], "count": 0, "tickets": []}`.

## HubSpot API

`POST /crm/v3/objects/tickets/search` with filters
`hs_pipeline_stage IN <stages>` and `createdate GTE <epoch-ms>`, sorted by
`createdate DESCENDING`. Datetime properties are filtered by epoch
milliseconds per the HubSpot v3 search API. Requires `HUBSPOT_ACCESS_TOKEN`
(private app token) in the environment.

## Env

`HUBSPOT_ACCESS_TOKEN` — a HubSpot private app token. The `@agent` decorator
declares it under `environment.required`; the daemon resolves it from the
environment and exposes it as `ctx.env["HUBSPOT_ACCESS_TOKEN"]`.
