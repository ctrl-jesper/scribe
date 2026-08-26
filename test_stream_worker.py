#!/usr/bin/env python3
"""Tests for stream_worker.py: WAV framing, silence parsing, the handoff rule, slicing,
the merge, and the two failure paths. Audio is synthesised here; no microphone needed.

The whisper-server is stubbed everywhere except the final integration test, which is
skipped cleanly when the configured port answers nothing.

All fixture strings are synthetic and self-contained; none depend on a real user's
dictionary.json (the shipped one ships empty).

Run: python3 test_stream_worker.py
"""
import importlib.util, io, json, math, os, struct, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------------------
# Isolated SCRIBE_HOME, created and populated BEFORE either module is imported. Both
# pipeline.py (loaded directly below) and the pipeline.py that stream_worker.py imports for
# itself resolve their paths from this same SCRIBE_HOME at import time, so state files,
# dictionary, and config all stay inside this throwaway directory. Never touches a real
# user's ~/.config/scribe.
# --------------------------------------------------------------------------------------
SCRIBE_HOME = tempfile.mkdtemp(prefix="scribe-test-home-")
os.environ["SCRIBE_HOME"] = SCRIBE_HOME


def _port_answers(port):
    """True if something on 127.0.0.1:<port> answers HTTP 200 to a bare GET."""
    try:
        out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                              "--max-time", "3", "http://127.0.0.1:%d/" % port],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "200"
    except Exception:
        return False


# Default test port is 8090. If a warm whisper-server already answers on 8080 (this
# machine's own dev server), point the temp config at it instead so the integration leg
# below actually exercises a live transcription rather than skipping. This only affects
# this test's own throwaway config, never a shipped default.
TEST_PORT = 8080 if _port_answers(8080) else 8090

CONFIG = {
    "language": "en",
    "mic_name": "Test Microphone",
    "hotkey_keycode": 61,
    "hotkey_flag": "alt",
    "server_port": TEST_PORT,
    "model_file": "ggml-test-model.bin",
    "vocabulary": ["Acme", "Northlight"],
    "speaker_note": "",
    "mode": "dict",
    "polish_enabled": False,
    "claude_bin": os.path.join(SCRIBE_HOME, "no-such-claude"),
    "claude_model": "claude-haiku-4-5-20251001",
}
with open(os.path.join(SCRIBE_HOME, "config.json"), "w") as f:
    json.dump(CONFIG, f)
with open(os.path.join(SCRIBE_HOME, "dictionary.json"), "w") as f:
    json.dump({"replacements": {}}, f)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sw = _load("stream_worker")
p = _load("pipeline")
# Inline replacement pairs, defined here rather than read from any shipped dictionary.json
# (which ships empty). Same pairs test_pipeline.py uses, kept local so this file stays
# self-contained.
REPS = {
    "Akme": "Acme",
    "Nortlite": "Northlight",
}

# Scratch directory for synthetic audio fixtures this file builds directly (distinct from
# the SCRIBE_HOME state dir that StreamSession itself writes chunk WAVs into).
FIXTURES = tempfile.mkdtemp(prefix="scribe-test-fixtures-")

cases, skips = [], []
def check(name, got, want):
    cases.append((name, got == want, got, want))

def check_raises(name, exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        cases.append((name, True, exc, exc_type))
    except Exception as exc:                                  # wrong exception is a failure
        cases.append((name, False, repr(exc), exc_type))
    else:
        cases.append((name, False, "no exception", exc_type))


# --------------------------------------------------------------------------------------
# Synthetic audio
# --------------------------------------------------------------------------------------

def tone_second(freq):
    """One second of 16kHz mono s16le at `freq`, loud enough to count as speech."""
    n = sw.SAMPLE_RATE
    return struct.pack("<%dh" % n,
                       *[int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / n)) for i in range(n)])

def silence_second():
    return b"\x00\x00" * sw.SAMPLE_RATE

def build_pcm(path, seconds, silent_at=()):
    """`seconds` seconds of audio, each second a different tone so slices are distinguishable."""
    blocks = [silence_second() if s in silent_at else tone_second(180 + 7 * s)
              for s in range(seconds)]
    data = b"".join(blocks)
    with open(path, "wb") as f:
        f.write(data)
    return data


class FakeTranscriber:
    """Stands in for ChunkTranscriber: records what was submitted, never hits the server."""

    def __init__(self, texts=None):
        self.submitted = []
        self.results = {}
        self.error = None
        self._texts = texts or {}

    def submit(self, index, wav_path):
        self.submitted.append((index, wav_path))
        self.results[index] = self._texts.get(index, "")


def replay(pcm_path, boundaries, total, transcriber):
    """Drive a StreamSession over a timeline the way run_file does: step in POLL_S ticks and
    reveal each boundary only once a live run would have parsed it."""
    session = sw.StreamSession(pcm_path, transcriber)
    t = 0.0
    while t < total:
        t = min(t + sw.POLL_S, total)
        session.maybe_handoff([mid for mid, known_at in boundaries if known_at <= t], t)
    session.close_tail()
    return session


def wav_payload(path):
    """The PCM bytes of a WAV written by write_wav (fixed 44-byte header)."""
    with open(path, "rb") as f:
        return f.read()[44:]


# --- 1. WAV header wrap ---------------------------------------------------------------
hdr = sw.wav_header(32000)
check("wav header is 44 bytes", len(hdr), 44)
check("wav header RIFF/WAVE", (hdr[0:4], hdr[8:12]), (b"RIFF", b"WAVE"))
check("wav header riff size = 36 + data", struct.unpack("<I", hdr[4:8])[0], 36 + 32000)
check("wav header fmt chunk", (hdr[12:16], struct.unpack("<IHH", hdr[16:24])), (b"fmt ", (16, 1, 1)))
check("wav header 16kHz mono s16",
      struct.unpack("<IIHH", hdr[24:36]), (16000, 32000, 2, 16))
check("wav header data size", (hdr[36:40], struct.unpack("<I", hdr[40:44])[0]), (b"data", 32000))

_wav = sw.write_wav(os.path.join(FIXTURES, "hdr.wav"), b"\x01\x02" * 100)
check("write_wav length = 44 + payload", os.path.getsize(_wav), 44 + 200)


# --- 1b. ffmpeg invocation ------------------------------------------------------------
_live = sw.live_ffmpeg_args("3", "/tmp/x.pcm")
check("live ffmpeg records the named avfoundation device",
      _live[_live.index("-i") + 1], ":3")
check("live ffmpeg has all five output legs (pcm, silencedetect, three band meters)",
      (_live.count("-map"), _live.count("-f")),
      (5, 6))   # -f avfoundation (input) + -f s16le (pcm leg) + four -f null legs
check("live ffmpeg's silencedetect leg is intact",
      _live[_live.index("-af"):_live.index("-af") + 5],
      ["-af", sw.SILENCE_FILTER, "-f", "null", "-"])
check("live ffmpeg maps the three tagged band legs",
      [a for a in _live if a.startswith("[o")], ["[olo]", "[omi]", "[ohi]"])
check("live ffmpeg's band graph is the file-verified one",
      _live[_live.index("-filter_complex") + 1], sw.BAND_FILTER)
check("file mode gets no band legs (no live HUD to feed)",
      ("-filter_complex" in sw.file_ffmpeg_args("/tmp/a.wav", "/tmp/o.pcm"),
       sw.file_ffmpeg_args("/tmp/a.wav", "/tmp/o.pcm").count("-map")), (False, 2))
# Without this ffmpeg buffers ~8s before its first write, which would delay MIC_READY.
check("live ffmpeg flushes packets so the pcm file grows immediately",
      "-flush_packets" in _live, True)

_live_default_path = sw.live_ffmpeg_args("3")
# The pcm output path is the argument right after the PCM_OUT_ARGS block (…-f s16le PATH).
check("live ffmpeg defaults to the Scribe state dir's stream path",
      _live_default_path[_live_default_path.index("-flush_packets") + 4], sw.pcm_path())

def input_spec(args):
    """Everything ffmpeg is told about the input, up to and including `-i PATH`."""
    return args[:args.index("-i") + 2]

check("file mode declares the format for headerless .pcm input",
      input_spec(sw.file_ffmpeg_args("/tmp/a.pcm", "/tmp/o.pcm"))[-8:],
      ["-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "/tmp/a.pcm"])
check("file mode lets ffmpeg detect the format of a .wav input",
      input_spec(sw.file_ffmpeg_args("/tmp/a.wav", "/tmp/o.pcm"))[-2:], ["-i", "/tmp/a.wav"])
check("wav input carries no raw-format override",
      "s16le" in input_spec(sw.file_ffmpeg_args("/tmp/a.wav", "/tmp/o.pcm")), False)


# --- 2. silencedetect parsing + midpoints ---------------------------------------------
SAMPLE_STDERR = """frame=  100 fps=0.0 q=-0.0 size=     512KiB time=00:00:16.00 bitrate= 256.0kbits/s
[Parsed_silencedetect_0 @ 0x83504cf00] silence_start: 10
[Parsed_silencedetect_0 @ 0x83504cf00] silence_end: 11.000063 | silence_duration: 1.000063
[Parsed_silencedetect_0 @ 0x83504cf00] silence_start: 24
[Parsed_silencedetect_0 @ 0x83504cf00] silence_end: 25.5 | silence_duration: 1.5
[Parsed_silencedetect_0 @ 0x83504cf00] silence_start: 40
size=    1562KiB time=00:00:50.00 bitrate= 256.0kbits/s speed=4.77e+03x"""

_parser = sw.SilenceParser()
for _line in SAMPLE_STDERR.split("\n"):
    _parser.feed_line(_line)
check("parse midpoints of completed silences",
      [round(m, 4) for m in _parser.midpoints()], [10.5, 24.75])
check("parse records when each boundary became known",
      [round(k, 4) for _m, k in _parser.boundaries], [11.0001, 25.5])
check("open silence_start yields no boundary yet", len(_parser.boundaries), 2)
check("boundaries known by t=20 exclude the later one",
      [round(m, 4) for m in _parser.midpoints_known_by(20.0)], [10.5])

_p2 = sw.SilenceParser()
_p2.feed_line("[Parsed_silencedetect_0 @ 0x1] silence_end: 3.0 | silence_duration: 1.0")
check("silence_end without a start is ignored", _p2.midpoints(), [])


# --- 2b. band meter parsing -------------------------------------------------------------
# ffmpeg's three band legs share one stdout and print each reading as a frame header, an
# astats RMS line, and the band tag added by ametadata. The legs are not guaranteed to
# interleave, so the parser pairs values as they complete and a frame header must throw a
# dangling value away rather than let it pair with the next leg's band tag.

def band_lines(lines):
    """Feed lines to a fresh BandLevelParser and return what it wrote to stdout."""
    parser = sw.BandLevelParser()
    saved, sys.stdout = sys.stdout, io.StringIO()
    try:
        for line in lines:
            parser.feed_line(line)
        return sys.stdout.getvalue().splitlines()
    finally:
        sys.stdout = saved

check("a complete reading becomes one LEVEL line",
      band_lines(["frame:0    pts:0       pts_time:0",
                  "lavfi.astats.Overall.RMS_level=-32.123456",
                  "band=low"]),
      ["LEVEL low -32.1"])
check("all three bands come through in order",
      band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=-30.0", "band=low",
                  "frame:0", "lavfi.astats.Overall.RMS_level=-28.5", "band=mid",
                  "frame:0", "lavfi.astats.Overall.RMS_level=-44.25", "band=high"]),
      ["LEVEL low -30.0", "LEVEL mid -28.5", "LEVEL high -44.2"])
check("a frame header discards a dangling value instead of mispairing it",
      band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=-30.0",
                  "frame:1", "band=high"]),
      [])
check("parsing recovers on the next complete reading after a broken pair",
      band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=-30.0",
                  "frame:1", "band=high",
                  "frame:2", "lavfi.astats.Overall.RMS_level=-41.5", "band=mid"]),
      ["LEVEL mid -41.5"])
check("a band tag with no value pending emits nothing",
      band_lines(["band=low"]), [])
check("a malformed dB value is dropped, not emitted",
      band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=n/a", "band=low"]), [])
# astats reports digital silence as "-inf", which float() accepts: without the finite check
# the HUD would be handed "LEVEL low -inf" on a channel it parses as a number.
check("a -inf reading (digital silence) is dropped, not emitted",
      band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=-inf", "band=low"]), [])
check("a nan reading is dropped too",
      band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=nan", "band=mid"]), [])
check("every emitted dB value is a plain number",
      [float(l.split()[2]) < 0 for l in
       band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=-inf", "band=low",
                   "frame:1", "lavfi.astats.Overall.RMS_level=-33.0", "band=low"])],
      [True])
check("a malformed value does not poison the next reading",
      band_lines(["frame:0", "lavfi.astats.Overall.RMS_level=n/a", "band=low",
                  "frame:1", "lavfi.astats.Overall.RMS_level=-20.0", "band=high"]),
      ["LEVEL high -20.0"])


# --- 2c. the bounded level queue ----------------------------------------------------------
# ffmpeg drives all five output legs from one loop, so if its stdout pipe fills the PCM
# capture leg stops writing and never resumes. The band pump therefore hands readings to a
# bounded queue and never blocks; readings that do not fit are dropped on purpose.
_sink = []
_em = sw.LevelEmitter(write=_sink.append, maxsize=3)      # not started: nothing drains it
check("readings are accepted while there is room",
      [_em.offer("LEVEL low -30.0") for _ in range(3)], [True, True, True])
check("a full queue drops the reading instead of blocking", _em.offer("LEVEL mid -30.0"), False)
check("drops are counted", (_em.dropped, _em.queue.qsize()), (1, 3))
check("dropping does not stop later readings once room appears",
      (_em.queue.get(), _em.offer("LEVEL high -20.0")), ("LEVEL low -30.0", True))

_drained = []
_em2 = sw.LevelEmitter(write=_drained.append, maxsize=8)
_em2.start()
for _i in range(5):
    _em2.offer("LEVEL low -%d.0" % (30 + _i))
check("a running emitter writes every queued reading, in order and none dropped",
      (_em2.stop(), _drained),
      (0, ["LEVEL low -30.0", "LEVEL low -31.0", "LEVEL low -32.0",
           "LEVEL low -33.0", "LEVEL low -34.0"]))

_parsed = []
_bp = sw.BandLevelParser(_parsed.append)
for _line in ["frame:0", "lavfi.astats.Overall.RMS_level=-27.5", "band=mid"]:
    _bp.feed_line(_line)
check("the parser emits through the sink it was given, not straight to stdout",
      _parsed, ["LEVEL mid -27.5"])


# --- 2d. capture-progress watchdog --------------------------------------------------------
# A wedged ffmpeg still reports itself alive; only the PCM file stops growing. The threshold
# is 3.0s, ~30x the worst write gap measured on a real-time capture (median 0.068s, max
# 0.092s over 625 writes), so it cannot fire on a hiccup.

def watchdog_trips_at(samples, stall_after=sw.CAPTURE_STALL_S):
    """Feed (size, time) observations; return the time it tripped, or None."""
    wd = sw.CaptureWatchdog(stall_after=stall_after)
    for size, when in samples:
        if wd.observe(size, when):
            return when
    return None

# the microphone takes a moment to open: the file legitimately sits at 0 bytes first
COLD_START = [(0, t / 4.0) for t in range(0, 40)]         # 10s at zero bytes
check("a slow microphone cold start never trips the watchdog",
      watchdog_trips_at(COLD_START), None)
check("cold start then normal growth does not trip",
      watchdog_trips_at(COLD_START + [(32000 * i, 10.0 + i) for i in range(1, 20)]), None)

STEADY = [(32000 * i, float(i)) for i in range(1, 10)]    # 1s of audio per second
check("steady capture never trips", watchdog_trips_at(STEADY), None)
check("a brief hiccup shorter than the threshold does not trip",
      watchdog_trips_at(STEADY + [(32000 * 9, 9.0 + t / 4.0) for t in range(1, 12)]), None)
check("a wedged capture trips once the threshold is passed",
      watchdog_trips_at(STEADY + [(32000 * 9, 9.0 + t / 4.0) for t in range(1, 20)]), 12.0)
check("growth after a hiccup resets the clock",
      watchdog_trips_at(STEADY + [(32000 * 9, 11.9), (32000 * 10, 12.0), (32000 * 10, 14.5)]),
      None)
_wd = sw.CaptureWatchdog()
_wd.observe(32000, 1.0)
_wd.observe(32000, 5.0)
check("the watchdog reports how long the capture stood still", _wd.stalled_for, 4.0)
check("a shrinking file is treated as no growth (still stalled)",
      watchdog_trips_at([(32000, 1.0), (16000, 2.0), (16000, 5.0)]), 5.0)


# --- 3. handoff rule on a synthetic 50s timeline ---------------------------------------
PCM50 = os.path.join(FIXTURES, "t50.pcm")
RAW50 = build_pcm(PCM50, 50, silent_at=(10, 24, 40))
TIMELINE = [(10.5, 11.0), (24.5, 25.0), (40.5, 41.0)]        # (midpoint, known_at)

ft = FakeTranscriber()
sess = replay(PCM50, TIMELINE, 50.0, ft)
check("handoff cuts only at silences, latest qualifying one",
      [(round(a, 2), round(b, 2)) for a, b in sess.cuts],
      [(0.0, 10.5), (10.5, 24.5), (24.5, 40.5), (40.5, 50.0)])
check("handoff produced 4 chunks (3 handoffs + tail)", sess.chunk_count, 4)

# every handed-off chunk respects the 5s floor, and each accumulated >= 22s before cutting
check("no chunk shorter than the 5s floor",
      [round(b - a, 2) for a, b in sess.cuts[:-1] if b - a < sw.MIN_CHUNK_S], [])
check("no handoff before 22s had accumulated",
      all(sess.cuts[i][1] - sess.cuts[i][0] >= 0 and
          sess.cuts[i][1] >= sess.cuts[i][0] + sw.MIN_CHUNK_S for i in range(3)), True)

# a boundary closer than 5s to handoff_start must not be used even though 22s accumulated
_near = sw.HandoffTracker()
check("boundary inside the 5s floor is rejected", _near.next_cut([4.0], 30.0), None)
check("boundary beyond recorded audio is rejected", _near.next_cut([28.0], 25.0), None)
check("latest qualifying boundary wins", _near.next_cut([6.0, 12.0, 19.0], 25.0), 19.0)
check("nothing handed off before 22s accumulate", _near.next_cut([12.0], 21.9), None)

# no silence anywhere -> zero handoffs, degrade to a single batch chunk
PCM_NOSIL = os.path.join(FIXTURES, "nosil.pcm")
RAW_NOSIL = build_pcm(PCM_NOSIL, 50)
ft2 = FakeTranscriber()
sess2 = replay(PCM_NOSIL, [], 50.0, ft2)
check("no silence -> no handoff, single tail chunk", sess2.chunk_count, 1)
check("no silence -> tail spans the whole recording",
      [(round(a, 2), round(b, 2)) for a, b in sess2.cuts], [(0.0, 50.0)])


# --- 4. slicing: sample-aligned and lossless -------------------------------------------
_payloads = [wav_payload(path) for _i, path in ft.submitted]
check("every slice is sample-aligned (even byte count)",
      [len(x) % sw.BYTES_PER_SAMPLE for x in _payloads], [0, 0, 0, 0])
check("slices reconcatenate to the original PCM exactly", b"".join(_payloads), RAW50)
check("first slice is exactly 10.5s of audio", len(_payloads[0]), int(10.5 * sw.BYTES_PER_SECOND))
check("empty slice is not enqueued", sw.slice_pcm(PCM50, 12.0, 12.0), b"")

_tail_only = FakeTranscriber()
_s3 = sw.StreamSession(PCM50, _tail_only)
_s3.tracker.handoff_start = 50.0
check("tail at end-of-audio enqueues nothing", _s3.close_tail(), None)
check("tail at end-of-audio adds no chunk", _s3.chunk_count, 0)

# The tail reads to EOF rather than converting a duration back to a byte offset: at some
# file sizes that round-trip truncates the final sample.
_odd = os.path.join(FIXTURES, "odd.pcm")
with open(_odd, "wb") as _f:
    _f.write(b"\x01\x02" * 1001)                              # 2002 bytes; 2002/32000 s
check("tail reads to EOF, losing no sample to float rounding",
      len(sw.slice_pcm(_odd, 0.0)), 2002)
check("duration round-trip would have truncated it",
      len(sw.slice_pcm(_odd, 0.0, 2002 / float(sw.BYTES_PER_SECOND))), 2000)

# chunk WAVs from a live/file session land inside the Scribe state dir, not beside the repo.
check("enqueued chunks are written under the Scribe state dir",
      all(os.path.dirname(path) == sw.chunk_dir() for _i, path in ft.submitted), True)


# --- 5. merge: single-space join, cleanup applied ONCE on the merged text ---------------
check("merge joins chunks with single spaces",
      sw.finalize_text(["  first part  ", "second part"], {}),
      "first part second part")
check("merge drops empty chunks",
      sw.finalize_text(["only this", "", "   "], {}),
      "only this")
check("dictionary applies across a chunk seam",
      sw.finalize_text(["the Akme and", "Nortlite deal"], REPS),
      "the Acme and Northlight deal")
check("collapser runs on the merged text, not per chunk",
      sw.finalize_text(["That is a partner.", "That is a partner."], REPS),
      "That is a partner.")
check("merge leaves clean prose untouched",
      sw.finalize_text(["run multiple agendas", "at the customer this quarter"], REPS),
      "run multiple agendas at the customer this quarter")


# emit() persists for the polish/recall hotkeys and reports whether the clipboard is fresh.
# sw imports its own copy of pipeline (via stream_worker.py's top-level `import pipeline`),
# separate from `p` loaded above, but both resolved SCRIBE_HOME identically at import time
# so their state paths already agree; no manual path sync needed.
check("stream_worker's pipeline and the directly-loaded pipeline agree on state paths",
      sw.pipeline.OUTPUT_PATH, p.OUTPUT_PATH)
_stdout, sys.stdout = sys.stdout, io.StringIO()               # emit prints; keep it out of the report
_emit_ok = sw.emit("hello there")
_emit_printed, sys.stdout = sys.stdout.getvalue(), _stdout
check("emit reports success when no clipboard copy was asked for", _emit_ok, True)
check("emit prints the text for dictate.lua", _emit_printed, "hello there\n")
check("emit persists the text for the recall hotkey", open(p.OUTPUT_PATH).read(), "hello there")


# --- 6. failure paths -------------------------------------------------------------------
check("SIGINT before MIC_READY exits 3", sw.stop_exit_code(False, 0), sw.EXIT_EMPTY)
check("ready but zero bytes captured exits 3", sw.stop_exit_code(True, 0), sw.EXIT_EMPTY)
check("ready with audio exits 0", sw.stop_exit_code(True, 32000), 0)

CFG = {"server_url": "http://127.0.0.1:%d/inference" % TEST_PORT, "language": "en", "prompt": "GLOSSARY"}

_calls = []
def _flaky(wav_path, url, language, prompt):
    _calls.append(wav_path)
    if len(_calls) == 1:
        raise RuntimeError("server busy")
    return "recovered text"

check("transcribe retries once and succeeds",
      sw.transcribe_with_retry(_flaky, "/tmp/a.wav", CFG), "recovered text")
check("retry made exactly two attempts", len(_calls), 2)

def _always_fails(wav_path, url, language, prompt):
    raise RuntimeError("server down")

check_raises("two failures raise ChunkTranscriptionError",
             sw.ChunkTranscriptionError, sw.transcribe_with_retry,
             _always_fails, "/tmp/chunk-3.wav", CFG)
try:
    sw.transcribe_with_retry(_always_fails, "/tmp/chunk-3.wav", CFG)
except sw.ChunkTranscriptionError as exc:
    check("failure carries the audio path for recovery", exc.wav_path, "/tmp/chunk-3.wav")

def _prompt_spy(wav_path, url, language, prompt):
    return prompt

check("full glossary prompt is passed per chunk",
      sw.transcribe_with_retry(_prompt_spy, "/tmp/a.wav", CFG), "GLOSSARY")

# the real consumer thread must record the error rather than emit partial text
_tr = sw.ChunkTranscriber(CFG, transcribe_fn=_always_fails)
_tr.start()
_tr.submit(0, os.path.join(FIXTURES, "hdr.wav"))
_tr.finish()
check("ChunkTranscriber records the failure", isinstance(_tr.error, sw.ChunkTranscriptionError), True)
check("failed chunk produces no text", _tr.texts_in_order(1), [""])

_ok = sw.ChunkTranscriber(CFG, transcribe_fn=lambda w, u, l, p: "chunk " + os.path.basename(w))
_ok.start()
_ok.submit(0, os.path.join(FIXTURES, "one.wav"))
_ok.submit(1, os.path.join(FIXTURES, "two.wav"))
_ok.finish()
check("chunks come back in submission order", _ok.texts_in_order(2), ["chunk one.wav", "chunk two.wav"])


# --- 6b. clipboard, empty transcripts, logging, and the ffmpeg binary ---------------------
# A failed pbcopy must be reported, not swallowed: dictate.lua pastes on exit 0, so a stale
# clipboard would paste the PREVIOUS dictation. subprocess.run is stubbed rather than calling
# the real pbcopy, so running these tests never touches anyone's clipboard.
_copied = []
_saved_copy = sw.pipeline.copy_to_clipboard
sw.pipeline.copy_to_clipboard = lambda text: (_copied.append(text), False)[1]
_stdout, sys.stdout = sys.stdout, io.StringIO()
_copy_failed = sw.emit("words", copy=True)
sys.stdout = _stdout
sw.pipeline.copy_to_clipboard = _saved_copy
check("emit hands the text to pipeline's clipboard helper", _copied, ["words"])
check("emit reports a failed clipboard copy", _copy_failed, False)

# whisper returns "[BLANK_AUDIO]" for silence, not "": the streaming worker must treat that
# as nothing-was-said (exit 3) rather than pasting the marker.
check("a merged transcript of only non-speech markers counts as empty",
      p.is_effectively_empty(sw.finalize_text(["[BLANK_AUDIO]", "[BLANK_AUDIO]"], {})), True)
check("a merged transcript with words does not",
      p.is_effectively_empty(sw.finalize_text(["[BLANK_AUDIO]", "but I spoke"], {})), False)

check("stream_worker logs through pipeline's helper rather than its own",
      sw.log is sw.pipeline.log, True)

check("ffmpeg binary comes from config when given",
      sw.live_ffmpeg_args("3", "/tmp/x.pcm", "/usr/local/bin/ffmpeg")[0], "/usr/local/bin/ffmpeg")
check("file mode takes the configured ffmpeg too",
      sw.file_ffmpeg_args("/tmp/a.wav", "/tmp/o.pcm", "/usr/local/bin/ffmpeg")[0],
      "/usr/local/bin/ffmpeg")
check("the fallback ffmpeg path is absolute", os.path.isabs(sw.FFMPEG), True)

# --language reaches a curl form field, so it gets the same rule as config.json.
_stderr, sys.stderr = sys.stderr, io.StringIO()
check_raises("a --language carrying curl's @ sigil is refused", SystemExit,
             sw.main, ["--from-file", os.path.join(FIXTURES, "hdr.wav"), "--language", "@/tmp/x"])
sys.stderr = _stderr


# --- 7. integration: file mode must match the batch path exactly -------------------------
# Version-independent path: the opt/ symlink whisper-cpp maintains, unlike the Cellar path,
# does not change on a Homebrew version bump.
JFK = "/opt/homebrew/opt/whisper-cpp/share/whisper-cpp/jfk.wav"

if not os.path.exists(JFK):
    skips.append("integration: sample file missing (%s)" % JFK)
elif not _port_answers(TEST_PORT):
    skips.append("integration: whisper-server not answering on 127.0.0.1:%d" % TEST_PORT)
else:
    cfg = p.load_config()
    # max_age=None: jfk.wav is a sample shipped with whisper-cpp, so it is legitimately old.
    # The staleness guard exists for the recorder's own state/dictation.wav, not for a file
    # named explicitly.
    batch = p.run(JFK, "dict", cfg, max_age=None)
    stream = subprocess.run([sys.executable, os.path.join(HERE, "stream_worker.py"),
                             "--from-file", JFK], capture_output=True, text=True, timeout=180,
                            env=dict(os.environ, SCRIBE_HOME=SCRIBE_HOME))
    check("file mode exits 0 on jfk.wav", stream.returncode, 0)
    check("file mode text is identical to the batch path", stream.stdout.strip(), batch.strip())


fails = [c for c in cases if not c[1]]
for name, ok, got, want in cases:
    print("  %s  %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("        got:  %r\n        want: %r" % (got, want))
for reason in skips:
    print("  SKIP  %s" % reason)
print("\n%d/%d passed%s" % (len(cases) - len(fails), len(cases),
                            (", %d skipped" % len(skips)) if skips else ""))
raise SystemExit(1 if fails else 0)
