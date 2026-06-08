# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in one of these example
agents, please report it privately. **Do not open a public GitHub issue.**

- Preferred: open a [private security advisory](https://github.com/friday-platform/agent-sdk-examples/security/advisories/new).
- Or email **security@hellofriday.ai**.

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (proof-of-concept, affected example, environment)
- Any suggested mitigation

We will acknowledge your report within 3 business days and provide an estimated
timeline for a fix. Please give us a reasonable window to investigate and patch
before any public disclosure.

## Supported Versions

These are example agents, not a released package. Security fixes are applied to
the latest state of `main`; there are no versioned releases to back-port to.

## Scope

In scope:

- The example agents in this repository (e.g. `hubspot/`) and their CI/tooling.

Out of scope — please report to the appropriate project:

- The `friday-agent-sdk` package itself — see [friday-platform/agent-sdk](https://github.com/friday-platform/agent-sdk).
- The Friday daemon / runtime — see [friday-platform/friday-studio](https://github.com/friday-platform/friday-studio).
- Third-party dependencies — please report directly to the upstream project.

## A note on credentials

Each example talks to a third-party API (e.g. HubSpot) using a token you supply
through the environment. The agents read it from `ctx.env`, never from source.
**Never commit real tokens or customer data** — `.env` files and credentials are
gitignored, and issue/PR reports should be redacted.
