# Linux Setup Guide

**Tested on:** Fedora 43 (kernel 6.18), Python 3.14, PipeWire audio

Playback uses `ffplay` (part of ffmpeg) by default — no macOS tools needed.
Set `SPEAK_PLAYER` to override (e.g. `SPEAK_PLAYER="mpv --no-video"`).

## Prerequisites

```bash
# 1. Check Python (need >= 3.12)
python3 --version

# 2. Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install ffmpeg (playback, duration, lip-sync, seek)
sudo dnf install ffmpeg        # Fedora
# sudo apt install ffmpeg      # Debian/Ubuntu

# 4. Verify
which ffmpeg ffprobe ffplay
```

Optional: `spd-say` or `espeak-ng` gives `speak.py` an offline fallback voice
when no ElevenLabs API key is configured.

## Setup Steps

### 1. Get ElevenLabs API Key

1. Sign up at https://elevenlabs.io (free tier: 10,000 chars/month)
2. Go to https://elevenlabs.io/app/settings/api-keys
3. Create new API key (starts with `sk_`)

### 2. Configure Environment

```bash
# Create .env file in the repo root
cat > .env << 'EOF'
ELEVENLABS_API_KEY=<YOUR_API_KEY>
EOF

# CRITICAL: Clear any empty env vars (prevents crash)
unset SPEAK_PORT SPEAK_CACHE_DIR ELEVENLABS_VOICE_ID
```

### 3. Run the Daemon

```bash
uv run daemon/server.py
# Dashboard at http://127.0.0.1:7865
```

Test it:

```bash
./scripts/say.sh "Hello from Linux"
```

### 4. Run as a systemd User Service (optional)

Keeps the daemon running across logins and restarts it on failure.

```bash
mkdir -p ~/.config/systemd/user
cp linux/speak-daemon.service ~/.config/systemd/user/
# Edit WorkingDirectory/ExecStart if your repo or uv live elsewhere
systemctl --user daemon-reload
systemctl --user enable --now speak-daemon
systemctl --user status speak-daemon
```

Logs: `journalctl --user -u speak-daemon -f`

## Troubleshooting

- **No audio**: confirm PipeWire/PulseAudio is running (`pactl info`) and
  `ffplay /usr/share/sounds/alsa/Front_Center.wav` plays.
- **Different player**: set `SPEAK_PLAYER` in `.env` or the service
  environment, e.g. `SPEAK_PLAYER="mpv --no-video --really-quiet"`.
- **Port in use**: set `SPEAK_PORT` in `.env`.
