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
SESSION=""
PRIORITY=false
ACTION=""
LIMIT=50
REPLAY_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --voice)    VOICE="$2"; shift 2 ;;
    --channel)  CHANNEL="$2"; shift 2 ;;
    --session)  SESSION="$2"; shift 2 ;;
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

# Resolve the session name for speaker attribution. A Claude Code background
# job carries its live name in $CLAUDE_JOB_DIR/state.json; any other session
# is named by the last title record in its transcript (the same mechanism the
# harness uses for its session list, so renames are picked up on the next call).
# Subagents inherit both env vars, so their lines attribute to the parent session.
resolve_session() {
  python3 - <<'PY' 2>/dev/null || true
import glob, json, os

def job_name():
    d = os.environ.get("CLAUDE_JOB_DIR")
    if not d:
        return None
    try:
        with open(os.path.join(d, "state.json")) as f:
            return json.load(f).get("name") or None
    except (OSError, ValueError):
        return None

def transcript_name():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    if not hits:
        return None
    path = max(hits, key=os.path.getmtime)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    name = None
    for line in tail.splitlines():
        if '"ai-title"' not in line and '"agent-name"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        name = rec.get("aiTitle") or rec.get("agentName") or name
    return name

print(job_name() or transcript_name() or "")
PY
}

# Build JSON body with safe serialization
json_body() {
  python3 -c "
import json, sys
d = {}
if sys.argv[1]: d['channel'] = sys.argv[1]
print(json.dumps(d))
" "$CHANNEL"
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
    curl -sf -X POST -H "Content-Type: application/json" -d "$(json_body)" "$DAEMON/queue/clear"
    ;;
  pause)
    curl -sf -X POST -H "Content-Type: application/json" -d "$(json_body)" "$DAEMON/queue/pause"
    ;;
  resume)
    curl -sf -X POST -H "Content-Type: application/json" -d "$(json_body)" "$DAEMON/queue/resume"
    ;;
  history)
    curl -sf "$DAEMON/history?limit=$LIMIT" | python3 -m json.tool
    ;;
  replay)
    REPLAY_BODY=$(python3 -c "import json, sys; print(json.dumps({'id': sys.argv[1]}))" "$REPLAY_ID")
    curl -sf -X POST -H "Content-Type: application/json" \
      -d "$REPLAY_BODY" "$DAEMON/history/replay"
    ;;
  speak)
    [[ -z "$TEXT" ]] && {
      echo "Usage: say.sh \"text\" [--voice NAME] [--channel CH] [--session NAME] [--priority]" >&2
      echo "       say.sh --status | --skip | --clear | --pause | --resume" >&2
      echo "       say.sh --history [--limit N] | --replay ID" >&2
      exit 1
    }

    # Persona gate: voices whose style is an in-character script must be performed,
    # not narrated in plain English. Reject out-of-character text before it hits TTS.
    if [[ "$VOICE" == "Jian-Yang" ]]; then
      if ! grep -qE '\[[A-Za-z]' <<<"$TEXT" \
         && ! grep -qiwE 'dis|dat|dey|dem|den|dere|wit|nutting|wery|wat' <<<"$TEXT"; then
        cat >&2 <<'EOF'
say.sh: refusing to speak as Jian-Yang in plain English.
This voice has an in-character style — perform it, don't narrate it.
Rewrite the line in Jian-Yang's voice, then re-run:
  - short, clipped, broken English; drop "the"/"a"
  - respell for his accent: this->dis, that->dat, they->dey, them->dem,
    then->den, with->wit, nothing->nutting, very->wery, what->wat
  - lead with an audio tag: [deadpan] or [flat, monotone]
  - full style: curl -s http://127.0.0.1:7865/voices   (find "Jian-Yang")
Example: [deadpan] Dis is not good. Your app do nutting. My app is better.
EOF
        exit 3
      fi
    fi

    if ! daemon_up; then
      echo "Daemon unreachable, using fallback" >&2
      ARGS=("$TEXT")
      [[ -n "$VOICE" ]] && ARGS+=(--voice "$VOICE")
      ARGS+=(--sync)
      python3 "$FALLBACK" "${ARGS[@]}"
      exit $?
    fi

    [[ -z "$SESSION" ]] && SESSION="$(resolve_session)"

    # Build JSON body using python3 for safe serialization
    BODY=$(python3 -c "
import json, sys
d = {'text': sys.argv[1]}
if sys.argv[2]: d['voice'] = sys.argv[2]
if sys.argv[3]: d['channel'] = sys.argv[3]
if sys.argv[4] == 'true': d['priority'] = True
if sys.argv[5]: d['session'] = sys.argv[5]
print(json.dumps(d))
" "$TEXT" "$VOICE" "$CHANNEL" "$PRIORITY" "$SESSION")

    curl -sf -X POST -H "Content-Type: application/json" -d "$BODY" "$DAEMON/speak"
    ;;
esac
