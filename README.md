# Speak — ElevenLabs TTS Skill for Claude Code

Text-to-speech skill that gives Claude Code a voice. Includes a multi-voice audio daemon with queuing, a web dashboard with animated portraits, and a simple CLI.

## 🚀 5-Minute Quickstart

**macOS users:** See **[docs/SETUP_MAC.md](docs/SETUP_MAC.md)** for detailed setup.

```bash
# 1. Install dependencies
brew install ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Configure API key
cp .env.example .env
# Edit .env and add: ELEVENLABS_API_KEY=sk_your_key

# 3. Start daemon
unset SPEAK_PORT SPEAK_PREROLL_MS SPEAK_RESUME_REWIND_MS SPEAK_COLLECTOR_WORKERS  # Shell-exported empties crash the daemon
uv run daemon/server.py

# 4. Test (in new terminal)
./scripts/say.sh "Hello, world!"
open http://127.0.0.1:7865  # Dashboard
```

Dashboard at **http://127.0.0.1:7865**

## Quick Start

```bash
# Clone
git clone <your-repo-url> speak
cd speak

# Configure
cp .env.example .env
# Edit .env — add your ELEVENLABS_API_KEY

# Start the daemon
uv run daemon/server.py

# Speak from any terminal
./scripts/say.sh "Hello, world!"
```

Dashboard at **http://127.0.0.1:7865**

## Requirements

- **macOS** (uses `afplay` for file-mode playback)
- **Python >= 3.12**
- **[uv](https://docs.astral.sh/uv/)** (runs the daemon with inline deps — no venv needed)
- **ffmpeg** (`brew install ffmpeg`) — envelope extraction, seeking, and live-mode playback. The `ffplay` binary ships with most ffmpeg builds and is the preferred live player; `ffmpeg -f audiotoolbox` is the fallback.
- **ElevenLabs API key** — [get one here](https://elevenlabs.io)

## Configuration

### `.env`

```bash
ELEVENLABS_API_KEY=your_key_here   # Required
ELEVENLABS_VOICE_ID=               # Default voice (optional, defaults to Claude)
SPEAK_CACHE_DIR=                   # Cache dir (default: ./cache)
SPEAK_PORT=                        # HTTP port (default: 7865)
```

Real environment variables always override `.env` values.

### Streaming engine

Playback streams by default: audio starts as soon as enough has arrived, rather than
after the whole file downloads. These knobs tune it.

| Variable | Default | What it does |
|---|---|---|
| `SPEAK_STREAMING` | `1` | Kill switch. Set to `0` to restore the old fetch-whole-file-then-play path wholesale — every entry plays in file mode. |
| `SPEAK_MODEL` | `eleven_v3` | Boot-default ElevenLabs model id for single-voice synthesis. This is the **boot default only**: both dashboards flip the model at runtime via `POST /config`, and a model chosen there is persisted to `config.json`, which wins over this variable on the next start. An unrecognised value in either source is warned about and ignored — the daemon only ever advertises a model the hop chain can actually route. With `SPEAK_STREAMING=0` the conversational model is unreachable (the legacy path always synthesizes with `eleven_v3`), so `POST /config` refuses it with **409** and `GET /config` reports `streaming_enabled: false` for clients to disable the option. |
| `SPEAK_PREROLL_MS` | `500` | How much audio must decode before live playback starts. Lower is faster to first sound and more likely to underrun. **Integer — a shell-exported empty value crashes the daemon.** |
| `SPEAK_RESUME_REWIND_MS` | `1000` | How far back a resume rewinds from the paused position, so a pause replays rather than skips. Must **exceed the feeder's worst-case lead** (750 ms: an 8000-byte lead plus a 4000-byte slice at 16 kB/s) or a live resume can SKIP audio instead of replaying it — the daemon warns at startup if it does not. **Integer — a shell-exported empty value crashes the daemon.** |
| `SPEAK_LIVE_PLAYER` | `auto` | Which live-mode player to use: `auto`, `ffplay`, or `audiotoolbox`. `auto` probes both at startup and picks the first that works; an unknown name is warned about and skipped. If none work, live mode disables itself and everything plays in file mode. |
| `SPEAK_COLLECTOR_WORKERS` | `8` | Concurrent streaming fetches. Must be **≥ 1** — the daemon warns at startup if it is lower, and a pool it cannot build fails every entry at its first collection. **Integer — a shell-exported empty value crashes the daemon.** |

Leave a knob out to get its default. A **blank line in `.env` is treated as absent** and is
safe, but a blank value **exported in your shell** is real and crashes the integer knobs at
import — hence the `unset` line in the quickstart.

### `voices.json`

Ships with 9 voices. Add your own ElevenLabs voices:

```json
{
  "name": "MyVoice",
  "id": "your-elevenlabs-voice-id",
  "color": "#ff6600",
  "style": "Brief description"
}
```

The daemon also falls back to the ElevenLabs API for voice names not in `voices.json`.

## Usage

### CLI

```bash
# Basic
./scripts/say.sh "Hello"

# Choose voice
./scripts/say.sh "Deep thoughts" --voice Adam

# Channel tagging (for multi-agent filtering)
./scripts/say.sh "Status update" --voice Elli --channel researcher

# Session attribution (auto-resolved from the Claude Code environment; override explicitly)
./scripts/say.sh "Build done" --session "My Project Session"

# Priority (jumps queue)
./scripts/say.sh "Alert!" --priority

# Queue control
./scripts/say.sh --status
./scripts/say.sh --skip
./scripts/say.sh --pause
./scripts/say.sh --resume
./scripts/say.sh --clear
./scripts/say.sh --history --limit 10
./scripts/say.sh --replay <id>
```

### As a Claude Code Skill

Install as a skill in `~/.claude/skills/speak/` (or wherever you like), then reference `$SPEAK_DIR/scripts/say.sh` in your `SKILL.md`. See the included `SKILL.md` for the full prompt.

### Dashboard

The web dashboard shows:
- Animated portraits with lip-sync during playback
- Speaker attribution on every line: voice, session, and agent channel
- Transport controls (pause/resume, skip, seek)
- Queue panel with per-channel pause toggles
- History panel with replay and voice filtering

### Multi-Agent Teams

Assign each agent a unique voice for audio differentiation:

```bash
# Agent 1
./scripts/say.sh "Research complete" --voice Rachel --channel researcher

# Agent 2
./scripts/say.sh "Tests passing" --voice Adam --channel tester
```

## Architecture

```
speak/
  daemon/server.py       Starlette HTTP server — TTS, queue, SSE, dashboard
  scripts/say.sh         CLI wrapper — talks to daemon, falls back to speak.py
  scripts/speak.py       Standalone TTS (no daemon needed)
  dashboard/index.html   Single-file web dashboard
  dashboard/portraits/   Voice portrait images (3 frames each for lip-sync)
  voices.json            Voice name/ID/color mappings
  cache/                 Cached audio for history replay
  .env                   Local configuration (git-ignored)
  SKILL.md               Claude Code skill prompt
```

### Key Design Decisions

- **No external dependencies in say.sh/speak.py** — only stdlib + `curl`/`afplay`/`python3`. The daemon uses `starlette`+`uvicorn` via `uv run`.
- **macOS-only playback** — `afinfo` for duration, `ffmpeg` for seeking/trimming. Two playback modes: **file mode** plays a finished file with `afplay`, and **live mode** pipes still-arriving audio to `ffplay` (or the `audiotoolbox` backend, `ffmpeg -f audiotoolbox`). The worker picks per entry — whatever is already downloaded when an entry reaches the head of the queue plays in file mode.
- **Single shared queue** — all agents enqueue to one `AudioQueue`. Channel-based filtering and per-channel pause allow multi-agent coordination without overlap.
- **SSE, not WebSocket** — dashboard uses Server-Sent Events for simplicity. Initial state on connect, then incremental events (see below).
- **Envelope extraction** — `ffmpeg` decodes to raw PCM, computes RMS per 50ms chunk, normalizes to 0-1 for lip-sync animation. In live mode the envelope is decoded incrementally alongside playback and shipped in batches.

### SSE contract

`GET /events` opens with a `state` snapshot, then streams incremental events.

| Event | When | Payload |
|---|---|---|
| `state` | once, on connect | Queue status + `recent_history`, plus `now_playing` when something is current: `{id, live, type, phase, epoch, elapsed_estimate, duration, total_duration, envelope_so_far, seq, chunk_ms}`. Also carries `model` and `streaming_enabled`, so a client renders the model control on connect without a second request. Lets a client that connects mid-playback rebuild the clock and the lip-sync envelope. `epoch`, `elapsed_estimate` and `envelope_so_far` are `null` while `phase` is `collecting` or `starting`. |
| `voice_active` | an entry starts, or the queue goes idle | Voice, text, channel, session, duration, envelope. In live mode `duration`, `total_duration` and `envelope` are `null`, and `live: true` plus an opaque `epoch` are present. File-mode payloads are unchanged. |
| `envelope_append` | ~every 300 ms during live playback | `{id, epoch, seq, values[], chunk_ms}` — `seq` is the absolute index of the first value, monotonic from 0 per epoch. Extends the lip-sync envelope without disturbing the running clock. |
| `voice_update` | live collection completes mid-playback | `{id, epoch, duration, total_duration, envelope, chunk_ms, segments}` — the now-known totals and the calibrated envelope. This is when the dashboard reveals its scrubber. |
| `pause_state` | pause/resume, global or per-channel | `{global_paused, channel_paused}` |
| `history_update` | an entry finishes | The history entry. |
| `voices_updated` | `voices.json` changes | `{reason, name}` |
| `config_updated` | the synthesis model is changed via `POST /config` | `{model}` — the new model. Fired only on an actual change, and only after `config.json` already holds the new value, so a client that re-reads on the event never sees a model the next boot would not restore. |

### Per-entry log line

Every finished entry logs one line:

```
tts id=cf249713 mode=live model=eleven_v3 gen=0 ttfb_ms=412 first_audio_ms=1160
    decoded_ms=3150 total_ms=3420 bytes=54912 framing=chunked
```

**`first_audio_ms` is the time-to-first-audio metric** — enqueue to the moment playback
actually starts, which is what the user waits through. `ttfb_ms` is only the vendor's
first response byte and says nothing about when sound came out; do not read it as TTFA.
`mode` tells you which path the entry took (`live` or `file`).

Clients must ignore any `envelope_append` or `voice_update` whose `(id, epoch)` does not
match the generation they last saw on a `voice_active` or `state` event — a stale event
from a superseded generation must never reset live state. `epoch` is opaque: it identifies
a generation and is never used for clock arithmetic (the daemon and the client share no
time origin).

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/speak` | Single-voice TTS |
| `POST` | `/speak/dialogue` | Multi-voice dialogue |
| `GET` | `/queue` | Queue status |
| `POST` | `/queue/skip` | Skip current |
| `POST` | `/queue/pause` | Pause playback |
| `POST` | `/queue/resume` | Resume playback |
| `POST` | `/queue/seek` | Seek within track |
| `POST` | `/queue/clear` | Clear queue |
| `GET` | `/history` | Playback history |
| `POST` | `/history/replay` | Replay cached audio |
| `GET` | `/voices` | Voice configuration |
| `GET` | `/events` | SSE event stream |
| `GET` | `/health` | Health check |
| `GET` | `/` | Dashboard |

## License

MIT
