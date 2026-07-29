# harbor shell integration — source this from ~/.zshrc or ~/.bashrc:
#     source ~/src/harbor/config/shell-integration.sh
#
# Wraps `crush` so the GPU box is up before the session starts. A hibernated
# model cannot be asked to wake itself, so bring-up has to happen out here —
# but you keep typing `crush`, not remembering a launcher.
crush() {
  if ! curl -s -m 2 "http://127.0.0.1:${TUNNEL_PORT:-8081}/health" 2>/dev/null | grep -q ok; then
    echo "crush: local model endpoint is down — bringing it up..." >&2
    harbor up || { echo "crush: harbor up failed; run harbor status" >&2; return 1; }
  fi
  command crush "$@"
}
