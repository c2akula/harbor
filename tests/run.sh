#!/usr/bin/env bash
# Test runner. Tiers by cost:
#   unit         no network, no VM, no money — run on every change (default)
#   integration  needs a live endpoint (harbor up first) — run before a release
#   lifecycle    starts/stops the real VM — run deliberately, it costs money
#
# Usage: tests/run.sh [unit|integration|lifecycle|all]
set -uo pipefail
cd "$(dirname "$0")/.."
tier="${1:-unit}"

run_tier() {
  local t="$1"
  [ -d "tests/$t" ] || { echo "· $t: no tests yet"; return 0; }
  echo "── $t ──────────────────────────────────────────"
  uv run python -m unittest discover -s "tests/$t" -p 'test_*.py' -v 2>&1 | tail -40
  return "${PIPESTATUS[0]}"
}

rc=0
case "$tier" in
  unit)        run_tier unit || rc=1 ;;
  integration) curl -s -m 3 http://127.0.0.1:"${TUNNEL_PORT:-8081}"/health | grep -q ok \
                 || { echo "integration tier needs a live endpoint — run harbor up" >&2; exit 2; }
               run_tier integration || rc=1 ;;
  lifecycle)   echo "lifecycle tier starts and stops the real VM (costs money)."
               read -r -p "continue? [y/N] " a; [ "$a" = "y" ] || exit 0
               run_tier lifecycle || rc=1 ;;
  all)         run_tier unit || rc=1; run_tier integration || rc=1 ;;
  *) echo "usage: tests/run.sh [unit|integration|lifecycle|all]" >&2; exit 2 ;;
esac
exit $rc
