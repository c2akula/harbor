#!/usr/bin/env bash
# Install harbor. Usage: ./install.sh [deployment-name]
#   with a name: sync deployments/<name>/config.toml into place (repo is
#   source of truth), then render systemd units from it.
set -euo pipefail
cd "$(dirname "$0")"

command -v uv >/dev/null || { echo "install.sh: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }
uv tool install --force --editable .

# config
install -d "$HOME/.config/harbor"
if [ -n "${1:-}" ]; then
  dep="deployments/$1/config.toml"
  [ -f "$dep" ] || { echo "install.sh: no such deployment: $1" >&2; exit 1; }
  install -m 644 "$dep" "$HOME/.config/harbor/config.toml"
elif [ ! -f "$HOME/.config/harbor/config.toml" ]; then
  # First-time install: nothing to render units from, so stop cleanly and
  # name the command that produces a config.
  echo ""
  echo "harbor is installed, but not configured yet."
  echo "next: harbor init"
  exit 0
fi

# Crush integration: the bash-policy PreToolUse hook + Crush-only skills
install -d "$HOME/.config/crush/hooks" "$HOME/.config/crush/skills"
install -m 755 config/hooks/bash-policy.py "$HOME/.config/crush/hooks/"
for d in config/skills/*/; do
  name="$(basename "$d")"
  [ "$name" = oracle-protocol ] && continue          # shared, installed below
  install -d "$HOME/.config/crush/skills/$name"
  install -m 644 "$d"SKILL.md "$HOME/.config/crush/skills/$name/"
done

# oracle-protocol skill is shared with Claude Code
install -d "$HOME/.claude/skills/oracle-protocol"
install -m 644 config/skills/oracle-protocol/SKILL.md "$HOME/.claude/skills/oracle-protocol/"

# systemd units are rendered by the package from the active config
"$HOME/.local/bin/harbor" install-units

echo "installed. 'harbor status' to check, 'harbor crush check' for config drift."
echo "the GPU box's own serving unit is applied by cloud/bootstrap.sh."
