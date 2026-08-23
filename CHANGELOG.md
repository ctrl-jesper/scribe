# Changelog

All notable changes to Scribe are documented in this file.

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
