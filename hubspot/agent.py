"""hubspot — searches HubSpot for new support tickets and returns their IDs.

A deterministic Friday agent (no LLM, no tool loop): it runs the HubSpot CRM v3
ticket search for the configured pipeline stage(s) created within a recent time
window, newest first, paginating until it has up to `limit` matches, and
returns their IDs.

Robustness built in:
  - pagination — follows the `paging.next.after` cursor up to `limit` results,
    bounded by HubSpot's 10,000-record search window;
  - retries — 429 (the search endpoint allows only ~5 req/s per token) and 5xx
    are retried with backoff, honoring the `Retry-After` header;
  - error reporting — HubSpot's error `category` and `correlationId` are
    surfaced so failures (bad scope, auth, validation) are actionable.

The HubSpot v3 search API filters datetime properties (`createdate`) by
epoch-milliseconds.

Config is optional and read from the prompt as a JSON envelope; every field has
a default, so an empty prompt also works:

    {"task": "fetch-new-tickets",
     "config": {"pipelineStages": ["1"], "withinMinutes": 60, "limit": 100}}

`limit` is the maximum number of tickets to return in total (paginated), not a
page size. `pipelineStages` defaults to ["1"] — the "New" stage of HubSpot's
default ticket pipeline (id "0"); accounts with customized pipelines use
different stage IDs (resolve them via GET /crm/v3/pipelines/tickets).

Output is a structured result whose `response` field holds the bare JSON array
of IDs, with `ticketIds` / `tickets` as structured equivalents:

    {"response": "[\"1000000001\",\"1000000002\"]",
     "ticketIds": ["1000000001", "1000000002"],
     "count": 2,
     "tickets": [{"id": "1000000001", "createdAt": "...", "pipelineStage": "1"}]}
"""

import json
import random
import time
from datetime import UTC, datetime

from friday_agent_sdk import AgentContext, HttpError, HttpResponse, agent, err, ok, run

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

# HubSpot returns at most 200 records per search page, and the search endpoint
# only exposes the first 10,000 matches for any query — so `limit` is clamped to
# that window and pages are capped at 200.
_PAGE_SIZE = 200
_MAX_RESULTS = 10000
# Guard the time window so a malformed config can't request an unbounded scan.
_MAX_WITHIN_MINUTES = 7 * 24 * 60  # 7 days

# Transient HTTP statuses worth retrying: 429 (the search endpoint is limited to
# ~5 req/s per token) plus 5xx. Retries honor Retry-After, else fall back to
# exponential backoff with jitter.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 0.5


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
    # `limit` is the max total tickets to return (paginated), capped at the
    # 10,000-record search window.
    limit = _clamp(_coerce_int(_pick(raw, ["limit"]), _DEFAULT_LIMIT), 1, _MAX_RESULTS)

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


def _build_search_body(
    stages: list[str], since_ms: int, limit: int, properties: list[str], after: str | None = None
) -> dict:
    """Build the HubSpot CRM v3 tickets search request body.

    Datetime properties are filtered by epoch milliseconds (as a string), per
    the HubSpot search API. `IN` on hs_pipeline_stage takes a `values` array;
    `sorts` uses the object form (propertyName/direction). `after` is the paging
    cursor from a previous response's `paging.next.after` (omitted on page one).
    """
    body: dict = {
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
    if after is not None:
        body["after"] = after
    return body


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


def _next_after(data: object) -> str | None:
    """Return the next-page cursor from `paging.next.after`, or None if last page."""
    if not isinstance(data, dict):
        return None
    paging = data.get("paging")
    if not isinstance(paging, dict):
        return None
    nxt = paging.get("next")
    if not isinstance(nxt, dict):
        return None
    after = nxt.get("after")
    return str(after) if after else None


# ───────────────────────────────────────────────────────────────────────
# HTTP — retries & error reporting
# ───────────────────────────────────────────────────────────────────────


def _sleep(seconds: float) -> None:
    # Wrapped so tests can patch out the wait.
    time.sleep(seconds)


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter (~0.5s, ~1s, ~2s, plus jitter)."""
    return _RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, _RETRY_BASE_SECONDS)


def _header(resp: HttpResponse, name: str) -> str | None:
    """Case-insensitive response header lookup."""
    name_lower = name.lower()
    for key, value in (resp.headers or {}).items():
        if key.lower() == name_lower:
            return value
    return None


def _retry_after(resp: HttpResponse) -> float | None:
    """Seconds to wait from the `Retry-After` header, if HubSpot sent one."""
    raw = _header(resp, "Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _error_message(resp: HttpResponse) -> str:
    """Format a HubSpot error response, surfacing its category + correlationId.

    HubSpot returns `{status, message, category, correlationId, errors[]}`.
    Pulling out category/correlationId makes failures actionable — the
    correlationId is what HubSpot Support needs, and the category distinguishes
    a config error (MISSING_SCOPES, INVALID_AUTHENTICATION) from a transient
    one. Falls back to the raw body when the response isn't the standard shape.
    """
    parts = [f"HubSpot ticket search failed: {resp.status}"]
    detail = resp.body[:300]
    category = None
    correlation_id = None
    try:
        data = json.loads(resp.body)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        category = data.get("category")
        correlation_id = data.get("correlationId")
        detail = data.get("message") or detail
    if category:
        parts.append(f"[{category}]")
    parts.append(detail)
    if correlation_id:
        parts.append(f"(correlationId: {correlation_id})")
    return " ".join(parts)


def _search(
    ctx: AgentContext, headers: dict, payload: str
) -> tuple[HttpResponse | None, str | None]:
    """POST one search page, retrying transient failures (429 + 5xx) with backoff.

    Returns `(response, None)` on success, or `(None, message)` on a terminal
    failure — a transport error, or a non-retryable HTTP error (400/401/403),
    surfaced via `_error_message`. Retries honor `Retry-After` when present.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = ctx.http.fetch(
                _TICKETS_SEARCH_URL,
                method="POST",
                headers=headers,
                body=payload,
                timeout_ms=20000,
            )
        except HttpError as e:
            if attempt < _MAX_RETRIES:
                _sleep(_backoff(attempt))
                continue
            return None, f"HubSpot ticket search failed: {e}"

        if resp.status < 400:
            return resp, None
        if resp.status in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
            _sleep(_retry_after(resp) or _backoff(attempt))
            continue
        return None, _error_message(resp)

    return None, "HubSpot ticket search failed: retries exhausted"  # defensive; loop returns first


# ───────────────────────────────────────────────────────────────────────
# Agent
# ───────────────────────────────────────────────────────────────────────


@agent(
    id="hubspot",
    version="1.1.0",
    description=(
        "Searches HubSpot for support tickets in the configured pipeline stage(s) "
        "created within a recent time window and returns their IDs. Paginated, with "
        "retries on rate-limit/5xx. Deterministic REST search — no LLM, no tool loop."
    ),
    environment={
        "required": [
            {
                "name": "HUBSPOT_ACCESS_TOKEN",
                "description": "HubSpot private app token with the crm.objects.tickets.read scope",
            }
        ]
    },
)
def execute(prompt: str, ctx: AgentContext):
    # Capabilities (ctx.http, ctx.stream, ...) are always initialized by the
    # host — never None — so we use them directly without guarding.
    token = (ctx.env or {}).get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        return err("HUBSPOT_ACCESS_TOKEN is not set")

    stages, within_minutes, limit, properties = _resolve_config(prompt, ctx)

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    since_ms = now_ms - within_minutes * 60 * 1000

    ctx.stream.intent(
        f"Searching HubSpot for tickets in stage(s) {', '.join(stages)} "
        f"from the last {within_minutes}m"
    )

    headers = _hs_headers(token)
    tickets: list[dict] = []
    after: str | None = None

    # Page through results (HubSpot returns <=200/page) until we have `limit`
    # tickets or run out of pages. `limit` is already clamped to the 10k window,
    # so following the cursor can never page past it.
    while len(tickets) < limit:
        page_size = min(_PAGE_SIZE, limit - len(tickets))
        body = _build_search_body(stages, since_ms, page_size, properties, after=after)
        resp, error = _search(ctx, headers, json.dumps(body))
        if error is not None:
            return err(error)
        try:
            data = resp.json() or {}
        except Exception as e:
            return err(f"HubSpot search response not JSON: {e}")
        tickets.extend(_extract_tickets(data))
        after = _next_after(data)
        if after is None:
            break

    tickets = tickets[:limit]
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
