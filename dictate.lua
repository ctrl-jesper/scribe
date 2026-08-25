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
--
-- Nothing on the push-to-talk path may block. macOS disables an event tap whose callback
-- takes too long, which is the same class of failure as the GC bug above. Every subprocess
-- here is therefore started with hs.task (async); hs.execute (synchronous) is never used.

local SCRIBE_VERSION = "0.1.1"

-- Clean up any prior instance on reload (dofile re-runs this file); prevents duplicate listeners.
if scribe then
    if scribe.ptt then scribe.ptt:stop() end
    if scribe.polishHotkey then scribe.polishHotkey:delete() end
    if scribe.recallHotkey then scribe.recallHotkey:delete() end
    if scribe.menu then scribe.menu:delete() end
    if scribe.cueTimer then scribe.cueTimer:stop() end
    if scribe.batchTimeout then scribe.batchTimeout:stop() end
    if scribe.recTask then pcall(function() scribe.recTask:terminate() end) end
    hs.alert.closeAll(0)                           -- drop any lingering transcribing/speak notice
end
scribe = {}
scribe.recording = false        -- the mic is open right now
scribe.cued = false             -- the "speak" cue has already fired for this recording
scribe.transcribing = false     -- a transcription is in flight; a new recording must wait
scribe.micIndex = nil           -- resolved by name below; nil means "do not record"
scribe.micName = nil            -- the name we are looking for, for error messages
scribe.workerPid = nil          -- PID of the streaming worker, so we signal only ours

local HOME        = os.getenv("HOME")
local SCRIBE_HOME = HOME .. "/.config/scribe"
local APP_DIR     = SCRIBE_HOME .. "/app"      -- where the installer puts pipeline.py / stream_worker.py
local STATE_DIR   = SCRIBE_HOME .. "/state"
local OUTPUT_PATH = STATE_DIR .. "/last-output.txt"   -- must match pipeline.py's OUTPUT_PATH
local WAV         = STATE_DIR .. "/dictation.wav"
local WORKER_PATH = APP_DIR .. "/stream_worker.py"
local PIPELINE    = APP_DIR .. "/pipeline.py"

hs.fs.mkdir(SCRIBE_HOME)                       -- no-op when they already exist; the batch
hs.fs.mkdir(STATE_DIR)                         -- recording needs STATE_DIR to be there

-- Everything user-specific comes from config.json, written by the installer and the setup
-- wizard. The fallbacks below keep Scribe usable if the file is missing or a key was never
-- written.
local cfg = hs.json.read(SCRIBE_HOME .. "/config.json") or {}

-- A JSON "" is truthy in Lua, so `cfg.foo or default` silently accepts a blank value. Every
-- string read from the config goes through here instead, and blank means "not configured".
local function configString(value)
    if type(value) ~= "string" then return nil end
    local trimmed = value:match("^%s*(.-)%s*$")
    if trimmed == "" then return nil end
    return trimmed
end

local function isExecutable(path)
    if type(path) ~= "string" or path == "" then return false end
    local permissions = hs.fs.attributes(path, "permissions")
    return permissions ~= nil and permissions:find("x", 1, true) ~= nil
end

-- Pick the first candidate that actually exists and is executable. The installer writes the
-- resolved absolute paths into config.json; the fallbacks cover Apple Silicon (/opt/homebrew)
-- and Intel (/usr/local) for someone who edited the config by hand.
local function pickBinary(configured, fallbacks)
    local candidates = {}
    local fromConfig = configString(configured)
    if fromConfig then candidates[#candidates + 1] = fromConfig end
    for _, path in ipairs(fallbacks) do candidates[#candidates + 1] = path end
    for _, path in ipairs(candidates) do
        if isExecutable(path) then return path, candidates end
    end
    return nil, candidates
end

local FFMPEG, FFMPEG_TRIED = pickBinary(cfg.ffmpeg_bin,
    { "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg" })
local PYTHON, PYTHON_TRIED = pickBinary(cfg.python_bin,
    { "/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3" })

local PTT_KEYCODE    = cfg.hotkey_keycode or 61          -- 61 = right option
local PTT_FLAG       = configString(cfg.hotkey_flag) or "alt"   -- the modifier that keycode raises
local POLISH_ENABLED = cfg.polish_enabled == true        -- optional LLM pass; hidden when off
local CUE_SOUND      = hs.sound.getByName("Tink")

scribe.micName = configString(cfg.mic_name)

-- Streaming mode (beta): persisted toggle, off by default. Batch (ffmpeg -> pipeline.py) is
-- the default path; streaming hands the mic straight to stream_worker.py instead.
scribe.streaming = hs.settings.get("scribe.streaming")
if scribe.streaming == nil then scribe.streaming = false end

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

local function log(message)
    print("[Scribe] " .. message)
end

-- A problem the user has to act on: say it on screen AND in the console, because the console
-- is not open while they are dictating.
local function alertProblem(message, seconds)
    log(message)
    hs.alert.show("Scribe: " .. message, seconds or 3)
end

-- ---------------------------------------------------------------------------
-- Subprocess helpers (all async; see the header note about the event tap)
-- ---------------------------------------------------------------------------

local function runAsync(binary, args)
    if not isExecutable(binary) then return nil end
    local task = hs.task.new(binary, nil, args)
    if task then task:start() end
    return task
end

-- pkill -f takes an extended regular expression, so a literal path has to have its regex
-- metacharacters escaped or a '.' would match any character.
local function escapeForRegex(text)
    return (text:gsub("[%.%^%$%*%+%?%(%)%[%]%{%}%|\\]", "\\%0"))
end

local WAV_PATTERN    = "ffmpeg.*" .. escapeForRegex(WAV)
local WORKER_PATTERN = escapeForRegex(WORKER_PATH)

local function signalPid(pid)
    if not pid then return false end
    runAsync("/bin/kill", { "-INT", tostring(pid) })
    return true
end

-- Stop the streaming worker. Preference order: the hs.task handle we own, then the PID we
-- recorded, then a pkill anchored to the absolute installed path. The old code ran
-- `pkill -f stream_worker.py`, which matched any file with that name anywhere on the machine.
local function stopStreamWorker()
    if scribe.recTask then
        local ok = pcall(function() scribe.recTask:interrupt() end)
        if ok then return end
    end
    if signalPid(scribe.workerPid) then return end
    runAsync("/usr/bin/pkill", { "-INT", "-f", WORKER_PATTERN })
end

local function stopBatchRecorder()
    if scribe.recTask then
        -- SIGINT (not terminate) lets ffmpeg finalize the WAV header before it exits.
        local ok = pcall(function() scribe.recTask:interrupt() end)
        if ok then return end
    end
    runAsync("/usr/bin/pkill", { "-INT", "-f", WAV_PATTERN })
end

-- ---------------------------------------------------------------------------
-- Microphone resolution
-- ---------------------------------------------------------------------------

-- ffmpeg lists video devices first and audio devices second, each numbered from zero, e.g.
--   [AVFoundation indev @ 0x7f8] AVFoundation video devices:
--   [AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
--   [AVFoundation indev @ 0x7f8] AVFoundation audio devices:
--   [AVFoundation indev @ 0x7f8] [0] MacBook Air Microphone
-- Only the audio section may be searched, or an unmatched name lands on a camera.
local AUDIO_HEADER = "avfoundation audio devices"
local VIDEO_HEADER = "avfoundation video devices"

local function parseAudioDevices(listing)
    local devices = {}
    local inAudioSection = false
    for line in (listing or ""):gmatch("[^\r\n]+") do
        local lowered = line:lower()
        if lowered:find(AUDIO_HEADER, 1, true) then
            inAudioSection = true
        elseif lowered:find(VIDEO_HEADER, 1, true) then
            inAudioSection = false
        elseif inAudioSection then
            local index, name = line:match("%[(%d+)%]%s+(.-)%s*$")
            if index and name and name ~= "" then
                devices[#devices + 1] = { index = index, name = name }
            end
        end
    end
    return devices
end

-- Compare names as plain text, never as a Lua pattern. In a pattern '-' is a quantifier, so
-- "Built-in Microphone" and "Jabra Evolve2-65" could never match themselves.
local function findMic(devices, wanted)
    if not wanted then return nil end
    local target = wanted:lower()
    for _, device in ipairs(devices) do
        if device.name == wanted then return device end
    end
    for _, device in ipairs(devices) do
        if device.name:lower() == target then return device end
    end
    for _, device in ipairs(devices) do
        if device.name:lower():find(target, 1, true) then return device end
    end
    return nil
end

local function resolveMic()
    if not FFMPEG then return end
    if not scribe.micName then
        scribe.micIndex = nil
        return
    end
    scribe.micTask = hs.task.new(FFMPEG, function(_, _, stderr)
        local devices = parseAudioDevices(stderr)
        local device = findMic(devices, scribe.micName)
        if device then
            scribe.micIndex = device.index
            log("mic -> [" .. device.index .. "] " .. device.name)
        else
            -- Deliberately no fallback index. Recording into whatever happens to sit at a
            -- guessed index is how a dictation ends up captured from a phone or a camera.
            scribe.micIndex = nil
            log("microphone '" .. scribe.micName .. "' not found among "
                .. #devices .. " audio device(s); recording is disabled until it is back")
        end
    end, { "-f", "avfoundation", "-list_devices", "true", "-i", "" })
    scribe.micTask:start()
end

-- ---------------------------------------------------------------------------
-- Status cues
-- ---------------------------------------------------------------------------

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

local function finishTranscription()
    scribe.transcribing = false
    closeTranscribing()
    setStatus(IDLE)
end

-- ---------------------------------------------------------------------------
-- Recording
-- ---------------------------------------------------------------------------

-- ffmpeg is stopped with SIGINT so it can finalize the WAV header, and then exits non-zero by
-- design. Those codes are a normal stop; anything else means the recorder really died.
local NORMAL_STOP_EXITS = { [0] = true, [2] = true, [130] = true, [255] = true }

local function wavBytes()
    local attributes = hs.fs.attributes(WAV)
    if not attributes or attributes.mode ~= "file" then return nil end
    return attributes.size or 0
end

local function transcribeAndPaste()
    setStatus("⏳")                                               -- transcribing
    -- Two halves of the same guarantee, both provided by pipeline.py:
    --   --not-older-than  refuses a WAV written before this recording started, so a recorder
    --                     that silently failed cannot get the PREVIOUS dictation pasted.
    --   --consume         deletes the WAV once it has been read, so it cannot be reused.
    scribe.pipeTask = hs.task.new(PYTHON, function(exitCode, _, stderr)
        finishTranscription()
        if exitCode == 0 then
            hs.eventtap.keyStroke({ "cmd" }, "v")
        elseif exitCode == 3 then
            return                                                -- nothing was said: reset quietly
        else
            alertProblem("dictation failed (see console)", 2)
            log("pipeline error: " .. (stderr or ""))
        end
    end, { PIPELINE, WAV, "--copy", "--consume",
           "--not-older-than", string.format("%d", scribe.sessionStart or 0) })
    scribe.pipeTask:start()
end

-- Decide, once the batch recorder has actually exited, whether there is anything to
-- transcribe. Called from the recorder's termination callback, or from a safety timer if that
-- callback never arrives.
local function finishBatchRecording(exitCode, stderr)
    if not scribe.pendingTranscribe then return end
    scribe.pendingTranscribe = false
    if scribe.batchTimeout then
        scribe.batchTimeout:stop()
        scribe.batchTimeout = nil
    end

    local bytes = wavBytes()
    local recorderOk = exitCode ~= nil and NORMAL_STOP_EXITS[exitCode]
    if exitCode == nil then
        -- The safety timer fired first. Trust the file rather than the missing exit code.
        recorderOk = true
    end

    if not recorderOk then
        finishTranscription()
        alertProblem("recording failed (ffmpeg exit " .. tostring(exitCode) .. ")", 3)
        log("recorder stderr: " .. (stderr or ""))
        return
    end
    -- 44 bytes is an empty WAV: header only, no samples. Transcribing that would just replay
    -- whatever the previous run left on disk.
    if not bytes or bytes <= 44 then
        finishTranscription()
        alertProblem("no audio was recorded, check the microphone", 3)
        log("recorder stderr: " .. (stderr or ""))
        return
    end
    transcribeAndPaste()
end

local function batchRecorderFinished(exitCode, _, stderr)
    if scribe.recording and not NORMAL_STOP_EXITS[exitCode] then
        -- Died while we still believe we are recording: the mic never opened. Say so now
        -- rather than letting the release paste a stale result.
        scribe.recording, scribe.cued = false, false
        if scribe.cueTimer then scribe.cueTimer:stop() end
        setStatus(IDLE)
        alertProblem("could not open the microphone (ffmpeg exit " .. tostring(exitCode) .. ")", 4)
        log("recorder stderr: " .. (stderr or ""))
        return
    end
    finishBatchRecording(exitCode, stderr)
end

-- Streaming worker's termination callback: unlike the batch pipeline, the worker transcribes
-- and copies to the clipboard itself, so this only needs to react to how it finished.
local function streamWorkerFinished(exitCode, _, stderr)
    scribe.workerPid = nil
    if scribe.recording then
        -- The worker died while the key is still held. Reset now, or the release
        -- would sit at "transcribing" forever waiting for a process that is gone.
        scribe.recording, scribe.cued = false, false
        if scribe.cueTimer then scribe.cueTimer:stop() end
        finishTranscription()
        alertProblem("recording stopped unexpectedly (worker exit "
            .. tostring(exitCode) .. ")", 4)
        log("stream worker stderr: " .. (stderr or ""))
        return
    end
    finishTranscription()
    if exitCode == 0 then
        hs.eventtap.keyStroke({ "cmd" }, "v")
    elseif exitCode == 3 then
        return                                                     -- empty/aborted dictation: reset quietly
    else
        alertProblem("dictation failed (see console)", 2)
        log("stream worker error: " .. (stderr or ""))
    end
end

local function startRecording()
    if scribe.recording then return end
    if scribe.transcribing then
        -- Starting now would kill the worker mid-transcription, or truncate the WAV the
        -- pipeline is reading. Either way the dictation in flight would be lost.
        hs.alert.show("Scribe: still transcribing", 1)
        return
    end
    if not FFMPEG then
        alertProblem("ffmpeg not found, run install.sh again", 4)
        return
    end
    -- Latch the mode for this whole recording. Toggling streaming from the menu mid-recording
    -- must not send the release down the other branch.
    scribe.activeStreaming = scribe.streaming
    if scribe.activeStreaming and not PYTHON then
        alertProblem("python3 not found, run install.sh again", 4)
        return
    end
    if not scribe.micName then
        alertProblem("no microphone configured, run setup", 4)
        return
    end
    if not scribe.micIndex then
        alertProblem("microphone '" .. scribe.micName .. "' not found, run setup", 4)
        resolveMic()                                              -- try again for the next press
        return
    end

    scribe.recording, scribe.cued = true, false
    setStatus("🎙️…")                                              -- warming up (mic cold start)

    if scribe.activeStreaming then
        if scribe.workerPid then signalPid(scribe.workerPid) end  -- a worker left by a crashed run
        -- The worker owns ffmpeg internally; dictate.lua only starts it, watches stdout for the
        -- ready cue, and signals it to stop. It transcribes, copies to the clipboard, and exits.
        scribe.recTask = hs.task.new(PYTHON, streamWorkerFinished,
            function(_, stdOut, _)                                 -- stream callback: cue once mic is capturing
                if stdOut and stdOut:find("MIC_READY") then cueSpeak() end
                return true
            end,
            { WORKER_PATH, "--mic", scribe.micIndex, "--copy" })
    else
        -- Delete first, so a recorder that never produces a file cannot leave the previous
        -- dictation lying here to be transcribed and pasted as if it were new. The session
        -- timestamp is the belt to that pair of braces: pipeline.py refuses any WAV whose
        -- mtime predates it.
        os.remove(WAV)
        scribe.sessionStart = os.time()
        scribe.pendingTranscribe = false
        scribe.recTask = hs.task.new(FFMPEG, batchRecorderFinished,
            function(_, stdOut, stdErr)                            -- stream callback: cue once frames flow
                if ((stdOut or "") .. (stdErr or "")):find("size=") then cueSpeak() end
                return true
            end,
            { "-y", "-f", "avfoundation", "-i", ":" .. scribe.micIndex,
              "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", WAV })
    end
    if not scribe.recTask then
        -- hs.task.new returns nil if it cannot launch. Fail here rather than
        -- raising a Lua error inside the event tap callback.
        scribe.recording, scribe.cued = false, false
        setStatus(IDLE)
        alertProblem("could not start the recorder", 4)
        return
    end
    scribe.recTask:start()
    if scribe.activeStreaming then
        -- Remember the worker's own PID so stopping it never has to guess by name.
        local ok, pid = pcall(function() return scribe.recTask:pid() end)
        scribe.workerPid = ok and pid or nil
    end
    scribe.cueTimer = hs.timer.doAfter(1.6, cueSpeak)              -- fallback cue if the stream callback misses it
end

local function stopRecording()
    if not scribe.recording then return end
    scribe.recording, scribe.cued = false, false
    if scribe.cueTimer then scribe.cueTimer:stop() end
    scribe.transcribing = true
    setStatus("⏳")
    showTranscribing()

    if scribe.activeStreaming then
        -- Worker owns ffmpeg and finishes the job itself (transcribe + clipboard copy); its
        -- termination callback above handles the paste, so there is nothing to schedule here.
        stopStreamWorker()
    else
        -- Transcription is scheduled by the recorder's termination callback, once the WAV is
        -- closed and has been checked, not by a fixed delay that cannot tell success from
        -- failure.
        scribe.pendingTranscribe = true
        stopBatchRecorder()
        scribe.batchTimeout = hs.timer.doAfter(5, function()
            log("recorder did not report its exit within 5s; checking the WAV instead")
            finishBatchRecording(nil, nil)
        end)
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
    if not PYTHON then
        alertProblem("python3 not found, run install.sh again", 4)
        return
    end
    setStatus("✨")                                               -- polishing
    hs.alert.show("polishing last dictation (~11s)...", 1)
    scribe.polishTask = hs.task.new(PYTHON, function(exitCode, _, stderr)
        setStatus(IDLE)
        if exitCode == 0 then
            hs.eventtap.keyStroke({ "cmd" }, "v")
        else
            alertProblem("polish failed (see console)", 2)
            log("polish error: " .. (stderr or ""))
        end
    end, { PIPELINE, "--polish-last", "--copy" })
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
    items[#items + 1] = { title = "-" }
    items[#items + 1] = { title = "Scribe " .. SCRIBE_VERSION, disabled = true }
    scribe.menu:setMenu(items)
end
buildMenu()

-- ---------------------------------------------------------------------------
-- Startup health check
-- ---------------------------------------------------------------------------

if not FFMPEG then
    alertProblem("ffmpeg not found (looked in: " .. table.concat(FFMPEG_TRIED, ", ")
        .. "). Run install.sh again.", 8)
end
if not PYTHON then
    alertProblem("python3 not found (looked in: " .. table.concat(PYTHON_TRIED, ", ")
        .. "). Run install.sh again.", 8)
else
    -- /usr/bin/python3 can be an Xcode command line tools stub that exits non-zero. Prove the
    -- interpreter runs before the user finds out mid-dictation. Async, so nothing blocks.
    scribe.pythonCheck = hs.task.new(PYTHON, function(exitCode, _, stderr)
        if exitCode ~= 0 then
            alertProblem("python3 at " .. PYTHON .. " does not run (exit "
                .. tostring(exitCode) .. "). Run install.sh again.", 8)
            log("python check stderr: " .. (stderr or ""))
        end
    end, { "-c", "import sys" })
    scribe.pythonCheck:start()
end
if not scribe.micName then
    alertProblem("no microphone configured. Run: python3 " .. APP_DIR .. "/scribe_setup.py", 8)
end

log("version " .. SCRIBE_VERSION .. "; ffmpeg=" .. tostring(FFMPEG) .. "; python=" .. tostring(PYTHON))

local loadedMsg = "Scribe " .. SCRIBE_VERSION
    .. " loaded (hold the push-to-talk key = talk, ⌘⌥⌃L = recall"
if POLISH_ENABLED then loadedMsg = loadedMsg .. ", ⌘⌥⌃P = polish" end
hs.alert.show(loadedMsg .. ")", 2.5)
