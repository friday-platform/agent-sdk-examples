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


def _decode(raw):
    sent = json.loads(raw)
    sent["body"] = json.loads(sent["body"])
    return sent


def _sent_request(fetch):
    """Decode the JSON request from the agent's most recent Http.fetch() call."""
    return _decode(fetch.call_args[0][0])


def _sent_request_at(fetch, index):
    """Decode the JSON request from the agent's nth Http.fetch() call."""
    return _decode(fetch.call_args_list[index][0][0])


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


def test_page_size_capped_at_200():
    # limit is a total (here 9999); each page still requests at most 200.
    fetch = _ok_fetch([_TICKET])  # one page, no cursor -> single request
    agent.execute(json.dumps({"config": {"limit": 9999}}), _ctx(fetch=fetch))
    assert _sent_request(fetch)["body"]["limit"] == 200


def test_emits_intent_before_searching():
    stream_emit = MagicMock()
    agent.execute("", _ctx(fetch=_ok_fetch([]), stream_emit=stream_emit))
    event_type, payload = stream_emit.call_args[0]
    assert event_type == "data-intent"
    assert "Searching HubSpot" in payload


def test_follows_pagination_cursor_across_pages():
    page1 = _envelope(
        body=_search_body([{"id": "1", "properties": {}}], paging={"next": {"after": "1"}})
    )
    page2 = _envelope(
        body=_search_body([{"id": "2", "properties": {}}], paging={"next": {"after": "2"}})
    )
    page3 = _envelope(body=_search_body([{"id": "3", "properties": {}}]))  # no paging -> last
    fetch = MagicMock(side_effect=[page1, page2, page3])

    result = agent.execute("", _ctx(fetch=fetch))

    assert isinstance(result, OkResult)
    assert result.data["ticketIds"] == ["1", "2", "3"]
    assert fetch.call_count == 3
    # the 2nd and 3rd requests carry the cursor from the previous page
    assert _sent_request_at(fetch, 1)["body"]["after"] == "1"
    assert _sent_request_at(fetch, 2)["body"]["after"] == "2"


def test_limit_caps_total_across_pages():
    # A page that always advertises a next cursor; limit stops the walk.
    page = _envelope(
        body=_search_body(
            [{"id": str(i), "properties": {}} for i in range(200)],
            paging={"next": {"after": "x"}},
        )
    )
    fetch = MagicMock(return_value=page)

    result = agent.execute(json.dumps({"config": {"limit": 5}}), _ctx(fetch=fetch))

    assert isinstance(result, OkResult)
    assert result.data["count"] == 5
    assert fetch.call_count == 1  # first page already exceeds the limit


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


def test_transport_error_retried_then_reported(monkeypatch):
    monkeypatch.setattr(agent, "_sleep", lambda _s: None)
    fetch = MagicMock(side_effect=RuntimeError("connection refused"))
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert result.error.startswith("HubSpot ticket search failed:")
    assert "connection refused" in result.error
    assert fetch.call_count == 1 + agent._MAX_RETRIES


def test_terminal_4xx_is_reported_without_retry():
    fetch = MagicMock(return_value=_envelope(status=400, body="bad filter"))
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert "400" in result.error
    assert "bad filter" in result.error
    assert fetch.call_count == 1  # 400 is terminal — not retried


def test_non_json_body_is_reported():
    fetch = MagicMock(return_value=_envelope(status=200, body="<html>nope</html>"))
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert "not JSON" in result.error


# --- retries & error classification -------------------------------------


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(agent, "_sleep", lambda _s: None)
    fetch = MagicMock(
        side_effect=[
            _envelope(status=429, headers={"Retry-After": "1"}, body='{"category": "RATE_LIMITS"}'),
            _envelope(body=_search_body([{"id": "1", "properties": {}}])),
        ]
    )
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, OkResult)
    assert result.data["ticketIds"] == ["1"]
    assert fetch.call_count == 2


def test_retry_after_header_is_honored(monkeypatch):
    slept = []
    monkeypatch.setattr(agent, "_sleep", lambda s: slept.append(s))
    fetch = MagicMock(
        side_effect=[
            _envelope(status=429, headers={"Retry-After": "7"}, body="{}"),
            _envelope(body=_search_body([])),
        ]
    )
    agent.execute("", _ctx(fetch=fetch))
    assert slept == [7.0]


def test_persistent_5xx_exhausts_retries(monkeypatch):
    monkeypatch.setattr(agent, "_sleep", lambda _s: None)
    fetch = MagicMock(return_value=_envelope(status=503, body="{}"))
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert "503" in result.error
    assert fetch.call_count == 1 + agent._MAX_RETRIES


def test_403_missing_scopes_fails_fast_and_surfaces_correlation_id(monkeypatch):
    slept = []
    monkeypatch.setattr(agent, "_sleep", lambda s: slept.append(s))
    fetch = MagicMock(
        return_value=_envelope(
            status=403,
            body=json.dumps(
                {
                    "category": "MISSING_SCOPES",
                    "correlationId": "abc-123",
                    "message": "token is missing required scopes",
                }
            ),
        )
    )
    result = agent.execute("", _ctx(fetch=fetch))
    assert isinstance(result, ErrResult)
    assert "MISSING_SCOPES" in result.error
    assert "abc-123" in result.error
    assert "token is missing required scopes" in result.error
    assert fetch.call_count == 1  # config error — not retried
    assert slept == []  # and never slept
