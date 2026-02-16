---
name: speak
description: Speak text aloud via ElevenLabs TTS. Voice is the primary communication channel. Audio queues sequentially across all agents.
allowed-tools: Bash, Read
---

# Voice (TTS)

> Paths below use `{base}` as shorthand for this skill's base directory, which is provided automatically via the "Base directory for this skill" context injected at the top of the prompt when the skill loads. Construct the full path from that value — do NOT rely on environment variables.

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
{base}/scripts/say.sh "Your message here"
{base}/scripts/say.sh "Your message" --voice Claude
{base}/scripts/say.sh "Your message" --voice Adam --channel agent-1
{base}/scripts/say.sh "Urgent!" --priority
```

Queue operations:

```bash
{base}/scripts/say.sh --status
{base}/scripts/say.sh --skip
{base}/scripts/say.sh --clear
{base}/scripts/say.sh --pause
{base}/scripts/say.sh --resume
{base}/scripts/say.sh --history --limit 10
{base}/scripts/say.sh --replay <id>
```

## Rules

- Always output text too — TTS supplements, never replaces
- Speak what matters, not a literal readback of your text output
- **Never speak secrets** — API keys, tokens, passwords, credentials, or other sensitive data must never be spoken aloud. Redact or omit them from spoken output even if they appear in text output.
- Multiple speak calls queue up and play in order
- All agents share one audio queue — you will never talk over each other

## Audio Tags

ElevenLabs V3 supports freeform expressive tags in brackets. These direct **how** the voice performs — not what sounds it makes.

**Works well:**
- Emotions & delivery: `[excited]` `[deadpan]` `[sarcastically]` `[conspiratorial]` `[smug]`
- Intensity dynamics: `[slowly, building intensity]` `[suddenly shouting]` `[composing herself, calm]`
- Character voices: `[old timey radio announcer]` `[valley girl voice]` `[deep movie trailer voice]`
- Singing: `[singing softly]` — surprisingly effective, can carry a tune
- Theatrical: `[aside, whispering to audience]` `[back to announcer voice]` `[dramatic pause]`
- Compound directions: `[whispering, conspiratorial]` `[speaking normally, laughs]`

**Doesn't work:**
- Sound effects: `[sound of keyboard clicking]` `[car driving by]` `[thunder rumbling]` `[door creaking]` — the model cannot generate non-voice sounds
- Physical states: `[out of breath]` — mostly ignored
- Volume control: `[even quieter]` `[normal volume]` — unreliable

Tags direct voice *acting*, not audio *production*. Think stage directions, not foley.

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
Your voice is <Name>. When speaking, use: {base}/scripts/say.sh "message" --voice <Name>
Speak at the end of every turn — voice is how you communicate completion and status.
```

- **Lead** uses Claude (default). Teammates get different voices so the user can tell them apart.
- Match voice to role when it fits (e.g., Adam for serious infra, Elli for exploration)
- Use `--channel <agent-name>` per teammate for dashboard filtering
