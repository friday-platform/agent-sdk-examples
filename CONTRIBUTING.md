# Contributing to Friday Agent SDK Examples

Thanks for your interest! This repo is a collection of self-contained example
agents built with the public
[`friday-agent-sdk`](https://pypi.org/project/friday-agent-sdk/). Contributions
that add a new example, improve an existing one, or fix a bug are welcome.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you agree to uphold it.

## Reporting bugs and requesting examples

- **Bug in an example:** open a [bug report](https://github.com/friday-platform/agent-sdk-examples/issues/new?template=bug_report.yml).
- **New example / improvement:** open a [feature request](https://github.com/friday-platform/agent-sdk-examples/issues/new?template=feature_request.yml)
  describing the use case before writing code.
- **Bug in the SDK itself** (not an example): report it in
  [friday-platform/agent-sdk](https://github.com/friday-platform/agent-sdk).
- **Security issues:** see [SECURITY.md](SECURITY.md) — do not open a public issue.

## Prerequisites

| Tool | Version | Why |
| --- | --- | --- |
| [Python](https://www.python.org/) | `3.12+` | Required by `friday-agent-sdk` |
| [uv](https://docs.astral.sh/uv/) | latest | Python package manager + runner |
| [Friday Studio](https://github.com/friday-platform/friday-studio) | latest | Provides the daemon to run an agent end-to-end (not needed for tests) |

## Setup

```bash
git clone https://github.com/friday-platform/agent-sdk-examples
cd agent-sdk-examples

# Each example is its own uv project:
cd hubspot-search
uv sync
```

Lint/format tooling is shared from the repo root ([`ruff.toml`](ruff.toml)).

## Running checks

Before opening a PR, run what CI runs
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

```bash
# Lint + format — whole repo, from the root
uv run ruff check .
uv run ruff format --check .

# Tests — from an example directory
cd hubspot-search && uv run pytest
```

## Adding a new example

Each example is a **top-level directory** containing:

- `agent.py` — a single, self-contained agent ending in
  `if __name__ == "__main__": run()`. Keep it self-contained — no helper modules.
- `README.md` — what it demonstrates, its input/output contract, and required env vars.
- `pyproject.toml` + `uv.lock` — declares `friday-agent-sdk` and a `pytest` dev group.
- `tests/` — a pytest suite that runs **without external credentials** (mock `ctx.http`).
- `metadata.json` — optional, for registration.

Then add a row to the **Examples** table in the root [`README.md`](README.md).

## Making changes

1. Fork and create a branch from `main`.
2. Keep changes focused — one logical change per PR.
3. Add or update tests for any behavioural change.
4. Update the example's `README.md` if you change its behaviour or contract.
5. Follow existing code style — `ruff` ([`ruff.toml`](ruff.toml)).
6. Write a clear commit message in
   [Conventional Commits](https://www.conventionalcommits.org/) style
   (e.g. `feat(hubspot-search): add pagination`).

> These examples are unreleased and unversioned — please **don't** bump the
> `version` field in `pyproject.toml` / `metadata.json` / the `@agent` decorator.

## Submitting a pull request

- Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md) — fill in the summary
  and test plan.
- Link related issues with `Closes #123`.
- Make sure CI is green before requesting review.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
