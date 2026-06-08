# Review: hubspot-agent

**Date:** 2026-06-08
**Branch:** main (no feature branch; reviewing the `hubspot/` agent + tests as they stand on HEAD `371dc8b`)
**Verdict:** Needs Work → **Resolved** (v1.1.1)

> **Update (2026-06-08):** All findings below were fixed in v1.1.1 — the
> pagination loop now breaks on a no-progress/empty page and an explicit 10k
> bound; the misleading comment is gone; `_retry_after` handles the RFC-7231
> HTTP-date form; and regression tests cover the empty-page-with-cursor hang and
> the malformed-`Retry-After` fallback (43 tests, all green). The two
> "Needs Decision" items (redundant `response` field, unsurfaced `properties`
> knob) were left for the author and are unchanged.

## Summary

The `hubspot` example agent (single-file `friday_agent_sdk` agent: paginated
ticket search with retries and structured errors) is well-architected — clean
separation of pure helpers from I/O, a correct HTTP-boundary test seam, careful
token hygiene, and ~97% test coverage that protects real behavior. One **Critical
correctness bug** blocks merge: the pagination loop can spin forever (verified:
1000+ HTTP calls) when a page returns zero tickets but still advertises a cursor.
Everything else is trim-to-taste or a documented design decision.

## Critical

### Pagination loop hangs on an empty page that still has a cursor
**Location:** `hubspot/agent.py:458-471` (loop), false-assurance comment at `455-457`
**Problem:** The loop exits only when `len(tickets) >= limit` or `after is None`.
A page with `results: []` but a non-null `paging.next.after` advances neither
condition → unbounded re-POSTing of the search.
**Evidence:** Verified by direct execution — a stubbed `http_fetch` returning
`{"results": [], "paging": {"next": {"after": "x"}}}` produced **1000+** calls
(harness tripwire), never terminating on its own. Two realistic triggers: (1) a
page whose rows are all malformed/missing `id`, so `_extract_tickets` returns
`[]` while `_next_after` still yields a cursor; (2) HubSpot legitimately
returning a permission/filter-trimmed empty page mid-window. The comment at
`455-457` — *"`limit` is already clamped to the 10k window, so following the
cursor can never page past it"* — is the load-bearing false assumption:
`_MAX_RESULTS` clamps `limit` (`agent.py:218`) but never bounds the loop.
**Recommendation:** Break when a page yields zero new tickets (`if not
page_tickets: break`) — cheapest fix, also handles the legitimate empty-page case
— and/or add an independent iteration cap (`ceil(_MAX_RESULTS / _PAGE_SIZE)` = 50).
Add a regression test (see Tests) and let it drive the fix.
**Worth doing: Yes** — it's a production hang that hammers a rate-limited (~5 req/s)
endpoint; traces directly to this work; fix is ~1 line.

## Important

### The 10k search window is not actually bounded; comment over-claims
**Location:** `hubspot/agent.py:218, 455-458`
**Problem:** Clamping `limit` to 10000 doesn't keep the agent inside HubSpot's
10,000-record paging window — the window is on the cursor offset, not the
requested total. At `limit=10000` the final page requests an offset at/near the
boundary, which HubSpot answers with a 400.
**Evidence:** Same root cause as the Critical: "clamped to 10k" is treated as
"safe to page." Degrades to a handled `_error_message` 400 (not a crash), but the
agent returns an error instead of the ~10k results it could have returned.
**Recommendation:** Bound the loop independently (the Critical fix covers this),
clamp `limit` to `_MAX_RESULTS - _PAGE_SIZE`, and delete the "can never page past
it" comment.
**Worth doing: Yes** — bundles into the Critical fix at near-zero extra cost; the
misleading comment is actively harmful to the next reader.

### `Retry-After` only parses delta-seconds, not the HTTP-date form
**Location:** `hubspot/agent.py:338-346`
**Problem:** RFC 7231 allows `Retry-After` as seconds *or* an HTTP-date.
`float(raw)` handles only seconds; a date raises `ValueError` → `None` → falls
back to backoff. The docstring ("Seconds to wait…") overstates coverage.
**Evidence:** HubSpot sends delta-seconds in practice, so this is a *safe
degradation*, not a live bug.
**Worth doing: No** (fix), **Yes** (one-word docstring honesty + a test for the
fallback branch — see Tests). Cost of not fixing: nil for HubSpot; parsing the
date branch would be speculative generality for this example.

### Minor speculative surface (demoted — pre-existing, harmless)
**Location:** `hubspot/agent.py:124-132` (snake_case config aliases), `409`
(unreachable "retries exhausted" return)
**Problem:** `_CONFIG_KEYS`/`_pick` accept `pipeline_stages`/`within_minutes`
aliases nothing in this repo emits; line 409 is unreachable.
**Worth doing: No.** The aliases are from the initial port (not this change) and
are harmless tolerance, not the author's mess to clean up here. The line-409
return is a reasonable total-function guard (keeps control flow obvious to readers
and type-checkers) — I'd *keep* it; deleting it is churn, not simplification.
(This is a deliberate disagreement with the reviewing agent's "delete it" call.)

## Tests

Verdict from the test lens: **Solid.** Mocking is at the correct seam
(`http_fetch` only), assertions are on agent behavior (returned `OkResult`/
`ErrResult` data + the decoded outbound request) not mock internals, the `_sleep`
monkeypatch isolates retry logic without hiding it (one test captures slept values
to prove `Retry-After` is honored), ~1:1 test-to-impl ratio, ~97% line coverage.
The `before/after` epoch-ms window assertions are robust (bounded by captured
timestamps, not fixed tolerances), and the unit/integration overlap is intentional
layering. Two gaps:

1. **No test for the empty-page-with-cursor case** (`test_execute.py` pagination
   section). Every pagination test returns ≥1 result per page, so the suite
   structurally cannot catch the Critical hang above. **Worth doing: Yes** — this
   is the highest-value missing test; write it first (assert a bounded
   `fetch.call_count` / use a `side_effect` list that raises if exceeded) and let
   it drive the loop fix.
2. **Malformed/absent `Retry-After` fallback untested** (`agent.py:344-346`
   uncovered). A regression to `int(raw)` or removing the `except` would convert a
   transient 429 into a hard failure, uncaught. **Worth doing: Yes** — cheap: 429
   with `Retry-After: "soon"` then success, assert the slept value lands in the
   `_backoff(0)` range (`0.5–1.0s`), locking both the parse branch and the `or`
   fallback.

Not worth adding: a standalone `limit == 200` boundary test (already bracketed by
`test_page_size_capped_at_200` + `test_limit_caps_total_across_pages`) or a direct
`_backoff` jitter test (would border on testing `random.uniform`).

## Needs Decision

1. **Redundant `response` field** (`agent.py:478-485`). The result carries the IDs
   three ways: `response` (a JSON *string* of the IDs), `ticketIds` (the same list),
   and `tickets` (their source dicts); `count` is derivable too. If the Friday host
   contract requires a top-level string `response`, keep it and say so in the
   docstring; if not, drop `response` and keep the structured fields. Pre-existing
   from the initial port — author's call.
2. **`properties` config is plumbed but never surfaced** (`agent.py:218` →
   `_extract_tickets` only emits `id`/`createdAt`/`pipelineStage`). A caller setting
   `"properties": ["subject"]` pays for the larger HubSpot response but never sees
   `subject`. Either thread requested properties into the output `tickets`, or drop
   the config knob to avoid the trap.
