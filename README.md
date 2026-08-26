# Scribe

Local, private, free push-to-talk dictation for macOS, with your own vocabulary.

## Why

Cloud dictation tools are usually a subscription, and they send your audio to a server you
do not control. Scribe runs [whisper.cpp](https://github.com/ggerganov/whisper.cpp) entirely
on your Mac, through a warm background server so there is no per-dictation model reload, with
a personal vocabulary that boosts the names and jargon you actually say, and an optional AI
cleanup pass that runs through your own Claude CLI, on your own subscription. Hold a key,
talk, release. The text is typed wherever your cursor is.

## Requirements

- macOS (Apple Silicon or Intel)
- [Homebrew](https://brew.sh)
- About 600 MB of disk space for the speech model
- A working microphone

Tested on Apple Silicon. Intel support is implemented but not yet verified on Intel
hardware, since the maintainer only has Apple Silicon machines to test on. If you run
Scribe on an Intel Mac, please report back with what worked and what did not.

Optional:
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code), already logged in, if
  you want the AI polish pass. Plain dictation works fully without it.

## Install

```
git clone <this repo>
cd scribe
./install.sh
```

The installer:

1. Checks you are on macOS and that Homebrew is present (it will not install Homebrew for you).
2. Installs `whisper-cpp`, `ffmpeg`, and Hammerspoon via Homebrew, skipping anything already present.
3. Creates `~/.config/scribe/` and copies the app code into it.
4. Downloads the speech model (about 547 MB) from Hugging Face, resumable if interrupted.
5. Picks a free port, writes it into `config.json`, and installs the `com.scribe.whisper-server`
   launchd service so the transcription server starts on login and stays warm.
6. Wires Scribe into your Hammerspoon config and runs the first-run setup wizard.

It asks for two macOS permissions the first time you use it:

- **Accessibility**, for Hammerspoon, so it can type the transcribed text into other apps.
- **Microphone**, on your first recording.

Safe to re-run: an upgrade refreshes the app code and the launchd service but never touches
your config, dictionary, or downloaded model.

## First-run setup

The installer ends by running a short wizard (`scribe_setup.py`), six questions and a check:

1. **Microphone** - picked from a numbered list of what macOS currently sees, stored by name
   rather than index so it survives a headset or phone connecting later.
2. **Language** - pin one language code (`en`, `da`, `de`, ...). Auto-detect drifts on accented
   speech; pinning does not.
3. **Your words** - names, companies, products, and jargon you say often (boosts accuracy at
   the transcription stage), plus any fixed corrections you already know you need
   (`wrong=right`, comma separated).
4. **Speaker note** - one optional line describing how you speak, used only by the AI polish
   pass if you turn it on.
5. **AI polish** - offered only if it finds the Claude CLI on your machine; you choose whether
   to turn it on.
6. Writes `config.json` and `dictionary.json` and prints a summary.

It then offers a two second recording from the microphone you chose and reports the level it
heard, so a silent or wrong input is caught during setup rather than in the middle of real
dictation. The check is optional and never blocks the install.

Rerun it any time your setup changes:

```
python3 ~/.config/scribe/app/scribe_setup.py
```

Answers you already gave are shown as defaults, so a rerun only asks you to confirm or change them.

## Usage

Hold the push-to-talk key (Right Option by default), wait for the cue, speak, release.

1. **Hold** the key.
2. **Wait for the cue** - a short tone, and a small dark pill appears center-screen. The
   microphone has a brief cold start; speaking before the cue clips your first word.
3. **Speak.** The pill shows three bars that react to your voice, one per frequency band:
   bass on the left, mids in the middle, treble on the right. If the bars move when you
   talk, the right microphone is live.
4. **Release.** The bars collapse into three dots that bounce while Scribe transcribes,
   then the pill fades and the cleaned text pastes at your cursor.

The menu-bar icon mirrors the same states in miniature: three still dots when idle, small
live-level bars while recording, and a left-to-right dot cycle while transcribing or
running the AI polish. If the drawn icon ever fails to render, it falls back to emoji.

Other actions:

- **Recall the last dictation** - `⌘⌥⌃L`. Restores your most recent result (plain or polished)
  to the clipboard and pastes it, even if you have copied something else since.
- **Polish the last dictation** - `⌘⌥⌃P`, shown only if you enabled polish in setup. Runs the
  AI cleanup pass on the dictation you just made, about 10 seconds, without re-recording. Tip:
  undo the instant paste first, so the polished version replaces it rather than duplicating it.
- **Streaming mode (beta)** - a checkbox in the menu-bar dropdown, off by default. While you
  talk, Scribe transcribes at your natural pauses in the background, so only a short tail is
  left once you release the key. In the maintainer's own testing, this was about a third
  faster than batch mode on a 47-second dictation (3.33s versus 5.23s), measured once on one
  machine, not a general benchmark. Short dictations see no benefit at all: streaming only
  starts handing audio to the background once you have spoken for roughly 22 seconds, so
  anything shorter behaves exactly like batch mode. It never cuts mid-speech: if you never
  pause, it degrades gracefully to the same behavior as the default (batch) mode.

## Configuration

Everything lives in `~/.config/scribe/config.json`. Edit it directly, or rerun the setup
wizard.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `language` | string | `"en"` | Transcription language code. Pinned, not auto-detected. |
| `mic_name` | string | `""` | Microphone, matched by name so it survives device changes. |
| `hotkey_keycode` | number | `61` | Push-to-talk key. See common values below. |
| `hotkey_flag` | string | `"alt"` | The modifier flag that keycode raises (`alt`, `ctrl`, `cmd`, `shift`). |
| `server_port` | number | `8090` | Port the local whisper-server listens on (localhost only). |
| `model_file` | string | `"ggml-large-v3-turbo-q5_0.bin"` | Speech model filename. Baked into the launchd service at install time; no code reads this key at runtime, so editing it here has no effect. There is currently no supported way to switch models short of editing `install.sh` yourself. |
| `vocabulary` | list of strings | `[]` | Names and terms boosted at transcription time. |
| `speaker_note` | string | `""` | One line of speaker context, used only by the AI polish prompt. |
| `mode` | `"dict"` or `"full"` | `"dict"` | Default pipeline: dictionary-only, or with AI polish. |
| `polish_enabled` | boolean | `false` | Turns on the AI polish hotkey and menu item. |
| `claude_bin` | string | `"~/.local/bin/claude"` | Path to the Claude CLI. |
| `claude_model` | string | `"claude-haiku-4-5-20251001"` | Model used for the polish pass. |
| `ffmpeg_bin` | string | resolved at install | Absolute path to ffmpeg. Written by the installer so the Homebrew prefix is correct on both Apple Silicon and Intel. |
| `python_bin` | string | resolved at install | Absolute path to the Python that runs the pipeline. |
| `config_version` | number | `1` | Schema version. Lets a future release migrate an older config rather than silently ignoring renamed keys. |

Common `hotkey_keycode` / `hotkey_flag` pairs (macOS virtual keycodes):

| Key | `hotkey_keycode` | `hotkey_flag` |
|---|---|---|
| Right Option (default) | `61` | `"alt"` |
| Left Option | `58` | `"alt"` |
| Right Control | `62` | `"ctrl"` |

`dictionary.json` holds deterministic, case-insensitive, word-boundary replacements applied to
every transcript:

```json
{
  "replacements": {
    "Akme Korp": "Acme Corp"
  }
}
```

Use it only for garbles that are never a legitimate word on their own; anything ambiguous
(a word that could be either the garble or something you actually meant) belongs in the
optional AI polish pass instead, which has context the dictionary does not.

File locations:

| Path | Contents |
|---|---|
| `~/.config/scribe/config.json` | Settings, above |
| `~/.config/scribe/dictionary.json` | Replacement rules |
| `~/.config/scribe/models/` | The downloaded speech model |
| `~/.config/scribe/app/` | Installed app code (`pipeline.py`, `stream_worker.py`, `dictate.lua`, `scribe_setup.py`) |
| `~/.config/scribe/state/` | Recording, transcript, and server log files (see Privacy, below) |

## How it works

```
  hold hotkey
      |
      v
  [ ffmpeg ]  --records the configured mic-->  16kHz mono audio
      |
      v
  [ whisper-server ]   (warm, launchd, bound to 127.0.0.1 only)
      |  raw transcript
      v
  [ dictionary.json ]  --deterministic word-boundary fixes-->
      |
      v
  [ loop collapser ]   --removes transcriber stutter repeats-->
      |
      v
  (optional, opt-in)
  [ claude -p polish ] --context homophones, grammar, your own Claude CLI-->
      |
      v
  clipboard + paste at cursor
```

Streaming mode runs the same stages, but hands off finished stretches of speech to the warm
server while you are still talking, cutting only at real pauses, so release only has a short
tail left to transcribe.

## Privacy and data processing

**Audio.** Captured from the microphone you selected and sent only to a whisper.cpp server
bound to `127.0.0.1` (localhost only, never reachable over the network). Audio never leaves
the machine. What happens to the file on disk depends on the mode:

- **Batch mode** (default) records to a single file, `~/.config/scribe/state/dictation.wav`,
  and deletes it as soon as it has been transcribed successfully. If transcription fails,
  the file is kept so the words can be recovered by hand, and it is then overwritten by your
  next batch dictation.
- **Streaming mode** slices your speech into short chunks as you talk. Each chunk's audio
  file is deleted as soon as it transcribes successfully. If a chunk fails to transcribe
  twice in a row, its audio file is deliberately kept on disk instead of being deleted, so
  the words can be recovered by hand; nothing removes it automatically after that.

To clear any of this yourself, delete the files under `~/.config/scribe/state/` directly, or
run `uninstall.sh` and agree to delete `~/.config/scribe` (see Uninstall, below).

**Text.** Every dictation writes two files under `~/.config/scribe/state/`: `last-dict.txt`
(the plain, un-polished result) and `last-output.txt` (the text that was actually pasted,
used by the recall hotkey). Neither is deleted automatically; each is simply overwritten by
your next dictation. Transcripts also sit on the macOS clipboard after every dictation.
Nothing is sent anywhere by default.

**The one optional network flow of dictated content.** If you enable the AI polish pass, the
*text* of a dictation (never the audio) is sent to Anthropic's Claude through your own,
locally installed Claude CLI, under your own account and subject to its own terms. It is off
by default, opt-in during setup, and only runs when you invoke it.

**Other network traffic.** A one-time model download from Hugging Face at install time, and
Homebrew package installs. No telemetry, no analytics, no accounts, no update phone-home.
Scribe operates no servers of its own.

### GDPR, in plain language

This is informational, not legal advice.

Because all core processing happens on-device, using Scribe does not by itself transmit
personal data to the Scribe project or to any third party. The project's maintainers never
see your data and operate no processing infrastructure, so normal use does not create a
controller/processor relationship with the project.

Purely personal or household use falls outside the GDPR's material scope in any case (the
household exemption, Art. 2(2)(c)).

For professional or organizational use, your organization remains the controller of whatever
content you dictate, which may include personal data of third parties (a colleague's name in
a note, for instance). Keeping the processing local generally simplifies that assessment:
there is no third-country transfer and no new processor to account for, since the data never
leaves a device your organization already governs.

If you enable the optional AI polish in a professional context, the dictated text goes to
Anthropic under your own agreement with Anthropic. Treat that flow the way you would treat any
other use of your Claude subscription, under Anthropic's own commercial terms as between you
and Anthropic, and check your organization's position before enabling it if you regularly
dictate other people's personal data.

Everything Scribe writes lives under `~/.config/scribe/` (config, dictionary, model, and the
state/transcript files described above), so a DPO-minded reviewer can inspect all of it
directly. The uninstaller removes it on request (see Uninstall, below).

### Known limitations

For a security-conscious reader, in plain terms:

- The local transcription server (`127.0.0.1`, never exposed to the network) has no
  authentication of its own. Any process running as your user can send it audio and get a
  transcript back.
- `config.json` and `dictionary.json` are read and acted on, not just stored data. Do not
  paste in a config file someone else gave you without reading it first, particularly the
  `claude_bin` key, which controls what binary gets executed during the polish pass.
- Scribe runs inside Hammerspoon and relies on Hammerspoon's own Accessibility permission to
  paste text into other apps. That permission is keystroke-level. Anything able to write to
  `~/.config/scribe/app/` (where `dictate.lua` and the Python scripts are installed) gains
  that same level of access.

## Troubleshooting

**Server not up.**
```
launchctl list | grep com.scribe.whisper-server        # want a real PID
launchctl unload ~/Library/LaunchAgents/com.scribe.whisper-server.plist
launchctl load -w ~/Library/LaunchAgents/com.scribe.whisper-server.plist
tail -f ~/.config/scribe/state/whisper-server.err
tail -f ~/.config/scribe/state/whisper-server.log
```

**Nothing pastes.** Check System Settings > Privacy & Security > Accessibility. Hammerspoon
must be enabled there, and its own window must not still show a "not enabled" warning; if it
does, toggle the entry off and back on and reload Hammerspoon.

**Wrong microphone, or no audio.** Rerun the setup wizard and pick the microphone again:
```
python3 ~/.config/scribe/app/scribe_setup.py
```

**A note on iPhone Continuity.** Scribe resolves its microphone by name, on purpose, rather
than by device index. The index macOS assigns can silently become your iPhone's Continuity
microphone when it is nearby; matching by name keeps Scribe on the mic you actually configured.

## Uninstall

```
bash uninstall.sh
```

This always removes the launchd service and the Hammerspoon loader block. It asks before
deleting `~/.config/scribe` (your config, dictionary, and the speech model). It never touches
the Homebrew packages themselves; the script prints the commands to remove those yourself if
you want to.

## License

MIT. See [LICENSE](LICENSE).
