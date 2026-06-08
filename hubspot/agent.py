"""hubspot — searches HubSpot for new support tickets and returns their IDs.

A deterministic Friday agent: it runs a single HubSpot CRM v3 search (no LLM,
no tool loop) for tickets in the configured pipeline stage(s) created within a
recent time window, newest first, and returns the matching ticket IDs.

The HubSpot v3 search API filters datetime properties (`createdate`) by
epoch-milliseconds — see the official hubspot-api-python "Search by date"
example.

Config is optional and read from the prompt as a JSON envelope; every field
has a default, so an empty prompt also works:

    {"task": "fetch-new-tickets",
     "config": {"pipelineStages": ["1"], "withinMinutes": 60, "limit": 100}}

Output is a structured result whose `response` field holds the bare JSON array
of IDs, with `ticketIds` / `tickets` as structured equivalents:

    {"response": "[\"1000000001\",\"1000000002\"]",
     "ticketIds": ["1000000001", "1000000002"],
     "count": 2,
     "tickets": [{"id": "1000000001", "createdAt": "...", "pipelineStage": "1"}]}
"""

import json
from datetime import UTC, datetime

from friday_agent_sdk import AgentContext, HttpError, agent, err, ok, run

_HUBSPOT_API = "https://api.hubapi.com"
_TICKETS_SEARCH_URL = f"{_HUBSPOT_API}/crm/v3/objects/tickets/search"

# Defaults: pipeline stage "1" ("New" in HubSpot's default ticket pipeline),
# tickets created within the last hour, up to 100 records.
_DEFAULT_PIPELINE_STAGES = ["1"]
_DEFAULT_WITHIN_MINUTES = 60
_DEFAULT_LIMIT = 100
# Only the fields used downstream: the ID (always returned by HubSpot) plus a
# little metadata for observability. Subject/content are intentionally NOT
# requested — keeping the output lean and free of customer PII.
_DEFAULT_PROPERTIES = ["createdate", "hs_pipeline_stage"]

# HubSpot search caps a single page at 200 records.
_MAX_LIMIT = 200
# Guard the time window so a malformed config can't request an unbounded scan.
_MAX_WITHIN_MINUTES = 7 * 24 * 60  # 7 days


# ───────────────────────────────────────────────────────────────────────
# Config parsing
# ───────────────────────────────────────────────────────────────────────


def _coerce_int(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _coerce_str_list(value: object) -> list[str]:
    """Normalise a value into a list of non-empty strings.

    Accepts a single string (wrapped into a one-element list) or a list/tuple
    of stringifiable items. Anything else yields an empty list so the caller
    can fall back to defaults.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return []
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _pick(raw: dict, keys: list[str]) -> object:
    for k in keys:
        if k in raw and raw[k] is not None:
            return raw[k]
    return None


# Keys that identify our config object — used to tell our envelope apart from
# any other JSON the runtime may thread into the prompt and `ctx.input.config`
# (e.g. a trigger/signal payload).
_CONFIG_KEYS = (
    "pipelineStages",
    "pipeline_stages",
    "stages",
    "withinMinutes",
    "within_minutes",
    "limit",
    "properties",
)


def _scan_balanced_json(text: str) -> list[dict]:
    """Extract every top-level balanced `{...}` object from text as parsed dicts.

    The runtime prompt may concatenate our config envelope with unrelated JSON
    (e.g. a trigger/signal payload), so a single greedy parse isn't enough — we
    collect all objects and let the caller pick the one shaped like config.
    """
    out: list[dict] = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        end = i
        while end < len(text):
            ch = text[end]
            if in_string:
                if ch == "\\":
                    end += 1
                elif ch == '"':
                    in_string = False
                end += 1
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if depth == 0 and not in_string:
            try:
                parsed = json.loads(text[i : end + 1])
                if isinstance(parsed, dict):
                    out.append(parsed)
            except (ValueError, json.JSONDecodeError):
                pass
            i = end + 1
        else:
            i += 1
    return out


def _config_from_obj(obj: object) -> dict | None:
    """Return the config dict if `obj` is (or wraps) our config envelope.

    Accepts both `{config: {...our keys...}}` and a flat `{...our keys...}`.
    Returns None for unrelated objects (e.g. a trigger/signal payload, which
    has none of our keys) so they're ignored rather than mistaken for config.
    """
    if not isinstance(obj, dict):
        return None
    inner = obj.get("config")
    if isinstance(inner, dict) and any(k in inner for k in _CONFIG_KEYS):
        return inner
    if any(k in obj for k in _CONFIG_KEYS):
        return obj
    return None


def _resolve_config(prompt: str, ctx: AgentContext) -> tuple[list[str], int, int, list[str]]:
    """Resolve (pipelineStages, withinMinutes, limit, properties) with defaults.

    The prompt is the authoritative config source: we scan ALL JSON objects in
    it and pick the one shaped like our config, since the runtime may also
    thread an unrelated payload (e.g. a trigger signal) into the prompt and
    `ctx.input.config` — we must not mistake that for config (doing so would
    silently override the configured stage/window/limit). Missing or malformed
    values fall back to defaults, so even a freeform prompt runs cleanly.
    """
    raw: dict | None = None

    # 1. Prompt is authoritative — find the object shaped like our config.
    for obj in _scan_balanced_json(prompt or ""):
        candidate = _config_from_obj(obj)
        if candidate is not None:
            raw = candidate
            break

    # 2. Fallback: structured input wired by the runtime, but ONLY if it
    #    actually carries our config keys (an unrelated payload is ignored).
    if raw is None:
        try:
            ci = ctx.input.config if ctx and ctx.input else {}
        except Exception:
            ci = {}
        raw = _config_from_obj(ci) or {}

    stages = _coerce_str_list(_pick(raw, ["pipelineStages", "pipeline_stages", "stages"]))
    if not stages:
        stages = list(_DEFAULT_PIPELINE_STAGES)

    within = _clamp(
        _coerce_int(_pick(raw, ["withinMinutes", "within_minutes"]), _DEFAULT_WITHIN_MINUTES),
        1,
        _MAX_WITHIN_MINUTES,
    )
    limit = _clamp(_coerce_int(_pick(raw, ["limit"]), _DEFAULT_LIMIT), 1, _MAX_LIMIT)

    properties = _coerce_str_list(_pick(raw, ["properties"]))
    if not properties:
        properties = list(_DEFAULT_PROPERTIES)

    return stages, within, limit, properties


# ───────────────────────────────────────────────────────────────────────
# HubSpot search
# ───────────────────────────────────────────────────────────────────────


def _hs_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _build_search_body(stages: list[str], since_ms: int, limit: int, properties: list[str]) -> dict:
    """Build the HubSpot CRM v3 tickets search request body.

    Datetime properties are filtered by epoch milliseconds (as a string), per
    the HubSpot search API. `IN` on hs_pipeline_stage takes a `values` array;
    `sorts` uses the object form (propertyName/direction).
    """
    return {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "hs_pipeline_stage",
                        "operator": "IN",
                        "values": stages,
                    },
                    {
                        "propertyName": "createdate",
                        "operator": "GTE",
                        "value": str(since_ms),
                    },
                ]
            }
        ],
        "properties": properties,
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
        "limit": limit,
    }


def _extract_tickets(data: object) -> list[dict]:
    """Pull a compact {id, createdAt, pipelineStage} list from a search response."""
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []
    tickets: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("id") or "").strip()
        if not tid:
            continue
        props = r.get("properties") if isinstance(r.get("properties"), dict) else {}
        tickets.append(
            {
                "id": tid,
                "createdAt": str(props.get("createdate") or ""),
                "pipelineStage": str(props.get("hs_pipeline_stage") or ""),
            }
        )
    return tickets


# ───────────────────────────────────────────────────────────────────────
# Agent
# ───────────────────────────────────────────────────────────────────────


@agent(
    id="hubspot",
    version="1.0.0",
    description=(
        "Searches HubSpot for support tickets in the configured pipeline stage(s) "
        "created within a recent time window and returns their IDs. Deterministic "
        "REST search — no LLM, no tool loop."
    ),
    environment={
        "required": [{"name": "HUBSPOT_ACCESS_TOKEN", "description": "HubSpot private app token"}]
    },
)
def execute(prompt: str, ctx: AgentContext):
    if ctx.http is None:
        return err("HTTP capability unavailable")

    token = (ctx.env or {}).get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        return err("HUBSPOT_ACCESS_TOKEN is not set")

    stages, within_minutes, limit, properties = _resolve_config(prompt, ctx)

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    since_ms = now_ms - within_minutes * 60 * 1000
    body = _build_search_body(stages, since_ms, limit, properties)

    if ctx.stream is not None:
        ctx.stream.intent(
            f"Searching HubSpot for tickets in stage(s) {', '.join(stages)} "
            f"from the last {within_minutes}m"
        )

    try:
        resp = ctx.http.fetch(
            _TICKETS_SEARCH_URL,
            method="POST",
            headers=_hs_headers(token),
            body=json.dumps(body),
            timeout_ms=20000,
        )
    except HttpError as e:
        return err(f"HubSpot ticket search failed: {e}")

    if resp.status >= 400:
        return err(f"HubSpot ticket search failed: {resp.status} {resp.body[:300]}")

    try:
        data = resp.json() or {}
    except Exception as e:
        return err(f"HubSpot search response not JSON: {e}")

    tickets = _extract_tickets(data)
    ids = [t["id"] for t in tickets]

    # `response` holds the bare JSON array of IDs; `ticketIds` / `tickets` are
    # structured equivalents, plus a little metadata for observability.
    return ok(
        {
            "response": json.dumps(ids),
            "ticketIds": ids,
            "count": len(ids),
            "tickets": tickets,
        }
    )


if __name__ == "__main__":
    run()
