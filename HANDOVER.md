# Scribe: handover for a fresh session

Written 2026-08-26 at the end of a long build session, for whoever picks this up next.
Read this, then `ROADMAP.md`. Everything else is in the code.

## The one rule

**Audio never leaves the machine.** Not for accuracy, not for latency, not for convenience.
It is the structural difference between Scribe and every cloud dictation product and the
reason the project exists. Anything that would trade it away is out of scope, no matter how
good the feature is. `ROADMAP.md` states this first, deliberately.

The narrow, already-drawn exception: the optional polish and prompt-mode passes send dictated
*text* through the user's own local Claude CLI on their own account, opt-in and off by
default. Audio is never part of it. Planned features that read the screen keep that data
local too.

## Where things stand

Shipped and public at github.com/ctrl-jesper/scribe, currently **v0.4.0**. The maintainer runs
this exact codebase as his daily driver: his personal config lives in `~/.config/scribe/`
(never in the repo), and `install.sh` upgrades him in place without touching it.

Working: local transcription on a warm whisper.cpp server, push-to-talk with paste at cursor,
vocabulary boosting, correction dictionary, repetition-loop collapsing, on-demand and
automatic AI polish, prompt mode (rewrites rambling dictation into a prompt targeted at
Fable/Opus/Sonnet), streaming mode, and a voice-reactive HUD with a matching menu-bar icon.

438 test assertions across two suites, all passing. Run them before and after any change:

    python3 test_pipeline.py && python3 test_stream_worker.py && python3 scribe_setup.py --selftest

## What to build next

`ROADMAP.md` has the full reasoning. The queue, in order, from a competitive review:

1. **Snippets** (S) - say a trigger phrase, get a saved block of text.
2. **Backtrack** (S-M) - resolve spoken self-corrections ("coffee at 2, actually 3").
3. **Transforms** (M) - select text, hit a hotkey, a named rule rewrites it in place.
4. **Spoken punctuation and lists** (S) - deterministic, no LLM call.
5. **Cancel in flight, hands-free lock** (S) - Escape abandons; double-press locks recording on.
6. **Language hotkey** (S) - switch dictation language without opening settings.

**The sequencing decision, already made:** several *later* features (proper nouns from screen
context, mid-sentence capitalization, per-app formatting) all need the same thing: reading the
frontmost app and the text around the cursor via the macOS accessibility API. That is a new
permission to request and justify. All six items above were chosen because they need none of
it - Transforms reads the selection with a clipboard round trip instead. So ship the queue
first, then treat the accessibility layer as one deliberate commitment, built once as a small
module with a clean interface, not four times ad hoc.

## How this codebase expects to be worked on

Hard-won, in roughly the order the lessons hurt:

- **Both paths, always.** There are two recording paths: batch (`dictate.lua` drives ffmpeg
  and calls `pipeline.py`) and streaming (`stream_worker.py` owns its own ffmpeg). The
  maintainer runs **streaming**. A feature built only in batch looks completely broken to him;
  this cost two debugging rounds once already. If a change touches recording, it touches both.
- **Verify against files before touching the microphone.** Every ffmpeg change in this project
  was proven by running the real command against a wav file first. It works, do it.
- **Lua cannot be executed in the agent environment.** No interpreter, and the `hs` CLI would
  inject into the running instance. Static checks only: a comment/string-stripped balance
  check plus a forward-reference scan, and *validate the checker against deliberately broken
  input before trusting it*. Say plainly in any report that Lua is statically verified only.
- **Two Hammerspoon traps, both already paid for.** `"roundedRectangle"` is not a valid canvas
  element type (use `"rectangle"` with `roundedRectRadii`), and a "dot" whose *height* varies
  still reads as a bar; dots need a fixed diameter and a moving y.
- **Never lose the user's words.** Every failure path pastes something. Exit codes carry this:
  0 fine, 1 nothing pasted, 3 nothing was said, 4 optimizer unavailable (raw text pasted),
  5 polish unavailable (unpolished text pasted). Extend the pattern, do not break it.
- **Phase markers, not new plumbing.** `OPTIMIZING` and `POLISHING` are flushed stdout lines
  the Lua watches to switch the HUD, exactly like `MIC_READY`. Reuse the mechanism.
- **The repo is public and the maintainer's data is not.** Personal vocabulary, client names,
  and usernames live in `~/.config/scribe/`. Grep any change for them before committing.
- **Parallel agents need file ownership.** Assign each agent exact files and a written
  cross-file protocol, or they drift. One agent once ran `git stash` in a shared tree; do not.

## Known debt, recorded and not fixed

- `optimizer_blocked_reason` does not check the CLI is *executable*, only that it exists. The
  polish path was fixed; this one is the same hole, reachable from prompt mode.
- Streaming writes the polished text to `last-dict.txt` while batch keeps the pre-polish
  transcript, so the polish hotkey after a streaming auto-polish re-polishes polished text.
  Fixing it means changing `emit()`, which is a live-verified path.
- `dictate.lua` has no automated parse check anywhere. A syntax error there breaks every
  user's hotkey at once. A CI job running `luac -p` would close the largest single risk in
  the project.
- Repo scaffolding never finished: no SECURITY.md, private vulnerability reporting still
  disabled on GitHub, no issue template, no CI, no topics set, no demo GIF. Drafts for most of
  these exist from an earlier audit.
- Intel support is implemented but has never run on Intel hardware.

## The maintainer

Not a professional developer, and explicit about it: code he has to maintain should be
readable without its author present. Prefers the conclusion first, no hype, and honest
uncertainty over confident guesses. He will tell you when something is wrong in plain terms;
believe him and look for the real cause rather than patching the symptom. He values being
told when a plan is a bad idea more than he values agreement.
