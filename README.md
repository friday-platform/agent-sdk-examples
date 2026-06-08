# Friday Agent SDK — Examples

[![CI](https://github.com/friday-platform/agent-sdk-examples/actions/workflows/ci.yml/badge.svg)](https://github.com/friday-platform/agent-sdk-examples/actions/workflows/ci.yml)
[![friday-agent-sdk](https://img.shields.io/pypi/v/friday-agent-sdk.svg?label=friday-agent-sdk)](https://pypi.org/project/friday-agent-sdk/)
[![Python versions](https://img.shields.io/pypi/pyversions/friday-agent-sdk.svg)](https://pypi.org/project/friday-agent-sdk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **These examples track the [`friday-agent-sdk`](https://pypi.org/project/friday-agent-sdk/) (alpha) — APIs may change.**
> Pin an exact SDK version when you copy one of these into your own project.

Runnable example agents built with the [Friday Agent SDK](https://github.com/friday-platform/agent-sdk).
Each one is a standalone, registerable agent you can read, run, and copy as the
starting point for your own. The host manages credentials and routes LLM, HTTP,
and MCP calls on the agent's behalf, so your code stays a plain Python function
— no provider SDKs, no key plumbing.

- **SDK reference & guides:** [friday-platform/agent-sdk](https://github.com/friday-platform/agent-sdk)
- **SDK on PyPI:** [`friday-agent-sdk`](https://pypi.org/project/friday-agent-sdk/)
- **Friday platform docs:** https://docs.hellofriday.ai/
- **Daemon & `atlas` CLI:** [friday-platform/friday-studio](https://github.com/friday-platform/friday-studio)

## Requirements

The SDK is not standalone — an agent runs inside the Friday host:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for per-example environments
- A running [Friday daemon](https://github.com/friday-platform/friday-studio)
  (provides the host runtime and the `atlas` CLI)
- Any credentials an example declares (e.g. `HUBSPOT_ACCESS_TOKEN`), configured
  in the daemon's environment — agents never see raw keys directly

## The programming model

A Friday agent is a single Python program. You decorate one handler with
`@agent(...)`, return a result with `ok(...)` / `err(...)`, and call `run()` in
`__main__`. The SDK handles the transport: the host spawns the process per call,
hands your handler the `prompt` and an `AgentContext`, and serializes whatever
you return.

```python
from friday_agent_sdk import AgentContext, agent, err, ok, run

@agent(
    id="my-agent",
    version="1.0.0",
    description="What this agent does.",
    environment={"required": [{"name": "SOME_TOKEN", "description": "..."}]},
)
def execute(prompt: str, ctx: AgentContext):
    if ctx.http is None:
        return err("HTTP capability unavailable")
    # ...do work...
    return ok({"response": "..."})

if __name__ == "__main__":
    run()
```

What the `AgentContext` gives you:

| Field | Use |
| --- | --- |
| `ctx.http` | Outbound HTTP — `ctx.http.fetch(url, method=..., headers=..., body=..., timeout_ms=...)`, raises `HttpError` |
| `ctx.llm` | LLM calls, when an example needs a model in the loop |
| `ctx.stream` | Progress events, e.g. `ctx.stream.intent("Searching…")` |
| `ctx.env` | Resolved values for whatever the decorator declared under `environment` |
| `ctx.input` | Structured input wired from the workspace (e.g. `ctx.input.config`) |

Capabilities (`http`, `llm`, `stream`) can be `None` when not granted, so guard
before use — the examples do.

## Quick start

```bash
cd hubspot
uv sync                       # create .venv from the example's uv.lock

# Register with a local Friday daemon (it then handles one execute call):
curl -X POST http://localhost:8080/api/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"path": "'"$(pwd)"'/agent.py"}'
```

Each example's own `README.md` documents its input/output contract and any
environment variables it needs.

## Examples

| Example | What it shows |
| --- | --- |
| [`hubspot`](hubspot) | A **deterministic** agent (no LLM): read config from the prompt, make one authenticated REST call with `ctx.http`, and return a structured result. |

More to come — each new example lands as its own top-level directory.

## Contributing

A new example is a top-level directory with its own `agent.py`, `README.md`,
`pyproject.toml`, and `uv.lock`. Lint and format are shared from the repo root
([`ruff.toml`](ruff.toml)) and enforced in CI:

```bash
uv run ruff check .
uv run ruff format .
```

## License

[MIT](LICENSE) © Tempest Labs, Inc.
