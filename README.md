# harbor — a private, guarded coding harness

Run a strong open-weight coding model as your daily agentic driver on
infrastructure **you** control, with an optional, tightly firewalled
escalation path to a frontier model. Proprietary code never leaves machines
you administer.

harbor is the harness, not the model server: it owns your agent's
configuration, enforces policy on what the agent may do, runs deterministic
workflows, and (optionally) manages the GPU box's lifecycle and cost.

## Three ways to run it

| Mode | You have | harbor does |
|---|---|---|
| `endpoint` | a model server already | just the harness — no cloud account |
| `hyperstack` | nothing yet | rents and manages a GPU box, parks it when idle |
| `provider` | another cloud | manages your box through a class you implement |

## Quickstart (endpoint mode, ~5 minutes)

```
git clone <this repo> && cd harbor
./install.sh
harbor init        # a guided wizard: validates as you answer,
                   # ends with your next command
crush run "hello"
```

## Why trust it with your code

The primary model runs on your own machines over your tailnet — zero public
ingress, per-user API keys. The optional frontier "oracle" is blind: no file
tools, a neutral working directory, and every outgoing question is checked
against your repository's own content before it may leave. Details and
honest non-goals: [docs/security.md](docs/security.md).

## Documentation

| Page | For |
|---|---|
| [docs/security.md](docs/security.md) | the threat model — why harbor exists |
| [docs/setup-endpoint.md](docs/setup-endpoint.md) | you already run a server |
| [docs/setup-managed.md](docs/setup-managed.md) | harbor runs the box |
| [docs/operating.md](docs/operating.md) | day to day: lifecycle, keys, effort, capacity |
| [docs/workflows.md](docs/workflows.md) | Crush agents vs `harbor flow` |
| [docs/extending.md](docs/extending.md) | bring your own provider |

## Good to know

- One 48 GB card serves about five subagent-heavy developers at once;
  see [operating](docs/operating.md) for the envelope and levers.
- The test suite (`tests/run.sh`) needs no network, no VM, no money.
- CI's primary gate is a path allowlist — unknown files fail the build.

## Licence

Apache-2.0 — see [LICENSE](LICENSE).
