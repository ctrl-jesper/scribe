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

MODEL_FILE="ggml-large-v3-turbo-q5_0.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL_FILE"
MODEL_PATH="$MODELS_DIR/$MODEL_FILE"
MODEL_MIN_BYTES=524288000   # 500 MB; the real file is around 547 MB

PLIST_LABEL="com.scribe.whisper-server"
PLIST_TEMPLATE="$REPO_DIR/$PLIST_LABEL.plist.template"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

HS_INIT="$HOME/.hammerspoon/init.lua"
HS_BEGIN="-- BEGIN scribe"
HS_END="-- END scribe"

DEFAULT_PORT=8090

# Runtime files copied from the repo into the app dir on every run.
RUNTIME_FILES=(pipeline.py stream_worker.py dictate.lua scribe_setup.py)

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Platform guard
# ---------------------------------------------------------------------------

step "Checking the platform"
[ "$(uname -s)" = "Darwin" ] || die "Scribe is macOS only (found $(uname -s))."
info "macOS $(sw_vers -productVersion) on $(uname -m)"

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

WHISPER_SERVER="$BREW_PREFIX/bin/whisper-server"
[ -x "$WHISPER_SERVER" ] || WHISPER_SERVER="$(command -v whisper-server || true)"
[ -n "$WHISPER_SERVER" ] && [ -x "$WHISPER_SERVER" ] || die "whisper-server not found after installing whisper-cpp."
info "whisper-server at $WHISPER_SERVER"

# ---------------------------------------------------------------------------
# 4. Config home and app files
# ---------------------------------------------------------------------------

step "Creating $CONFIG_HOME"
mkdir -p "$APP_DIR" "$MODELS_DIR" "$STATE_DIR"

for f in "${RUNTIME_FILES[@]}"; do
    [ -f "$REPO_DIR/$f" ] || die "Missing runtime file in the repo: $f"
done

for f in "${RUNTIME_FILES[@]}"; do
    cp "$REPO_DIR/$f" "$APP_DIR/$f"
    info "installed app/$f"
done

# User data is only seeded, never overwritten.
seed_file() {
    local example="$1" target="$2" label="$3"
    if [ -f "$target" ]; then
        info "$label already exists, keeping yours"
        return 0
    fi
    if [ -f "$example" ]; then
        cp "$example" "$target"
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
  "claude_model": "claude-haiku-4-5-20251001"
}
CONFIG_DEFAULT
    info "created config.json with built-in defaults"
fi

if ! seed_file "$REPO_DIR/dictionary.example.json" "$DICTIONARY_FILE" "dictionary.json"; then
    printf '{\n  "replacements": {}\n}\n' >"$DICTIONARY_FILE"
    info "created dictionary.json with built-in defaults"
fi

# The example files carry sample words so the format is obvious to read. Those
# samples must not become the user's actual settings, so a freshly seeded file
# keeps the schema and any comments but starts empty. An existing file is
# never touched.
if [ "$config_existed" = "no" ] || [ "$dictionary_existed" = "no" ]; then
    python3 - "$CONFIG_FILE" "$config_existed" "$DICTIONARY_FILE" "$dictionary_existed" <<'CLEAR_SAMPLES'
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
# 5. Speech model
# ---------------------------------------------------------------------------

step "Speech model ($MODEL_FILE, about 547 MB)"

file_size() {
    # stat(1) on macOS: -f%z prints the byte size.
    stat -f%z "$1" 2>/dev/null || echo 0
}

if [ -f "$MODEL_PATH" ] && [ "$(file_size "$MODEL_PATH")" -gt "$MODEL_MIN_BYTES" ]; then
    info "already downloaded, skipping ($(file_size "$MODEL_PATH") bytes)"
else
    info "downloading from huggingface.co (resumable, this takes a few minutes)"
    curl -L -C - --fail --progress-bar -o "$MODEL_PATH" "$MODEL_URL" \
        || die "Model download failed. Re-run this installer to resume where it stopped."
    downloaded="$(file_size "$MODEL_PATH")"
    if [ "$downloaded" -le "$MODEL_MIN_BYTES" ]; then
        die "Downloaded model is only $downloaded bytes, expected more than $MODEL_MIN_BYTES. Delete $MODEL_PATH and re-run."
    fi
    info "downloaded $downloaded bytes"
fi

# ---------------------------------------------------------------------------
# 6. Port selection
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
    python3 - "$CONFIG_FILE" "$DEFAULT_PORT" <<'READ_PORT'
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

python3 - "$CONFIG_FILE" "$PORT" "$MODEL_FILE" <<'WRITE_PORT'
import json, os, sys, tempfile

path, port, model_file = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(path) as fh:
    config = json.load(fh)
config["server_port"] = port
config.setdefault("model_file", model_file)

directory = os.path.dirname(path) or "."
handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
with os.fdopen(handle, "w") as fh:
    json.dump(config, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
WRITE_PORT
info "wrote server_port into config.json"

# ---------------------------------------------------------------------------
# 7. launchd service
# ---------------------------------------------------------------------------

step "Installing the launchd service ($PLIST_LABEL)"
mkdir -p "$HOME/Library/LaunchAgents"

if [ ! -f "$PLIST_TEMPLATE" ]; then
    warn "plist template not found in the repo, using a built-in one"
    PLIST_TEMPLATE="$STATE_DIR/$PLIST_LABEL.plist.template"
    cat >"$PLIST_TEMPLATE" <<'PLIST_FALLBACK'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.scribe.whisper-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>__WHISPER_SERVER__</string>
        <string>-m</string>
        <string>__MODEL_PATH__</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>__PORT__</string>
        <string>-t</string>
        <string>4</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>__HOME__/.config/scribe/state/whisper-server.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/.config/scribe/state/whisper-server.err</string>
</dict>
</plist>
PLIST_FALLBACK
fi

# The last three rules keep Intel Macs working. The template may name the
# Apple Silicon Homebrew path directly, both for the binary and in PATH, so
# rewrite that prefix and then point the binary at the one actually found.
# All three are no-ops on a machine where the template already fits.
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__MODEL_PATH__|$MODEL_PATH|g" \
    -e "s|/opt/homebrew/bin|$BREW_PREFIX/bin|g" \
    -e "s|__WHISPER_SERVER__|$WHISPER_SERVER|g" \
    -e "s|$BREW_PREFIX/bin/whisper-server|$WHISPER_SERVER|g" \
    "$PLIST_TEMPLATE" >"$PLIST_PATH"
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
# 8. Hammerspoon wiring
# ---------------------------------------------------------------------------

step "Wiring Scribe into Hammerspoon"
mkdir -p "$(dirname "$HS_INIT")"
touch "$HS_INIT"

# Drop any previous Scribe block, then append a fresh one. This keeps the
# install idempotent and lets an upgrade change the loader line.
awk -v b="$HS_BEGIN" -v e="$HS_END" '
    index($0, b) { skip = 1 }
    !skip        { print }
    index($0, e) { skip = 0 }
' "$HS_INIT" >"$HS_INIT.scribe-tmp"
mv "$HS_INIT.scribe-tmp" "$HS_INIT"

{
    printf '%s\n' "$HS_BEGIN"
    printf '%s\n' 'dofile(os.getenv("HOME") .. "/.config/scribe/app/dictate.lua")'
    printf '%s\n' "$HS_END"
} >>"$HS_INIT"
info "loader block written to $HS_INIT"

# ---------------------------------------------------------------------------
# 9. Setup wizard
# ---------------------------------------------------------------------------

step "Running the setup wizard"
# The wizard needs a terminal. Piped installs (curl ... | bash) do not have
# one, and a cancelled wizard should not lose the rest of the install, so
# neither case is allowed to abort the script.
if [ -t 0 ]; then
    wizard_status=0
    python3 "$APP_DIR/scribe_setup.py" || wizard_status=$?
    if [ "$wizard_status" -ne 0 ]; then
        warn "setup wizard exited with status $wizard_status."
        warn "everything else is installed. Run it again when ready:"
        warn "  python3 $APP_DIR/scribe_setup.py"
    fi
else
    info "no terminal attached, skipping the interactive wizard."
    info "run it yourself now: python3 $APP_DIR/scribe_setup.py"
fi

# ---------------------------------------------------------------------------
# 10. Checklist
# ---------------------------------------------------------------------------

cat <<CHECKLIST

==> Install complete. Three things left, all in the macOS UI:

  1. Open Hammerspoon (Applications > Hammerspoon), then click its menu-bar
     icon and choose "Reload config". Keep "Launch at login" on.
  2. Grant Accessibility: System Settings > Privacy & Security > Accessibility
     > enable Hammerspoon. Without it the hotkey cannot type into other apps.
  3. Grant Microphone the first time you record: hold the key, speak, and
     approve the prompt macOS shows. Then record once more.

  Using it: hold RIGHT OPTION, wait for the "speak" cue, talk, release.
  The text is typed into whatever app has focus.

  Your files:
    config:      $CONFIG_FILE
    dictionary:  $DICTIONARY_FILE
    app code:    $APP_DIR
    model:       $MODEL_PATH
    server logs: $STATE_DIR

  Change your settings later:  python3 $APP_DIR/scribe_setup.py
  Remove Scribe:               bash $REPO_DIR/uninstall.sh

CHECKLIST
