#!/usr/bin/env python3
"""Scribe streaming worker: record, hand off finished speech to whisper while the user is
still talking, and finish fast on key release.

Usage:
    stream_worker.py --mic INDEX [--language en] [--copy] [--timings]
                     [--optimize-for fable|opus|sonnet]
    stream_worker.py --from-file PATH [--language en] [--copy] [--timings]  # same logic, no microphone

Architecture: "rolling handoff, never force-cut".

One ffmpeg captures the microphone to a raw PCM file AND runs silencedetect on a second
output leg. In live mode it also runs three band-limited RMS meters (bass/mid/treble) whose
readings this worker republishes on its own stdout as "LEVEL <band> <dB>" lines for the
dictate.lua HUD. File mode has no HUD to feed, so it gets no band legs. While recording, whenever enough audio has piled up unprocessed, the worker
cuts it at the MIDPOINT OF A REAL SILENCE and sends that piece to the warm whisper-server.
On key release only the short tail is left to transcribe, so the wait is short and roughly
constant instead of growing with dictation length.

Two measured facts shape every decision here:
  * The warm server costs ~2.05s + ~0.04s per audio-second per request and serializes
    requests. So: few large chunks, submitted sequentially, never many small ones.
  * Cutting at a real silence (>=0.5s) and transcribing the pieces separately with the
    glossary prompt gives word-identical text versus the whole file. Cutting mid-speech
    makes whisper invent fluent completions biased toward the glossary names. So a cut
    happens ONLY at a detected silence; if there is no silence, nothing is handed off and
    the run simply degrades to plain batch behaviour.

Prompt mode (--optimize-for): after the chunks are merged and cleaned, the text is rewritten
into a prompt aimed at one target model (fable, opus, sonnet) by pipeline.optimize_prompt,
and that rewrite is what is emitted and copied. The worker prints the OPTIMIZING phase marker
on its own stdout, the same channel MIC_READY and the LEVEL meters already use, immediately
before the rewriting CLI starts.

Exit codes: 0 success (text on stdout and, with --copy, on the clipboard), 3 nothing was
captured (dictate.lua treats this as an aborted dictation: no paste, no error), 1 failure,
4 the rewrite asked for by --optimize-for was unavailable so the RAW cleaned transcription
was emitted and copied instead (the words are never lost, only left un-rewritten).

Every run appends timestamped lines to the shared Scribe log (pipeline.log).
"""
import argparse, os, queue, re, shutil, signal, struct, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import pipeline

from pipeline import log

# Fallback only. The real path comes from config.json's "ffmpeg_bin", which install.sh writes;
# resolving from PATH here keeps an Intel Mac (/usr/local/bin) working without a config file.
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2                       # mono s16le
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE

SILENCE_FILTER = "silencedetect=noise=-35dB:d=0.5"

# Three parallel band-limited RMS meters (bass <400Hz, mid 400-2500Hz, treble >2500Hz) whose
# tagged readings drive the HUD's three bars in dictate.lua. asplit feeds the same live audio
# to all three; ametadata tags each reading with its band (astats itself has no band concept)
# and prints to stdout with direct=1 so readings flush per window instead of at EOF. Verified
# against files: the full command shape (PCM leg + silencedetect -af leg + these three legs)
# runs clean, silencedetect still fires (2/2 events vs control), PCM output byte-exact.
BAND_FILTER = (
    "[0:a]asplit=3[lo][mi][hi];"
    "[lo]lowpass=f=400,"
    "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
    "ametadata=mode=add:key=band:value=low,"
    "ametadata=print:file=-:direct=1[olo];"
    "[mi]highpass=f=400,lowpass=f=2500,"
    "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
    "ametadata=mode=add:key=band:value=mid,"
    "ametadata=print:file=-:direct=1[omi];"
    "[hi]highpass=f=2500,"
    "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
    "ametadata=mode=add:key=band:value=high,"
    "ametadata=print:file=-:direct=1[ohi]"
)

MIN_ACCUM_S = 22.0                         # only hand off once this much audio is unprocessed
MIN_CHUNK_S = 5.0                          # never produce a chunk shorter than this
POLL_S = 0.25                              # how often the handoff rule is evaluated

# How long the PCM file may stand still, once it has started growing, before the capture is
# treated as wedged. Measured on this machine over 40s of wall-clock-paced capture through
# the real five-leg command: 625 write events, gap between writes median 0.068s, p95 0.075s,
# worst 0.092s. 3s is ~30x the worst normal gap, so a hiccup cannot trip it, and it is short
# enough that a wedge is caught long before the user finishes talking. The counter only
# starts after the first byte: the microphone legitimately takes a second or two to open.
CAPTURE_STALL_S = 3.0

# Readings buffered between the band pump and stdout before new ones are dropped. Three
# bands at ~15 readings/s each is ~45 lines/s, so 256 is roughly 5s of meter history: enough
# to ride out a consumer that pauses briefly, small enough that a wedged consumer never
# builds an unbounded backlog. Dropping a meter frame costs a stale HUD bar for one frame.
LEVEL_QUEUE_MAX = 256

EXIT_EMPTY = 3                             # nothing captured; dictate.lua resets quietly
EXIT_FAIL = 1
EXIT_OPTIMIZER_FALLBACK = 4                # same meaning as pipeline.EXIT_OPTIMIZER_FALLBACK


def pcm_path():
    """Live capture target, inside the Scribe state directory. dictate.lua never touches it."""
    return pipeline.STREAM_PCM_PATH


def chunk_dir():
    """Where chunk WAVs and the claimed PCM copies live; created on demand."""
    return pipeline.ensure_state_dir()


# --------------------------------------------------------------------------------------
# WAV framing
# --------------------------------------------------------------------------------------

def wav_header(pcm_len, rate=SAMPLE_RATE, channels=1, bits=16):
    """44-byte canonical WAV header for `pcm_len` bytes of raw PCM."""
    block_align = channels * bits // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + pcm_len, b"WAVE",
        b"fmt ", 16, 1, channels, rate, rate * block_align, block_align, bits,
        b"data", pcm_len,
    )


def write_wav(path, pcm_bytes):
    with open(path, "wb") as f:
        f.write(wav_header(len(pcm_bytes)))
        f.write(pcm_bytes)
    return path


# --------------------------------------------------------------------------------------
# Silence boundaries
# --------------------------------------------------------------------------------------

class SilenceParser:
    """Turns ffmpeg silencedetect stderr into boundary candidates.

    A candidate is the MIDPOINT of a completed silence, so a cut there leaves real silence
    on both sides. Only completed silences count: an open silence_start with no silence_end
    yet may still turn out to be the end of the recording.
    """

    _END = re.compile(r"silence_end:\s*(-?[0-9.]+)")
    _START = re.compile(r"silence_start:\s*(-?[0-9.]+)")

    def __init__(self):
        self._open_start = None
        self.boundaries = []      # list of (midpoint_s, known_at_s)

    def feed_line(self, line):
        """Feed one stderr line. Returns the boundary it completed, or None."""
        end = self._END.search(line)
        if end is not None:
            if self._open_start is None:
                return None       # silence_end without a start we saw; ignore
            end_s = float(end.group(1))
            boundary = (max(0.0, (self._open_start + end_s) / 2.0), end_s)
            self._open_start = None
            self.boundaries.append(boundary)
            return boundary
        start = self._START.search(line)
        if start is not None:
            self._open_start = float(start.group(1))
        return None

    def midpoints(self):
        return [mid for mid, _known_at in list(self.boundaries)]

    def midpoints_known_by(self, t):
        """Boundaries a live run would already have seen at time `t`. Used by file mode to
        replay the live timeline instead of cheating with full hindsight."""
        return [mid for mid, known_at in list(self.boundaries) if known_at <= t]


class HandoffTracker:
    """The handoff rule, and the only place `handoff_start` moves."""

    def __init__(self, min_accum=MIN_ACCUM_S, min_chunk=MIN_CHUNK_S):
        self.handoff_start = 0.0
        self.min_accum = min_accum
        self.min_chunk = min_chunk

    def next_cut(self, boundaries, recorded_duration):
        """The boundary to cut at now, or None. Never force-cuts."""
        if recorded_duration - self.handoff_start < self.min_accum:
            return None
        floor = self.handoff_start + self.min_chunk
        usable = [b for b in boundaries if floor <= b <= recorded_duration]
        return max(usable) if usable else None

    def take(self, cut):
        """Consume the range up to `cut` and return it as (start, end)."""
        start = self.handoff_start
        self.handoff_start = cut
        return start, cut


def slice_pcm(path, start_s, end_s=None):
    """Sample-aligned byte slice of the raw PCM file. Adjacent slices reconcatenate exactly.

    `end_s=None` reads to end-of-file, which is how the tail is taken: converting the total
    duration back to a byte offset would truncate the last sample for some file sizes.
    """
    start = _align(int(start_s * BYTES_PER_SECOND))
    with open(path, "rb") as f:
        f.seek(start)
        if end_s is None:
            return f.read()
        end = _align(int(end_s * BYTES_PER_SECOND))
        return f.read(end - start) if end > start else b""


def _align(offset):
    return offset - (offset % BYTES_PER_SAMPLE)


# --------------------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------------------

class ChunkTranscriptionError(RuntimeError):
    """A chunk failed twice. Carries the audio path so the words can be recovered by hand."""

    def __init__(self, wav_path, cause):
        RuntimeError.__init__(self, "chunk transcription failed for %s: %s" % (wav_path, cause))
        self.wav_path = wav_path
        self.cause = cause


def transcribe_with_retry(transcribe_fn, wav_path, cfg, attempts=2):
    """Transcribe one chunk, retrying once.

    A first failure is usually transient (server busy, request timed out). A second one is
    real, and must surface as an error: emitting the other chunks would silently drop words
    the user actually said.
    """
    last = None
    for attempt in range(attempts):
        try:
            return transcribe_fn(wav_path, cfg["server_url"], cfg.get("language", "en"),
                                 cfg.get("prompt", ""))
        except Exception as exc:            # any failure is worth one retry
            last = exc
            log("chunk transcription attempt %d/%d failed for %s: %s"
                % (attempt + 1, attempts, wav_path, exc))
    raise ChunkTranscriptionError(wav_path, last)


class ChunkTranscriber:
    """One consumer thread, chunks transcribed strictly in submission order.

    The whisper-server handles one request at a time anyway, so a single worker keeps the
    ordering trivial and avoids piling up requests it cannot start.
    """

    def __init__(self, cfg, transcribe_fn=None):
        self.cfg = cfg
        self.transcribe_fn = transcribe_fn or pipeline.transcribe
        self.queue = queue.Queue()
        self.results = {}
        self.error = None
        self._thread = threading.Thread(target=self._consume)
        self._thread.daemon = True

    def start(self):
        self._thread.start()

    def submit(self, index, wav_path):
        self.queue.put((index, wav_path))

    def finish(self):
        """Drain the queue and stop the consumer."""
        self.queue.put(None)
        self._thread.join()

    def texts_in_order(self, count):
        return [self.results.get(i, "") for i in range(count)]

    def _consume(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            index, wav_path = item
            if self.error is not None:
                continue                    # already failed; drain the rest without work
            try:
                self.results[index] = transcribe_with_retry(self.transcribe_fn, wav_path, self.cfg)
                _quiet_remove(wav_path)     # keep only audio that still needs recovering
            except ChunkTranscriptionError as exc:
                self.error = exc


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# Session: the shared state machine that live and file mode both drive
# --------------------------------------------------------------------------------------

class StreamSession:
    """Slicing, chunk numbering and handoff bookkeeping.

    Live mode and file mode differ only in where the audio and the boundary timeline come
    from; both call `maybe_handoff` as the timeline advances and `close_tail` at release.
    """

    _sessions_started = 0                   # keeps chunk filenames unique within one process

    def __init__(self, path, transcriber, tracker=None):
        self.pcm_path = path
        self.transcriber = transcriber
        self.tracker = HandoffTracker() if tracker is None else tracker
        self.chunk_count = 0
        self.cuts = []                      # (start_s, end_s) per chunk, for --timings
        StreamSession._sessions_started += 1
        self._token = "%d-%d" % (os.getpid(), StreamSession._sessions_started)

    def maybe_handoff(self, boundaries, recorded_duration):
        """Hand off one chunk if the rule allows it. Returns the cut range, or None."""
        cut = self.tracker.next_cut(boundaries, recorded_duration)
        if cut is None:
            return None
        start, end = self.tracker.take(cut)
        self._enqueue(start, end)
        return (start, end)

    def close_tail(self):
        """Everything not yet handed off becomes the final chunk, read to end-of-file."""
        return self._enqueue(self.tracker.handoff_start, None)

    def _enqueue(self, start, end):
        data = slice_pcm(self.pcm_path, start, end)
        if not data:
            return None
        path = os.path.join(chunk_dir(), "chunk-%s-%02d.wav" % (self._token, self.chunk_count))
        write_wav(path, data)
        self.transcriber.submit(self.chunk_count, path)
        self.chunk_count += 1
        if end is None:
            end = start + len(data) / float(BYTES_PER_SECOND)
        self.cuts.append((start, end))
        return (start, end)


def finalize_text(chunk_texts, replacements):
    """Join the chunks with single spaces, then clean the merged text ONCE.

    Running the dictionary and the loop collapser per chunk would miss a name or a stutter
    that straddles a chunk seam, and would treat each seam as a sentence break it is not.
    """
    merged = " ".join(t.strip() for t in chunk_texts if t.strip())
    return pipeline.collapse_repetitions(pipeline.apply_dictionary(merged, replacements))


def emit(text, copy=False):
    """Persist for the polish/recall hotkeys, optionally copy, and print.

    Returns False if the clipboard copy failed: dictate.lua pastes on exit 0, so exiting 0
    without a fresh clipboard would paste whatever was there before.
    """
    pipeline.write_state(pipeline.LAST_PATH, text)
    pipeline.write_state(pipeline.OUTPUT_PATH, text)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    if copy:
        return pipeline.copy_to_clipboard(text)
    return True


def stop_exit_code(mic_ready, pcm_bytes):
    """0 if there is audio worth transcribing, else EXIT_EMPTY.

    Releasing the key before the microphone ever produced a sample is a normal accident,
    not an error: dictate.lua turns exit code 3 into a silent reset.
    """
    if not mic_ready or pcm_bytes <= 0:
        return EXIT_EMPTY
    return 0


# --------------------------------------------------------------------------------------
# ffmpeg plumbing
# --------------------------------------------------------------------------------------

# Without -flush_packets ffmpeg buffers ~256KB (~8s of audio) before its first write to the
# PCM file. Measured: first bytes appear after 7.71s instead of 0.06s, which both delays the
# MIC_READY cue past the point of usefulness and makes recorded_duration lag real time by up
# to 8s, so handoffs fire late and at the wrong boundary.
PCM_OUT_ARGS = ["-c:a", "pcm_s16le", "-flush_packets", "1", "-f", "s16le"]


def live_ffmpeg_args(mic_index, path=None, ffmpeg_bin=None):
    """One process, five output legs: raw PCM to disk, silencedetect to stderr, and the three
    tagged band meters to stdout for the HUD. File mode deliberately lacks the band legs;
    there is no live HUD to feed when transcribing a file."""
    path = pcm_path() if path is None else path
    return ([ffmpeg_bin or FFMPEG, "-hide_banner", "-y",
             "-f", "avfoundation", "-i", ":%s" % mic_index,
             "-filter_complex", BAND_FILTER,
             "-ar", str(SAMPLE_RATE), "-ac", "1", "-map", "0:a"] + PCM_OUT_ARGS +
            [path, "-map", "0:a", "-af", SILENCE_FILTER, "-f", "null", "-",
             "-map", "[olo]", "-f", "null", "-",
             "-map", "[omi]", "-f", "null", "-",
             "-map", "[ohi]", "-f", "null", "-"])


def file_ffmpeg_args(src, path, ffmpeg_bin=None):
    """Two legs only, reading a file instead of the microphone: raw PCM plus silencedetect.

    No band meters here on purpose; the HUD they feed only exists during a live recording."""
    src_args = ["-i", src]
    if src.endswith(".pcm"):                # headerless: tell ffmpeg what it is looking at
        src_args = ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", src]
    return ([ffmpeg_bin or FFMPEG, "-hide_banner", "-y"] + src_args +
            ["-ar", str(SAMPLE_RATE), "-ac", "1", "-map", "0:a"] + PCM_OUT_ARGS +
            [path, "-map", "0:a", "-af", SILENCE_FILTER, "-f", "null", "-"])


def print_level_line(line):
    """Default sink for meter readings: the worker's own stdout, flushed per line."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class LevelEmitter(threading.Thread):
    """Bounded hand-off between the band pump and stdout, so a slow reader cannot wedge us.

    ffmpeg runs all five output legs in one transcode loop, so if its stdout pipe fills
    because nothing drains it, the PCM capture leg stops writing too and never resumes
    (measured: capture froze at 15.87s and stayed frozen, and SIGINT then needed the SIGKILL
    escalation). The band pump must therefore never block. It offers each reading to this
    bounded queue and moves on; only this thread ever blocks on stdout, and readings that do
    not fit are dropped. A dropped meter frame is invisible; a frozen recording is not.
    """

    def __init__(self, write=print_level_line, maxsize=LEVEL_QUEUE_MAX):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = queue.Queue(maxsize)
        self.dropped = 0
        self._write = write

    def offer(self, line):
        """Queue one line if there is room. Never blocks; returns True if it was queued."""
        try:
            self.queue.put_nowait(line)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def run(self):
        while True:
            line = self.queue.get()
            if line is None:
                return
            self._write(line)

    def stop(self, timeout=2.0):
        """Ask the writer to finish. Returns the number of readings dropped, for one log line.

        The sentinel is offered, not forced: if the queue is still full the writer is stuck on
        a reader that has stopped reading, and blocking here would just move the stall into
        the shutdown path. The thread is a daemon, so leaving it is safe.
        """
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.join(timeout=timeout)
        return self.dropped


def _finite_or_none(value):
    """A real dB number, or None. astats reports digital silence as "-inf", which float()
    accepts happily; forwarding it would put "LEVEL low -inf" on the HUD's number channel."""
    try:
        number = float(value)
    except ValueError:
        return None
    return number if -float("inf") < number < float("inf") else None


class BandLevelParser:
    """Pairs astats RMS lines with their band tag and re-emits them as atomic LEVEL lines.

    ffmpeg's three band legs share one stdout, each reading printing as:
        frame:N ...                                  (header; discards a dangling half-pair)
        lavfi.astats.Overall.RMS_level=<dB>
        band=low|mid|high
    The legs are NOT guaranteed to interleave in any particular order (verified against a
    file, where ffmpeg drained one leg entirely before the next), so pairs are matched as
    they complete and a frame header clears a stale value rather than letting it mispair.
    Each completed pair is re-emitted as one line, "LEVEL <band> <dB>", on the worker's own
    stdout: the same channel dictate.lua already watches for MIC_READY. `emit` is where that
    line goes; live mode hands it a LevelEmitter so the pump never blocks on stdout.
    """

    def __init__(self, emit=print_level_line):
        self._pending = None
        self._emit = emit

    def feed_line(self, line):
        if line.startswith("frame:"):
            self._pending = None
        elif line.startswith("lavfi.astats.Overall.RMS_level="):
            self._pending = _finite_or_none(line.split("=", 1)[1])
        elif line.startswith("band=") and self._pending is not None:
            self._emit("LEVEL %s %.1f" % (line[5:].strip(), self._pending))
            self._pending = None


class StderrPump(threading.Thread):
    """Read ffmpeg stderr and feed complete lines to the parser.

    ffmpeg ends its progress lines with \\r, so readline() would stall; reading the raw fd
    returns whatever is available and we split on both terminators ourselves.
    """

    def __init__(self, stream, parser, tail_lines=40):
        threading.Thread.__init__(self)
        self.daemon = True
        self.stream = stream
        self.parser = parser
        self.tail = []
        self.tail_lines = tail_lines

    def run(self):
        fd = self.stream.fileno()
        buf = ""
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            buf += data.decode("utf-8", "replace")
            buf = buf.replace("\r", "\n")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                self._handle(line)
        if buf:
            self._handle(buf)

    def _handle(self, line):
        if not line.strip():
            return
        self.parser.feed_line(line)
        self.tail.append(line)
        if len(self.tail) > self.tail_lines:
            self.tail.pop(0)

    def tail_text(self):
        return "\n".join(self.tail)


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class CaptureWatchdog:
    """Decides whether a live recording has stopped growing while ffmpeg still looks alive.

    A wedged ffmpeg is invisible to `proc.poll()`: the process is up, the PCM file simply
    never grows again. Without this, the run ends normally and pastes a silently truncated
    transcript. Kept as a plain decision object with no clock and no filesystem of its own so
    a test can feed it a timeline of (size, time) observations.

    The counter starts at the first byte, never at process start: opening the microphone
    legitimately leaves the file at zero for a second or two, and that is not a stall.
    """

    def __init__(self, stall_after=CAPTURE_STALL_S):
        self.stall_after = stall_after
        self.last_size = 0
        self.last_growth_at = None          # None until the first byte is captured
        self.stalled_for = 0.0              # how long the last observation had stood still

    def observe(self, size, now):
        """Record one (size, time) sample. True means the capture is wedged."""
        if size > self.last_size:
            self.last_size = size
            self.last_growth_at = now
            self.stalled_for = 0.0
            return False
        if self.last_growth_at is None:     # still waiting for the microphone to open
            return False
        self.stalled_for = now - self.last_growth_at
        return self.stalled_for >= self.stall_after


# --------------------------------------------------------------------------------------
# Live mode
# --------------------------------------------------------------------------------------

def _report_levels_dropped(dropped):
    """One summary line if meter readings were dropped, and nothing at all if none were.

    Dropping means the HUD stopped keeping up, which is worth knowing about after the fact
    but must never turn into per-reading noise in the log.
    """
    if dropped:
        log("dropped %d meter reading(s): the stdout reader could not keep up" % dropped)


def run_live(mic_index, cfg, copy=False, timings=False, optimize_for=None):
    stop = threading.Event()

    def on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    chunk_dir()                             # make sure the state directory exists before ffmpeg writes
    live_pcm = pcm_path()
    _quiet_remove(live_pcm)
    t_start = time.time()
    proc = subprocess.Popen(live_ffmpeg_args(mic_index, live_pcm, cfg.get("ffmpeg_bin")),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
    parser = SilenceParser()
    pump = StderrPump(proc.stderr, parser)
    pump.start()
    levels = LevelEmitter()                 # only this thread may block on stdout
    levels.start()
    band_pump = StderrPump(proc.stdout, BandLevelParser(levels.offer))  # stdout leg, same pump
    band_pump.start()

    transcriber = ChunkTranscriber(cfg)
    transcriber.start()
    session = StreamSession(live_pcm, transcriber)

    watchdog = CaptureWatchdog()
    mic_ready = False
    t_ready = None
    while not stop.is_set():
        size = _file_size(live_pcm)
        if not mic_ready and size > 0:
            mic_ready = True
            t_ready = time.time()
            sys.stdout.write("MIC_READY\n")
            sys.stdout.flush()
        if proc.poll() is not None:
            _stop_ffmpeg(proc)
            log("ffmpeg exited early (rc=%s) on mic %s: %s"
                % (proc.returncode, mic_index, pump.tail_text()[-400:]))
            sys.stderr.write("ffmpeg exited early (rc=%s) while recording from mic %s:\n%s\n"
                             % (proc.returncode, mic_index, pump.tail_text()))
            return EXIT_FAIL
        if watchdog.observe(size, time.time()):
            # ffmpeg is still up but has stopped writing audio. Finishing normally here would
            # transcribe a truncated recording and paste it as if nothing had gone wrong.
            _stop_ffmpeg(proc)
            _report_levels_dropped(levels.stop())
            log("capture stalled: pcm stuck at %d bytes for %.1fs on mic %s (audio kept at %s): %s"
                % (size, watchdog.stalled_for, mic_index, live_pcm, pump.tail_text()[-400:]))
            sys.stderr.write("recording stalled after %.1fs with no new audio (stuck at %.1fs "
                             "captured on mic %s).\nAudio kept for recovery: %s\n"
                             % (watchdog.stalled_for, size / float(BYTES_PER_SECOND),
                                mic_index, live_pcm))
            return EXIT_FAIL
        if mic_ready:
            session.maybe_handoff(parser.midpoints(), size / float(BYTES_PER_SECOND))
        time.sleep(POLL_S)

    t_release = time.time()
    _stop_ffmpeg(proc)                      # SIGINT, then wait: the PCM file is flushed on exit
    pump.join(timeout=2.0)
    band_pump.join(timeout=2.0)
    _report_levels_dropped(levels.stop())
    # Recording is over but transcription can still take seconds, and a fresh key press
    # starts a worker that deletes the live PCM file and records over it. Restoring the
    # default signal handlers keeps that pkill -INT able to kill us instead of being swallowed.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    code = stop_exit_code(mic_ready, _file_size(live_pcm))
    if code != 0:
        log("nothing captured (mic_ready=%s, bytes=%d); exiting %d"
            % (mic_ready, _file_size(live_pcm), code))
        return code

    # Claim the recording under our own name before any new worker can clear the live path.
    own_pcm = os.path.join(chunk_dir(), "stream-%d.pcm" % os.getpid())
    try:
        os.rename(live_pcm, own_pcm)
        session.pcm_path = own_pcm
    except OSError:
        own_pcm = None                      # rename failed; the live path is still ours to read

    total = _file_size(session.pcm_path) / float(BYTES_PER_SECOND)
    session.close_tail()
    code = _finish(session, transcriber, cfg, copy, timings, t_release,
                   extra={"mic_ready": (t_ready - t_start) if t_ready else 0.0,
                          "record": total},
                   optimize_for=optimize_for)
    if own_pcm:
        _quiet_remove(own_pcm)
    return code


def _stop_ffmpeg(proc, grace=5.0):
    """SIGINT lets ffmpeg close its outputs cleanly; escalate only if it hangs."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        return
    deadline = time.time() + grace
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        proc.kill()
        proc.wait()


# --------------------------------------------------------------------------------------
# File mode
# --------------------------------------------------------------------------------------

def run_file(src, cfg, copy=False, timings=False, optimize_for=None):
    """Replay the live handoff logic over a recorded file, as if released at EOF."""
    if not os.path.exists(src):
        sys.stderr.write("input file not found: %s\n" % src)
        return EXIT_FAIL

    path = os.path.join(chunk_dir(), "stream-file-%d.pcm" % os.getpid())
    t_start = time.time()
    proc = subprocess.run(file_ffmpeg_args(src, path, cfg.get("ffmpeg_bin")),
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        log("ffmpeg failed to decode %s (rc=%d): %s" % (src, proc.returncode, stderr[-400:]))
        sys.stderr.write("ffmpeg failed to decode %s (rc=%d):\n%s\n"
                         % (src, proc.returncode, stderr[-2000:]))
        return EXIT_FAIL

    parser = SilenceParser()
    for line in stderr.replace("\r", "\n").split("\n"):
        parser.feed_line(line)

    total = _file_size(path) / float(BYTES_PER_SECOND)
    if total <= 0:
        log("no audio decoded from %s; exiting %d" % (src, EXIT_EMPTY))
        sys.stderr.write("no audio decoded from %s\n" % src)
        return EXIT_EMPTY

    transcriber = ChunkTranscriber(cfg)
    transcriber.start()
    session = StreamSession(path, transcriber)

    # Walk the timeline in the same POLL_S steps the live loop uses, revealing each boundary
    # only once a live run would have seen it, so file mode cuts exactly where live would.
    t = 0.0
    while t < total:
        t = min(t + POLL_S, total)
        session.maybe_handoff(parser.midpoints_known_by(t), t)

    t_release = time.time()
    session.close_tail()
    code = _finish(session, transcriber, cfg, copy, timings, t_release,
                   extra={"decode": t_release - t_start, "record": total},
                   optimize_for=optimize_for)
    _quiet_remove(path)
    return code


# --------------------------------------------------------------------------------------
# Shared finish
# --------------------------------------------------------------------------------------

def _finish(session, transcriber, cfg, copy, timings, t_release, extra, optimize_for=None):
    transcriber.finish()
    if transcriber.error is not None:
        log("FAIL %s (audio kept at %s)" % (transcriber.error, transcriber.error.wav_path))
        sys.stderr.write("%s\nAudio kept for recovery: %s\n"
                         % (transcriber.error, transcriber.error.wav_path))
        return EXIT_FAIL

    text = finalize_text(transcriber.texts_in_order(session.chunk_count),
                         pipeline.load_replacements())
    if pipeline.is_effectively_empty(text):
        # Nothing was said (or whisper returned only [BLANK_AUDIO]); dictate.lua resets
        # quietly. Exiting 0 here would paste the previous clipboard contents.
        log("EMPTY: nothing transcribed from %d chunk(s)" % session.chunk_count)
        return EXIT_EMPTY

    code = 0
    if optimize_for:
        optimized = pipeline.optimize_prompt(text, cfg, optimize_for)
        if optimized:
            text = optimized
        else:
            # Emit the raw cleaned transcription anyway: a failed rewrite must never cost the
            # user their words. The exit code is the only signal that it was not rewritten.
            code = EXIT_OPTIMIZER_FALLBACK

    copied = emit(text, copy=copy)
    if timings:
        parts = ["%s=%.2fs" % (k, v) for k, v in sorted(extra.items())]
        parts.append("chunks=%d" % session.chunk_count)
        parts.append("cuts=[%s]" % ", ".join("%.1f-%.1f" % c for c in session.cuts))
        parts.append("release_wait=%.2fs" % (time.time() - t_release))
        sys.stderr.write("timings: " + ", ".join(parts) + "\n")
    if not copied:
        return EXIT_FAIL                    # clipboard is stale; the caller must not paste
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scribe streaming dictation worker")
    ap.add_argument("--mic", help="avfoundation audio device index to record from")
    ap.add_argument("--from-file", dest="from_file",
                    help="transcribe a .wav/.pcm file through the same logic (no microphone)")
    ap.add_argument("--language", help="transcription language code; overrides config.json")
    ap.add_argument("--copy", action="store_true", help="also copy the result to the clipboard")
    ap.add_argument("--timings", action="store_true", help="print per-stage timings to stderr")
    ap.add_argument("--optimize-for", choices=list(pipeline.OPTIMIZE_TARGETS),
                    dest="optimize_for",
                    help="rewrite the dictation into a prompt aimed at this model instead of "
                         "emitting the transcript; exits %d if the rewrite is unavailable"
                         % EXIT_OPTIMIZER_FALLBACK)
    args = ap.parse_args(argv)
    if bool(args.mic) == bool(args.from_file):
        ap.error("give exactly one of --mic or --from-file")

    started = time.time()
    mode = "file" if args.from_file else "live"
    try:
        cfg = pipeline.load_config()
    except RuntimeError as exc:
        log("FAIL mode=%s: %s" % (mode, exc))
        sys.stderr.write("scribe: %s\n" % exc)
        return EXIT_FAIL
    if args.language:
        # Same rule as config.json: the value reaches a curl form field, so it may not carry
        # curl's @ / < sigils.
        if not pipeline.is_valid_language(args.language):
            ap.error('--language must be a 2-3 letter code such as "en" or "da", or "auto"')
        cfg["language"] = args.language
    log("start mode=%s source=%s%s"
        % (mode, args.from_file or args.mic,
           (" optimize-for=%s" % args.optimize_for) if args.optimize_for else ""))
    if args.from_file:
        code = run_file(args.from_file, cfg, copy=args.copy, timings=args.timings,
                        optimize_for=args.optimize_for)
    else:
        code = run_live(args.mic, cfg, copy=args.copy, timings=args.timings,
                        optimize_for=args.optimize_for)
    log("end mode=%s exit=%d %.2fs" % (mode, code, time.time() - started))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
