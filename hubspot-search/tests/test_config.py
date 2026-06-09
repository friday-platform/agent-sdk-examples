"""Unit tests for the hubspot-search agent's pure config-parsing helpers.

These functions carry the agent's trickiest logic — pulling a config envelope
out of a prompt that may also contain unrelated JSON — so they're worth testing
in isolation, with no AgentContext or HTTP involved.
"""

import json

import agent
from friday_agent_sdk import AgentContext, AgentInput

# --- small coercion helpers ---------------------------------------------


def test_coerce_int():
    assert agent._coerce_int(None, 7) == 7
    assert agent._coerce_int("10", 7) == 10
    assert agent._coerce_int(3.9, 7) == 3
    assert agent._coerce_int("not-a-number", 7) == 7


def test_clamp():
    assert agent._clamp(5, 1, 10) == 5
    assert agent._clamp(0, 1, 10) == 1
    assert agent._clamp(99, 1, 10) == 10


def test_coerce_str_list():
    assert agent._coerce_str_list(None) == []
    assert agent._coerce_str_list("solo") == ["solo"]
    assert agent._coerce_str_list(["a", " b ", ""]) == ["a", "b"]
    assert agent._coerce_str_list((1, 2)) == ["1", "2"]
    assert agent._coerce_str_list(5) == []


def test_pick_returns_first_present_non_null():
    assert agent._pick({"a": 1, "b": 2}, ["b", "a"]) == 2
    assert agent._pick({"a": None, "b": 3}, ["a", "b"]) == 3
    assert agent._pick({"a": 1}, ["x", "y"]) is None


# --- balanced-JSON scanning ---------------------------------------------


def test_scan_json_objects_extracts_each_object():
    assert agent._scan_json_objects('{"x": 1} noise {"y": 2}') == [{"x": 1}, {"y": 2}]


def test_scan_json_objects_handles_nesting_and_strings():
    assert agent._scan_json_objects('{"a": {"b": 1}}') == [{"a": {"b": 1}}]
    assert agent._scan_json_objects('{"s": "a{b}c"}') == [{"s": "a{b}c"}]
    assert agent._scan_json_objects(r'{"s": "a\"b"}') == [{"s": 'a"b'}]


def test_scan_json_objects_skips_invalid():
    assert agent._scan_json_objects("{not json}") == []
    assert agent._scan_json_objects("no objects here") == []


def test_scan_json_objects_recovers_object_after_invalid_prefix():
    # The real parser keeps scanning past an invalid `{`, recovering the valid
    # object that follows (a brace-counting scanner would skip the whole span).
    assert agent._scan_json_objects('{bad {"good": 1}') == [{"good": 1}]


# --- config envelope detection ------------------------------------------


def test_config_from_obj_accepts_wrapped_and_flat():
    assert agent._config_from_obj({"config": {"limit": 5}}) == {"limit": 5}
    assert agent._config_from_obj({"limit": 5}) == {"limit": 5}


def test_config_from_obj_rejects_unrelated():
    assert agent._config_from_obj({"signal": "cron"}) is None
    assert agent._config_from_obj({"config": {"signal": "cron"}}) is None
    assert agent._config_from_obj("not a dict") is None


# --- _resolve_config -----------------------------------------------------


def test_resolve_defaults_on_empty_prompt():
    stages, within, limit, props = agent._resolve_config("", AgentContext())
    assert stages == ["1"]
    assert within == 60
    assert limit == 100
    assert props == ["createdate", "hs_pipeline_stage"]


def test_resolve_reads_config_envelope_from_prompt():
    prompt = json.dumps(
        {
            "task": "x",
            "config": {
                "pipelineStages": ["5", "6"],
                "withinMinutes": 30,
                "limit": 7,
                "properties": ["subject"],
            },
        }
    )
    stages, within, limit, props = agent._resolve_config(prompt, AgentContext())
    assert stages == ["5", "6"]
    assert within == 30
    assert limit == 7
    assert props == ["subject"]


def test_resolve_clamps_out_of_range_values():
    _, within, limit, _ = agent._resolve_config(
        json.dumps({"config": {"limit": 99999, "withinMinutes": 999999}}), AgentContext()
    )
    assert limit == 10000  # _MAX_RESULTS (HubSpot's 10k search window)
    assert within == 7 * 24 * 60  # _MAX_WITHIN_MINUTES (7 days)

    _, within, limit, _ = agent._resolve_config(
        json.dumps({"config": {"limit": 0, "withinMinutes": 0}}), AgentContext()
    )
    assert limit == 1
    assert within == 1


def test_resolve_ignores_unrelated_envelope_and_picks_config():
    # The daemon may thread a signal envelope into the prompt alongside config.
    prompt = '{"signal": "cron"} {"config": {"limit": 5}}'
    _, _, limit, _ = agent._resolve_config(prompt, AgentContext())
    assert limit == 5


def test_resolve_falls_back_to_ctx_input_config():
    ctx = AgentContext()
    ctx.input = AgentInput({"config": {"limit": 5, "pipelineStages": ["9"]}})
    stages, _, limit, _ = agent._resolve_config("", ctx)
    assert stages == ["9"]
    assert limit == 5
