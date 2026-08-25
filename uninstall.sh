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

installed_version=""
[ -f "$CONFIG_HOME/VERSION" ] && installed_version="$(tr -d '[:space:]' <"$CONFIG_HOME/VERSION" 2>/dev/null || true)"
if [ -n "$installed_version" ]; then
    info "removing Scribe $installed_version"
fi

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
# Whole-line equality, never "contains": a file that merely mentions the marker
# string in a comment must not lose everything after that line.
if [ -e "$HS_INIT" ] && grep -qxF -- "$HS_BEGIN" "$HS_INIT"; then
    # A dotfiles repo often symlinks init.lua. Edit what the link points at, so
    # the link survives instead of being replaced by a regular file.
    HS_TARGET="$HS_INIT"
    if [ -L "$HS_INIT" ]; then
        HS_TARGET="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$HS_INIT")"
        info "$HS_INIT is a symlink; editing $HS_TARGET and leaving the link alone"
    fi

    backup="$HS_TARGET.scribe-backup-$(date +%Y%m%d-%H%M%S)"
    cp "$HS_TARGET" "$backup"
    info "backed up your init.lua to $backup"

    awk -v b="$HS_BEGIN" -v e="$HS_END" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        !skip   { print }
    ' "$HS_TARGET" >"$HS_TARGET.scribe-tmp"
    mv "$HS_TARGET.scribe-tmp" "$HS_TARGET"
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
    info "$CONFIG_HOME holds your config, your dictionary, your recordings, and the speech model ($size)."
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

  Your init.lua was backed up before it was edited; the backup sits next to it
  with a .scribe-backup-<timestamp> suffix. Delete it once you are happy.

  Homebrew packages were left alone, because other tools may use them.
  Remove them yourself if you no longer need them:

    brew uninstall whisper-cpp
    brew uninstall ffmpeg
    brew uninstall --cask hammerspoon

  macOS keeps the Accessibility and Microphone permissions you granted
  Hammerspoon. Clear them in System Settings > Privacy & Security if you
  removed the Hammerspoon app.

CLOSING
