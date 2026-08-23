#!/usr/bin/env bash
#
# Scribe uninstaller.
#
# Always removes: the launchd service and the Hammerspoon loader block.
# Asks first before removing: ~/.config/scribe (your config, dictionary,
# and the 547 MB speech model).
# Never touches: Homebrew packages (it prints how to remove them yourself).
#
set -euo pipefail

CONFIG_HOME="$HOME/.config/scribe"
PLIST_LABEL="com.scribe.whisper-server"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
HS_INIT="$HOME/.hammerspoon/init.lua"
HS_BEGIN="-- BEGIN scribe"
HS_END="-- END scribe"

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. launchd service
# ---------------------------------------------------------------------------

step "Stopping the transcription server"
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl remove "$PLIST_LABEL" >/dev/null 2>&1 || true

if [ -f "$PLIST_PATH" ]; then
    rm -f "$PLIST_PATH"
    info "removed $PLIST_PATH"
else
    info "no launchd service found, nothing to remove"
fi

# ---------------------------------------------------------------------------
# 2. Hammerspoon loader block
# ---------------------------------------------------------------------------

step "Removing the Hammerspoon loader block"
if [ -f "$HS_INIT" ] && grep -q -- "$HS_BEGIN" "$HS_INIT"; then
    awk -v b="$HS_BEGIN" -v e="$HS_END" '
        index($0, b) { skip = 1 }
        !skip        { print }
        index($0, e) { skip = 0 }
    ' "$HS_INIT" >"$HS_INIT.scribe-tmp"
    mv "$HS_INIT.scribe-tmp" "$HS_INIT"
    info "removed the marked block from $HS_INIT"
    info "reload Hammerspoon (menu-bar icon > Reload config) to drop the hotkey"
else
    info "no Scribe block in $HS_INIT, nothing to remove"
fi

# ---------------------------------------------------------------------------
# 3. User data (asks first)
# ---------------------------------------------------------------------------

step "User data"
if [ -d "$CONFIG_HOME" ]; then
    size="$(du -sh "$CONFIG_HOME" 2>/dev/null | cut -f1 || echo "unknown size")"
    info "$CONFIG_HOME holds your config, your dictionary, and the speech model ($size)."
    printf '    Delete it? [y/N] '
    if [ -t 0 ]; then
        read -r answer
    else
        answer="n"
        printf 'n (not a terminal, keeping it)\n'
    fi
    case "$answer" in
        [yY]|[yY][eE][sS])
            rm -rf "$CONFIG_HOME"
            info "deleted $CONFIG_HOME"
            ;;
        *)
            info "kept $CONFIG_HOME. Delete it later with: rm -rf $CONFIG_HOME"
            ;;
    esac
else
    info "$CONFIG_HOME does not exist, nothing to remove"
fi

# ---------------------------------------------------------------------------
# 4. Closing note
# ---------------------------------------------------------------------------

cat <<'CLOSING'

==> Scribe is uninstalled.

  Homebrew packages were left alone, because other tools may use them.
  Remove them yourself if you no longer need them:

    brew uninstall whisper-cpp
    brew uninstall ffmpeg
    brew uninstall --cask hammerspoon

  macOS keeps the Accessibility and Microphone permissions you granted
  Hammerspoon. Clear them in System Settings > Privacy & Security if you
  removed the Hammerspoon app.

CLOSING
