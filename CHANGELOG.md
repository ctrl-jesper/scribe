# Changelog

All notable changes to Scribe are documented in this file.

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
