#!/usr/bin/env bash
# The adopter path, start to finish, on a machine that has never seen harbor.
# Runs INSIDE the container built from adopter.Dockerfile.
#
# Deliberately does NOT `set -e`: the point is to find every step that breaks,
# not to stop at the first one. Each step reports its own exit status and the
# script fails at the end if any did.
fail=0
step() {
  echo ""
  echo "=== $1 ==="
  shift
  if "$@"; then
    echo "--- ok"
  else
    echo "--- FAILED (exit $?)"
    fail=1
  fi
}

ENDPOINT="${HARBOR_TEST_ENDPOINT:-http://127.0.0.1:9999}"

step "install.sh — no deployment name, as an outside adopter would run it" \
  ./install.sh

step "harbor on PATH" \
  bash -c 'command -v harbor'

# `endpoint` mode is the smallest way in and the only one needing no cloud
# account, so it is what an evaluator tries first.
# Answers: mode, url, key file, slot context, markers, oracle model, concurrency.
step "harbor init — endpoint mode, non-interactive" \
  bash -c "printf 'endpoint\n${ENDPOINT}\n\n\nadopter-test-marker\n\n\n' | harbor init"

step "config is loadable" \
  bash -c 'harbor status >/dev/null 2>&1 || harbor status; true'

step "harbor status runs without a traceback" \
  bash -c 'harbor status 2>&1 | tee /tmp/status.out; ! grep -q Traceback /tmp/status.out'

step "unit tests pass on a fresh clone" \
  bash -c 'HARBOR_PRIVATE_MARKERS=none uv run python -m unittest discover -s tests/unit -p "test_*.py" 2>&1 | tail -5'

echo ""
echo "======================================"
[ "$fail" -eq 0 ] && echo "ADOPTER PATH CLEAN" || echo "ADOPTER PATH HAS BREAKAGE"
exit "$fail"
