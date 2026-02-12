#!/usr/bin/env python3
"""ElevenLabs V3 Text-to-Speech for Claude Code.

Generates speech using ElevenLabs V3 API and plays via afplay.
Falls back to macOS 'say' if no API key is configured.

Environment variables (via .env file or shell environment):
  ELEVENLABS_API_KEY   - Your ElevenLabs API key
  ELEVENLABS_VOICE_ID  - Default voice ID or voice name to use
"""

import argparse
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


_load_dotenv()

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "eleven_v3"
DEFAULT_FORMAT = "mp3_44100_128"
TEMP_PREFIX = "claude-tts-"


def _load_voice_name_map():
    voices_path = REPO_ROOT / "voices.json"
    mapping = {}
    if not voices_path.exists():
        return mapping
    try:
        entries = json.loads(voices_path.read_text())
    except json.JSONDecodeError:
        return mapping

    if not isinstance(entries, list):
        return mapping

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        voice_id = entry.get("id")
        if not isinstance(name, str) or not isinstance(voice_id, str):
            continue
        name = name.strip()
        voice_id = voice_id.strip()
        if name and voice_id:
            mapping[name.lower()] = voice_id
    return mapping


VOICE_BY_NAME = _load_voice_name_map()


def get_config():
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "")
    return {"api_key": api_key, "voice_id": voice_id}


def resolve_voice_id(voice):
    if not voice:
        return ""
    voice = voice.strip()
    if not voice:
        return ""
    return VOICE_BY_NAME.get(voice.lower(), voice)


def cleanup_old_temp_files():
    """Remove TTS temp files older than 1 hour."""
    pattern = os.path.join(tempfile.gettempdir(), f"{TEMP_PREFIX}*.mp3")
    cutoff = time.time() - 3600
    for f in glob.glob(pattern):
        try:
            if os.path.getmtime(f) < cutoff:
                os.unlink(f)
        except OSError:
            pass


def list_voices(api_key):
    req = Request(f"{API_BASE}/voices", headers={"xi-api-key": api_key})
    try:
        with urlopen(req) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        print(f"Error: {e.code} {e.reason}", file=sys.stderr)
        sys.exit(1)

    voices = data.get("voices", [])
    print(f"{'Voice ID':<28} {'Name':<20} {'Category':<15} {'Labels'}")
    print("-" * 80)
    for v in voices:
        labels = v.get("labels", {})
        label_str = ", ".join(f"{k}: {val}" for k, val in labels.items()) if labels else ""
        print(f"{v['voice_id']:<28} {v['name']:<20} {v.get('category', ''):<15} {label_str}")


def speak_elevenlabs(text, api_key, voice_id, model=DEFAULT_MODEL, sync=False):
    url = f"{API_BASE}/text-to-speech/{voice_id}"
    payload = json.dumps({
        "text": text,
        "model_id": model,
        "output_format": DEFAULT_FORMAT,
    }).encode()

    req = Request(url, data=payload, headers={
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    })

    try:
        with urlopen(req) as resp:
            audio_data = resp.read()
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        print(f"ElevenLabs API error {e.code}: {body}", file=sys.stderr)
        return False
    except URLError as e:
        print(f"Network error: {e.reason}", file=sys.stderr)
        return False

    fd, tmp_path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp3")
    with os.fdopen(fd, "wb") as f:
        f.write(audio_data)

    if sync:
        try:
            subprocess.run(["afplay", tmp_path], check=True)
        finally:
            os.unlink(tmp_path)
    else:
        # Detached: afplay runs after script exits, temp file cleaned up later
        subprocess.Popen(
            ["afplay", tmp_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    return True


def speak_macos(text, sync=False):
    if sync:
        subprocess.run(["say", text])
    else:
        subprocess.Popen(
            ["say", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def speak(text, voice_id=None, model=DEFAULT_MODEL, sync=False):
    cleanup_old_temp_files()

    config = get_config()
    api_key = config["api_key"]
    voice = resolve_voice_id(voice_id or config["voice_id"])

    if not api_key:
        speak_macos(text, sync)
        return

    if not voice:
        print("No ELEVENLABS_VOICE_ID set. Use --list-voices to find one.", file=sys.stderr)
        speak_macos(text, sync)
        return

    if not speak_elevenlabs(text, api_key, voice, model, sync):
        print("ElevenLabs failed, falling back to macOS say", file=sys.stderr)
        speak_macos(text, sync)


def show_tag_help():
    print("""ElevenLabs V3 Audio Tags
========================
Wrap tags in square brackets within your text. V3 interprets them
for expressive, natural delivery.

Emotions & Delivery:
  [excited]    [sad]        [angry]      [calm]
  [serious]    [cheerful]   [nervous]    [confused]

Actions:
  [whispers]   [laughs]     [sighs]      [gasps]
  [clears throat]           [sniffles]

Pacing:
  [pause]      [slowly]     [quickly]

Examples:
  "[excited] All 47 tests are passing!"
  "[sighs] The build failed again. Here's what went wrong."
  "[whispers] I found a security vulnerability in the auth module."
  "[laughs] Well, that was an interesting bug."
  "[clears throat] I need your attention on something important."

Tags are suggestions — results vary by voice and context.
Not all tags work with all voices. Experiment to find what works.""")


def main():
    parser = argparse.ArgumentParser(
        description="Text-to-speech via ElevenLabs V3 or macOS say"
    )
    parser.add_argument("text", nargs="?", help="Text to speak (or pipe via stdin)")
    parser.add_argument("--voice", "-v", help="Override voice ID or voice name")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="Model ID")
    parser.add_argument("--sync", action="store_true", help="Wait for audio to finish")
    parser.add_argument("--list-voices", action="store_true", help="List available voices")
    parser.add_argument("--tag-help", action="store_true", help="Show audio tag reference")
    args = parser.parse_args()

    if args.tag_help:
        show_tag_help()
        return

    if args.list_voices:
        config = get_config()
        if not config["api_key"]:
            print("ELEVENLABS_API_KEY required. Set in .env or shell environment.", file=sys.stderr)
            sys.exit(1)
        list_voices(config["api_key"])
        return

    text = args.text
    if not text:
        if not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        else:
            print("Provide text as argument or via stdin", file=sys.stderr)
            sys.exit(1)

    if not text:
        sys.exit(0)

    speak(text, args.voice, args.model, args.sync)


if __name__ == "__main__":
    main()
