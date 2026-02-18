# macOS Setup Guide

**Tested on:** macOS Darwin 25.0.0 (Apple Silicon), Python 3.12.0

## Prerequisites

```bash
# 1. Check Python (need >= 3.12)
python3 --version

# 2. Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install ffmpeg (required for lip-sync and seek)
brew install ffmpeg

# 4. Verify macOS audio tools (should already exist)
which afplay afinfo say
```

## Setup Steps

### 1. Get ElevenLabs API Key

1. Sign up at https://elevenlabs.io (free tier: 10,000 chars/month)
2. Go to https://elevenlabs.io/app/settings/api-keys
3. Create new API key (starts with `sk_`)

### 2. Configure Environment

```bash
# Create .env file
cat > .env << 'EOF'
ELEVENLABS_API_KEY=<YOUR_API_KEY>
EOF

# CRITICAL: Clear any empty env vars (prevents crash)
unset SPEAK_PORT SPEAK_CACHE_DIR ELEVENLABS_VOICE_ID
```

### 3. Start Daemon

```bash
# Start server (runs on http://127.0.0.1:7865)
~/.local/bin/uv run daemon/server.py
```

**Expected output:**
```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:7865
```

### 4. Test It (Open New Terminal)

```bash
# Health check
curl http://127.0.0.1:7865/health
# Should return: {"status":"ok","version":"2.0","queue_size":0}

# Send test message
./scripts/say.sh "Hello from Claude Code!" --voice Claude

# Open dashboard
open http://127.0.0.1:7865
```

## CLI Usage

```bash
# Basic speak
./scripts/say.sh "Your message here"

# Choose voice
./scripts/say.sh "Deep voice" --voice Adam

# Queue controls
./scripts/say.sh --status
./scripts/say.sh --pause
./scripts/say.sh --resume
./scripts/say.sh --skip
./scripts/say.sh --clear

# History
./scripts/say.sh --history --limit 10
```

## Troubleshooting

### 1. `ValueError: invalid literal for int()`

**Cause:** Empty environment variable in shell.

**Fix:**
```bash
unset SPEAK_PORT SPEAK_CACHE_DIR ELEVENLABS_VOICE_ID
~/.local/bin/uv run daemon/server.py
```

### 2. `HTTP Error 401: Unauthorized`

**Cause:** Invalid API key.

**Fix:** Verify your API key works:
```bash
curl -H "xi-api-key: $ELEVENLABS_API_KEY" https://api.elevenlabs.io/v1/voices
# Should return JSON voice list, not {"detail":{"status":"invalid_api_key"}}
```

### 3. No lip-sync animation / pause doesn't work

**Cause:** `ffmpeg` not installed.

**Fix:**
```bash
brew install ffmpeg
# Restart daemon after install
```

## Available Voices

| Voice | Style |
|-------|-------|
| Claude | Cool, precise feminine AI |
| Rachel | Calm, clear, professional female |
| Adam | Deep, warm, authoritative male |
| Antoni | Friendly, conversational male |
| Josh | Deep, resonant, confident male |
| Bella | Soft, warm, approachable female |
| Charlotte | Warm, slightly accented female |
| Elli | Young, energetic female |
| Dorothy | Clear, pleasant, steady female |

See `voices.json` for full configuration.

## Next Steps

- **Dashboard:** http://127.0.0.1:7865 — Real-time queue with animated portraits
- **Multi-voice dialogue:** See `README.md` for advanced usage
- **API docs:** Full endpoint reference in `README.md`
