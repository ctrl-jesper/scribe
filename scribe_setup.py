#!/usr/bin/env python3
"""Scribe first-run setup wizard.

Asks a short set of questions and writes ~/.config/scribe/config.json and
~/.config/scribe/dictionary.json. Safe to run again at any time: every answer
is pre-filled with what you chose last time, shown in [brackets].

    python3 ~/.config/scribe/app/scribe_setup.py

Standard library only, and written for the python3 that ships with macOS.

Run `python3 scribe_setup.py --selftest` to exercise the pure functions
without touching your config.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

CONFIG_HOME = os.path.expanduser("~/.config/scribe")
CONFIG_PATH = os.path.join(CONFIG_HOME, "config.json")
DICTIONARY_PATH = os.path.join(CONFIG_HOME, "dictionary.json")
APP_DIR = os.path.join(CONFIG_HOME, "app")

# Schema version of config.json. Bump it, and add a migration, when the meaning
# of an existing key changes. Version 1 is the first numbered schema; a file
# without the key was written before numbering started and is compatible as-is.
CONFIG_VERSION = 1

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "language": "en",
    # Deliberately empty. Naming any particular Mac's microphone here would be
    # wrong on every other machine, and dictate.lua refuses to record (and says
    # so) rather than guessing a device index.
    "mic_name": "",
    "hotkey_keycode": 61,
    "hotkey_flag": "alt",
    "server_port": 8090,
    "model_file": "ggml-large-v3-turbo-q5_0.bin",
    "vocabulary": [],
    "speaker_note": "",
    "mode": "dict",
    "polish_enabled": False,
    "claude_bin": "~/.local/bin/claude",
    "claude_model": "claude-haiku-4-5-20251001",
    # Absolute paths resolved by install.sh: Homebrew is /opt/homebrew on Apple
    # Silicon and /usr/local on Intel, and the working python3 is not always
    # /usr/bin/python3. Empty means "let dictate.lua fall back".
    "ffmpeg_bin": "",
    "python_bin": "",
}

# An input whose loudest sample sits below this is almost certainly not hearing
# you: a muted device, or the wrong one.
SILENCE_DBFS = -50.0

# Names that usually mean the microphone built into the Mac itself, which is
# the right default: it never disappears the way a phone or headset does.
BUILT_IN_HINTS = ("macbook", "built-in", "built in", "imac", "mac mini", "mac studio")

# One line from `ffmpeg -f avfoundation -list_devices true -i ""`, e.g.
#   [AVFoundation indev @ 0x7f8] [1] Some Microphone
DEVICE_LINE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*\[(\d+)\]\s+(.*\S)\s*$")
AUDIO_HEADER = "audio devices"
VIDEO_HEADER = "video devices"


# ---------------------------------------------------------------------------
# Pure functions (covered by --selftest)
# ---------------------------------------------------------------------------


def parse_audio_devices(listing: str) -> List[Tuple[int, str]]:
    """Return [(avfoundation index, device name)] from ffmpeg's device listing.

    ffmpeg prints video devices first, then audio devices, both numbered from
    zero. Only the lines after the audio header belong to us.
    """
    devices = []  # type: List[Tuple[int, str]]
    in_audio_section = False
    for line in listing.splitlines():
        lowered = line.lower()
        if AUDIO_HEADER in lowered:
            in_audio_section = True
            continue
        if VIDEO_HEADER in lowered:
            in_audio_section = False
            continue
        if not in_audio_section:
            continue
        match = DEVICE_LINE.match(line)
        if match:
            devices.append((int(match.group(1)), match.group(2)))
    return devices


def default_device_position(devices: Sequence[Tuple[int, str]], previous_name: str = "") -> int:
    """Pick which entry of `devices` to offer as the default, by list position.

    Preference order: the device the user picked last time, then anything that
    looks like the Mac's own microphone, then the first device.
    """
    if not devices:
        return 0
    if previous_name:
        for position, (_, name) in enumerate(devices):
            if name == previous_name:
                return position
    for position, (_, name) in enumerate(devices):
        lowered = name.lower()
        if any(hint in lowered for hint in BUILT_IN_HINTS):
            return position
    return 0


def looks_like_language_code(text: str) -> bool:
    """Accept whisper-style codes (en, da, nan) and the literal 'auto'."""
    candidate = text.strip().lower()
    return candidate == "auto" or bool(re.match(r"^[a-z]{2,3}$", candidate))


def split_terms(text: str) -> List[str]:
    """Split a comma-separated answer into trimmed, non-empty terms."""
    return [part.strip() for part in text.split(",") if part.strip()]


def merge_terms(existing: Sequence[str], added: Sequence[str]) -> List[str]:
    """Merge two term lists: existing order first, no case-insensitive dupes."""
    merged = []  # type: List[str]
    seen = set()  # type: set
    for term in list(existing) + list(added):
        term = term.strip()
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(term)
    return merged


def parse_pairs(text: str) -> Tuple[Dict[str, str], List[str]]:
    """Parse "Wrong=Right, Other=Thing" into a dict, plus the entries skipped.

    An entry is skipped when it has no '=' or an empty side, because a
    replacement rule with a blank half would silently eat text.
    """
    pairs = {}  # type: Dict[str, str]
    skipped = []  # type: List[str]
    for entry in split_terms(text):
        if entry.count("=") != 1:
            skipped.append(entry)
            continue
        wrong, right = entry.split("=", 1)
        wrong, right = wrong.strip(), right.strip()
        if not wrong or not right:
            skipped.append(entry)
            continue
        pairs[wrong] = right
    return pairs, skipped


def merge_replacements(existing: Dict[str, str], added: Dict[str, str]) -> Dict[str, str]:
    """Merge dictionary rules, keeping existing order and letting new win."""
    merged = dict(existing)
    for wrong, right in added.items():
        merged[wrong] = right
    return merged


def merge_config(existing: Dict, answers: Dict) -> Dict:
    """Layer defaults, the config on disk, and this run's answers.

    Keys the wizard does not ask about are carried through untouched, so a
    hand-edited setting survives a rerun.
    """
    merged = dict(DEFAULT_CONFIG)
    merged.update(existing or {})
    merged.update(answers or {})
    merged["config_version"] = CONFIG_VERSION
    return merged


def parse_max_volume(output: str) -> Optional[float]:
    """Return the max_volume in dBFS that ffmpeg's volumedetect filter reported.

    The line looks like:
        [Parsed_volumedetect_0 @ 0x7f8] max_volume: -13.4 dB
    None means ffmpeg never reported one, which is not the same as silence.
    """
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", output)
    if match:
        return float(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def load_json(path: str, fallback: Dict) -> Dict:
    try:
        with open(path) as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return dict(fallback)
    except (ValueError, OSError) as error:
        print("  Could not read %s (%s). Starting from defaults." % (path, error))
        return dict(fallback)
    if not isinstance(loaded, dict):
        print("  %s is not a JSON object. Starting from defaults." % path)
        return dict(fallback)
    return loaded


def write_json_atomic(path: str, data: Dict) -> None:
    """Write JSON via a temp file in the same directory, then rename.

    A half-written config.json would break every dictation until repaired, so
    the file is only ever replaced whole.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


# ---------------------------------------------------------------------------
# Environment probes
# ---------------------------------------------------------------------------


def server_reachable(port: int) -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # answered, just not with a page
    except Exception:
        return False


def ffmpeg_binary(config: Dict) -> str:
    """Absolute ffmpeg path from config.json, else whatever is on PATH.

    install.sh writes ffmpeg_bin because the Homebrew prefix differs between
    Apple Silicon (/opt/homebrew) and Intel (/usr/local).
    """
    configured = str(config.get("ffmpeg_bin", "") or "").strip()
    if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
        return configured
    return shutil.which("ffmpeg") or "ffmpeg"


def list_devices_output(ffmpeg: str = "ffmpeg") -> str:
    """Ask ffmpeg for the device list. It always exits non-zero here."""
    try:
        result = subprocess.run(
            [ffmpeg, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stdin=subprocess.DEVNULL,  # ffmpeg reads stdin for commands; keep it off ours
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except FileNotFoundError:
        print("  ffmpeg not found on PATH. Install it with: brew install ffmpeg")
        return ""
    except subprocess.TimeoutExpired:
        print("  ffmpeg did not answer within 20 seconds.")
        return ""
    return result.stdout.decode("utf-8", "replace")


def probe_microphone(ffmpeg: str, index: int, seconds: int = 2) -> Optional[float]:
    """Record briefly from one avfoundation input and return its peak in dBFS.

    None means the level could not be measured at all (ffmpeg missing, the
    device refused to open, macOS has not granted microphone access yet). That
    is reported as "could not check", never as a failure: this whole step is a
    convenience, and a wrong answer here must not block setup.
    """
    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-nostdin",
                "-f", "avfoundation", "-i", ":%d" % index,
                "-t", str(seconds),
                "-af", "volumedetect",
                "-f", "null", "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=seconds + 20,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    return parse_max_volume(result.stdout.decode("utf-8", "replace"))


def find_claude_bin(configured: str) -> Optional[str]:
    candidate = os.path.expanduser(configured or "")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("claude")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def ask(question: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (question, suffix)).strip()
    except EOFError:
        print()
        return default
    return answer or default


def ask_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            answer = input("%s%s: " % (question, suffix)).strip().lower()
        except EOFError:
            print()
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_device(devices: Sequence[Tuple[int, str]], previous_name: str) -> str:
    default_position = default_device_position(devices, previous_name)
    for position, (index, name) in enumerate(devices):
        marker = "*" if position == default_position else " "
        print("   %s %d) %s (avfoundation index %d)" % (marker, position + 1, name, index))
    while True:
        answer = ask("  Pick a microphone", str(default_position + 1))
        try:
            position = int(answer) - 1
        except ValueError:
            print("  Enter one of the numbers above.")
            continue
        if 0 <= position < len(devices):
            return devices[position][1]
        print("  Enter one of the numbers above.")


# ---------------------------------------------------------------------------
# The interview
# ---------------------------------------------------------------------------


def check_microphone(
    ffmpeg: str, devices: Sequence[Tuple[int, str]], chosen_name: str
) -> None:
    """Record two seconds from the chosen mic and report the peak level.

    Skippable and never fatal: it either reassures the user that the device they
    picked can hear them, or warns that it cannot, and setup continues either
    way.
    """
    index = None
    for device_index, name in devices:
        if name == chosen_name:
            index = device_index
            break
    if index is None:
        return

    print("\nMICROPHONE CHECK (optional)")
    print("   Records 2 seconds from '%s' and reports how loud it was." % chosen_name)
    print("   The first run may ask macOS for microphone access for your terminal.")
    if not ask_yes_no("  Run the check now (say something while it records)", True):
        print("   Skipped.")
        return

    print("   Recording 2 seconds, speak now ...")
    try:
        peak = probe_microphone(ffmpeg, index)
    except Exception as error:  # a convenience check must not break setup
        print("   Could not run the check (%s). Skipping it." % error)
        return

    if peak is None:
        print("   Could not measure a level. That usually means macOS has not")
        print("   granted microphone access yet, or the device is in use. Not a")
        print("   problem for setup: grant access when Scribe first records.")
    elif peak <= SILENCE_DBFS:
        print("   WARNING: peak level %.1f dBFS, which is effectively silence." % peak)
        print("   '%s' may be muted, or may not be the device you speak into." % chosen_name)
        print("   Rerun this wizard and pick a different one if dictation comes back empty.")
    else:
        print("   Peak level %.1f dBFS. That microphone is hearing you." % peak)


def run_wizard() -> int:
    config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    dictionary = load_json(DICTIONARY_PATH, {"replacements": {}})
    replacements = dictionary.get("replacements") or {}
    if not isinstance(replacements, dict):
        replacements = {}
    answers = {}  # type: Dict

    print()
    print("Scribe setup")
    print("Scribe turns held-key speech into typed text, entirely on this Mac.")
    print("Six short questions, then an optional microphone check.")
    print("Press Return to keep the value in [brackets].")

    port = int(config.get("server_port", DEFAULT_CONFIG["server_port"]))
    if server_reachable(port):
        print("\n  Transcription server: running on port %d." % port)
    else:
        print("\n  Transcription server: not answering on port %d yet." % port)
        print("  That is fine during setup. The installer starts it for you.")

    # 1. Microphone -----------------------------------------------------------
    print("\n1. MICROPHONE")
    print("   Scribe stores the microphone by name, so it still works after you")
    print("   plug in a headset or your phone offers itself as a camera.")
    ffmpeg = ffmpeg_binary(config)
    devices = parse_audio_devices(list_devices_output(ffmpeg))
    previous_mic = str(config.get("mic_name", "") or "")
    if devices:
        answers["mic_name"] = ask_device(devices, previous_mic)
    else:
        print("   No devices found. Type the name exactly as macOS shows it.")
        answers["mic_name"] = ask("  Microphone name", previous_mic)

    # 2. Language -------------------------------------------------------------
    print("\n2. LANGUAGE")
    print("   Pinning one language beats auto-detect for accented speech, because")
    print("   the model stops second-guessing which language it is hearing.")
    previous_language = str(config.get("language", "en") or "en")
    while True:
        language = ask("  Primary language code (en, da, de, fr, sv, ...)", previous_language)
        if looks_like_language_code(language):
            answers["language"] = language.strip().lower()
            break
        print("   That is not a language code. Use two or three letters, or 'auto'.")

    # 3. Vocabulary and corrections ------------------------------------------
    print("\n3. YOUR WORDS")
    print("   Dictation garbles names it has never seen. List the people,")
    print("   companies, products, and jargon you say often. Leave empty to skip.")
    existing_vocabulary = config.get("vocabulary") or []
    if not isinstance(existing_vocabulary, list):
        existing_vocabulary = []
    if existing_vocabulary:
        print("   Already known: %s" % ", ".join(str(v) for v in existing_vocabulary))
    added_vocabulary = split_terms(ask("  Names and terms, comma separated", ""))
    answers["vocabulary"] = merge_terms([str(v) for v in existing_vocabulary], added_vocabulary)

    print("\n   Optional: fixed corrections, if you already know what it gets wrong.")
    print("   Format: wrong=right, comma separated. Example: Akme=Acme, Kubernets=Kubernetes")
    if replacements:
        print("   Already known: %d rule(s)." % len(replacements))
    new_pairs, skipped = parse_pairs(ask("  Corrections", ""))
    for entry in skipped:
        print("   Skipped '%s': it needs exactly one = with text on both sides." % entry)
    replacements = merge_replacements(replacements, new_pairs)

    # 4. Speaker note ---------------------------------------------------------
    print("\n4. SPEAKER NOTE (optional)")
    print("   One line about how you speak, used only by the optional AI polish.")
    print("   Example: Danish native speaker dictating in English.")
    answers["speaker_note"] = ask("  Speaker note", str(config.get("speaker_note", "") or ""))

    # 5. Polish ---------------------------------------------------------------
    print("\n5. AI POLISH (optional)")
    claude_bin = find_claude_bin(str(config.get("claude_bin", "") or ""))
    if claude_bin:
        print("   Found the Claude CLI at %s." % claude_bin)
        print("   Polish cleans up filler and punctuation through your own Claude")
        print("   subscription on this machine, takes about 10 seconds, and is")
        print("   opt-in per use from the menu-bar icon or its hotkey.")
        answers["claude_bin"] = claude_bin
        answers["polish_enabled"] = ask_yes_no(
            "  Enable polish", bool(config.get("polish_enabled", False))
        )
    else:
        print("   Claude CLI not found, so polish stays off. Plain dictation is")
        print("   unaffected. To enable it later, install the Claude CLI and run")
        print("   this wizard again.")
        answers["polish_enabled"] = False

    # Microphone check --------------------------------------------------------
    # Catches the wrong-mic case here rather than after a dictation comes back
    # empty. Optional, and never fatal.
    check_microphone(ffmpeg, devices, str(answers.get("mic_name", "") or ""))

    # 6. Write ----------------------------------------------------------------
    final_config = merge_config(config, answers)
    # Keep any other top-level keys the dictionary file carries, such as the
    # explanatory comment shipped with the example.
    final_dictionary = dict(dictionary)
    final_dictionary["replacements"] = replacements

    write_json_atomic(CONFIG_PATH, final_config)
    write_json_atomic(DICTIONARY_PATH, final_dictionary)

    print_summary(final_config, replacements)
    return 0


def print_summary(config: Dict, replacements: Dict[str, str]) -> None:
    vocabulary = config.get("vocabulary") or []
    rows = [
        ("Microphone", str(config.get("mic_name") or "(not set)")),
        ("Language", str(config.get("language"))),
        ("Server port", str(config.get("server_port"))),
        ("Vocabulary terms", str(len(vocabulary))),
        ("Correction rules", str(len(replacements))),
        ("Speaker note", str(config.get("speaker_note") or "(none)")),
        ("AI polish", "on" if config.get("polish_enabled") else "off"),
    ]
    width = max(len(label) for label, _ in rows)

    print("\n" + "-" * (width + 26))
    print("Your settings")
    print("-" * (width + 26))
    for label, value in rows:
        print("  %-*s  %s" % (width, label, value))
    print("-" * (width + 26))

    print(
        """
Saved to:
  {config}
  {dictionary}

Next:
  1. Open Hammerspoon, click its menu-bar icon, choose "Reload config".
  2. Grant Accessibility: System Settings > Privacy & Security >
     Accessibility > enable Hammerspoon.
  3. Grant Microphone the first time you record, then record once more.

Using it: hold RIGHT OPTION, wait for the "speak" cue, talk, release.
Change these answers later: python3 {app}/scribe_setup.py
""".format(config=CONFIG_PATH, dictionary=DICTIONARY_PATH, app=APP_DIR)
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

SAMPLE_LISTING = """ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers
[AVFoundation indev @ 0x7f9] AVFoundation video devices:
[AVFoundation indev @ 0x7f9] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f9] [1] Capture screen 0
[AVFoundation indev @ 0x7f9] AVFoundation audio devices:
[AVFoundation indev @ 0x7f9] [0] External Phone Microphone
[AVFoundation indev @ 0x7f9] [1] MacBook Air Microphone
[AVFoundation indev @ 0x7f9] [2] Conference App Audio
[in#0 @ 0x7f9] Error opening input: Input/output error
"""


def claude_login_line() -> str:
    """One informational line about the Claude CLI's login state, for the selftest.

    The check itself lives in pipeline.py so the wizard, dictate.lua and the dictation paths
    all agree on what "logged out" means. Every failure is reported as "cannot be determined":
    this is information, never a verdict, and it must never fail the selftest.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pipeline

        state = pipeline.claude_auth_state(pipeline.load_config())
    except Exception as exc:
        return "claude CLI login: cannot be determined (%s)" % exc
    return {
        pipeline.AUTH_LOGGED_IN: "claude CLI login: logged in",
        pipeline.AUTH_LOGGED_OUT:
            "claude CLI login: NOT logged in; polish and prompt mode will paste raw text "
            "until you run: claude /login",
        pipeline.AUTH_NO_CLI:
            "claude CLI login: not configured (no CLI at the configured claude_bin), so the "
            "optional polish and prompt passes are off",
    }.get(state, "claude CLI login: cannot be determined")


def run_selftest() -> int:
    checks = 0

    devices = parse_audio_devices(SAMPLE_LISTING)
    assert devices == [
        (0, "External Phone Microphone"),
        (1, "MacBook Air Microphone"),
        (2, "Conference App Audio"),
    ], devices
    checks += 1

    assert parse_audio_devices("") == []
    assert parse_audio_devices("no devices here at all") == []
    checks += 2

    # Video devices must never leak into the audio list.
    assert all("Camera" not in name for _, name in devices)
    checks += 1

    assert default_device_position(devices) == 1, "built-in mic should win by default"
    assert default_device_position(devices, "Conference App Audio") == 2, "previous pick wins"
    assert default_device_position(devices, "Gone Away Microphone") == 1, "missing pick falls back"
    assert default_device_position([]) == 0
    checks += 4

    assert split_terms(" a , b ,, c ") == ["a", "b", "c"]
    checks += 1

    assert looks_like_language_code("en") and looks_like_language_code(" DA ")
    assert looks_like_language_code("auto") and looks_like_language_code("nan")
    assert not looks_like_language_code("2")
    assert not looks_like_language_code("english")
    assert not looks_like_language_code("")
    checks += 5

    assert merge_terms(["Alpha", "Beta"], ["beta", "Gamma"]) == ["Alpha", "Beta", "Gamma"], \
        "merge should dedupe case-insensitively and keep order"
    assert merge_terms([], []) == []
    assert merge_terms(["Alpha"], [" ", ""]) == ["Alpha"]
    checks += 3

    pairs, skipped = parse_pairs("Akme=Acme, Kubernets = Kubernetes")
    assert pairs == {"Akme": "Acme", "Kubernets": "Kubernetes"}, pairs
    assert skipped == []
    checks += 2

    pairs, skipped = parse_pairs("broken, =empty, too=many=signs, Good=Fine")
    assert pairs == {"Good": "Fine"}, pairs
    assert skipped == ["broken", "=empty", "too=many=signs"], skipped
    checks += 2

    merged = merge_replacements({"Old": "New", "Keep": "Keep"}, {"Old": "Newer"})
    assert merged == {"Old": "Newer", "Keep": "Keep"}, merged
    assert list(merged) == ["Old", "Keep"], "existing order should survive"
    checks += 2

    config = merge_config(
        {"language": "da", "hand_edited": 7}, {"language": "en", "polish_enabled": True}
    )
    assert config["language"] == "en", "answers beat the file on disk"
    assert config["hand_edited"] == 7, "unknown keys must survive a rerun"
    assert config["hotkey_keycode"] == 61, "defaults fill the gaps"
    assert config["polish_enabled"] is True
    assert set(DEFAULT_CONFIG).issubset(set(config)), "every contract key present"
    checks += 5

    assert DEFAULT_CONFIG["mic_name"] == "", "no machine-specific microphone as a default"
    assert config["config_version"] == CONFIG_VERSION, "the schema version is stamped"
    stamped = merge_config({"config_version": 0}, {})
    assert stamped["config_version"] == CONFIG_VERSION, "an older file is restamped"
    assert "ffmpeg_bin" in DEFAULT_CONFIG and "python_bin" in DEFAULT_CONFIG
    # install.sh writes the real paths; the defaults must not overwrite them.
    kept = merge_config({"ffmpeg_bin": "/usr/local/bin/ffmpeg"}, {})
    assert kept["ffmpeg_bin"] == "/usr/local/bin/ffmpeg", "resolved paths survive a rerun"
    checks += 5

    volumedetect = (
        "[Parsed_volumedetect_0 @ 0x7f8] n_samples: 32000\n"
        "[Parsed_volumedetect_0 @ 0x7f8] mean_volume: -31.2 dB\n"
        "[Parsed_volumedetect_0 @ 0x7f8] max_volume: -13.4 dB\n"
    )
    assert parse_max_volume(volumedetect) == -13.4, parse_max_volume(volumedetect)
    assert parse_max_volume("max_volume: 0.0 dB") == 0.0
    assert parse_max_volume("nothing useful here") is None
    assert parse_max_volume("") is None
    assert -91.0 <= SILENCE_DBFS <= 0.0, "the silence threshold has to be a dBFS value"
    checks += 5

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "config.json")
        write_json_atomic(path, {"language": "en", "vocabulary": ["Acme"]})
        assert load_json(path, {}) == {"language": "en", "vocabulary": ["Acme"]}
        assert os.listdir(directory) == ["config.json"], "no temp file left behind"
        assert load_json(os.path.join(directory, "missing.json"), {"a": 1}) == {"a": 1}
        with open(os.path.join(directory, "bad.json"), "w") as handle:
            handle.write("{not json")
        assert load_json(os.path.join(directory, "bad.json"), {"a": 1}) == {"a": 1}
        checks += 4

    print(claude_login_line())
    print("selftest: %d checks passed" % checks)
    return 0


def main(argv: Sequence[str]) -> int:
    if "--selftest" in argv:
        return run_selftest()
    if not sys.stdin.isatty():
        print(
            "scribe_setup.py needs a terminal to ask its questions.\n"
            "Run it directly: python3 %s/scribe_setup.py" % APP_DIR,
            file=sys.stderr,
        )
        return 1
    try:
        return run_wizard()
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was written.")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
