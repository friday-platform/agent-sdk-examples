"""Unit tests for the hubspot-search agent's request-body builder and result parser."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import agent
from friday_agent_sdk import HttpResponse


def test_build_search_body_shape():
    body = agent._build_search_body(["1", "2"], 1700000000000, 50, ["createdate"])
    assert body["filterGroups"] == [
        {
            "filters": [
                {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": ["1", "2"]},
                {"propertyName": "createdate", "operator": "GTE", "value": "1700000000000"},
            ]
        }
    ]
    assert body["sorts"] == [{"propertyName": "createdate", "direction": "DESCENDING"}]
    assert body["properties"] == ["createdate"]
    assert body["limit"] == 50


def test_build_search_body_uses_string_epoch_for_createdate():
    # HubSpot's v3 search filters datetime properties by epoch-ms as a string.
    body = agent._build_search_body(["1"], 1700000000000, 10, ["createdate"])
    value = body["filterGroups"][0]["filters"][1]["value"]
    assert value == "1700000000000"
    assert isinstance(value, str)


def test_build_search_body_includes_after_cursor_only_when_given():
    assert "after" not in agent._build_search_body(["1"], 1, 10, ["createdate"])
    body = agent._build_search_body(["1"], 1, 10, ["createdate"], after="200")
    assert body["after"] == "200"


def test_next_after_reads_paging_cursor():
    assert agent._next_after({"paging": {"next": {"after": "100"}}}) == "100"
    assert agent._next_after({"paging": {"next": {"after": 100}}}) == "100"
    assert agent._next_after({"results": []}) is None  # no paging -> last page
    assert agent._next_after({"paging": {}}) is None
    assert agent._next_after(None) is None


def test_extract_tickets_maps_fields_and_skips_bad_rows():
    data = {
        "results": [
            {"id": " 512 ", "properties": {"createdate": "c", "hs_pipeline_stage": "1"}},
            {"id": 777, "properties": {}},  # numeric id, no requested props
            {"id": ""},  # blank id -> skipped
            "not-a-dict",  # skipped
            {"properties": {"createdate": "c"}},  # no id -> skipped
        ]
    }
    assert agent._extract_tickets(data) == [
        {"id": "512", "createdAt": "c", "pipelineStage": "1"},
        {"id": "777", "createdAt": "", "pipelineStage": ""},
    ]


def test_extract_tickets_tolerates_malformed_payloads():
    assert agent._extract_tickets(None) == []
    assert agent._extract_tickets({}) == []
    assert agent._extract_tickets({"results": "nope"}) == []
    assert agent._extract_tickets({"results": []}) == []


def _resp(headers):
    return HttpResponse(status=429, headers=headers, body="")


def test_retry_after_parses_delta_seconds():
    assert agent._retry_after(_resp({"Retry-After": "7"})) == 7.0


def test_retry_after_absent_or_malformed_is_none():
    assert agent._retry_after(_resp({})) is None
    assert agent._retry_after(_resp({"Retry-After": "soon"})) is None


def test_retry_after_parses_http_date():
    # Header name is matched case-insensitively; an HTTP-date is RFC-7231-valid.
    future = datetime.now(tz=UTC) + timedelta(seconds=120)
    secs = agent._retry_after(_resp({"retry-after": format_datetime(future, usegmt=True)}))
    assert secs is not None
    assert 0 < secs <= 120  # ~120s ahead
    past = datetime.now(tz=UTC) - timedelta(seconds=120)
    assert agent._retry_after(_resp({"Retry-After": format_datetime(past, usegmt=True)})) == 0.0
