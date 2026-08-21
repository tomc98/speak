---
name: speak
description: Speak text aloud via ElevenLabs TTS. Voice is the primary communication channel. Audio queues sequentially across all agents.
allowed-tools: Bash, Read
---

# Voice (TTS)

> Paths below use `{base}` as shorthand for this skill's base directory, which is provided automatically via the "Base directory for this skill" context injected at the top of the prompt when the skill loads. Construct the full path from that value — do NOT rely on environment variables.

## When to Speak

**Speak every turn.** The user works on other things while you run — voice is how they know you need attention or are done.

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

## Speaker Attribution

Every spoken line is attributed on the dashboards as a stack: **voice → session → agent**.

- **Session** — resolved automatically by `say.sh` from the Claude Code environment
  (`$CLAUDE_JOB_DIR/state.json` name, falling back to the session transcript's latest
  title record). Renames are picked up on the next call, and subagents inherit the
  environment, so their lines attribute to the parent session. No action needed;
  `--session "Name"` overrides.
- **Agent** (`--channel`) — the main agent speaks with no `--channel`. Every spawned
  subagent/teammate must be told its name in its spawn brief (see Team Voice
  Assignment) so its lines carry the third stack line. Channels also drive
  per-channel pause and filtering on the dashboards.

## Rules

- Always output text too — TTS supplements, never replaces
- Speak what matters, not a literal readback of your text output
- **Never narrate intent as fact** — don't speak "it's in chat / on screen" unless that message already exists; files, notes, and subagent reports aren't chat delivery
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

## Picking a Voice

Default assistant voice is **Claude**. The full roster is dynamic — query the daemon at session start rather than relying on a hardcoded list:

```bash
curl -s http://127.0.0.1:7865/voices
```

Each record has `name`, `id`, `color`, `style`, `kind`, and `has_portrait`. Pick a voice whose `style` description matches your role (e.g. a precise debugging agent → crystalline/deliberate; an exploration agent → young/energetic).

**Selection order:**
1. User-requested voice (if specified)
2. `Claude` (the default)
3. First voice in the roster

### The `kind` field

`kind` is an open enum describing where a voice came from. Known values:

- `default` — the built-in roster
- `codex` — voices intended for Codex-impersonating agents
- `user` — the user's own cloned voice
- `custom` — any other user-added voice

Unless instructed otherwise, agents should pick from `kind: "default"`. A Codex-impersonating agent should prefer `kind: "codex"` when one exists, falling back to `default`.

### Character voices

Some voices carry an **in-character speaking style** in their `style` field — for these, the `style` is a *script direction to perform*, not just a label. When you speak as one, write the spoken line **as that character**: follow every directive in the `style`, including the suggested audio tags **and any phonetic respellings** it specifies.

- **Jian-Yang** (`kind: "custom"`) — deadpan, blunt, broken English. Drop articles, keep sentences short and clipped, and respell words for his accent so the TTS leans in: `this→dis`, `that→dat`, `they→dey`, `them→dem`, `then→den`, `with→wit`, `nothing→nutting`, `very→wery`, `what→wat`. Lead with `[deadpan]` / `[flat, monotone]`. e.g. `[deadpan] Dis is not good. Your app do nutting. My app is better.`
- **Pickle-Rick** (`kind: "custom"`) — manic genius pickle scientist, ALWAYS AT FULL VOLUME. The voice only works shouted: open `[shouting] L-LOOK, MORTY,`, write most of the line in ALL CAPS, and pin the register with `[suddenly shouting]` / `[yelling]` / `[manic]`. Banned: `[sighs]`, `[muttering]`, calm asides, quiet trail-offs — any low-energy beat kills it. Mania mechanics stay, at volume: stammered restarts, em-dash self-interrupts, rapid concrete specifics, rhetorical question whose ANSWER is the payoff, pickle gloat, doubled repeat (`IT'S JUST— IT'S JUST SCIENCE!`), closer `I'M [X] RIIICK!`. Never open with meta-setup, slow name repetition, or the payoff itself; everyone is 'Morty' unless being singled out.

The rule generalizes: if a voice's `style` reads like a persona/accent rather than a neutral descriptor, render the text in that persona before calling `say.sh --voice <Name>`.

## Managing voices

Tom manages the roster through the **macOS Voice Manager** UI (under `macos/` in this skill). The same operations are available on the daemon for scripted changes:

- `GET /voices` — list voices (includes `kind`, `has_portrait`)
- `POST /voices` — add `{name, id, color, style, kind?}`
- `PATCH /voices/{name}` — partial update; changing `name` renames the voice
- `DELETE /voices/{name}` — remove a voice
- `POST /portraits/{name}?frame=default|slight|open` — upload a portrait frame (raw PNG body)

## Dashboard

The dashboard at `http://127.0.0.1:7865` shows:
- **Portraits** with lip-sync animation during playback, labelled with the
  voice → session → agent attribution stack
- **Transport bar** — pause/resume (Space), skip (Right arrow)
- **Audio scrubber** — progress bar with elapsed/remaining time, drag to seek
- **Queue panel** — upcoming items, per-channel pause toggles
- **History panel** — past entries with replay, click rows to expand full text, channel filters

## Team Voice Assignment

When spawning a team, the lead should `curl -s http://127.0.0.1:7865/voices` and assign each teammate a **unique voice** from the live roster (no hardcoded mapping). Match `style` to role when it fits. Include in every teammate's prompt:

```
Your voice is <Name>. When speaking, use: {base}/scripts/say.sh "message" --voice <Name> --channel <agent-name>
Speak at the end of every turn — voice is how you communicate completion and status.
```

- **Lead** keeps Claude (default). Teammates get different voices so the user can tell them apart.
- Use `--channel <agent-name>` per teammate — it drives dashboard filtering and renders
  as the agent line under the portrait, so the user can see which agent is talking.
