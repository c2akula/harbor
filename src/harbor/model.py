"""Switch which checkpoint the box serves.

harbor provisions vLLM, not llama.cpp. That is a measured choice, not a
preference: under agent fan-out llama.cpp's static slots go *backwards*
(4 concurrent agents finished slower than running them one at a time) while
vLLM's continuous batching gave 7.5x on the same card. Anyone wanting
subagents or several users needs vLLM, so harbor targets it exclusively —
`[endpoint] url` covers people who run their own server instead.

The systemd unit is a RENDERED ARTIFACT: generated wholesale from config and
the chosen model, written atomically over ssh — never line-edited.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from .config import Config

# Weights and venv live on the persistent volume: it survives hibernation
# where the instance's own ephemeral disk does not (verified — 24GB of weights
# intact across a park/resume that wiped /ephemeral in the same cycle).
VOLUME = "/weights"
UNIT_PATH = "/etc/systemd/system/vllm.service"

# Not optional: without these, tool calls 400 and thinking leaks into visible
# output. Rendered into the unit rather than left to config, so they cannot
# be omitted.
AGENT_FLAGS = (
    "--enable-auto-tool-choice --tool-call-parser qwen3_xml "
    "--reasoning-parser qwen3"
)

# Every model id a client may ask for — vLLM rejects unknown ids, and the
# Explore/Execute providers each request their own. First name is the one
# reported back in responses. Must stay a superset of what crush.py requests
# (contract-tested).
SERVED_NAMES = "qwen qwen-coding qwen-explore"

# Prepended to the checkpoint's own chat template. Crush sends the mode
# doctrine (system_prompt_prefix) as a second system message; the checkpoint
# template accepts a system message only in first position. Consecutive
# leading system messages are merged into one; a system message after any
# other role still hits the template's own rejection.
#
# Content arrives in two shapes: a plain string, or — as the server hands it
# to the template for multimodal-capable models — a list of typed parts.
# Text-only shapes merge; anything carrying a non-text part is left for the
# checkpoint template's own handling (vision payloads are never doctrine).
TEMPLATE_SHIM = """\
{%- set _lead = namespace(n=0, texts=[]) %}
{%- for m in messages %}
    {%- if loop.index0 == _lead.n and m.role == 'system' %}
        {%- if m.content is string %}
            {%- set _lead.texts = _lead.texts + [m.content] %}
            {%- set _lead.n = _lead.n + 1 %}
        {%- elif m.content is iterable and m.content is not mapping and (m.content | rejectattr('text', 'defined') | list | length) == 0 %}
            {%- set _lead.texts = _lead.texts + [m.content | map(attribute='text') | join('')] %}
            {%- set _lead.n = _lead.n + 1 %}
        {%- endif %}
    {%- endif %}
{%- endfor %}
{%- if _lead.n > 1 %}
    {%- set messages = [{'role': 'system', 'content': _lead.texts | join('\\n\\n')}] + messages[_lead.n:] %}
{%- endif %}
"""


@dataclass(frozen=True)
class ModelSpec:
    checkpoint: str      # directory under VOLUME, not a GGUF file
    max_model_len: int   # must be >= what the client advertises
    max_num_seqs: int    # concurrent sequences; vLLM batches continuously
    note: str = ""


KNOWN = {
    # AWQ 4-bit beats FP8 on BOTH axes on a 46GB card: ~40% faster and 3x the
    # KV cache (846k vs 281k tokens). This is a 3B-active MoE, so decode is
    # memory-bandwidth bound and halving the weight bytes beats FP8's compute
    # advantage. No measurable quality cost on sealed reference tests.
    # 262144 is the checkpoint's native window, served in full.
    "qwen35-awq": ModelSpec("qwen-awq", 262144, 16, "AWQ 4-bit — the default"),
    "qwen35-fp8": ModelSpec("qwen-fp8", 262144, 16, "FP8 — more VRAM, less KV"),
}


def render_unit(cfg: Config, spec: ModelSpec,
                api_keys: list[str] | None = None) -> str:
    """The complete systemd unit. RequiresMountsFor keeps systemd from starting
    the server before the weights volume is mounted.

    Binds the box's tailnet address: clients talk to the server directly and
    the tailnet is the security boundary. Every issued per-user key is a valid
    --api-key value; revocation is a re-render without it.
    """
    key_list = " ".join(api_keys) if api_keys else \
        cfg.model_key_file.read_text().strip()
    return f"""[Unit]
Description=vLLM serving {spec.checkpoint} from the persistent weights volume
RequiresMountsFor={VOLUME}
After=network-online.target tailscaled.service

[Service]
User={cfg.ssh_user}
# The venv's bin must be on PATH, not merely the vllm entry point: vLLM shells
# out to `ninja`, which lives only in the venv; without it the server
# crash-loops after loading weights.
Environment=PATH={VOLUME}/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=HF_HOME={VOLUME}/hf
ExecStart={VOLUME}/venv/bin/vllm serve {VOLUME}/{spec.checkpoint} \\
  --served-model-name {SERVED_NAMES} --host {cfg.vm_ip} --port 8080 \\
  --chat-template {VOLUME}/chat-template.jinja \\
  --api-key {key_list} \\
  {AGENT_FLAGS} \\
  --enable-prefix-caching --mamba-cache-mode align \\
  --gpu-memory-utilization 0.95 \\
  --max-model-len {spec.max_model_len} --max-num-seqs {spec.max_num_seqs}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def remote_script(spec: ModelSpec, name: str = "",
                  host: str = "127.0.0.1") -> str:
    """What runs on the box once the unit has been streamed to it over stdin.
    Enables as well as restarts: restart alone leaves a disabled unit disabled,
    and the box then comes back dead after the next park. The current-model
    marker lets a key change re-render this unit without being told the model.
    """
    return (
        f"test -d '{VOLUME}/{spec.checkpoint}' || {{ echo 'checkpoint missing "
        f"on the volume: {VOLUME}/{spec.checkpoint}' >&2; exit 1; }}\n"
        f"test -f '{VOLUME}/{spec.checkpoint}/chat_template.jinja' || "
        f"{{ echo 'checkpoint has no chat_template.jinja' >&2; exit 1; }}\n"
        f"cat > /tmp/harbor-shim.jinja <<'SHIM'\n{TEMPLATE_SHIM}SHIM\n"
        f"cat /tmp/harbor-shim.jinja "
        f"'{VOLUME}/{spec.checkpoint}/chat_template.jinja' "
        f"> {VOLUME}/chat-template.jinja\n"
        + (f"echo '{name}' > {VOLUME}/current-model\n" if name else "")
        + f"sudo tee {UNIT_PATH} >/dev/null\n"
        f"sudo chmod 600 {UNIT_PATH}\n"      # the unit embeds API keys
        "sudo systemctl daemon-reload\n"
        "sudo systemctl enable vllm\n"
        "sudo systemctl restart vllm\n"
        # Health on the BOUND address: a tailnet-bound server does not answer
        # on loopback.
        f"until curl -s -m 2 http://{host}:8080/health >/dev/null 2>&1; "
        "do sleep 5; done\n"
    )


def switch(cfg: Config, name: str) -> int:
    spec = KNOWN.get(name)
    if spec is None:
        print(f"harbor model: unknown model '{name}'. Known: "
              f"{', '.join(KNOWN)}", file=sys.stderr)
        return 2

    if spec.max_model_len < cfg.slot_context:
        print(f"note: {name} serves {spec.max_model_len} context but config "
              f"advertises slot_context {cfg.slot_context}; clients would be "
              "told a window the server will not honour.", file=sys.stderr)

    # The serving key list comes from the box; seed it with the operator's
    # own key so the first render after adopting per-user keys locks no one
    # out.
    from . import keys as keys_mod
    if not keys_mod.list_names(cfg):
        own = cfg.model_key_file.read_text().strip()
        keys_mod._run(cfg, f"umask 077 && echo '{own}' > operator.key")
    api_keys = keys_mod.tokens(cfg)

    r = subprocess.run(
        ["ssh", "-i", str(cfg.ssh_key), "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new",
         f"{cfg.ssh_user}@{cfg.vm_ip}",
         remote_script(spec, name, host=cfg.vm_ip)],
        input=render_unit(cfg, spec, api_keys), text=True,
    )
    if r.returncode != 0:
        return r.returncode
    print(f"serving: {spec.checkpoint} "
          f"({spec.max_num_seqs} concurrent · {spec.max_model_len} ctx)"
          f"{' — ' + spec.note if spec.note else ''}")
    return 0


def rerender(cfg: Config) -> int:
    """Re-apply the unit for whatever the box currently serves — the key
    add/revoke path, which changes the list without changing the model."""
    from . import keys as keys_mod
    current = keys_mod._run(cfg, f"cat {VOLUME}/current-model 2>/dev/null "
                            "|| true").strip()
    if current not in KNOWN:
        print("harbor: cannot tell what the box serves — run "
              f"'harbor model <name>' once (known: {', '.join(KNOWN)})",
              file=sys.stderr)
        return 1
    return switch(cfg, current)
