# Setup: endpoint mode

You already run an OpenAI-compatible model server. harbor adds the harness —
Crush ownership, policy hooks, workflows, guarded escalation — and touches no
machine. **No cloud account needed.**

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- [Crush](https://github.com/charmbracelet/crush) (the wizard offers to help)
- A model server answering `/v1/chat/completions` (vLLM, llama.cpp, Ollama…)

## Steps

```
git clone <this repo> && cd harbor
./install.sh
harbor init          # pick: endpoint
```

The wizard walks the rest: it probes your endpoint, checks your key file,
requires the confidentiality markers, offers to sync Crush, and ends with
the exact next command.

## What you end up with

| Piece | Where |
|---|---|
| config | `~/.config/harbor/config.toml` |
| Crush provider + policy hooks | `~/.config/crush/` |
| oracle guard | active — consults are checked against your repo before leaving |

## Try it

```
crush run "hello"
harbor status        # endpoint reachability + serving model, any time
```

## Notes

- `harbor up/down/model` stand down in this mode — there is no machine to
  manage. Everything else works.
- Thinking effort is a config label (`[endpoint] effort`, `none|low|medium|
  high|max`); re-run `harbor crush sync` after changing it.
- Serving vLLM yourself? The unit hints in
  [setup-managed.md](setup-managed.md) apply even when harbor doesn't manage
  the box.
