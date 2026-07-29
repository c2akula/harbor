#!/usr/bin/env bash
# Bootstrap a fresh GPU box to serve vLLM from a persistent volume.
# Run inside tmux on the box; progress to ~/bootstrap.log.
#
# PREREQUISITE: the image must carry driver >= 580 / CUDA 13 — vLLM ships
# CUDA-13 binaries and crash-loops on older drivers only after loading
# weights. Many provider images are still on CUDA 12.x; check first.
#
# Everything durable lives on the volume: an instance's own ephemeral disk
# does not survive hibernation.
set -euo pipefail
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$HOME/bootstrap.log"; }
trap 'log "FAILED at line $LINENO"' ERR

KEY="${1:?usage: bootstrap.sh <model-api-key> [checkpoint-repo]}"
REPO="${2:-cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit}"
VOL=/weights

log "gpu + driver"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | cut -d. -f1)
if [ "${driver:-0}" -lt 580 ]; then
  log "DRIVER $driver IS TOO OLD — vLLM needs >= 580 (CUDA 13)."
  log "Provision a newer image, or upgrade in place then re-run:"
  log "  sudo apt-get update && sudo apt-get install -y cuda-drivers-580 && sudo reboot"
  log "(a 570->580 in-place upgrade hits a libnvidia file conflict; if so, add"
  log " -o Dpkg::Options::=--force-overwrite to the install)"
  exit 1
fi

log "persistent volume at $VOL"
if ! mountpoint -q "$VOL"; then
  log "$VOL is not mounted. Attach a volume, then (device is usually /dev/vdc):"
  log "  lsblk                                   # confirm it is EMPTY first"
  log "  sudo mkfs.ext4 -L harbor-weights /dev/vdX"
  log "  sudo mkdir -p $VOL && sudo mount /dev/vdX $VOL"
  log "  echo \"UUID=\$(sudo blkid -s UUID -o value /dev/vdX) $VOL ext4 defaults,nofail 0 2\" | sudo tee -a /etc/fstab"
  exit 1
fi
sudo chown "$(id -u):$(id -g)" "$VOL"
export HF_HOME="$VOL/hf" UV_CACHE_DIR="$VOL/uvcache"

log "uv"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

log "vllm into $VOL/venv — on the volume, so a resume needs no reinstall"
[ -d "$VOL/venv" ] || uv venv "$VOL/venv" --python 3.12
source "$VOL/venv/bin/activate"
uv pip install -q vllm==0.26.0
python -c "import vllm; print('vllm', vllm.__version__)" | tee -a "$HOME/bootstrap.log"

log "checkpoint: $REPO"
uv pip install -q "huggingface_hub[cli]"
target="$VOL/qwen-awq"
[ -d "$target" ] || hf download "$REPO" --local-dir "$target"
log "checkpoint at $target ($(du -sh "$target" | cut -f1))"

log "chat template: checkpoint's own, with the leading-system-merge shim"
# Clients may send a doctrine prefix as a second system message; the
# checkpoint template accepts a system message only in first position.
[ -f "$target/chat_template.jinja" ] || { log "no chat_template.jinja in checkpoint"; exit 1; }
cat > /tmp/harbor-shim.jinja <<'SHIM'
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
    {%- set messages = [{'role': 'system', 'content': _lead.texts | join('\n\n')}] + messages[_lead.n:] %}
{%- endif %}
SHIM
cat /tmp/harbor-shim.jinja "$target/chat_template.jinja" > "$VOL/chat-template.jinja"

log "seed systemd unit — 'harbor model' rewrites this when you switch models"
sudo tee /etc/systemd/system/vllm.service >/dev/null << UNIT
[Unit]
Description=vLLM serving from the persistent weights volume
RequiresMountsFor=$VOL
After=network-online.target

[Service]
User=$(id -un)
# The venv's bin must be on PATH, not merely the vllm entry point: vLLM shells
# out to ninja for JIT kernel compilation and ninja lives only in the venv.
# Without this the service loads weights and then crash-loops.
Environment=PATH=$VOL/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=HF_HOME=$VOL/hf
ExecStart=$VOL/venv/bin/vllm serve $target \\
  --served-model-name qwen qwen-coding qwen-explore --host 127.0.0.1 --port 8080 \\
  --chat-template $VOL/chat-template.jinja \\
  --api-key $KEY \\
  --enable-auto-tool-choice --tool-call-parser qwen3_xml \\
  --reasoning-parser qwen3 \\
  --enable-prefix-caching --mamba-cache-mode align \\
  --gpu-memory-utilization 0.95 --max-model-len 262144 --max-num-seqs 16
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now vllm

log "waiting for health (first load compiles kernels; several minutes)"
for _ in $(seq 1 150); do
  curl -s -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1 && { log "SERVING"; exit 0; }
  sleep 10
done
log "TIMEOUT — sudo journalctl -u vllm -n 50"
exit 1
