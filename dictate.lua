-- Scribe: hold-to-talk dictation glue for Hammerspoon
--
-- Load from ~/.hammerspoon/init.lua with:
--     dofile(os.getenv("HOME") .. "/.config/scribe/app/dictate.lua")
--
-- Hold the push-to-talk key (default RIGHT OPTION, keycode 61). Wait for the "speak" cue,
-- which fires only once the microphone is actually capturing (ffmpeg has a 1-2s cold start,
-- so speaking before the cue clips the start of your first word). At the cue a pill-shaped
-- HUD appears center-screen: three bars that answer to your actual voice (three real frequency
-- bands, read live off ffmpeg's astats filter), collapsing into three dots that bounce left to
-- right, iMessage-style, while Scribe transcribes. Release to transcribe + paste.
--
-- A menu-bar icon in the same three-mark family shows live status. Listeners live in the
-- global table `scribe` so Hammerspoon's garbage collector does not reclaim them (that reclaim
-- is what made recording "stop working").
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
    if scribe.animTimer then scribe.animTimer:stop() end          -- HUD morph / typing bounce / optimizing orbit
    if scribe.menuAnimTimer then scribe.menuAnimTimer:stop() end   -- menu-bar glyph animation
    if scribe.hud then scribe.hud:delete() end
    if scribe.recTask then pcall(function() scribe.recTask:terminate() end) end
    hs.alert.closeAll(0)                           -- drop any lingering alert
end
scribe = {}
scribe.recording = false        -- the mic is open right now
scribe.cued = false             -- the "speak" cue has already fired for this recording
scribe.transcribing = false     -- a transcription is in flight; a new recording must wait
scribe.micIndex = nil           -- resolved by name below; nil means "do not record"
scribe.micName = nil            -- the name we are looking for, for error messages
scribe.workerPid = nil          -- PID of the streaming worker, so we signal only ours
scribe.optimizing = false       -- the prompt rewrite has started for the dictation in flight
scribe.latchArmed = false       -- the latch key was tapped while PTT is still held, this hold
scribe.latched = false          -- PTT was released while armed; recording continues hands-free
scribe.latchStopping = false    -- a PTT press just stopped a latched recording; the matching
                                 -- key-up of that same press must not start or stop anything

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

-- Latch: tap this key while PTT is still held to keep recording hands-free after release; press
-- PTT again to stop. Same shape as PTT_KEYCODE/PTT_FLAG above. Left shift types no character on
-- its own, which is why it is the default: like PTT itself, the latch gesture never needs to
-- consume an event. On by default (the maintainer asked for this feature); cfg.latch_enabled =
-- false switches it off. Read with ~= false, not == true, so the default is "on" rather than
-- "off" unless the config says otherwise.
local LATCH_ENABLED  = cfg.latch_enabled ~= false
local LATCH_KEYCODE  = cfg.latch_keycode or 56           -- 56 = left shift
local LATCH_FLAG     = configString(cfg.latch_flag) or "shift"

local POLISH_ENABLED = cfg.polish_enabled == true        -- optional LLM pass; hidden when off
local CUE_SOUND      = hs.sound.getByName("Tink")

scribe.micName = configString(cfg.mic_name)

-- Streaming mode (beta): persisted toggle, off by default. Batch (ffmpeg -> pipeline.py) is
-- the default path; streaming hands the mic straight to stream_worker.py instead.
scribe.streaming = hs.settings.get("scribe.streaming")
if scribe.streaming == nil then scribe.streaming = false end

-- Prompt mode: when armed, the Python side rewrites the finished transcription into a prompt
-- aimed at the chosen model before it reaches the clipboard. Persisted like the streaming
-- toggle; "off" is the default and leaves every existing path byte-identical.
local PROMPT_TARGETS = { "off", "fable", "opus", "sonnet" }   -- also the menu order
local PROMPT_LABELS = {
    off    = "Off",
    fable  = "Optimize for Fable",
    opus   = "Optimize for Opus",
    sonnet = "Optimize for Sonnet",
}
local PROMPT_BADGES = { fable = "FABLE", opus = "OPUS", sonnet = "SONNET" }   -- no entry for "off"

-- A value read back from hs.settings could be anything (a hand-edited plist, an older build),
-- and it is about to become a command-line argument. Anything unrecognised means "off".
local function normalizePromptTarget(value)
    for _, target in ipairs(PROMPT_TARGETS) do
        if value == target then return value end
    end
    return "off"
end

scribe.promptTarget = normalizePromptTarget(hs.settings.get("scribe.promptTarget"))

-- Auto-polish: when armed, every dictation runs the LLM cleanup (the same pass the polish
-- hotkey applies after the fact) before the text is pasted, in both batch and streaming mode.
-- Persisted like the toggles above, off by default. Compared against true rather than read
-- straight, so a hand-edited plist or an older build cannot leave it holding a non-boolean.
scribe.autoPolish = hs.settings.get("scribe.autoPolish") == true

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
-- Live level measurement
-- ---------------------------------------------------------------------------

-- Three bars = three real frequency bands (bass/mid/treble of the incoming voice), not three
-- staggered copies of one overall loudness number: the bands genuinely diverge moment to
-- moment. asplit sends the same live audio down three parallel filter chains, each band-limited
-- then measured by astats; ametadata tags each reading with which band it is before printing,
-- since astats itself has no band-name concept. direct=1 flushes per reading instead of
-- buffering to end-of-file.
local ASTATS_BAND_FILTER = table.concat({
    "[0:a]asplit=3[lo][mi][hi];",
    "[lo]lowpass=f=400,",
      "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,",
      "ametadata=mode=add:key=band:value=low,",
      "ametadata=print:file=-:direct=1[olo];",
    "[mi]highpass=f=400,lowpass=f=2500,",
      "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,",
      "ametadata=mode=add:key=band:value=mid,",
      "ametadata=print:file=-:direct=1[omi];",
    "[hi]highpass=f=2500,",
      "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,",
      "ametadata=mode=add:key=band:value=high,",
      "ametadata=print:file=-:direct=1[ohi]",
})

-- Per-band dB ranges mapped to bar height. A band-limited slice reads quieter than the full
-- signal, and unevenly so. Each band gets its own floor/ceiling so all three bars live in the
-- same visual range on normal speech; tune per band if one still looks flat or pinned live.
local BAND_RANGE = {
    low  = { floor = -50, ceil = -16 },
    mid  = { floor = -50, ceil = -12 },
    high = { floor = -58, ceil = -20 },
}

-- ---------------------------------------------------------------------------
-- Pill HUD geometry (shared by the center-screen HUD and the menu-bar glyph)
-- ---------------------------------------------------------------------------

local BAR_COUNT = 3
local BAR_W, BAR_GAP = 7, 17
local PILL_W, PILL_H = 130, 68
local MIN_BAR_H, MAX_BAR_H = 9, 40
local DOT_D = 8

-- Armed badge: a small uppercase model name near the pill's bottom edge, shown only while
-- prompt mode is armed. Faint by design; it is a reminder, not a second thing to read.
local BADGE_SIZE = 10
local BADGE_H    = 12
local BADGE_Y    = PILL_H - 15

-- hs.canvas wants a font NAME, and the bold system font's real name is not a stable literal
-- across macOS releases, so ask hs.styledtext for it. Helvetica-Bold ships with every macOS
-- and covers the case where that lookup ever changes shape; a missing key must not be able to
-- break HUD construction.
local BADGE_FONT = "Helvetica-Bold"
do
    local ok, name = pcall(function() return hs.styledtext.defaultFonts.boldSystem.name end)
    if ok and type(name) == "string" and name ~= "" then BADGE_FONT = name end
end

local function barFrame(i, h)
    local totalW = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP
    local startX = (PILL_W - totalW) / 2
    local x = startX + (i - 1) * (BAR_W + BAR_GAP)
    return { x = x, y = (PILL_H - h) / 2, w = BAR_W, h = h }
end

local function screenCenterFrame(w, h)
    local f = hs.screen.mainScreen():frame()
    return { x = f.x + (f.w - w) / 2, y = f.y + (f.h - h) / 2 - 60, w = w, h = h }
end

-- Build the HUD once (index 1 = pill background, 2..4 = the three bars/dots, 5 = the armed
-- badge). Reused across every dictation rather than recreated, so there is no per-dictation
-- canvas-allocation cost. The badge is built in every time and blanked when prompt mode is off
-- rather than inserted and removed, so the bar indices setBar() writes to can never shift.
local function makeHUD()
    local c = hs.canvas.new(screenCenterFrame(PILL_W, PILL_H))
    c:level(hs.canvas.windowLevels.overlay)
    c:behavior(hs.canvas.windowBehaviors.canJoinAllSpaces | hs.canvas.windowBehaviors.stationary)
    c:clickActivating(false)
    local elems = {
        { type = "rectangle", action = "fill",   -- "roundedRectangle" is not a real hs.canvas
          -- type; rounded corners come from roundedRectRadii on a plain rectangle. Borderless
          -- dark pill: no stroke, by design.
          fillColor = { red = 0.11, green = 0.11, blue = 0.12, alpha = 0.82 },
          roundedRectRadii = { xRadius = PILL_H / 2, yRadius = PILL_H / 2 },
          frame = { x = 0, y = 0, w = PILL_W, h = PILL_H } },
    }
    for i = 1, BAR_COUNT do
        elems[#elems + 1] = { type = "rectangle", action = "fill",
            fillColor = { white = 0.84, alpha = 0.9 },
            roundedRectRadii = { xRadius = BAR_W / 2, yRadius = BAR_W / 2 },
            frame = barFrame(i, MIN_BAR_H) }
    end
    elems[#elems + 1] = { type = "text", text = "",
        textColor = { white = 1, alpha = 0.45 },
        textSize = BADGE_SIZE, textFont = BADGE_FONT, textAlignment = "center",
        frame = { x = 0, y = BADGE_Y, w = PILL_W, h = BADGE_H } }
    c:replaceElements(elems)
    return c
end

-- The badge sits one past the last bar; setBar() owns 2..4 and is untouched by this.
local BADGE_INDEX = BAR_COUNT + 2

-- nil (the "off" case, since PROMPT_BADGES has no "off" key) blanks the label rather than
-- removing the element.
local function setBadge(label)
    if not scribe.hud then return end
    scribe.hud:elementAttribute(BADGE_INDEX, "text", label or "")
end

local function setBar(i, frame, isDot)
    scribe.lastBarFrame = scribe.lastBarFrame or {}
    scribe.lastBarFrame[i] = frame  -- read by the menu-bar poll and by the release morph's start point
    if not scribe.hud then return end
    scribe.hud:elementAttribute(i + 1, "frame", frame)
    scribe.hud:elementAttribute(i + 1, "roundedRectRadii",
        isDot and { xRadius = DOT_D / 2, yRadius = DOT_D / 2 } or { xRadius = BAR_W / 2, yRadius = BAR_W / 2 })
end

local function showHUD()
    if not scribe.hud then scribe.hud = makeHUD() end
    for i = 1, BAR_COUNT do setBar(i, barFrame(i, MIN_BAR_H), false) end
    -- The LATCHED target, not the live menu value: the badge must say what this dictation will
    -- actually do. It stays up through the whole lifecycle, optimizing included.
    setBadge(PROMPT_BADGES[scribe.activePromptTarget])
    scribe.hud:alpha(1)
    scribe.hud:show()
end

local function hideHUD(fade)
    if not scribe.hud then return end
    if fade then scribe.hud:hide(0.3) else scribe.hud:hide() end
end

-- ---------------------------------------------------------------------------
-- Recording animation: each bar is driven by its own frequency band, smoothed
-- like a real audio meter so the three move as a small ripple rather than
-- snapping in lockstep.
-- ---------------------------------------------------------------------------

local function dbToHeight(band, db)
    local r = BAND_RANGE[band]
    if not r then return MIN_BAR_H end
    local t = (db - r.floor) / (r.ceil - r.floor)
    if t < 0 then t = 0 elseif t > 1 then t = 1 end
    t = t ^ 0.6   -- boosts quiet-to-moderate speech so movement stays visible, not just loud peaks
    return MIN_BAR_H + t * (MAX_BAR_H - MIN_BAR_H)
end

-- One smoother per band (bar 1 = low, bar 2 = mid, bar 3 = high), each fed by its own
-- genuinely different signal. Asymmetric, like a real audio meter: a bar RISES quickly when
-- energy arrives (speech still feels instant) but FALLS slowly (per-window jitter melts away
-- instead of flickering).
local ATTACK_ALPHA, DECAY_ALPHA = 0.5, 0.15
local emaLow, emaMid, emaHigh = MIN_BAR_H, MIN_BAR_H, MIN_BAR_H

local function smoothTo(current, target)
    local a = (target > current) and ATTACK_ALPHA or DECAY_ALPHA
    return current + a * (target - current)
end

local function pushBandLevel(band, db)
    -- Only while the mic is open. A reading that arrives after release (the recorder is
    -- signalled, not killed, so a last chunk can still land) would otherwise fight the release
    -- morph for the same bar frames.
    if not scribe.recording then return end
    local target = dbToHeight(band, db)
    if band == "low" then
        emaLow = smoothTo(emaLow, target)
        setBar(1, barFrame(1, emaLow), false)
    elseif band == "mid" then
        emaMid = smoothTo(emaMid, target)
        setBar(2, barFrame(2, emaMid), false)
    elseif band == "high" then
        emaHigh = smoothTo(emaHigh, target)
        setBar(3, barFrame(3, emaHigh), false)
    end
end

-- hs.task hands stdout over in arbitrary chunks that are not necessarily whole lines, so both
-- parsers below keep a buffer and only ever act on complete lines. Returns the leftover tail.
local function completeLines(buffer, onLine)
    for line in buffer:gmatch("([^\n]*)\n") do onLine(line) end
    local lastNL = buffer:find("\n[^\n]*$")
    if not lastNL then return buffer end   -- no newline yet: keep the partial line for next time
    return buffer:sub(lastNL + 1)
end

-- Parses the streaming worker's stdout: clean, atomic "LEVEL <band> <dB>" lines that the
-- worker's own Python parser produces from the same three-band graph, plus MIC_READY. The
-- worker pre-pairs readings on its side, so this parser is a straight line-per-reading match.
local workerBuf = ""
local function feedWorkerChunk(text)
    if not text or text == "" then return end
    workerBuf = completeLines(workerBuf .. text, function(line)
        local band, db = line:match("^LEVEL (%a+) (-?[%d%.]+)")
        if band and db then pushBandLevel(band, tonumber(db)) end
    end)
end

-- Parses ffmpeg's tagged astats stdout as it streams in. Each reading prints as two lines, an
-- "RMS_level=" line followed by a "band=low|mid|high" line identifying which of the three
-- parallel filter chains it came from; a "frame:N ..." header line precedes both and is
-- ignored. The three bands are NOT assumed to arrive in any particular order or interleaving,
-- so each band updates independently whenever its own reading actually arrives.
local levelBuf = ""
local pendingRMS = nil
local function feedLevelChunk(text)
    if not text or text == "" then return end
    levelBuf = completeLines(levelBuf .. text, function(line)
        local db = line:match("RMS_level=(-?[%d%.]+)")
        local band = line:match("^band=(%a+)")
        if line:find("^frame:") then
            pendingRMS = nil   -- a header before the tag means the pair broke; discard, never mispair
        elseif db then
            pendingRMS = tonumber(db)
        elseif band and pendingRMS then
            pushBandLevel(band, pendingRMS)
            pendingRMS = nil
        end
    end)
end

-- ---------------------------------------------------------------------------
-- Transcribing animation: the iMessage typing bounce. Each dot lifts on its
-- own delay (150ms stagger) and settles before the next starts, 1.15s loop.
-- ---------------------------------------------------------------------------

local function startTypingBounce()
    if scribe.animTimer then scribe.animTimer:stop() end
    local t0 = hs.timer.secondsSinceEpoch()
    local period, stagger, lift = 1.15, 0.15, 7
    scribe.animTimer = hs.timer.doEvery(0.03, function()
        local t = hs.timer.secondsSinceEpoch() - t0
        for i = 1, BAR_COUNT do
            local phase = ((t - (i - 1) * stagger) % period) / period
            -- ease in/out lift over the first ~40% of the cycle, settle for the rest
            local lifted = 0
            if phase < 0.4 then
                lifted = lift * math.sin((phase / 0.4) * math.pi)
            end
            local base = barFrame(i, DOT_D)
            base.y = base.y - lifted
            setBar(i, base, true)
        end
    end)
end

-- ---------------------------------------------------------------------------
-- Optimizing animation: the same three dots leave their row and orbit the
-- pill's center, 120 degrees apart, one revolution per 1.8s. Deliberately a
-- different KIND of motion from the typing bounce, so "still transcribing" and
-- "rewriting your prompt" are told apart at a glance rather than by speed.
-- ---------------------------------------------------------------------------

local ORBIT_RADIUS, ORBIT_PERIOD = 14, 1.8

local function startOrbit()
    -- One animation timer at a time, as everywhere else here: reusing animTimer means every
    -- existing stop path (finishTranscription, the reload cleanup, the recorder-died branch)
    -- already stops this one too.
    if scribe.animTimer then scribe.animTimer:stop() end
    local cx, cy = PILL_W / 2, PILL_H / 2
    local step = 2 * math.pi / BAR_COUNT   -- 120 degrees between dots
    local t0 = hs.timer.secondsSinceEpoch()
    scribe.animTimer = hs.timer.doEvery(0.03, function()
        local t = hs.timer.secondsSinceEpoch() - t0
        local base = ((t % ORBIT_PERIOD) / ORBIT_PERIOD) * 2 * math.pi
        for i = 1, BAR_COUNT do
            local angle = base + (i - 1) * step
            setBar(i, { x = cx + ORBIT_RADIUS * math.cos(angle) - DOT_D / 2,
                        y = cy + ORBIT_RADIUS * math.sin(angle) - DOT_D / 2,
                        w = DOT_D, h = DOT_D }, true)
        end
    end)
end

-- ---------------------------------------------------------------------------
-- Release transition: bars pinch and sweep into dots, left to right with a
-- slight overlap, before the steady-state typing bounce takes over. Only uses
-- elementAttribute()/frame/roundedRectRadii, so this adds motion, not new
-- Hammerspoon API surface.
-- ---------------------------------------------------------------------------

local function lerp(a, b, t) return a + (b - a) * t end
local function easeOutCubic(t) return 1 - (1 - t) ^ 3 end

local function startMorphToDots()
    if scribe.animTimer then scribe.animTimer:stop() end
    local startFrame, targetFrame = {}, {}
    for i = 1, BAR_COUNT do
        startFrame[i] = (scribe.lastBarFrame and scribe.lastBarFrame[i]) or barFrame(i, MIN_BAR_H)
        targetFrame[i] = barFrame(i, DOT_D)
    end

    local stagger, dur = 0.07, 0.26   -- ~70ms between bars, ~260ms each -> ~400ms total sweep
    local total = stagger * (BAR_COUNT - 1) + dur
    local t0 = hs.timer.secondsSinceEpoch()
    scribe.animTimer = hs.timer.doEvery(0.02, function()
        local t = hs.timer.secondsSinceEpoch() - t0
        if t >= total then
            startTypingBounce()   -- hand off to the steady-state bounce
            return
        end
        for i = 1, BAR_COUNT do
            local localT = t - (i - 1) * stagger
            if localT <= 0 then
                setBar(i, startFrame[i], false)
            else
                local p = math.min(localT / dur, 1)
                local e = easeOutCubic(p)
                local pinch = math.sin(p * math.pi)   -- 0 at both ends, peak at the midpoint
                local sF, tF = startFrame[i], targetFrame[i]
                local cx = lerp(sF.x + sF.w / 2, tF.x + tF.w / 2, e)   -- interpolate the CENTER,
                local cy = lerp(sF.y + sF.h / 2, tF.y + tF.h / 2, e)   -- so a narrowing bar stays
                local w = math.max(2, lerp(sF.w, tF.w, e) - pinch * (sF.w * 0.35))  -- centered, not drifting
                local h = lerp(sF.h, tF.h, e)
                local radius = lerp(BAR_W / 2, DOT_D / 2, e)
                if scribe.hud then
                    scribe.hud:elementAttribute(i + 1, "frame", { x = cx - w / 2, y = cy - h / 2, w = w, h = h })
                    scribe.hud:elementAttribute(i + 1, "roundedRectRadii", { xRadius = radius, yRadius = radius })
                end
            end
        end
    end)
end

-- ---------------------------------------------------------------------------
-- Menu-bar glyph: the same three-mark family at a scale that fits the menu
-- bar, drawn as a template image so it adapts to light and dark menu bars.
-- ---------------------------------------------------------------------------

local MB_W, MB_H = 20, 14
local MB_DOT_D = 3.6   -- fixed diameter for the idle/transcribing dots; only their Y moves

local function mbSlotX(i, w)
    local gap = 4.4
    local totalW = BAR_COUNT * w + (BAR_COUNT - 1) * gap
    local startX = (MB_W - totalW) / 2
    return startX + (i - 1) * (w + gap)
end

-- Recording: bars, width fixed, height varies to read as a waveform.
local function makeMenuBars(heights)
    local c = hs.canvas.new({ x = 0, y = 0, w = MB_W, h = MB_H })
    local elems = {}
    local w = 2.6
    for i = 1, BAR_COUNT do
        local h = heights[i]
        elems[#elems + 1] = { type = "rectangle", action = "fill",
            fillColor = { black = 1, alpha = 0.85 },
            roundedRectRadii = { xRadius = w / 2, yRadius = w / 2 },
            frame = { x = mbSlotX(i, w), y = (MB_H - h) / 2, w = w, h = h } }
    end
    c:replaceElements(elems)
    local img = c:imageFromCanvas()
    c:delete()
    return img
end

-- Idle and transcribing: dots, a fixed small circle per mark, moved up by yOffset[i] to bounce.
-- Distinct from the bars above on purpose: a rectangle that only changes height still reads as
-- a bar no matter how round its corners are, so the dots keep a fixed diameter and move in Y.
local function makeMenuDots(yOffsets)
    local c = hs.canvas.new({ x = 0, y = 0, w = MB_W, h = MB_H })
    local elems = {}
    local baseY = (MB_H - MB_DOT_D) / 2
    for i = 1, BAR_COUNT do
        elems[#elems + 1] = { type = "rectangle", action = "fill",
            fillColor = { black = 1, alpha = 0.85 },
            roundedRectRadii = { xRadius = MB_DOT_D / 2, yRadius = MB_DOT_D / 2 },  -- true circle
            frame = { x = mbSlotX(i, MB_DOT_D), y = baseY - (yOffsets[i] or 0), w = MB_DOT_D, h = MB_DOT_D } }
    end
    c:replaceElements(elems)
    local img = c:imageFromCanvas()
    c:delete()
    return img
end

local IDLE_ICON = nil   -- built below, once the menu bar itself exists

-- If canvas drawing is unavailable, every state falls back to an emoji title instead. That is
-- the only case where Scribe still puts status text in the menu bar.
local function applyMenuIcon(image, fallbackText)
    if not scribe.menu then return end
    if scribe.iconsOk and image then
        scribe.menu:setIcon(image, true)
    else
        scribe.menu:setTitle(fallbackText)
    end
end

local function setMenuIdle()
    if scribe.menuAnimTimer then scribe.menuAnimTimer:stop() end
    applyMenuIcon(IDLE_ICON, "🎙️")
end

local MB_MIN_H, MB_MAX_H = 3, 11
local function scaleToMenuBar(hudHeight)
    local t = (hudHeight - MIN_BAR_H) / (MAX_BAR_H - MIN_BAR_H)
    if t < 0 then t = 0 elseif t > 1 then t = 1 end
    return MB_MIN_H + t * (MB_MAX_H - MB_MIN_H)
end

-- Reflects the SAME live level driving the HUD bars, polled at a modest rate, rather than a
-- canned on/off toggle: a blink every N milliseconds has nothing to do with your actual voice.
local function setMenuRecording()
    if scribe.menuAnimTimer then scribe.menuAnimTimer:stop() end
    if not scribe.iconsOk then
        applyMenuIcon(nil, "🔴")
        return
    end
    scribe.menuAnimTimer = hs.timer.doEvery(0.15, function()
        local heights = {}
        for i = 1, BAR_COUNT do
            local f = scribe.lastBarFrame and scribe.lastBarFrame[i]
            heights[i] = scaleToMenuBar(f and f.h or MIN_BAR_H)
        end
        applyMenuIcon(makeMenuBars(heights), "🔴")
    end)
end

local function setMenuTranscribing()
    if scribe.menuAnimTimer then scribe.menuAnimTimer:stop() end
    if not scribe.iconsOk then
        applyMenuIcon(nil, "⏳")
        return
    end
    local frame = 0
    local shapes = { { 1.6, 0, 0 }, { 0, 1.6, 0 }, { 0, 0, 1.6 } }   -- 3-frame left-to-right lift
    scribe.menuAnimTimer = hs.timer.doEvery(0.35, function()
        frame = frame % 3 + 1
        applyMenuIcon(makeMenuDots(shapes[frame]), "⏳")
    end)
end

scribe.menu = hs.menubar.new()
local okIcon, idleImage = pcall(makeMenuDots, { 0, 0, 0 })
IDLE_ICON = okIcon and idleImage or nil
scribe.iconsOk = (okIcon and IDLE_ICON ~= nil
    and pcall(function() scribe.menu:setIcon(IDLE_ICON, true) end)) or false
if not scribe.iconsOk then
    log("menu-bar icon could not be drawn; falling back to emoji titles")
    scribe.menu:setTitle("🎙️")
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
--   [AVFoundation indev @ 0x7f8] [0] Built-in Microphone
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
        -- Fresh baseline so the menu-bar poll and the next release's morph never read stale
        -- frames left over from the previous dictation.
        scribe.lastBarFrame = { barFrame(1, MIN_BAR_H), barFrame(2, MIN_BAR_H), barFrame(3, MIN_BAR_H) }
        setMenuRecording()   -- first, so the menu bar updates even if the HUD below errors
        local ok, err = pcall(showHUD)
        if not ok then log("HUD error: " .. tostring(err)) end
        -- Both modes feed real band levels (batch parses ffmpeg directly, streaming parses the
        -- worker's LEVEL lines), so both start from the same clean baseline.
        emaLow, emaMid, emaHigh = MIN_BAR_H, MIN_BAR_H, MIN_BAR_H
        levelBuf, pendingRMS, workerBuf = "", nil, ""
    end
end

-- Everything a finished (or failed) run has to undo: the HUD animation, the HUD itself, and
-- the menu-bar state. Errors still speak for themselves through alertProblem.
local function finishTranscription()
    scribe.transcribing = false
    scribe.optimizing = false
    if scribe.animTimer then scribe.animTimer:stop() end
    hideHUD(true)
    setMenuIdle()
end

-- The Python side prints a flushed phase line when an LLM pass begins (pipeline stdout in
-- batch, worker stdout in streaming): OPTIMIZING for the prompt rewrite, POLISHING for the
-- auto-polish. They mean the same thing to the user, an AI pass is running and it takes about
-- ten seconds, so both drive this one state and this one animation. Swap the typing bounce for
-- the orbit; the menu bar keeps its existing transcribing state, which already reads as "working".
-- Guarded twice: once so a marker arriving while the mic is still open cannot fight the level
-- bars for the same frames, and once so a repeated marker does not restart the orbit mid-turn.
local function enterOptimizing()
    if scribe.recording or scribe.optimizing then return end
    scribe.optimizing = true
    startOrbit()
    setMenuTranscribing()
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
    -- Two halves of the same guarantee, both provided by pipeline.py:
    --   --not-older-than  refuses a WAV written before this recording started, so a recorder
    --                     that silently failed cannot get the PREVIOUS dictation pasted.
    --   --consume         deletes the WAV once it has been read, so it cannot be reused.
    local args = { PIPELINE, WAV, "--copy", "--consume",
                   "--not-older-than", string.format("%d", scribe.sessionStart or 0) }
    -- The LATCHED target and the LATCHED auto-polish, so a menu change made during
    -- transcription cannot redirect the run that is already under way. The two are mutually
    -- exclusive by construction (see the latch in startRecording): pipeline.py refuses
    -- --mode full together with --optimize-for.
    if scribe.activePromptTarget and scribe.activePromptTarget ~= "off" then
        args[#args + 1] = "--optimize-for"
        args[#args + 1] = scribe.activePromptTarget
    elseif scribe.activeAutoPolish then
        args[#args + 1] = "--mode"
        args[#args + 1] = "full"
    end
    scribe.pipeTask = hs.task.new(PYTHON, function(exitCode, _, stderr)
        finishTranscription()
        if exitCode == 0 then
            hs.eventtap.keyStroke({ "cmd" }, "v")
        elseif exitCode == 4 then
            -- The text IS on the clipboard and ready to paste, it is just the raw
            -- transcription: the optimizer could not run. Paste exactly as for 0, then say so.
            hs.eventtap.keyStroke({ "cmd" }, "v")
            alertProblem("prompt optimizer unavailable, pasted the raw transcription", 2.5)
            log("pipeline stderr: " .. (stderr or ""))
        elseif exitCode == 5 then
            -- Same contract as 4, for the auto-polish: pasted-ready, but unpolished.
            hs.eventtap.keyStroke({ "cmd" }, "v")
            alertProblem("polish unavailable, pasted the unpolished transcription", 2.5)
            log("pipeline stderr: " .. (stderr or ""))
        elseif exitCode == 6 then
            -- Same contract again, with the cause known: the AI pass never ran because the
            -- Claude CLI is logged out. Say the fix rather than "unavailable".
            hs.eventtap.keyStroke({ "cmd" }, "v")
            alertProblem("Claude CLI not logged in (run: claude /login), pasted the raw "
                .. "transcription", 2.5)
            log("pipeline stderr: " .. (stderr or ""))
        elseif exitCode == 3 then
            return                                                -- nothing was said: reset quietly
        else
            alertProblem("dictation failed (see console)", 2)
            log("pipeline error: " .. (stderr or ""))
        end
    end,
    function(_, stdOut, _)                                        -- stream callback: the phase markers
        if stdOut and (stdOut:find("OPTIMIZING") or stdOut:find("POLISHING")) then
            enterOptimizing()
        end
        return true
    end,
    args)
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
        scribe.latchArmed, scribe.latched = false, false   -- a stale latch would misread the next PTT press
        if scribe.cueTimer then scribe.cueTimer:stop() end
        if scribe.animTimer then scribe.animTimer:stop() end
        hideHUD(true)
        setMenuIdle()
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
        scribe.latchArmed, scribe.latched = false, false   -- a stale latch would misread the next PTT press
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
    elseif exitCode == 4 then
        -- Same contract as the batch path: pasted-ready, but raw, because the optimizer was
        -- unavailable.
        hs.eventtap.keyStroke({ "cmd" }, "v")
        alertProblem("prompt optimizer unavailable, pasted the raw transcription", 2.5)
        log("stream worker stderr: " .. (stderr or ""))
    elseif exitCode == 5 then
        -- Same again for the auto-polish: the words are on the clipboard, unpolished.
        hs.eventtap.keyStroke({ "cmd" }, "v")
        alertProblem("polish unavailable, pasted the unpolished transcription", 2.5)
        log("stream worker stderr: " .. (stderr or ""))
    elseif exitCode == 6 then
        -- Same contract, cause known: the AI pass never ran because the Claude CLI is logged
        -- out. Say the fix rather than "unavailable".
        hs.eventtap.keyStroke({ "cmd" }, "v")
        alertProblem("Claude CLI not logged in (run: claude /login), pasted the raw "
            .. "transcription", 2.5)
        log("stream worker stderr: " .. (stderr or ""))
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
    -- Same latch for the prompt-mode target, for the same reason: the badge, the argv and the
    -- OPTIMIZING phase all have to agree with each other for the whole of THIS dictation.
    scribe.activePromptTarget = scribe.promptTarget
    -- And for auto-polish. Both gates from the menu are repeated here rather than trusted,
    -- because the persisted setting outlives them: POLISH_ENABLED, so a config with the polish
    -- switched off does not ask for a pass that can only fail, and the prompt-mode precedence,
    -- so a target armed after auto-polish was switched on cannot put both flags on one command
    -- line (pipeline.py refuses --mode full with --optimize-for outright).
    scribe.activeAutoPolish = POLISH_ENABLED and scribe.autoPolish
        and scribe.activePromptTarget == "off"
    scribe.optimizing = false
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
    -- Every fresh recording starts unlatched and unarmed, even if a previous one somehow left
    -- these set (e.g. the recorder died mid-latch below); a stale scribe.latched here would
    -- make the very next PTT press stop instead of start.
    scribe.latchArmed, scribe.latched, scribe.latchStopping = false, false, false

    if scribe.activeStreaming then
        if scribe.workerPid then signalPid(scribe.workerPid) end  -- a worker left by a crashed run
        -- The worker owns ffmpeg internally; dictate.lua only starts it, watches stdout for the
        -- ready cue and the LEVEL lines that drive the HUD, and signals it to stop. It
        -- transcribes, copies to the clipboard, and exits.
        local workerArgs = { WORKER_PATH, "--mic", scribe.micIndex, "--copy" }
        if scribe.activePromptTarget ~= "off" then
            workerArgs[#workerArgs + 1] = "--optimize-for"
            workerArgs[#workerArgs + 1] = scribe.activePromptTarget
        elseif scribe.activeAutoPolish then
            workerArgs[#workerArgs + 1] = "--polish"
        end
        scribe.recTask = hs.task.new(PYTHON, streamWorkerFinished,
            function(_, stdOut, _)                                 -- stream callback: cue, levels, phase
                if stdOut then
                    if stdOut:find("MIC_READY") then cueSpeak() end
                    if stdOut:find("OPTIMIZING") or stdOut:find("POLISHING") then
                        enterOptimizing()
                    end
                    feedWorkerChunk(stdOut)                        -- "LEVEL <band> <dB>" -> HUD bars
                end
                return true
            end,
            workerArgs)
    else
        -- Delete first, so a recorder that never produces a file cannot leave the previous
        -- dictation lying here to be transcribed and pasted as if it were new. The session
        -- timestamp is the belt to that pair of braces: pipeline.py refuses any WAV whose
        -- mtime predates it.
        os.remove(WAV)
        scribe.sessionStart = os.time()
        scribe.pendingTranscribe = false
        -- One process, several output legs: the WAV Scribe transcribes, plus three tagged dB
        -- readings, one per frequency band, that drive the HUD's bars. -filter_complex turns
        -- off ffmpeg's automatic stream mapping, so the WAV leg needs its own explicit -map.
        scribe.recTask = hs.task.new(FFMPEG, batchRecorderFinished,
            function(_, stdOut, stdErr)                            -- stream callback: cue, then levels
                if ((stdOut or "") .. (stdErr or "")):find("size=") then cueSpeak() end
                if stdOut then feedLevelChunk(stdOut) end
                return true
            end,
            { "-y", "-f", "avfoundation", "-i", ":" .. scribe.micIndex,
              "-filter_complex", ASTATS_BAND_FILTER,
              "-map", "0:a", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", WAV,
              "-map", "[olo]", "-f", "null", "-",
              "-map", "[omi]", "-f", "null", "-",
              "-map", "[ohi]", "-f", "null", "-" })
    end
    if not scribe.recTask then
        -- hs.task.new returns nil if it cannot launch. Fail here rather than
        -- raising a Lua error inside the event tap callback.
        scribe.recording, scribe.cued = false, false
        setMenuIdle()
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
    -- The bars sweep into dots and then bounce; that is the "transcribing" notice now, in both
    -- modes, so it starts on release rather than waiting for the recorder to exit.
    startMorphToDots()
    setMenuTranscribing()

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

-- Latch feedback: "armed" is a one-shot cue at the moment of the gesture, the same
-- hs.alert.show pattern already used elsewhere in this file for a state change (see the menu
-- toggles below). "latched" writes into the HUD's one badge slot, the same slot the prompt-mode
-- target already uses (see showHUD/setBadge above), so the reminder stays on screen for as long
-- as the hands-free recording runs rather than flashing and being missed. Both reuse existing
-- plumbing; neither adds a new HUD element or a new kind of alert.
local function latchArmedFeedback()
    hs.alert.show("Scribe: latch armed, release to go hands-free", 1)
end

local function latchOnFeedback()
    setBadge("LATCHED")
    hs.alert.show("Scribe: latched, press PTT again to stop", 1.2)
end

-- Push-to-talk: watch the modifier without consuming it, so normal typing still works. The
-- latch key (default left shift) is folded into the same event tap rather than a second one, so
-- there is exactly one place that ever calls startRecording/stopRecording.
scribe.ptt = hs.eventtap.new({ hs.eventtap.event.types.flagsChanged }, function(e)
    local keyCode = e:getKeyCode()
    local flags = e:getFlags()

    if LATCH_ENABLED and keyCode == LATCH_KEYCODE then
        -- Only the down edge of a tap matters: arm while PTT is actively held and not already
        -- latched. Shift going back up on its own does nothing here, and types no character
        -- either way, so this branch never needs to consume the event.
        if flags[LATCH_FLAG] and scribe.recording and not scribe.latched then
            scribe.latchArmed = true
            latchArmedFeedback()
        end
        return false
    end

    if keyCode == PTT_KEYCODE then
        if flags[PTT_FLAG] then
            -- PTT key down.
            if scribe.latched then
                -- A second PTT press while latched stops the hands-free recording and finishes
                -- it normally, through the exact same stopRecording() as every other release.
                -- latchStopping marks that this press already did the job, so the matching
                -- key-up (below) does not try to start or stop anything of its own.
                scribe.latched = false
                scribe.latchStopping = true
                stopRecording()
            else
                startRecording()
            end
        else
            -- PTT key up.
            if scribe.latchStopping then
                scribe.latchStopping = false          -- the stopping press's key-up: no-op
            elseif scribe.latchArmed and scribe.recording then
                -- Armed, and PTT is being released: keep recording hands-free instead of
                -- stopping. stopRecording() is deliberately not called on this path.
                scribe.latchArmed = false
                scribe.latched = true
                latchOnFeedback()
            else
                scribe.latchArmed = false
                stopRecording()
            end
        end
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
    setMenuTranscribing()                                         -- same "working" state as a dictation
    hs.alert.show("polishing last dictation (~11s)...", 1)
    scribe.polishTask = hs.task.new(PYTHON, function(exitCode, _, stderr)
        setMenuIdle()
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
        -- Auto-polish only exists where the polish itself does, so it lives inside the same
        -- gate. While a prompt target is armed the optimizer wins and this setting does
        -- nothing, so the item says that outright rather than showing a checkmark that would
        -- quietly have no effect.
        if scribe.promptTarget ~= "off" then
            items[#items + 1] = { title = "Auto-polish (overridden by prompt mode)",
                                  disabled = true }
        else
            items[#items + 1] = { title = "Auto-polish every dictation",
                checked = scribe.autoPolish,
                fn = function()
                    scribe.autoPolish = not scribe.autoPolish
                    hs.settings.set("scribe.autoPolish", scribe.autoPolish)
                    buildMenu()
                    hs.alert.show(scribe.autoPolish and "Auto-polish ON" or "Auto-polish OFF", 1)
                end }
        end
    end
    items[#items + 1] = { title = "Recall last dictation   (⌘⌥⌃L)", fn = doRecall }
    items[#items + 1] = { title = "Streaming mode (beta)", checked = scribe.streaming, fn = function()
        scribe.streaming = not scribe.streaming
        hs.settings.set("scribe.streaming", scribe.streaming)
        buildMenu()
        hs.alert.show(scribe.streaming and "Streaming mode ON" or "Streaming mode OFF", 1)
    end }
    -- Radio group: exactly one target is armed at a time, so these are mutually exclusive
    -- checkmarks rather than four independent toggles. Same persist-then-rebuild pattern as
    -- the streaming toggle above.
    items[#items + 1] = { title = "Prompt mode", disabled = true }
    for _, target in ipairs(PROMPT_TARGETS) do
        items[#items + 1] = { title = PROMPT_LABELS[target],
            checked = (scribe.promptTarget == target),
            fn = function()
                scribe.promptTarget = target
                hs.settings.set("scribe.promptTarget", target)
                buildMenu()
                hs.alert.show(target == "off" and "Prompt mode OFF"
                    or ("Prompt mode: " .. PROMPT_LABELS[target]), 1)
            end }
    end
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

    -- The polish and prompt passes run the user's own Claude CLI, and a logged-out CLI fails
    -- every one of them the same way. Say it once here, where the fix is one command, instead
    -- of leaving the user to discover it mid-dictation. Exit 1 means logged out and the line
    -- to show is on stdout; every other outcome exits 0 and says nothing, deliberately, since
    -- no CLI configured and a CLI too old for the subcommand are both fine setups. Async, like
    -- everything else here.
    scribe.authCheck = hs.task.new(PYTHON, function(exitCode, stdOut, stderr)
        if exitCode ~= 0 then
            local message = (stdOut or ""):match("^%s*(.-)%s*$")
            if message == "" then
                message = "Claude CLI not logged in - run: claude /login"
            end
            alertProblem(message, 8)
            log("auth check stderr: " .. (stderr or ""))
        end
    end, { PIPELINE, "--check-auth" })
    scribe.authCheck:start()
end
if not scribe.micName then
    alertProblem("no microphone configured. Run: python3 " .. APP_DIR .. "/scribe_setup.py", 8)
end

log("version " .. SCRIBE_VERSION .. "; ffmpeg=" .. tostring(FFMPEG) .. "; python=" .. tostring(PYTHON))

local loadedMsg = "Scribe " .. SCRIBE_VERSION
    .. " loaded (hold the push-to-talk key = talk, ⌘⌥⌃L = recall"
if POLISH_ENABLED then loadedMsg = loadedMsg .. ", ⌘⌥⌃P = polish" end
hs.alert.show(loadedMsg .. ")", 2.5)
