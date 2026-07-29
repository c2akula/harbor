#!/usr/bin/env bash
# PostToolUse(Bash) hook: mechanical oracle-escalation trigger for redirected
# (claude-local) sessions. The local model's own stuck-detection fires late, so
# this counts consecutive failures of the SAME command and injects a nudge at 3.
#
# Scope: does nothing unless ANTHROPIC_BASE_URL is set (i.e. a local backend).
# KNOWN LIMITATION: headless `claude -p` does NOT run
# PostToolUse on a FAILED tool call — only on success — so this nudge is
# effectively INTERACTIVE-ONLY (claude-local pairing, the qualified mode).
# In headless/batch autonomous runs it will not fire; that mode is disqualified
# for the 35B anyway. tool_response carries no exit_code field, so failure is
# detected from output markers across stdout+stderr.
set -uo pipefail
[ -z "${ANTHROPIC_BASE_URL:-}" ] && exit 0

input=$(cat)
sid=$(printf '%s' "$input" | jq -r '.session_id // "nosession"' 2>/dev/null)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
out=$(printf '%s' "$input" | jq -r '(.tool_response.stdout // "") + (.tool_response.stderr // "")' 2>/dev/null)
[ -z "$cmd" ] && exit 0

state_dir="$HOME/.local/state/oracle-nudge"
mkdir -p "$state_dir"
state="$state_dir/$sid"

failed=0
if printf '%s' "$out" | grep -qE 'FAILED|error:|Error:|Segmentation fault|Aborted|exit(ed)? (with )?(status|code) [1-9]'; then
  failed=1
fi

sig=$(printf '%s' "$cmd" | md5sum | cut -c1-12)
prev_sig=$(sed -n 1p "$state" 2>/dev/null || echo "")
count=$(sed -n 2p "$state" 2>/dev/null || echo 0)

if [ "$failed" = "1" ]; then
  if [ "$sig" = "$prev_sig" ]; then count=$((count + 1)); else count=1; fi
  printf '%s\n%s\n' "$sig" "$count" > "$state"
  if [ "$count" -ge 3 ]; then
    printf '%s\n0\n' "$sig" > "$state"   # reset so the nudge fires once per streak
    cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"ESCALATION NUDGE: the same command has now failed 3 times in a row. Stop retrying. Consider (a) a fundamentally different diagnostic step, or (b) composing an abstracted oracle consult per the oracle-protocol skill and offering it to the user for approval."}}
EOF
  fi
else
  printf '%s\n0\n' "$sig" > "$state"
fi
exit 0
