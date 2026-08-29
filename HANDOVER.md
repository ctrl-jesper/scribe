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

Shipped and public at github.com/ctrl-jesper/scribe, currently **v0.5.2**. The maintainer runs
this exact codebase as his daily driver: his personal config lives in `~/.config/scribe/`
(never in the repo), and `install.sh` upgrades him in place without touching it.

Working: local transcription on a warm whisper.cpp server, push-to-talk with paste at cursor,
vocabulary boosting, correction dictionary, repetition-loop collapsing, on-demand and
automatic AI polish, prompt mode (rewrites rambling dictation into a prompt targeted at
Fable/Opus/Sonnet), streaming mode, and a voice-reactive HUD with a matching menu-bar icon.

Added in 0.5.0: hands-free recording (latch), a 15 minute cap on any single recording, spoken
punctuation, second thoughts (spoken self-corrections), phrases (say a trigger, get a saved
block), and CI.

421 and 191 test assertions across the two suites, plus 42 selftest checks, all passing. Run
them before and after any change:

    python3 test_pipeline.py && python3 test_stream_worker.py && python3 scribe_setup.py --selftest

CI now runs both suites and parses `dictate.lua` with a real Lua 5.4 compiler on every push and
pull request, which is the only genuine syntax check this project has ever had for the
Hammerspoon script. Use it: push a branch and let it parse the Lua you cannot execute locally.

## What to build next

`ROADMAP.md` has the full reasoning. Most of the original queue shipped in 0.5.0. What is left:

1. **Recasts** (M) - select text anywhere, press a hotkey, a named rule rewrites it in place.
   Was called Transforms. **Not started, and not yet agreed:** the maintainer asked whether it
   earns its place given he already has Claude Desktop and Claude Code open all day, so the
   only thing it buys is avoiding the app switch. Get his answer before building it.
2. **Language hotkey** (S) - switch dictation language without opening settings. Directly
   useful given he works in Danish and English daily.

Deliberately dropped: cancel-in-flight (Escape to abandon a dictation), which he said he does
not want, and spoken numbered lists, which are a false-positive machine.

Naming, decided with him: the features are Phrases, Recasts, Second thoughts, and Latch, so
they do not simply copy the competitor's names. Note that "latch" now means the hands-free
lock and nothing else in `dictate.lua`; the older comments that used it for the frozen
prompt-mode target were reworded to "frozen" precisely so the word means one thing.

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

- `optimizer_blocked_reason` does not check the CLI is *executable*, only that it exists. A fix
  exists on the `fix/optimizer-oserror-guard` branch, verified to merge cleanly, but it has not
  been reviewed or merged. Check whether it landed before re-fixing it.
- Streaming writes the polished text to `last-dict.txt` while batch keeps the pre-polish
  transcript, so the polish hotkey after a streaming auto-polish re-polishes polished text.
  Fixing it means changing `emit()`, which is a live-verified path.
- Two spoken "new paragraph" commands in a row produce four newlines rather than collapsing to
  one break: whisper inserts a comma between them, which defeats the repetition collapser's
  exact-token match. Minor, and a robust fix needs its own design decision.
- Second thoughts supports single-word written numbers ("two", "three") but not compounds
  ("twenty-three"). Documented in the code, not fixed.
- Repo scaffolding still unfinished: no SECURITY.md, private vulnerability reporting still
  disabled on GitHub, no issue template, no topics set, no demo GIF. CI now exists. Drafts for
  most of the rest exist from an earlier audit.
- Intel support is implemented but has never run on Intel hardware.
- The 0.5.0 Lua changes (latch, the recording cap, the menu-bar marker) parse under a real Lua
  compiler in CI, but their runtime behaviour was confirmed only by the maintainer reloading
  Hammerspoon. If something about the gesture or the timer misbehaves, that is where to look.
- `hideHUD(true)` fades over 0.3s, and a dictation starting inside that window calls `show()`
  while the fade is still in flight, which may cause an occasional one-off missing HUD. Found
  while diagnosing the permanent-HUD bug in 0.5.1, deliberately left unfixed: it is transient,
  lower confidence, and mixing it into that fix would have been a refactor-while-fixing. If the
  maintainer reports a HUD that misses once and then recovers by itself, this is the suspect.
- The Lua side now logs to `state/scribe-lua.log`. Read it FIRST for any HUD or hotkey report:
  before 0.5.1 those failures went only to the Hammerspoon console and left no evidence at all,
  which is precisely why the permanent-HUD bug survived for weeks.

## The maintainer

Not a professional developer, and explicit about it: code he has to maintain should be
readable without its author present. Prefers the conclusion first, no hype, and honest
uncertainty over confident guesses. He will tell you when something is wrong in plain terms;
believe him and look for the real cause rather than patching the symptom. He values being
told when a plan is a bad idea more than he values agreement.
