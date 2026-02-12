#!/usr/bin/env bash
# say.sh — TTS via voice daemon
# Falls back to speak.py if daemon is unreachable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FALLBACK="$SCRIPT_DIR/speak.py"

# Load .env if present (real env vars win via ${VAR:-} pattern)
if [[ -f "$REPO_ROOT/.env" ]]; then
  while IFS='=' read -r key value; do
    key="${key%%#*}"          # strip inline comments
    key="${key// /}"          # strip spaces
    [[ -z "$key" || "$key" == \#* ]] && continue
    value="${value%\"}"       # strip surrounding quotes
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    : "${!key:=$value}"       # only set if not already in env
    export "$key"
  done < "$REPO_ROOT/.env"
fi

SPEAK_PORT="${SPEAK_PORT:-7865}"
DAEMON="http://127.0.0.1:$SPEAK_PORT"

# Parse arguments
TEXT=""
VOICE="Claude"
CHANNEL=""
PRIORITY=false
ACTION=""
LIMIT=50
REPLAY_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --voice)    VOICE="$2"; shift 2 ;;
    --channel)  CHANNEL="$2"; shift 2 ;;
    --priority) PRIORITY=true; shift ;;
    --status)   ACTION="status"; shift ;;
    --skip)     ACTION="skip"; shift ;;
    --clear)    ACTION="clear"; shift ;;
    --pause)    ACTION="pause"; shift ;;
    --resume)   ACTION="resume"; shift ;;
    --history)  ACTION="history"; shift ;;
    --limit)    LIMIT="$2"; shift 2 ;;
    --replay)   ACTION="replay"; REPLAY_ID="$2"; shift 2 ;;
    -*)         echo "Unknown option: $1" >&2; exit 1 ;;
    *)          TEXT="$1"; shift ;;
  esac
done

# Check daemon health
daemon_up() {
  curl -sf --connect-timeout 1 "$DAEMON/health" >/dev/null 2>&1
}

# Build JSON body with channel support
channel_body() {
  if [[ -n "$CHANNEL" ]]; then
    echo "{\"channel\":\"$CHANNEL\"}"
  else
    echo '{}'
  fi
}

# Dispatch actions
case "${ACTION:-speak}" in
  status)
    curl -sf "$DAEMON/queue" | python3 -m json.tool
    ;;
  skip)
    curl -sf -X POST "$DAEMON/queue/skip"
    ;;
  clear)
    curl -sf -X POST -H "Content-Type: application/json" -d "$(channel_body)" "$DAEMON/queue/clear"
    ;;
  pause)
    curl -sf -X POST -H "Content-Type: application/json" -d "$(channel_body)" "$DAEMON/queue/pause"
    ;;
  resume)
    curl -sf -X POST -H "Content-Type: application/json" -d "$(channel_body)" "$DAEMON/queue/resume"
    ;;
  history)
    curl -sf "$DAEMON/history?limit=$LIMIT" | python3 -m json.tool
    ;;
  replay)
    curl -sf -X POST -H "Content-Type: application/json" \
      -d "{\"id\":\"$REPLAY_ID\"}" "$DAEMON/history/replay"
    ;;
  speak)
    [[ -z "$TEXT" ]] && {
      echo "Usage: say.sh \"text\" [--voice NAME] [--channel CH] [--priority]" >&2
      echo "       say.sh --status | --skip | --clear | --pause | --resume" >&2
      echo "       say.sh --history [--limit N] | --replay ID" >&2
      exit 1
    }

    if ! daemon_up; then
      echo "Daemon unreachable, using fallback" >&2
      ARGS=("$TEXT")
      [[ -n "$VOICE" ]] && ARGS+=(--voice "$VOICE")
      ARGS+=(--sync)
      python3 "$FALLBACK" "${ARGS[@]}"
      exit $?
    fi

    # Build JSON body using python3 for safe serialization
    BODY=$(python3 -c "
import json, sys
d = {'text': sys.argv[1]}
if sys.argv[2]: d['voice'] = sys.argv[2]
if sys.argv[3]: d['channel'] = sys.argv[3]
if sys.argv[4] == 'true': d['priority'] = True
print(json.dumps(d))
" "$TEXT" "$VOICE" "$CHANNEL" "$PRIORITY")

    curl -sf -X POST -H "Content-Type: application/json" -d "$BODY" "$DAEMON/speak"
    ;;
esac
