#!/usr/bin/env bash
# Install harbor.
#   ./install.sh                     — install from this checkout
#   ./install.sh <deployment-name>   — also sync deployments/<name>/config.toml
#   ./install.sh --join '<blob>'     — teammate onboarding: install, then
#                                      join the team's box (clones the repo
#                                      itself when piped from curl)
set -euo pipefail

blob=""
dep=""
case "${1:-}" in
  --join) blob="${2:?usage: install.sh --join '<blob>'}" ;;
  ?*)     dep="$1" ;;
esac

command -v uv >/dev/null || { echo "install.sh: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }

# When piped (curl | sh) there is no checkout to install from — make one.
SRC="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
if [ ! -f "$SRC/pyproject.toml" ]; then
  command -v git >/dev/null || { echo "install.sh: git is required" >&2; exit 1; }
  SRC="$HOME/.local/share/harbor"
  if [ -d "$SRC/.git" ]; then git -C "$SRC" pull --ff-only
  else git clone https://github.com/c2akula/harbor "$SRC"; fi
fi
cd "$SRC"

uv tool install --force --editable .

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

# Teammate path: the join blob carries everything else.
if [ -n "$blob" ]; then
  exec "$HOME/.local/bin/harbor" join "$blob"
fi

# config
install -d "$HOME/.config/harbor"
if [ -n "$dep" ]; then
  f="deployments/$dep/config.toml"
  [ -f "$f" ] || { echo "install.sh: no such deployment: $dep" >&2; exit 1; }
  install -m 644 "$f" "$HOME/.config/harbor/config.toml"
elif [ ! -f "$HOME/.config/harbor/config.toml" ]; then
  # First-time install: nothing to render units from, so stop cleanly and
  # name the command that produces a config.
  echo ""
  echo "harbor is installed, but not configured yet."
  echo "next: harbor init"
  exit 0
fi

# systemd units are rendered by the package from the active config
"$HOME/.local/bin/harbor" install-units

echo "installed. 'harbor status' to check, 'harbor crush check' for config drift."
echo "the GPU box's own serving unit is applied by cloud/bootstrap.sh."
