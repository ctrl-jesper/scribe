# Changelog

All notable changes to Scribe are documented in this file.

## [0.5.2] - 2026-08-29

### Fixed

- Prompt mode no longer swallows a dictation that asks for something to be written. Say "write
  me a prompt for X" or "draft me a plan for Y" and the rewrite now hands that request on to
  your target model instead of quietly treating its own output as the answer.
- Prompt mode keeps more of what you actually said: URLs and names now sit alongside numbers
  and filenames in the preserve list, a spoken "maybe" or "I'm not sure" stays a hedge instead
  of being resolved into a decision, a choice you left open for the assistant stays open, and
  your pronouns are left as spoken.
- Brevity no longer outranks fidelity when the prompt is aimed at Fable. The instruction to
  keep the prompt short had been competing with the instruction to preserve everything, and
  short was winning, which cost the odd URL or hedge.
- The per-model writing guidance can no longer leak into the rewritten prompt. Lines meant to
  shape how the prompt is written, such as the note not to ask the model to narrate its
  reasoning, occasionally came back out as content you never dictated.

## [0.5.1] - 2026-08-29

### Fixed

- **The HUD could vanish permanently.** Dictation, transcription and pasting kept working, but
  the pill stopped appearing until Hammerspoon was reloaded. The canvas was built exactly once
  and the only trigger to rebuild it was the handle being `nil`, which a broken canvas never
  is, so nothing could ever notice it had gone bad. It is now re-anchored to the current screen
  on every show, and a failed draw discards the canvas and rebuilds it once, so the worst case
  is a single dictation without a HUD rather than every dictation until a reload.
- **The HUD is no longer pinned to the screen it was first drawn on.** A new screen watcher
  re-anchors it when the display configuration changes, mirroring what the microphone watcher
  already did for audio devices. `hs.screen.mainScreen()` means "the screen holding the focused
  window", so this was never a multi-monitor-only problem.
- **The menu bar reported the wrong version.** It had said 0.1.1 since that release because the
  string was hardcoded; it is now read from the `VERSION` file the installer writes, so it
  cannot go stale again.

### Added

- A log file for the Hammerspoon side at `~/.config/scribe/state/scribe-lua.log`, in the same
  format as the Python log. Lua-side failures previously went only to the Hammerspoon console,
  which is why the HUD bug above could run for weeks leaving no evidence.
## [0.5.0] - 2026-08-27

### Added

- **Second thoughts.** Spoken self-corrections resolve themselves: "book the flight, scratch
  that, book the train" keeps only the train, and "coffee at 2, actually 3" gives you 3.
  Deterministic and local, with no AI call. Deliberately narrow, because "actually" is an
  ordinary word: it fires only on an explicit retraction command, or when a number or time is
  replaced by another of the same kind within a few words. "I actually think that's right" and
  "the 2 funds actually performed well" are left untouched. A retraction never reaches past the
  start of the current sentence and never empties the text.
- **Phrases.** Say a trigger, get a saved block of text, configured in `dictionary.json`
  alongside the replacement rules. Values may span several lines. Expansion runs after the AI
  polish pass, so stored boilerplate reaches the clipboard exactly as written instead of being
  reworded. In prompt mode the expansion runs first, so the expanded text is part of the
  dictation sent to the Claude CLI, as the whole dictation is in that mode anyway.

- **Hands-free recording (latch).** Tap Left Shift while holding push-to-talk, and recording
  continues after you release the key; press push-to-talk again to finish. Left Shift is the
  default because tapping it types no character, so the gesture consumes no event and normal
  typing, including Alt Gr brackets on a Nordic keyboard layout, is untouched. The menu-bar
  icon carries a persistent marker while latched.
- **A cap on recording length**, `max_recording_seconds`, 15 minutes by default. There was
  none before. On reaching it Scribe stops and transcribes what you said rather than
  discarding it, so neither a forgotten hands-free recording nor a stuck push-to-talk key can
  record indefinitely.
- **Spoken punctuation.** "new paragraph", "new line", "open quote", "close quote", "open
  parenthesis" and "close parenthesis" become the marks themselves. Deterministic and local,
  with no AI call. The ambiguous single words ("comma", "period", "colon") are available
  behind `"spoken_punctuation": {"single_word_marks": true}` but stay off by default, because
  they are ordinary words in ordinary speech.
- **Continuous integration.** Every push and pull request now parses `dictate.lua` with a real
  Lua 5.4 compiler and runs both Python test suites. A syntax error in the Hammerspoon script
  used to be able to break every user's hotkey with nothing to catch it.

### Changed

- Both recording paths now share one deterministic cleanup function, `clean_transcript`, so
  the batch and streaming paths cannot drift apart as features are added to one and not the
  other.

## [0.4.1] - 2026-08-27

### Added

- A logged-out Claude CLI is now named as the cause instead of hiding behind "polish
  unavailable". Scribe checks the CLI's login state once when Hammerspoon loads and says
  what to run (`claude /login`), and a dictation whose polish or prompt rewrite was refused
  for want of a login reports it with its own exit code and its own notice. The words are
  pasted either way, exactly as before: a pass that could not run has never been allowed to
  cost you your dictation.
- `python3 pipeline.py --check-auth`: prints one line and exits 1 when the configured Claude
  CLI reports itself logged out, and exits 0 in silence otherwise, including when no CLI is
  installed (a valid setup: the AI passes are optional) or when the CLI is too old to answer
  the question. `scribe_setup.py --selftest` reports the same state as one informational
  line.

## [0.4.0] - 2026-08-26

### Added

- Auto-polish: run the AI cleanup pass automatically at the end of every dictation instead of
  pressing the hotkey afterwards. A checkbox in the menu, off by default, available in both
  batch and streaming modes. If prompt mode is armed it takes precedence and the polish is
  skipped, since the rewrite already cleans the text; the menu shows this rather than
  ignoring the setting.
- ROADMAP.md: what the project plans to build, what it will not, and the principle that
  audio never leaves the machine.

### Fixed

- A Claude CLI path that exists but is not executable could lose a streaming dictation
  entirely (no clipboard, no state file). It now falls back to the unpolished words like any
  other polish failure.
- A configuration with polish switched off but the mode set to full no longer reports a
  failure on every dictation; a disabled polish is a setting, not an error.

## [0.3.0] - 2026-08-26

### Added

- Prompt mode: dictate a stream-of-thought request and Scribe rewrites it into a tight,
  structured prompt before pasting, shaped for your chosen target model (Fable, Opus, or
  Sonnet) following each model's official prompting guidance. Armed via a radio group in the
  menu; the pill shows the target name while recording and the dots orbit during the rewrite.
  If the rewriter is unavailable, the plain transcription is pasted and a notice says so.
  Uses the same local Claude CLI as the polish pass; text only, never audio, off by default.

## [0.2.0] - 2026-08-26

### Added

- A live heads-up display: while you hold the key, a small borderless dark pill shows three
  bars that react to your actual voice, one per frequency band (bass, mid, treble), smoothed
  like an audio meter (fast attack, slow release). If the bars move, the right microphone is
  live, which also makes wrong-mic problems visible immediately.
- On release the bars morph into three dots that bounce in sequence while Scribe transcribes,
  then the pill fades as the text pastes.
- The menu-bar icon is redrawn in the same visual family and now reflects real state: still
  dots when idle, live level bars while recording, a dot cycle while transcribing.
- Streaming mode's worker now measures the three frequency bands during recording and streams
  the levels to the display, so both modes show a live meter.

### Changed

- The "speak" and "transcribing" text alerts are replaced by the display above. Error alerts
  are unchanged.

### Fixed

- A level reading arriving in the same instant as key release can no longer disturb the
  release animation.
- A partial line of process output without a newline is no longer discarded (affected the
  level meter only).

## [0.1.1] - 2026-08-25

### Fixed

- A bug that could paste a previous dictation instead of the one you just made.
- A microphone-selection bug that could record from the wrong device.
- Intel Mac support, which was broken by paths hardcoded to the Apple Silicon Homebrew
  location.
- Hardened the installer and the handling of `config.json` and `dictionary.json`.

### Documentation

- Requirements now note that Intel support is implemented but not yet verified on Intel
  hardware.
- Rewrote the Privacy section's audio and text retention claims to match what the code
  actually does: batch-mode audio is overwritten, not deleted, on the next batch dictation,
  and can persist indefinitely if you switch to streaming mode; streaming chunk files are
  kept on disk when a chunk fails to transcribe twice; and the `last-dict.txt` and
  `last-output.txt` transcript files are never deleted automatically.
- Replaced the "roughly halves the wait" streaming claim with the maintainer's actual,
  single-test measurement, and noted that streaming has no effect below about 22 seconds of
  speech.
- Marked `model_file` in the configuration table as fixed at install time rather than a
  live setting.
- Added a "Known limitations" section covering the unauthenticated local transcription
  server, config files as trusted input, and the scope of the Accessibility permission
  Scribe inherits through Hammerspoon.

## [0.1.0] - 2026-08-23

Initial public release.

### Added

- Local push-to-talk dictation on a warm `whisper-server` (launchd, `127.0.0.1` only), so
  there is no per-dictation model reload.
- Personal vocabulary boosting: names, companies, and jargon primed into the transcription
  prompt so they are spelled correctly at the source.
- Deterministic dictionary corrections: case-insensitive, word-boundary replacements for
  garbles that are never a legitimate word on their own.
- Repetition-loop collapser: removes transcriber stutter loops (word runs and duplicate
  sentences) without touching legitimate emphasis or normal prose.
- Streaming mode (beta): transcribes finished stretches of speech at natural pauses while
  you are still talking, so only a short tail remains at key release; degrades gracefully to
  batch behavior when there is no pause to cut at.
- Optional AI polish pass: cleans up homophones, filler, and grammar through the user's own
  Claude CLI and subscription, text only, opt-in per use.
- Setup wizard (`scribe_setup.py`): a short, rerunnable set of questions for microphone,
  language, vocabulary and corrections, speaker note, and AI polish.
- Installer (`install.sh`): installs dependencies via Homebrew, downloads the speech model,
  installs the `com.scribe.whisper-server` launchd service, and wires Scribe into Hammerspoon.
