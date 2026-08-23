-- Scribe: hold-to-talk dictation glue for Hammerspoon
--
-- Load from ~/.hammerspoon/init.lua with:
--     dofile(os.getenv("HOME") .. "/.config/scribe/app/dictate.lua")
--
-- Hold the push-to-talk key (default RIGHT OPTION, keycode 61). Wait for the "speak" cue,
-- which fires only once the microphone is actually capturing (ffmpeg has a 1-2s cold start,
-- so speaking before the cue clips the start of your first word). Release to transcribe + paste.
--
-- A menu-bar icon shows live status. Listeners live in the global table `scribe` so
-- Hammerspoon's garbage collector does not reclaim them (that reclaim is what made recording
-- "stop working").

-- Clean up any prior instance on reload (dofile re-runs this file); prevents duplicate listeners.
if scribe then
    if scribe.ptt then scribe.ptt:stop() end
    if scribe.polishHotkey then scribe.polishHotkey:delete() end
    if scribe.recallHotkey then scribe.recallHotkey:delete() end
    if scribe.menu then scribe.menu:delete() end
    if scribe.cueTimer then scribe.cueTimer:stop() end
    hs.alert.closeAll(0)                           -- drop any lingering transcribing/speak notice
end
scribe = {}
scribe.recording = false
scribe.cued = false
scribe.micIndex = "1"                          -- resolved by name below; this is only a fallback

local HOME        = os.getenv("HOME")
local SCRIBE_HOME = HOME .. "/.config/scribe"
local APP_DIR     = SCRIBE_HOME .. "/app"      -- where the installer puts pipeline.py / stream_worker.py
local STATE_DIR   = SCRIBE_HOME .. "/state"
local OUTPUT_PATH = STATE_DIR .. "/last-output.txt"   -- must match pipeline.py's OUTPUT_PATH
local WAV         = STATE_DIR .. "/dictation.wav"
local FFMPEG      = "/opt/homebrew/bin/ffmpeg"
local PYTHON      = "/usr/bin/python3"

-- Everything user-specific comes from config.json, written by the setup wizard. The fallbacks
-- below keep Scribe usable if the file is missing or a key was never written.
local cfg = hs.json.read(SCRIBE_HOME .. "/config.json") or {}
local PTT_KEYCODE    = cfg.hotkey_keycode or 61          -- 61 = right option
local PTT_FLAG       = cfg.hotkey_flag or "alt"          -- the modifier that keycode raises
local MIC_NAME       = cfg.mic_name or "MacBook Air Microphone"
local POLISH_ENABLED = cfg.polish_enabled == true        -- optional LLM pass; hidden when off
local CUE_SOUND      = hs.sound.getByName("Tink")

-- Streaming mode (beta): persisted toggle, off by default. Batch (ffmpeg -> pipeline.py) is
-- the default path; streaming hands the mic straight to stream_worker.py instead.
scribe.streaming = hs.settings.get("scribe.streaming")
if scribe.streaming == nil then scribe.streaming = false end

hs.fs.mkdir(SCRIBE_HOME)                       -- no-op when they already exist; the batch
hs.fs.mkdir(STATE_DIR)                         -- recording needs STATE_DIR to be there

-- Menu-bar icon: a microphone with a waveform behind it, drawn in code as a template image so
-- it adapts to light and dark menu bars. Status (recording, transcribing, polishing) shows as
-- a small emoji NEXT TO the icon; idle shows the icon alone.
local function makeMicIcon()
    local c = hs.canvas.new({ x = 0, y = 0, w = 20, h = 18 })
    local e = {}
    for _, b in ipairs({ { x = 2, h = 6 }, { x = 5, h = 10 }, { x = 15, h = 10 }, { x = 18, h = 6 } }) do
        e[#e + 1] = { type = "rectangle", action = "fill",            -- waveform bars, faded
            fillColor = { black = 1, alpha = 0.45 },
            roundedRectRadii = { xRadius = 0.75, yRadius = 0.75 },
            frame = { x = b.x - 0.75, y = 8.5 - b.h / 2, w = 1.5, h = b.h } }
    end
    e[#e + 1] = { type = "rectangle", action = "fill", fillColor = { black = 1 },   -- mic capsule
        roundedRectRadii = { xRadius = 2, yRadius = 2 }, frame = { x = 8, y = 1.5, w = 4, h = 8.5 } }
    e[#e + 1] = { type = "arc", action = "stroke", strokeColor = { black = 1 },      -- U-shaped holder
        strokeWidth = 1.4, arcRadii = false,
        center = { x = 10, y = 8.5 }, radius = 4.2, startAngle = 90, endAngle = 270 }
    e[#e + 1] = { type = "segments", action = "stroke", strokeColor = { black = 1 }, -- stem
        strokeWidth = 1.4, coordinates = { { x = 10, y = 12.7 }, { x = 10, y = 15 } } }
    e[#e + 1] = { type = "segments", action = "stroke", strokeColor = { black = 1 }, -- base
        strokeWidth = 1.4, coordinates = { { x = 7, y = 15.7 }, { x = 13, y = 15.7 } } }
    c:replaceElements(e)
    local img = c:imageFromCanvas()
    c:delete()
    return img
end

local IDLE = ""                                    -- idle shows the icon alone, no title text
scribe.menu = hs.menubar.new()
local okIcon = pcall(function() scribe.menu:setIcon(makeMicIcon(), true) end)
if not okIcon then IDLE = "🎙️" end                 -- fallback: emoji title if canvas drawing fails
local function setStatus(txt) if scribe.menu then scribe.menu:setTitle(txt) end end
setStatus(IDLE)

-- Resolve the avfoundation index of the configured mic by NAME. The index shifts when other
-- devices connect or disconnect, so a fixed index is not safe. Runs async (never blocks the
-- event tap).
local function resolveMic()
    scribe.micTask = hs.task.new(FFMPEG, function(_, _, stderr)
        local idx = stderr and stderr:match("%[(%d+)%] " .. MIC_NAME)
        if idx then
            scribe.micIndex = idx
            print("[Scribe] mic -> [" .. idx .. "] " .. MIC_NAME)
        else
            print("[Scribe] '" .. MIC_NAME .. "' not found; keeping index " .. tostring(scribe.micIndex))
        end
    end, { "-f", "avfoundation", "-list_devices", "true", "-i", "" })
    scribe.micTask:start()
end

local function cueSpeak()
    if scribe.recording and not scribe.cued then
        scribe.cued = true
        if CUE_SOUND then CUE_SOUND:play() end
        setStatus("🔴")
        hs.alert.show("● speak", 0.7)
    end
end

-- Center-screen "transcribing" notice, mirroring the "● speak" cue: shown on key release and
-- closed the moment the result pastes (or the run fails), so its lifetime shows real progress.
local function showTranscribing()
    scribe.transcribeAlert = hs.alert.show("● transcribing…", 120)
end
local function closeTranscribing()
    if scribe.transcribeAlert then
        hs.alert.closeSpecific(scribe.transcribeAlert)
        scribe.transcribeAlert = nil
    end
end

-- Streaming worker's termination callback: unlike the batch pipeline, the worker transcribes
-- and copies to the clipboard itself, so this only needs to react to how it finished.
local function streamWorkerFinished(exitCode, _, stderr)
    closeTranscribing()
    setStatus(IDLE)
    if exitCode == 0 then
        hs.eventtap.keyStroke({ "cmd" }, "v")
    elseif exitCode == 3 then
        return                                                     -- empty/aborted dictation: reset quietly
    else
        hs.alert.show("dictation failed (see console)", 2)
        print("[Scribe] stream worker error: " .. (stderr or ""))
    end
end

local function startRecording()
    if scribe.recording then return end
    scribe.recording, scribe.cued = true, false
    hs.execute("/usr/bin/pkill -INT -f 'ffmpeg.*dictation.wav'")   -- clear any stray recorder holding the mic
    if scribe.streaming then
        hs.execute("/usr/bin/pkill -INT -f stream_worker.py")      -- clear a worker left over from a crashed run
    end
    setStatus("🎙️…")                                              -- warming up (mic cold start)
    if scribe.streaming then
        -- The worker owns ffmpeg internally; dictate.lua only starts it, watches stdout for the
        -- ready cue, and signals it to stop. It transcribes, copies to the clipboard, and exits.
        scribe.recTask = hs.task.new(PYTHON, streamWorkerFinished,
            function(_, stdOut, _)                                 -- stream callback: cue once mic is capturing
                if stdOut and stdOut:find("MIC_READY") then cueSpeak() end
                return true
            end,
            { APP_DIR .. "/stream_worker.py", "--mic", scribe.micIndex, "--copy" })
    else
        scribe.recTask = hs.task.new(FFMPEG, nil,
            function(_, stdOut, stdErr)                            -- stream callback: cue once frames flow
                if ((stdOut or "") .. (stdErr or "")):find("size=") then cueSpeak() end
                return true
            end,
            { "-y", "-f", "avfoundation", "-i", ":" .. scribe.micIndex,
              "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", WAV })
    end
    scribe.recTask:start()
    scribe.cueTimer = hs.timer.doAfter(1.6, cueSpeak)              -- fallback cue if the stream callback misses it
end

local function transcribeAndPaste()
    setStatus("⏳")                                               -- transcribing
    scribe.pipeTask = hs.task.new(PYTHON, function(exitCode, _, stderr)
        closeTranscribing()
        setStatus(IDLE)
        if exitCode == 0 then
            hs.eventtap.keyStroke({ "cmd" }, "v")
        else
            hs.alert.show("dictation failed (see console)", 2)
            print("[Scribe] pipeline error: " .. (stderr or ""))
        end
    end, { APP_DIR .. "/pipeline.py", WAV, "--copy" })
    scribe.pipeTask:start()
end

local function stopRecording()
    if not scribe.recording then return end
    scribe.recording, scribe.cued = false, false
    if scribe.cueTimer then scribe.cueTimer:stop() end
    if scribe.streaming then
        -- Worker owns ffmpeg and finishes the job itself (transcribe + clipboard copy); its
        -- termination callback above handles the paste, so there is no timer to schedule here.
        local sentInterrupt = false
        if scribe.recTask and scribe.recTask.interrupt then
            sentInterrupt = pcall(function() scribe.recTask:interrupt() end)
        end
        if not sentInterrupt then
            hs.execute("/usr/bin/pkill -INT -f stream_worker.py")
        end
        setStatus("⏳")                                           -- worker is transcribing
        showTranscribing()
    else
        hs.execute("/usr/bin/pkill -INT -f 'ffmpeg.*dictation.wav'")   -- SIGINT lets ffmpeg finalize the WAV header
        setStatus("⏳")
        showTranscribing()
        hs.timer.doAfter(0.3, transcribeAndPaste)                     -- let the file flush before reading it
    end
end

-- Push-to-talk: watch the modifier without consuming it, so normal typing still works.
scribe.ptt = hs.eventtap.new({ hs.eventtap.event.types.flagsChanged }, function(e)
    if e:getKeyCode() == PTT_KEYCODE then
        if e:getFlags()[PTT_FLAG] then startRecording() else stopRecording() end
    end
    return false
end)
scribe.ptt:start()

-- Resolve the mic now, and again whenever audio devices change (a phone or headset joining).
resolveMic()
hs.audiodevice.watcher.setCallback(function() resolveMic() end)
hs.audiodevice.watcher.start()

-- On-demand polish: upgrade the LAST instant dictation via the LLM (~11s), then paste it.
-- Tip: undo the instant paste first, then polish, so you replace rather than duplicate.
local function doPolish()
    setStatus("✨")                                               -- polishing
    hs.alert.show("polishing last dictation (~11s)...", 1)
    scribe.polishTask = hs.task.new(PYTHON, function(exitCode, _, stderr)
        setStatus(IDLE)
        if exitCode == 0 then
            hs.eventtap.keyStroke({ "cmd" }, "v")
        else
            hs.alert.show("polish failed (see console)", 2)
            print("[Scribe] polish error: " .. (stderr or ""))
        end
    end, { APP_DIR .. "/pipeline.py", "--polish-last", "--copy" })
    scribe.polishTask:start()
end

-- Recall: restore the last dictation (dict or polished) to the clipboard and paste it, even if
-- you have since copied something else.
local function doRecall()
    local f = io.open(OUTPUT_PATH, "r")
    local text = f and f:read("*a")
    if f then f:close() end
    if text and #text > 0 then
        hs.pasteboard.setContents(text)
        hs.eventtap.keyStroke({ "cmd" }, "v")
        hs.alert.show("last dictation restored", 1)
    else
        hs.alert.show("no saved dictation yet", 1)
    end
end

if POLISH_ENABLED then
    scribe.polishHotkey = hs.hotkey.bind({ "cmd", "alt", "ctrl" }, "p", doPolish)
end
scribe.recallHotkey = hs.hotkey.bind({ "cmd", "alt", "ctrl" }, "l", doRecall)

-- Dropdown on the menu-bar icon: same actions as the hotkeys, plus the streaming toggle and
-- Hammerspoon management. Rebuilt on every toggle so the checkmark reflects the current state.
local function buildMenu()
    local items = {}
    if POLISH_ENABLED then
        items[#items + 1] = { title = "Polish last dictation   (⌘⌥⌃P)", fn = doPolish }
    end
    items[#items + 1] = { title = "Recall last dictation   (⌘⌥⌃L)", fn = doRecall }
    items[#items + 1] = { title = "Streaming mode (beta)", checked = scribe.streaming, fn = function()
        scribe.streaming = not scribe.streaming
        hs.settings.set("scribe.streaming", scribe.streaming)
        buildMenu()
        hs.alert.show(scribe.streaming and "Streaming mode ON" or "Streaming mode OFF", 1)
    end }
    items[#items + 1] = { title = "-" }
    items[#items + 1] = { title = "Reload Scribe config", fn = function() hs.reload() end }
    items[#items + 1] = { title = "Open Hammerspoon Console", fn = function() hs.openConsole() end }
    scribe.menu:setMenu(items)
end
buildMenu()

local loadedMsg = "Scribe loaded (hold the push-to-talk key = talk, ⌘⌥⌃L = recall"
if POLISH_ENABLED then loadedMsg = loadedMsg .. ", ⌘⌥⌃P = polish" end
hs.alert.show(loadedMsg .. ")", 2.5)
