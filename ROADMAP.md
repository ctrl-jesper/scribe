# Scribe roadmap

Where the project is going and, just as importantly, where it is not.

## The principle that outranks every feature

**Audio never leaves this machine.** Not for transcription, not for accuracy, not for
convenience. It is the structural difference between Scribe and every cloud dictation
product, it cannot be added back later once given up, and no feature on this page is worth
trading it for. A feature that requires sending audio off the device is not a feature this
project wants.

Two clarifications, because they come up:

- **Text is a narrower exception, already drawn.** The optional polish and prompt-mode passes
  send dictated *text* through the user's own locally installed Claude CLI, on the user's own
  account. Both are off by default and opt-in. Audio is never part of that, and nothing else
  is transmitted at all.
- **Reading the screen stays on the screen.** Some planned features (below) read the text
  around the cursor. That data is used locally to improve recognition and formatting and is
  never transmitted, not even with the optional text passes. Where a cloud competitor sends
  screen context to its servers, Scribe must not.

## What is done

Local transcription on a warm whisper.cpp server, push-to-talk with paste-at-cursor,
personal vocabulary boosting, a deterministic correction dictionary, repetition-loop
collapsing, optional AI polish (on demand or automatic), prompt mode (rewrites a
stream-of-thought dictation into a prompt targeted at a specific model), streaming mode, and
a voice-reactive HUD.

Added in 0.5.0, all local and all without a new permission: **Latch** (tap a key while holding
push-to-talk and it keeps recording hands-free), a cap on how long any single recording can
run, **spoken punctuation** ("new paragraph" and friends, deterministic, no AI call),
**Second thoughts** (spoken self-corrections such as "coffee at 2, actually 3", narrow by
design because "actually" is an ordinary word), and **Phrases** (say a trigger, get a saved
block of text, verbatim).

## Next: things that need no new permissions

These need no new macOS permission and no new foundation. They are the near-term queue.

| Feature | Size | What it is |
|---|---|---|
| **Recasts** | M | Select text anywhere, press a hotkey, and a named rule rewrites it in place (fix grammar, change tone, restructure). Several user-defined rules, each on its own shortcut. The selection can be read and replaced with a clipboard round trip, so this needs no accessibility permission. **Not agreed yet:** its only real advantage over pasting into an already-open Claude window is avoiding the app switch, so it is worth deciding whether that is worth an M before building it. |
| **Language hotkey** | S | Switch dictation language without opening settings, and optionally bind a default language per application. Sidesteps the unsolved per-word code-switching problem by making the switch explicit and instant. |

## Then: one deliberate foundation, not four ad-hoc ones

Several worthwhile features all depend on the same capability: reading the frontmost
application and the text immediately around the cursor, through the macOS accessibility API.
That is a real commitment, a new permission to request and justify, so it is taken once, as a
single small module with a clean interface, and only after the queue above has shipped.

Once it exists, these become incremental rather than each carrying their own plumbing:

- **Proper nouns from context.** Names visible on screen are the hardest case in dictation,
  because a name never typed before cannot be in any dictionary. Candidate terms would be
  extracted and ranked into the recognizer's vocabulary hint. Read locally, never transmitted.
  Worth doing well or not at all: a careless version makes recognition worse, not better.
- **Mid-sentence capitalization.** Dictating into the middle of an existing sentence should
  not capitalize as though starting a new one.
- **Per-application formatting.** Trailing periods suit a document and not a chat message.

## Not doing

- **Anything requiring an account, a server, or a subscription.** Team dictionaries, usage
  dashboards, cross-device sync, enterprise identity. All structurally incompatible with a
  local, free, account-less tool.
- **Meeting recording and transcription.** A different product.
- **True per-word code switching.** Unsolved by the commercial products too, whose language
  detection is per session rather than per word. The language hotkey covers the real need.
- **Spoken numbered lists.** "one... two... three" becoming a numbered list sounds harmless and
  is not: ordinary counting speech would trigger it constantly. Spoken punctuation ships
  without it deliberately.
- **Cancel in flight.** Escape abandoning a dictation in progress was considered and dropped:
  the maintainer does not want it, and every other path in this project is built to never lose
  words.
- **Tone adaptation as competitors market it.** Their own documentation describes changing
  capitalization and punctuation, not tone. Scribe will ship the honest version or nothing.
- **Benchmark chasing.** Accuracy claims measured by their author, with their methodology, on
  their data are not a target worth optimizing against. The user's own error patterns are.

## Maintenance status

A personal tool, published in case it is useful to someone else, maintained as time allows.
No support commitment and no roadmap guarantees: the list above is intent, not a promise.
