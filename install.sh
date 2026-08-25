#!/usr/bin/env bash
#
# Scribe installer - local push-to-talk dictation for macOS.
#
# Safe to run more than once: an upgrade refreshes the app code and the
# launchd service but never overwrites your config, dictionary, or model.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_HOME="$HOME/.config/scribe"
APP_DIR="$CONFIG_HOME/app"
MODELS_DIR="$CONFIG_HOME/models"
STATE_DIR="$CONFIG_HOME/state"
CONFIG_FILE="$CONFIG_HOME/config.json"
DICTIONARY_FILE="$CONFIG_HOME/dictionary.json"
VERSION_FILE="$CONFIG_HOME/VERSION"

# The schema version of config.json. Bump it here and add a migration when the
# meaning of an existing key changes.
CONFIG_VERSION=1

MODEL_FILE="ggml-large-v3-turbo-q5_0.bin"
# Pinned to an immutable commit rather than a branch, so the bytes cannot change
# under us, and verified against a pinned SHA-256 after download.
#   SHA-256 taken from Hugging Face's LFS metadata for this file
#   (api/models/ggerganov/whisper.cpp/tree/main -> lfs.oid) and confirmed against
#   a copy already on disk. The repo's own README publishes the matching SHA-1
#   e050f7970618a659205450ad97eb95a18d69c9ee as a third check.
MODEL_COMMIT="5359861c739e955e79d9a303bcbc70fb988958b1"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/$MODEL_COMMIT/$MODEL_FILE"
MODEL_SHA256="394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
MODEL_BYTES=574041195
MODEL_PATH="$MODELS_DIR/$MODEL_FILE"

PLIST_LABEL="com.scribe.whisper-server"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

# The author's private predecessor of Scribe used this label. Left loaded it
# serves a second copy of the model and holds a port. Detected, never removed
# without asking: this installer did not create it.

HS_INIT="$HOME/.hammerspoon/init.lua"
HS_BEGIN="-- BEGIN scribe"
HS_END="-- END scribe"

DEFAULT_PORT=8090

# Runtime files copied from the repo into the app dir on every run.
RUNTIME_FILES=(pipeline.py stream_worker.py dictate.lua scribe_setup.py)

SCRIBE_VERSION="$(tr -d '[:space:]' <"$REPO_DIR/VERSION" 2>/dev/null || true)"
[ -n "$SCRIBE_VERSION" ] || SCRIBE_VERSION="unknown"

PREVIOUS_VERSION=""
[ -f "$VERSION_FILE" ] && PREVIOUS_VERSION="$(tr -d '[:space:]' <"$VERSION_FILE" 2>/dev/null || true)"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Ask a yes/no question, but only when a terminal is attached. Piped installs
# (curl ... | bash) get the safe answer without hanging.
ask_yes_no() {
    local question="$1" answer=""
    if [ ! -t 0 ]; then
        info "$question -> no (no terminal attached)"
        return 1
    fi
    printf '    %s [y/N] ' "$question"
    read -r answer
    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# 1. Platform guard
# ---------------------------------------------------------------------------

step "Checking the platform"
[ "$(uname -s)" = "Darwin" ] || die "Scribe is macOS only (found $(uname -s))."
info "macOS $(sw_vers -productVersion) on $(uname -m)"
if [ -n "$PREVIOUS_VERSION" ] && [ "$PREVIOUS_VERSION" != "$SCRIBE_VERSION" ]; then
    info "upgrading Scribe $PREVIOUS_VERSION -> $SCRIBE_VERSION"
else
    info "installing Scribe $SCRIBE_VERSION"
fi

command -v python3 >/dev/null 2>&1 || die "python3 not found. Install the Xcode command line tools: xcode-select --install"

# ---------------------------------------------------------------------------
# 2. Homebrew
# ---------------------------------------------------------------------------

step "Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
    cat >&2 <<'MISSING_BREW'

ERROR: Homebrew is required and was not found.

Install it yourself (Scribe will not install it for you):

  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

Follow the "Next steps" Homebrew prints so that `brew` lands on your PATH,
then run this installer again.
MISSING_BREW
    exit 1
fi

BREW_PREFIX="$(brew --prefix)"
info "Homebrew at $BREW_PREFIX"

# ---------------------------------------------------------------------------
# 3. Dependencies
# ---------------------------------------------------------------------------

step "Installing dependencies (whisper-cpp, ffmpeg, Hammerspoon)"

install_formula() {
    local name="$1"
    if brew list --formula --versions "$name" >/dev/null 2>&1; then
        info "$name already installed, skipping"
    else
        info "installing $name ..."
        brew install --quiet --formula "$name"
    fi
}

install_cask() {
    local name="$1"
    if brew list --cask --versions "$name" >/dev/null 2>&1; then
        info "$name (cask) already installed, skipping"
    elif [ -d "/Applications/Hammerspoon.app" ]; then
        info "Hammerspoon.app already present outside Homebrew, skipping"
    else
        info "installing $name (cask) ..."
        brew install --quiet --cask "$name"
    fi
}

install_formula whisper-cpp
install_formula ffmpeg
install_cask hammerspoon

# ---------------------------------------------------------------------------
# 4. Resolving the binaries Scribe runs
# ---------------------------------------------------------------------------
#
# dictate.lua used to hardcode /opt/homebrew, which is the Apple Silicon prefix;
# on an Intel Mac Homebrew lives in /usr/local and recording failed. The paths
# are resolved here and written into config.json instead.

step "Resolving the binaries Scribe runs"

absolute_executable() {
    # Echo the argument only if it is an absolute path to an executable file.
    case "${1:-}" in
        /*) [ -x "$1" ] && printf '%s\n' "$1" ;;
    esac
}

WHISPER_SERVER="$(absolute_executable "$BREW_PREFIX/bin/whisper-server" || true)"
[ -n "$WHISPER_SERVER" ] || WHISPER_SERVER="$(absolute_executable "$(command -v whisper-server || true)" || true)"
[ -n "$WHISPER_SERVER" ] || die "whisper-server not found after installing whisper-cpp."
info "whisper-server at $WHISPER_SERVER"

FFMPEG_BIN="$(absolute_executable "$BREW_PREFIX/bin/ffmpeg" || true)"
[ -n "$FFMPEG_BIN" ] || FFMPEG_BIN="$(absolute_executable "$(command -v ffmpeg || true)" || true)"
[ -n "$FFMPEG_BIN" ] || die "ffmpeg not found after installing it. Try: brew install ffmpeg"
info "ffmpeg at $FFMPEG_BIN"

# /usr/bin/python3 can be an Xcode command line tools stub that prints a prompt
# and exits 1, so being executable is not enough: it has to actually run.
python_runs() {
    [ -x "${1:-}" ] || return 1
    "$1" -c 'import json, sys' >/dev/null 2>&1
}

PYTHON_BIN=""
for candidate in "/usr/bin/python3" "$BREW_PREFIX/bin/python3" "$(command -v python3 || true)"; do
    candidate="$(absolute_executable "$candidate" || true)"
    [ -n "$candidate" ] || continue
    if python_runs "$candidate"; then
        PYTHON_BIN="$candidate"
        break
    fi
    info "$candidate does not run (a stub?), trying the next candidate"
done
[ -n "$PYTHON_BIN" ] || die "No working python3 found. Install the Xcode command line tools: xcode-select --install"
info "python3 at $PYTHON_BIN"

# ---------------------------------------------------------------------------
# 5. A stale service from the private predecessor
# ---------------------------------------------------------------------------

step "Creating $CONFIG_HOME"
mkdir -p "$APP_DIR" "$MODELS_DIR" "$STATE_DIR"
# 0700 throughout: recordings and transcripts are the most private thing on the
# machine, and the defaults would let any other local account read them. chmod
# runs on every install so existing 0755 directories are tightened on upgrade.
chmod 700 "$CONFIG_HOME" "$APP_DIR" "$MODELS_DIR" "$STATE_DIR"

for f in "${RUNTIME_FILES[@]}"; do
    [ -f "$REPO_DIR/$f" ] || die "Missing runtime file in the repo: $f"
done

for f in "${RUNTIME_FILES[@]}"; do
    # rm first: a plain cp writes THROUGH a symlink sitting at the destination.
    rm -f "$APP_DIR/$f"
    install -m 600 "$REPO_DIR/$f" "$APP_DIR/$f"
    info "installed app/$f"
done

if [ -f "$REPO_DIR/VERSION" ]; then
    rm -f "$VERSION_FILE"
    install -m 600 "$REPO_DIR/VERSION" "$VERSION_FILE"
    info "installed VERSION ($SCRIBE_VERSION)"
else
    warn "no VERSION file in the repo; the installed copy will not record a version"
fi

# User data is only seeded, never overwritten.
seed_file() {
    local example="$1" target="$2" label="$3"
    if [ -f "$target" ]; then
        info "$label already exists, keeping yours"
        return 0
    fi
    if [ -f "$example" ]; then
        rm -f "$target"
        install -m 600 "$example" "$target"
        info "created $label from the example"
        return 0
    fi
    return 1
}

config_existed="no"; [ -f "$CONFIG_FILE" ] && config_existed="yes"
dictionary_existed="no"; [ -f "$DICTIONARY_FILE" ] && dictionary_existed="yes"

if ! seed_file "$REPO_DIR/config.example.json" "$CONFIG_FILE" "config.json"; then
    cat >"$CONFIG_FILE" <<CONFIG_DEFAULT
{
  "config_version": $CONFIG_VERSION,
  "language": "en",
  "mic_name": "",
  "hotkey_keycode": 61,
  "hotkey_flag": "alt",
  "server_port": $DEFAULT_PORT,
  "model_file": "$MODEL_FILE",
  "vocabulary": [],
  "speaker_note": "",
  "mode": "dict",
  "polish_enabled": false,
  "claude_bin": "~/.local/bin/claude",
  "claude_model": "claude-haiku-4-5-20251001",
  "ffmpeg_bin": "",
  "python_bin": ""
}
CONFIG_DEFAULT
    chmod 600 "$CONFIG_FILE"
    info "created config.json with built-in defaults"
fi

if ! seed_file "$REPO_DIR/dictionary.example.json" "$DICTIONARY_FILE" "dictionary.json"; then
    printf '{\n  "replacements": {}\n}\n' >"$DICTIONARY_FILE"
    chmod 600 "$DICTIONARY_FILE"
    info "created dictionary.json with built-in defaults"
fi

# The example files carry sample words so the format is obvious to read. Those
# samples must not become the user's actual settings, so a freshly seeded file
# keeps the schema and any comments but starts empty. An existing file is
# never touched.
#
# mic_name deliberately starts EMPTY rather than naming any particular Mac's
# microphone: an unresolvable name makes dictate.lua refuse to record and say so,
# whereas a wrong name that happens to resolve records from the wrong device.
if [ "$config_existed" = "no" ] || [ "$dictionary_existed" = "no" ]; then
    "$PYTHON_BIN" - "$CONFIG_FILE" "$config_existed" "$DICTIONARY_FILE" "$dictionary_existed" <<'CLEAR_SAMPLES'
import json, os, sys, tempfile

def rewrite(path, updates):
    with open(path) as fh:
        data = json.load(fh)
    data.update(updates)
    directory = os.path.dirname(path) or "."
    handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(handle, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

config_path, config_existed, dictionary_path, dictionary_existed = sys.argv[1:5]
if config_existed == "no":
    rewrite(config_path, {"vocabulary": [], "mic_name": "", "speaker_note": ""})
if dictionary_existed == "no":
    rewrite(dictionary_path, {"replacements": {}})
CLEAR_SAMPLES
    info "cleared the example words from the new config"
fi

# ---------------------------------------------------------------------------
# 7. Speech model
# ---------------------------------------------------------------------------

step "Speech model ($MODEL_FILE, about 547 MB)"

model_hash_ok() {
    [ -f "$1" ] || return 1
    printf '%s  %s\n' "$MODEL_SHA256" "$1" | shasum -a 256 --check --status -
}

if model_hash_ok "$MODEL_PATH"; then
    info "already downloaded and its SHA-256 matches, skipping"
else
    if [ -f "$MODEL_PATH" ]; then
        warn "the model already on disk does not match the expected SHA-256; downloading it again"
        rm -f "$MODEL_PATH"
    fi
    info "downloading from huggingface.co (about 547 MB, a few minutes)"
    info "pinned commit $MODEL_COMMIT"
    # Downloaded to a .part file and only moved into place once the hash checks
    # out, so a half-finished or tampered file is never usable. No -C -: resuming
    # keeps whatever prefix is already on disk, which is exactly how a poisoned
    # partial file survives a retry.
    rm -f "$MODEL_PATH.part"
    curl --proto '=https' --tlsv1.2 -L --fail --progress-bar \
        -o "$MODEL_PATH.part" "$MODEL_URL" \
        || { rm -f "$MODEL_PATH.part"; die "Model download failed. Check your connection and re-run this installer."; }

    if ! model_hash_ok "$MODEL_PATH.part"; then
        actual="$(shasum -a 256 "$MODEL_PATH.part" | awk '{print $1}')"
        rm -f "$MODEL_PATH.part"
        die "Downloaded model FAILED its integrity check and has been deleted.
       expected SHA-256: $MODEL_SHA256
       got SHA-256:      $actual
       Do not use a model that does not match. Re-run the installer; if it
       fails again, download the file yourself from
       $MODEL_URL and verify it before putting it in $MODELS_DIR."
    fi
    mv "$MODEL_PATH.part" "$MODEL_PATH"
    chmod 600 "$MODEL_PATH"
    info "downloaded and verified ($MODEL_BYTES bytes, SHA-256 matches)"
fi

# ---------------------------------------------------------------------------
# 8. Port selection and finalising config.json
# ---------------------------------------------------------------------------

step "Choosing the transcription server port"

# Unload any previous Scribe service first, so its own port does not look busy.
if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
fi

port_in_use() {
    lsof -iTCP:"$1" -sTCP:LISTEN -n -P >/dev/null 2>&1
}

read_config_port() {
    "$PYTHON_BIN" - "$CONFIG_FILE" "$DEFAULT_PORT" <<'READ_PORT'
import json, sys
path, fallback = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        value = json.load(fh).get("server_port", fallback)
    print(int(value))
except Exception:
    print(fallback)
READ_PORT
}

PORT="$(read_config_port)"
while port_in_use "$PORT"; do
    info "port $PORT is busy, trying $((PORT + 1))"
    PORT=$((PORT + 1))
    [ "$PORT" -lt 8200 ] || die "No free port found between $DEFAULT_PORT and 8199."
done
info "using port $PORT"

"$PYTHON_BIN" - "$CONFIG_FILE" "$PORT" "$MODEL_FILE" "$FFMPEG_BIN" "$PYTHON_BIN" "$CONFIG_VERSION" <<'WRITE_CONFIG'
import json, os, sys, tempfile

path, port, model_file, ffmpeg_bin, python_bin, config_version = sys.argv[1:7]
with open(path) as fh:
    config = json.load(fh)

config["server_port"] = int(port)
config.setdefault("model_file", model_file)
# Absolute, machine-specific paths: Homebrew lives under /opt/homebrew on Apple
# Silicon and /usr/local on Intel, and the working python3 is not always
# /usr/bin/python3. dictate.lua reads both keys.
config["ffmpeg_bin"] = ffmpeg_bin
config["python_bin"] = python_bin
config["config_version"] = int(config_version)
config.setdefault("mic_name", "")

directory = os.path.dirname(path) or "."
handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
with os.fdopen(handle, "w") as fh:
    json.dump(config, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
WRITE_CONFIG
info "wrote server_port, ffmpeg_bin, python_bin and config_version into config.json"

# ---------------------------------------------------------------------------
# 9. launchd service
# ---------------------------------------------------------------------------

step "Installing the launchd service ($PLIST_LABEL)"
mkdir -p "$HOME/Library/LaunchAgents"

# Written with plistlib rather than sed over a template. sed substitution breaks
# on a $HOME containing &, <, > or |: & and \1 are replacement metacharacters,
# and the angle brackets produce invalid XML.
"$PYTHON_BIN" - "$PLIST_PATH" "$PLIST_LABEL" "$WHISPER_SERVER" "$MODEL_PATH" "$PORT" "$STATE_DIR" "$BREW_PREFIX" <<'WRITE_PLIST'
import os, plistlib, sys

path, label, server, model, port, state_dir, brew_prefix = sys.argv[1:8]
plist = {
    "Label": label,
    # 127.0.0.1 is a hard literal on purpose. The transcription server must never
    # be reachable from outside this machine, so this binding is not configurable.
    "ProgramArguments": [
        server, "-m", model,
        "--host", "127.0.0.1",
        "--port", str(int(port)),
        "-t", "4",
    ],
    "EnvironmentVariables": {
        "PATH": os.path.join(brew_prefix, "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": os.path.join(state_dir, "whisper-server.log"),
    "StandardErrorPath": os.path.join(state_dir, "whisper-server.err"),
}
tmp = path + ".tmp"
with open(tmp, "wb") as fh:
    plistlib.dump(plist, fh)
os.replace(tmp, path)
WRITE_PLIST
info "wrote $PLIST_PATH"

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load -w "$PLIST_PATH"
info "service loaded, waiting for it to answer on port $PORT"

server_ready=""
for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$PORT/" || true)"
    if [ "$code" = "200" ]; then
        server_ready="ok"
        break
    fi
    if [ -n "$code" ] && [ "$code" != "000" ]; then
        # Any real HTTP status means the server is up, even if / is not a page.
        server_ready="responding ($code)"
        break
    fi
    sleep 1
done

if [ -n "$server_ready" ]; then
    info "transcription server is up: $server_ready"
else
    warn "the server did not answer on port $PORT within 30 seconds."
    warn "check the log: $STATE_DIR/whisper-server.err"
    warn "and the service state: launchctl list | grep $PLIST_LABEL"
fi

# ---------------------------------------------------------------------------
# 10. Hammerspoon wiring
# ---------------------------------------------------------------------------

step "Wiring Scribe into Hammerspoon"
mkdir -p "$(dirname "$HS_INIT")"
[ -e "$HS_INIT" ] || : >"$HS_INIT"

# A dotfiles repo often symlinks init.lua. Edit the file the link points AT, so
# the link survives instead of being replaced by a regular file.
HS_TARGET="$HS_INIT"
if [ -L "$HS_INIT" ]; then
    HS_TARGET="$("$PYTHON_BIN" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$HS_INIT")"
    info "$HS_INIT is a symlink; editing $HS_TARGET and leaving the link alone"
fi

HS_BACKUP="$HS_TARGET.scribe-backup-$(date +%Y%m%d-%H%M%S)"
cp "$HS_TARGET" "$HS_BACKUP"
info "backed up your init.lua to $HS_BACKUP"

# Drop any previous Scribe block, then append a fresh one. This keeps the
# install idempotent and lets an upgrade change the loader line.
#
# The comparison is whole-line equality, not "contains". A file that merely
# MENTIONS the marker string, for instance in a comment about this installer,
# used to lose everything from that line to the end of the file.
awk -v b="$HS_BEGIN" -v e="$HS_END" '
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    !skip   { print }
' "$HS_TARGET" >"$HS_TARGET.scribe-tmp"

{
    printf '%s\n' "$HS_BEGIN"
    printf '%s\n' 'dofile(os.getenv("HOME") .. "/.config/scribe/app/dictate.lua")'
    printf '%s\n' "$HS_END"
} >>"$HS_TARGET.scribe-tmp"

mv "$HS_TARGET.scribe-tmp" "$HS_TARGET"
info "loader block written to $HS_INIT"

# ---------------------------------------------------------------------------
# 11. Setup wizard
# ---------------------------------------------------------------------------

step "Running the setup wizard"
# The wizard needs a terminal. Piped installs (curl ... | bash) do not have
# one, and a cancelled wizard should not lose the rest of the install, so
# neither case is allowed to abort the script.
if [ -t 0 ]; then
    wizard_status=0
    "$PYTHON_BIN" "$APP_DIR/scribe_setup.py" || wizard_status=$?
    if [ "$wizard_status" -ne 0 ]; then
        warn "setup wizard exited with status $wizard_status."
        warn "everything else is installed. Run it again when ready:"
        warn "  $PYTHON_BIN $APP_DIR/scribe_setup.py"
    fi
else
    info "no terminal attached, skipping the interactive wizard."
    info "run it yourself now: $PYTHON_BIN $APP_DIR/scribe_setup.py"
fi

# Scribe refuses to record until a microphone is chosen, rather than guessing an
# index and recording from whatever sits there. Say so loudly if it is not set.
mic_configured="$("$PYTHON_BIN" - "$CONFIG_FILE" <<'READ_MIC'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print("yes" if str(json.load(fh).get("mic_name", "")).strip() else "no")
except Exception:
    print("no")
READ_MIC
)"
if [ "$mic_configured" != "yes" ]; then
    warn "no microphone is set yet, so Scribe will refuse to record."
    warn "run the wizard and pick one:  $PYTHON_BIN $APP_DIR/scribe_setup.py"
fi

# ---------------------------------------------------------------------------
# 12. Checklist
# ---------------------------------------------------------------------------

cat <<CHECKLIST

==> Scribe $SCRIBE_VERSION installed. Three things left, all in the macOS UI:

  1. Open Hammerspoon (Applications > Hammerspoon), then click its menu-bar
     icon and choose "Reload config". Keep "Launch at login" on.
  2. Grant Accessibility: System Settings > Privacy & Security > Accessibility
     > enable Hammerspoon. Without it the hotkey cannot type into other apps.
  3. Grant Microphone the first time you record: hold the key, speak, and
     approve the prompt macOS shows. Then record once more.

  Using it: hold RIGHT OPTION, wait for the "speak" cue, talk, release.
  The text is typed into whatever app has focus.

  Your files:
    version:     $VERSION_FILE ($SCRIBE_VERSION)
    config:      $CONFIG_FILE
    dictionary:  $DICTIONARY_FILE
    app code:    $APP_DIR
    model:       $MODEL_PATH (SHA-256 verified)
    server logs: $STATE_DIR
    init.lua backup: $HS_BACKUP

  ~/.config/scribe is 0700: your recordings and transcripts stay readable by
  you alone.

  Change your settings later:  $PYTHON_BIN $APP_DIR/scribe_setup.py
  Remove Scribe:               bash $REPO_DIR/uninstall.sh

CHECKLIST
