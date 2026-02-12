---
name: speak
description: Speak text aloud via ElevenLabs TTS. Voice is the primary communication channel. Audio queues sequentially across all agents.
allowed-tools: Bash, Read
---

# Voice (TTS)

> **Setup**: Set `$SPEAK_DIR` to the directory where this skill is installed (e.g., `export SPEAK_DIR=~/.claude/skills/speak`). All paths below use this variable.

## When to Speak

**Speak at the end of every turn.** The user works on other things while you run — voice is how they know you need attention or are done.

- **Always speak**, even if just "Done" or "Task complete" — it's an audio alert
- **Simple updates spoken**: completions, results, errors, questions, status — anything the user needs to hear
- **Technical details stay as text**: code snippets, diffs, file paths, long explanations — these are better read than heard
- Distill what matters into 1-3 spoken sentences. The text output has the full details.

**Skip speaking only when:**
- Doing silent consecutive tool calls with no user-facing output
- The user says "quiet" / "mute" / "stop speaking" — resume when he says "unmute" / "voice on"

## How to Speak

```bash
$SPEAK_DIR/scripts/say.sh "Your message here"
$SPEAK_DIR/scripts/say.sh "Your message" --voice Claude
$SPEAK_DIR/scripts/say.sh "Your message" --voice Adam --channel agent-1
$SPEAK_DIR/scripts/say.sh "Urgent!" --priority
```

Queue operations:

```bash
$SPEAK_DIR/scripts/say.sh --status
$SPEAK_DIR/scripts/say.sh --skip
$SPEAK_DIR/scripts/say.sh --clear
$SPEAK_DIR/scripts/say.sh --pause
$SPEAK_DIR/scripts/say.sh --resume
$SPEAK_DIR/scripts/say.sh --history --limit 10
$SPEAK_DIR/scripts/say.sh --replay <id>
```

## Rules

- Always output text too — TTS supplements, never replaces
- Speak what matters, not a literal readback of your text output
- Multiple speak calls queue up and play in order
- All agents share one audio queue — you will never talk over each other

## Audio Tags

ElevenLabs V3 tags — use naturally, don't force:
`[excited]` `[serious]` `[sighs]` `[laughs]` `[clears throat]` `[whispers]` `[calm]`

## Voice Roster

Default assistant voice is **Claude**. Dashboard: `http://127.0.0.1:7865`

| Voice | Style |
|-------|-------|
| **Claude** | Cool, precise feminine AI — crystalline, deliberate, faintly amused |
| Rachel | Calm, clear, professional female |
| Adam | Deep, warm, authoritative male |
| Antoni | Friendly, conversational male |
| Josh | Deep, resonant, confident male |
| Bella | Soft, warm, approachable female |
| Charlotte | Warm, slightly accented female |
| Elli | Young, energetic female |
| Dorothy | Clear, pleasant, steady female |

## Dashboard

The dashboard at `http://127.0.0.1:7865` shows:
- **Portraits** with lip-sync animation during playback
- **Transport bar** — pause/resume (Space), skip (Right arrow)
- **Audio scrubber** — progress bar with elapsed/remaining time, drag to seek
- **Queue panel** — upcoming items, per-channel pause toggles
- **History panel** — past entries with replay, click rows to expand full text, channel filters

## Team Voice Assignment

When spawning a team, assign each teammate a **unique voice**. Include in every teammate's prompt:

```
Your voice is <Name>. When speaking, use: $SPEAK_DIR/scripts/say.sh "message" --voice <Name>
Speak at the end of every turn — voice is how you communicate completion and status.
```

- **Lead** uses Claude (default). Teammates get different voices so the user can tell them apart.
- Match voice to role when it fits (e.g., Adam for serious infra, Elli for exploration)
- Use `--channel <agent-name>` per teammate for dashboard filtering
