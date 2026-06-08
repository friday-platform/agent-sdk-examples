# agent-sdk-examples

Worked examples of building Friday agents with the public Python
[`friday-agent-sdk`](https://pypi.org/project/friday-agent-sdk/). Each top-level
directory is a standalone, registerable agent you can read, run, and copy as a
starting point.

## The programming model

A Friday agent is a single Python program. You decorate one handler with
`@agent(...)`, return a result with `ok(...)` / `err(...)`, and call `run()` in
`__main__`. The SDK handles the transport (NATS): the daemon spawns the process
per call, hands your handler the `prompt` and an `AgentContext`, and serializes
whatever you return.

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

- `ctx.http` — outbound HTTP (`ctx.http.fetch(url, method=..., headers=..., body=..., timeout_ms=...)`), raises `HttpError`.
- `ctx.llm` — LLM calls, when an example needs a model in the loop.
- `ctx.stream` — progress events, e.g. `ctx.stream.intent("Searching…")`.
- `ctx.env` — resolved environment values for whatever the decorator declared under `environment`.
- `ctx.input` — structured input wired from the workspace/FSM (e.g. `ctx.input.config`).

Capabilities (`http`, `llm`, `stream`) can be `None` when not granted, so guard
before use — the examples do.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (recommended) for per-example environments
- `friday-agent-sdk>=0.1.8` (declared by each example's `pyproject.toml`)
- A running Friday daemon + NATS to actually execute an agent

## Running an example

```bash
cd hubspot
uv sync                       # creates .venv with friday-agent-sdk

# Register with a local Friday daemon (connects over NATS, handles one call):
curl -X POST http://localhost:8080/api/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"path": "'"$(pwd)"'/agent.py"}'
```

Each example's own `README.md` documents its input/output contract and any
environment variables it needs.

## Examples

| Example | What it shows |
| --- | --- |
| [`hubspot`](hubspot) | A **deterministic** agent (no LLM): read config from the prompt, make one authenticated REST call with `ctx.http`, return a structured result. |

More to come — each new example lands as its own top-level directory.

## License

[MIT](LICENSE) © Tempest Labs, Inc.
