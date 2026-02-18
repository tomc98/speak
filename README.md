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
unset SPEAK_PORT  # Prevent crash from empty env vars
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

- **macOS** (uses `afplay` for audio playback)
- **Python >= 3.12**
- **[uv](https://docs.astral.sh/uv/)** (runs the daemon with inline deps — no venv needed)
- **ffmpeg** (`brew install ffmpeg`) — for audio envelope extraction and seeking
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
- **macOS-only playback** — uses `afplay` for playback, `afinfo` for duration, `ffmpeg` for seeking/trimming.
- **Single shared queue** — all agents enqueue to one `AudioQueue`. Channel-based filtering and per-channel pause allow multi-agent coordination without overlap.
- **SSE, not WebSocket** — dashboard uses Server-Sent Events for simplicity. Initial state on connect, then incremental `voice_active`, `history_update`, and `pause_state` events.
- **Envelope extraction** — `ffmpeg` decodes to raw PCM, computes RMS per 50ms chunk, normalizes to 0-1 for lip-sync animation.

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
