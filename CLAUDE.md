# CLAUDE.md

ElevenLabs V3 TTS skill for Claude Code. Agents speak aloud via a shared audio queue backed by an HTTP daemon.

## Running

```bash
# Start the daemon (requires uv)
uv run daemon/server.py

# Speak (daemon must be running)
scripts/say.sh "Hello"
scripts/say.sh "Hello" --voice Adam --channel my-agent

# Standalone fallback (no daemon)
python3 scripts/speak.py "Hello" --voice VOICE_ID --sync
```

## Environment

Set in `.env` (copy from `.env.example`) or export in shell:

- `ELEVENLABS_API_KEY` — Required for TTS
- `ELEVENLABS_VOICE_ID` — Default voice ID (optional)
- `SPEAK_PORT` — HTTP port (default: 7865)
- `SPEAK_CACHE_DIR` — Audio cache directory (default: ./cache)

## Architecture

1. **`scripts/say.sh`** — Bash CLI. Parses args, POSTs to daemon. Falls back to `speak.py` if daemon is down.
2. **`daemon/server.py`** — Starlette+Uvicorn HTTP server (PEP 723 inline deps). TTS via ElevenLabs API, audio queue with `afplay`, caching, SSE, dashboard. All queue logic lives here.
3. **`scripts/speak.py`** — Standalone fallback. Calls API directly, plays via `afplay`, falls back to macOS `say`. No queue.
4. **`dashboard/index.html`** — Single-file web app. Connects via SSE (`/events`). Portraits in `dashboard/portraits/` have three frames per voice for lip-sync.
5. **`voices.json`** — Voice name/ID/color mappings. Loaded by server and dashboard.
6. **`cache/`** — MP3s keyed by history ID for replay. Auto-cleaned after 24h.

## Audio Tags

V3 tags in brackets direct voice *acting* — they're stage directions, not sound effects.

**Works:** emotions (`[deadpan]`, `[conspiratorial]`), intensity shifts (`[slowly, building intensity]` → `[suddenly shouting]`), character voices (`[old timey radio announcer]`), singing (`[singing softly]`), theatrical asides, compound directions (`[whispering, conspiratorial]`).

**Doesn't work:** sound effects (`[car driving by]`), physical states (`[out of breath]`), volume control (`[even quieter]`).

## Key Design Decisions

- No external deps in say.sh/speak.py — stdlib + curl/afplay/python3 only. Daemon uses starlette+uvicorn via `uv run`.
- macOS-only — uses `afplay` for playback, `afinfo` for duration, `ffmpeg` for seeking.
- Single shared queue — all agents enqueue to one AudioQueue. Channel-based filtering prevents overlap.
- SSE, not WebSocket — simpler. Initial state on connect, then incremental events.
- MP3 validation with auto-retry — `_fetch_tts` and `_fetch_dialogue` validate response headers and retry up to 2 times on invalid audio.
