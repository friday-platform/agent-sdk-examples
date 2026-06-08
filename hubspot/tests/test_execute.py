"""Integration tests for the hubspot agent's execute() handler.

`@agent` returns the handler unchanged, so we call `execute(prompt, ctx)`
directly with a hand-built AgentContext whose HTTP capability is backed by a
MagicMock — the same injection seam the SDK's own test_http.py uses
(`Http(http_fetch=<callable returning a JSON response string>)`).
"""

import json
import time
from unittest.mock import MagicMock

import agent
from friday_agent_sdk import AgentContext, ErrResult, Http, OkResult, StreamEmitter

_TICKET = {"id": "512", "properties": {"createdate": "1717500000000", "hs_pipeline_stage": "1"}}


def _envelope(*, status=200, body="", headers=None):
    """The JSON response string the host hands back to Http.fetch()."""
    return json.dumps({"status": status, "headers": headers or {}, "body": body})


def _search_body(results, *, total=None, paging=None):
    """A HubSpot tickets/search response body (the inner `body` payload)."""
    payload = {"results": list(results)}
    if total is not None:
        payload["total"] = total
    if paging is not None:
        payload["paging"] = paging
    return json.dumps(payload)


def _ok_fetch(results, **kw):
    """A http_fetch mock returning a 200 search response carrying `results`."""
    return MagicMock(return_value=_envelope(body=_search_body(results, **kw)))


def _ctx(*, token="test-token", fetch=None, stream_emit=None, env=None):
    if env is None:
        env = {} if token is None else {"HUBSPOT_ACCESS_TOKEN": token}
    ctx = AgentContext(env=env)
    if fetch is not None:
        ctx.http = Http(http_fetch=fetch)
    ctx.stream = StreamEmitter(stream_emit=stream_emit or MagicMock())
    return ctx


def _sent_request(fetch):
    """Decode the JSON request the agent passed through Http.fetch()."""
    sent = json.loads(fetch.call_args[0][0])
    sent["body"] = json.loads(sent["body"])
    return sent


# --- success path -------------------------------------------------------


def test_returns_ticket_ids_in_every_shape():
    fetch = _ok_fetch(
        [
            {"id": "512", "properties": {"createdate": "c1", "hs_pipeline_stage": "1"}},
            {"id": "777", "properties": {"createdate": "c2", "hs_pipeline_stage": "1"}},
        ]
    )
    result = agent.execute("", _ctx(fetch=fetch))

    assert isinstance(result, OkResult)
    assert result.data["response"] == json.dumps(["512", "777"])
    assert result.data["ticketIds"] == ["512", "777"]
    assert result.data["count"] == 2
    assert result.data["tickets"] == [
        {"id": "512", "createdAt": "c1", "pipelineStage": "1"},
        {"id": "777", "createdAt": "c2", "pipelineStage": "1"},
    ]


def test_empty_results_is_queue_empty():
    result = agent.execute("", _ctx(fetch=_ok_fetch([])))
    assert isinstance(result, OkResult)
    assert result.data == {"response": "[]", "ticketIds": [], "count": 0, "tickets": []}


def test_posts_to_search_endpoint_with_auth_and_defaults():
    fetch = _ok_fetch([_TICKET])
    before = int(time.time() * 1000)
    agent.execute("", _ctx(fetch=fetch))
    after = int(time.time() * 1000)

    sent = _sent_request(fetch)
    assert sent["url"] == agent._TICKETS_SEARCH_URL
    assert sent["method"] == "POST"
    assert sent["headers"]["Authorization"] == "Bearer test-token"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["timeout_ms"] == 20000

    body = sent["body"]
    stage_filter, date_filter = body["filterGroups"][0]["filters"]
    assert stage_filter == {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": ["1"]}
    assert date_filter["propertyName"] == "createdate"
    assert date_filter["operator"] == "GTE"
    # createdate is filtered by an epoch-millisecond *string* (HubSpot requirement)
    assert isinstance(date_filter["value"], str)
    assert date_filter["value"].isdigit()
    within_ms = 60 * 60 * 1000
    assert before - within_ms <= int(date_filter["value"]) <= after - within_ms
    assert body["sorts"] == [{"propertyName": "createdate", "direction": "DESCENDING"}]
    assert body["properties"] == ["createdate", "hs_pipeline_stage"]
    assert body["limit"] == 100


def test_prompt_config_overrides_defaults():
    fetch = _ok_fetch([_TICKET])
    prompt = json.dumps(
        {
            "task": "fetch",
            "config": {
                "pipelineStages": ["5", "6"],
                "withinMinutes": 30,
                "limit": 5,
                "properties": ["subject"],
            },
        }
    )
    before = int(time.time() * 1000)
    agent.execute(prompt, _ctx(fetch=fetch))
    after = int(time.time() * 1000)

    body = _sent_request(fetch)["body"]
    stage_filter, date_filter = body["filterGroups"][0]["filters"]
    assert stage_filter["values"] == ["5", "6"]
    assert body["limit"] == 5
    assert body["properties"] == ["subject"]
    within_ms = 30 * 60 * 1000
    assert before - within_ms <= int(date_filter["value"]) <= after - within_ms


def test_limit_is_clamped_to_hubspot_max():
    fetch = _ok_fetch([_TICKET])
    agent.execute(json.dumps({"config": {"limit": 9999}}), _ctx(fetch=fetch))
    assert _sent_request(fetch)["body"]["limit"] == 200


def test_emits_intent_before_searching():
    stream_emit = MagicMock()
    agent.execute("", _ctx(fetch=_ok_fetch([]), stream_emit=stream_emit))
    event_type, payload = stream_emit.call_args[0]
    assert event_type == "data-intent"
    assert "Searching HubSpot" in payload


def test_ignores_pagination_cursor_single_request():
    # Documents CURRENT behavior: the agent fetches exactly one page and
    # ignores paging.next.after — i.e. it silently truncates past the page
    # limit. See the improvements proposal (pagination / 10k-window gap).
    fetch = _ok_fetch(
        [{"id": "1", "properties": {}}],
        total=500,
        paging={"next": {"after": "100", "link": "?after=100"}},
    )
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, OkResult)
    assert result.data["ticketIds"] == ["1"]
    assert fetch.call_count == 1


# --- error paths --------------------------------------------------------


def test_missing_token_errors_without_calling_http():
    fetch = MagicMock()
    result = agent.execute("", _ctx(token=None, fetch=fetch))
    assert isinstance(result, ErrResult)
    assert "HUBSPOT_ACCESS_TOKEN" in result.error
    fetch.assert_not_called()


def test_missing_http_capability_errors():
    ctx = _ctx(fetch=_ok_fetch([]))
    ctx.http = None
    result = agent.execute("", ctx)
    assert isinstance(result, ErrResult)
    assert result.error == "HTTP capability unavailable"


def test_transport_error_is_reported():
    fetch = MagicMock(side_effect=RuntimeError("connection refused"))
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert result.error.startswith("HubSpot ticket search failed:")
    assert "connection refused" in result.error


def test_http_4xx_is_reported_with_status_and_body():
    fetch = MagicMock(return_value=_envelope(status=429, body="rate limited"))
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert "429" in result.error
    assert "rate limited" in result.error


def test_non_json_body_is_reported():
    fetch = MagicMock(return_value=_envelope(status=200, body="<html>nope</html>"))
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert "not JSON" in result.error
