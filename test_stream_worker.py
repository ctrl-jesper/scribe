#!/usr/bin/env python3
"""Tests for stream_worker.py: WAV framing, silence parsing, the handoff rule, slicing,
the merge, and the two failure paths. Audio is synthesised here; no microphone needed.

The whisper-server is stubbed everywhere except the final integration test, which is
skipped cleanly when the configured port answers nothing.

All fixture strings are synthetic and self-contained; none depend on a real user's
dictionary.json (the shipped one ships empty).

Run: python3 test_stream_worker.py
"""
import importlib.util, io, json, math, os, stat, struct, subprocess, sys, tempfile, time

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

# --- 5b. spoken punctuation: finalize_text is the streaming half of the shared chain ------
# This is the call the maintainer's own recording path (streaming) actually makes; a feature
# added only to pipeline.run()'s batch path would look completely broken to him.
check("finalize_text applies tier 1 by default (cfg omitted), even across a chunk seam",
      sw.finalize_text(["hello new", "paragraph world"], {}),
      "hello\n\nworld")
check("finalize_text still runs dictionary and collapse before the command converts",
      sw.finalize_text(["the Akme deal", "new paragraph", "next section"], REPS),
      "the Acme deal\n\nnext section")
check("finalize_text leaves tier 2 off by default: 'period' stays a literal word",
      sw.finalize_text(["the period of", "the loan"], {}),
      "the period of the loan")
_tier2_cfg = {"spoken_punctuation": {"enabled": True, "single_word_marks": True, "custom": {}}}
check("finalize_text honours a tier-2-enabled cfg passed in from the worker",
      sw.finalize_text(["wait comma", "that is wrong period"], {}, _tier2_cfg),
      "wait, that is wrong.")
_disabled_cfg = {"spoken_punctuation": {"enabled": False}}
check("finalize_text honours spoken_punctuation.enabled=false",
      sw.finalize_text(["hello new paragraph world"], {}, _disabled_cfg),
      "hello new paragraph world")


# --- 5c. phrases: pipeline.apply_phrases / pipeline.load_phrases ------------------------
# These belong to pipeline.py, but pipeline.py is owned by another agent working concurrently
# in a different worktree, so its own tests live here rather than in test_pipeline.py. `p` is
# the same pipeline.py module test_pipeline.py tests; consider moving this section there.
PHRASES = {
    "standard engagement caveat": ("This engagement is provided on our standard consulting "
                                   "terms and does not constitute financial advice."),
    "insert signature": "Best regards,\nYour Name Here",
}

# Happy path: mid-sentence trigger, everything around it survives untouched.
check("a mid-sentence trigger expands and the rest of the sentence survives",
      p.apply_phrases("he said standard engagement caveat during the call", PHRASES),
      "he said This engagement is provided on our standard consulting terms and does not "
      "constitute financial advice. during the call")

# Happy path (the pinned edge case): whisper appends sentence punctuation to a standalone
# dictation ("insert signature" arrives as "Insert signature."). DECISION: the trailing mark
# is consumed along with the trigger, so the expansion leaves no stray mark behind. A trigger
# spoken mid-sentence has no mark directly touching it, so that case (above) is unaffected.
check("a standalone trigger consumes whisper's own trailing period",
      p.apply_phrases("Insert signature.", PHRASES),
      "Best regards,\nYour Name Here")
check("the consumed mark may be any single sentence-ending punctuation, not only a period",
      p.apply_phrases("Insert signature!", PHRASES),
      "Best regards,\nYour Name Here")
check("a mark separated from the trigger by a space is NOT consumed (it is not whisper's own)",
      p.apply_phrases("insert signature .", PHRASES),
      "Best regards,\nYour Name Here .")

# Happy path: case variation.
check("matching is case-insensitive",
      p.apply_phrases("INSERT SIGNATURE now", PHRASES),
      "Best regards,\nYour Name Here now")

# Happy path: the multi-line value ("insert signature" above) survives with its newline intact;
# pin it explicitly too.
check("a multi-line phrase value keeps its newline",
      "\n" in p.apply_phrases("insert signature", PHRASES), True)

# Happy path: two triggers in one dictation, both expand.
check("two triggers in one dictation both expand",
      p.apply_phrases("insert signature and also standard engagement caveat", PHRASES),
      "Best regards,\nYour Name Here and also This engagement is provided on our "
      "standard consulting terms and does not constitute financial advice.")

# Word-boundary matching: a short trigger must never fire inside a longer word.
check("a short trigger does not fire inside a longer word",
      p.apply_phrases("please review the design carefully", {"sig": "SIGVAL"}),
      "please review the design carefully")

# Longest trigger first: a trigger that is a prefix of another must not shadow the specific one.
check("the longest matching trigger wins over a trigger that is its prefix",
      p.apply_phrases("insert signature please", {"insert": "SHORT", "insert signature": "LONG"}),
      "LONG please")

# The replacement is produced by a function, never a template string: the same trap
# apply_dictionary documents. A stored value of "\\1" as a re.sub template raises "invalid
# group reference"; passed as a function, as here, it is inserted literally.
check("a phrase value containing a backslash-group reference is inserted literally",
      p.apply_phrases("please use insert signature here", {"insert signature": "\\1 not a group"}),
      "please use \\1 not a group here")

# No-op paths: nothing configured, or nothing to expand.
check("apply_phrases is a no-op with no phrases configured",
      p.apply_phrases("insert signature", {}), "insert signature")
check("apply_phrases is a no-op on empty text", p.apply_phrases("", PHRASES), "")


def dict_home(dictionary, prefix="scribe-test-phrases-"):
    """A throwaway SCRIBE_HOME holding only a dictionary.json with the given content.

    `dictionary` is written verbatim: a string is written as-is (so a malformed-JSON case is
    possible), anything else is JSON-encoded. Mirrors test_pipeline.py's home_with(), kept
    local here so this file stays self-contained.
    """
    home = tempfile.mkdtemp(prefix=prefix)
    with open(os.path.join(home, "dictionary.json"), "w") as fh:
        fh.write(dictionary if isinstance(dictionary, str) else json.dumps(dictionary))
    return home


def load_phrases_in(home):
    """Run load_phrases() with SCRIBE_HOME pointed at `home`, then restore this test's own."""
    saved = os.environ["SCRIBE_HOME"]
    os.environ["SCRIBE_HOME"] = home
    try:
        p._resolve_paths()          # load_phrases reads the module-level DICT_PATH
        return p.load_phrases()
    finally:
        os.environ["SCRIBE_HOME"] = saved
        p._resolve_paths()


def error_text(fn, *args, **kwargs):
    """The message of whatever the call raised, or "" if it did not raise."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        return str(exc)
    return ""


# Failure path: a no-op dictionary.json (or no file at all) leaves phrases exactly as it does
# today: empty, and no crash.
check("no dictionary.json at all yields no phrases",
      load_phrases_in(tempfile.mkdtemp(prefix="scribe-test-nofile-")), {})
check("a dictionary.json with no \"phrases\" key yields no phrases, same as today",
      load_phrases_in(dict_home({"replacements": {"Akme": "Acme"}})), {})
check("phrases load correctly alongside a replacements key in the same file",
      load_phrases_in(dict_home({"replacements": {"Akme": "Acme"},
                                 "phrases": {"sig": "Signature Block"}})),
      {"sig": "Signature Block"})

# Failure path: a non-string phrase value names the file and the offending trigger, matching
# how load_replacements phrases its errors.
_bad_phrase_home = dict_home({"phrases": {"insert signature": 12345}})
check_raises("a non-string phrase value is rejected", RuntimeError,
             load_phrases_in, _bad_phrase_home)
_bad_phrase_msg = error_text(load_phrases_in, _bad_phrase_home)
check("the error names the dictionary file",
      os.path.join(_bad_phrase_home, "dictionary.json") in _bad_phrase_msg, True)
check("the error names the offending trigger", "insert signature" in _bad_phrase_msg, True)

# Failure path: "phrases" itself must be a JSON object.
check_raises("phrases as a list is rejected", RuntimeError,
             load_phrases_in, dict_home({"phrases": ["oops"]}))
check_raises("phrases as a string is rejected", RuntimeError,
             load_phrases_in, dict_home({"phrases": "oops"}))
check_raises("malformed dictionary JSON is a RuntimeError", RuntimeError,
             load_phrases_in, dict_home("{oops"))
check_raises("dictionary root as an array is rejected", RuntimeError,
             load_phrases_in, dict_home("[]"))


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


# --- 6c. prompt mode: the rewrite happens in the finish path, after the merge --------------
# The optimizer itself is pipeline's, and pipeline's own tests cover its prompt, its isolation
# and its sanitizer. What matters here is the worker's half of the contract: the finalized
# text is what gets rewritten, the phase marker lands on the worker's own stdout, the rewrite
# is what gets emitted and copied, and a failed rewrite still emits the words and returns 4.

class DoneTranscriber:
    """A finished ChunkTranscriber stand-in. _finish only drains it and reads its results."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.error = None

    def finish(self):
        pass

    def texts_in_order(self, count):
        return self._texts[:count]


class FinishedSession:
    """The two attributes _finish reads off a StreamSession once recording is over."""

    def __init__(self, chunk_count):
        self.chunk_count = chunk_count
        self.cuts = []


def finish_capturing(texts, optimize_for=None, cfg=None, copy=False, polish=False):
    """Run _finish over stubbed chunks. Returns (exit code, worker stdout, worker stderr)."""
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        code = sw._finish(FinishedSession(len(texts)), DoneTranscriber(texts), cfg or CFG,
                          copy, False, time.time(), {}, optimize_for=optimize_for,
                          polish=polish)
        return code, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


# --- 6a2. spoken punctuation through the REAL streaming finish path (_finish), not just
# finalize_text directly. This is what run_live/run_file actually call, so it is the closest
# this test suite gets to proving the maintainer's own recording path gets the feature.
_sp_code, _sp_out, _sp_err = finish_capturing(["hello new", "paragraph world"])
check("the streaming finish path emits the paragraph break", _sp_code, 0)
check("...with no stray double space or leftover command words", _sp_out, "hello\n\nworld\n")

_sp_tier2_cfg = dict(CFG, spoken_punctuation={"enabled": True, "single_word_marks": True,
                                              "custom": {}})
_sp2_code, _sp2_out, _ = finish_capturing(["wait comma that is", "wrong period"],
                                          cfg=_sp_tier2_cfg)
check("the streaming finish path honours a tier-2-enabled cfg", _sp2_out, "wait, that is wrong.\n")

_sp3_code, _sp3_out, _ = finish_capturing(["the period of the loan"])
check("the streaming finish path leaves tier 2 off by default: 'period' stays literal",
      _sp3_out, "the period of the loan\n")


_saved_optimize = sw.pipeline.optimize_prompt
REWRITE = "Context: a parser.\n\nTask: accept both formats."

_never_called = []
sw.pipeline.optimize_prompt = lambda text, cfg, target, status=None: _never_called.append(target)
_plain_code, _plain_out, _plain_err = finish_capturing(["plain dictation"])
check("without --optimize-for nothing is rewritten and the transcript is emitted",
      (_plain_code, _never_called, _plain_out), (0, [], "plain dictation\n"))

_opt_calls = []


def _fake_optimize(text, cfg, target, status=None):
    _opt_calls.append((text, target))
    return REWRITE


sw.pipeline.optimize_prompt = _fake_optimize
_ok_code, _ok_out, _ok_err = finish_capturing(["  the parser  ", "should take both formats"],
                                              optimize_for="opus")
check("prompt mode exits 0 when the rewrite succeeds", _ok_code, 0)
check("the optimizer is handed the merged, cleaned text and the chosen target",
      _opt_calls, [("the parser should take both formats", "opus")])
check("the rewrite is what the worker emits, not the transcript", _ok_out, REWRITE + "\n")
check("the emitted rewrite keeps its blank line", "\n\n" in _ok_out, True)

_opt_calls[:] = []
_collapsed_code, _collapsed_out, _ = finish_capturing(
    ["That is a partner.", "That is a partner."], optimize_for="fable")
check("the merge still collapses the transcript before the rewrite sees it",
      _opt_calls, [("That is a partner.", "fable")])

_copied = []
_saved_copy = sw.pipeline.copy_to_clipboard
sw.pipeline.copy_to_clipboard = lambda text: (_copied.append(text), True)[1]
_copy_code, _copy_out, _ = finish_capturing(["the parser"], optimize_for="sonnet", copy=True)
check("the rewrite is what reaches the clipboard", (_copy_code, _copied), (0, [REWRITE]))

# Fallback: the rewrite could not be produced. The words must still be emitted and copied.
sw.pipeline.optimize_prompt = lambda text, cfg, target, status=None: None
_copied[:] = []
_fb_code, _fb_out, _fb_err = finish_capturing(["keep my words"], optimize_for="opus", copy=True)
check("a failed rewrite returns the fallback exit code", _fb_code, sw.EXIT_OPTIMIZER_FALLBACK)
check("the worker and pipeline agree on the fallback code",
      (sw.EXIT_OPTIMIZER_FALLBACK, p.EXIT_OPTIMIZER_FALLBACK), (4, 4))
check("a failed rewrite still emits the raw transcription", _fb_out, "keep my words\n")
check("a failed rewrite still copies the raw transcription", _copied, ["keep my words"])

# A stale clipboard outranks the fallback code: the caller must not paste at all.
sw.pipeline.copy_to_clipboard = lambda text: False
_stale_code, _stale_out, _ = finish_capturing(["keep my words"], optimize_for="opus", copy=True)
check("a failed clipboard copy still wins over the fallback code", _stale_code, sw.EXIT_FAIL)
sw.pipeline.copy_to_clipboard = _saved_copy

# Through the real optimizer with a fake CLI: the marker must land on the worker's own stdout,
# the same channel dictate.lua already reads MIC_READY and the LEVEL meters from.
sw.pipeline.optimize_prompt = _saved_optimize
FAKE_BIN = tempfile.mkdtemp(prefix="scribe-test-bin-")


def write_script(name, body):
    path = os.path.join(FAKE_BIN, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


_worker_claude = write_script("optimize-claude",
                              'cat > /dev/null\n'
                              'printf "Context: a parser.\\n\\nTask: accept both formats.\\n"\n')
OPT_CFG = dict(CFG, claude_bin=_worker_claude, claude_model="claude-haiku-4-5-20251001")
_marker_code, _marker_out, _marker_err = finish_capturing(
    ["so make the parser take both formats"], optimize_for="fable", cfg=OPT_CFG)
check("prompt mode through the real optimizer exits 0", _marker_code, 0)
check("the worker prints the OPTIMIZING marker on its own stdout, first",
      _marker_out.splitlines()[0], p.PHASE_OPTIMIZING)
check("the rewrite follows the marker on the same stdout",
      "\n".join(_marker_out.splitlines()[1:]), REWRITE)

_missing_claude_cfg = dict(CFG, claude_bin=os.path.join(FAKE_BIN, "no-such-claude"),
                           claude_model="claude-haiku-4-5-20251001")
_gone_code, _gone_out, _gone_err = finish_capturing(["keep my words"], optimize_for="opus",
                                                    cfg=_missing_claude_cfg)
check("a missing claude CLI is a fallback, not a crash", _gone_code, sw.EXIT_OPTIMIZER_FALLBACK)
check("no marker is printed when the CLI is never invoked", _gone_out, "keep my words\n")


# --- 6c2. auto-polish: the same finish path, the same fail-safe, its own marker and code ------
# The polish itself is pipeline's, and pipeline's own tests cover its prompt and isolation.
# What matters here is the worker's half: the merged text is what gets polished, the POLISHING
# marker lands on the worker's own stdout, the polished text is what is emitted and copied, a
# polish that could not run still emits the words and returns 5, and --optimize-for wins.
_polish_claude = write_script("polish-claude",
                              'cat > /dev/null\nprintf "the cleaned dictation\\n"\n')
_polish_failing = write_script("polish-failing", "cat > /dev/null\nexit 1\n")
POLISH_CFG = dict(CFG, polish_enabled=True, claude_bin=_polish_claude,
                  claude_model="claude-haiku-4-5-20251001")

_ap_code, _ap_out, _ap_err = finish_capturing(["  so the raw  ", "dictation"], polish=True,
                                              cfg=POLISH_CFG)
check("auto-polish exits 0 when the polish runs", _ap_code, 0)
check("the worker prints the POLISHING marker on its own stdout, first",
      _ap_out.splitlines()[0], p.PHASE_POLISHING)
check("the polished text follows the marker on the same stdout",
      "\n".join(_ap_out.splitlines()[1:]), "the cleaned dictation")

_copied[:] = []
sw.pipeline.copy_to_clipboard = lambda text: (_copied.append(text), True)[1]
_ap_copy_code, _ap_copy_out, _ = finish_capturing(["the raw dictation"], polish=True,
                                                  cfg=POLISH_CFG, copy=True)
check("the polished text is what reaches the clipboard",
      (_ap_copy_code, _copied), (0, ["the cleaned dictation"]))

_copied[:] = []
_ap_fb_code, _ap_fb_out, _ap_fb_err = finish_capturing(
    ["keep my words"], polish=True, copy=True, cfg=dict(POLISH_CFG, claude_bin=_polish_failing))
check("a failed polish returns the polish fallback exit code",
      _ap_fb_code, sw.EXIT_POLISH_FALLBACK)
check("the worker and pipeline agree on the polish fallback code",
      (sw.EXIT_POLISH_FALLBACK, p.EXIT_POLISH_FALLBACK), (5, 5))
check("a failed polish still emits the unpolished transcription",
      "\n".join(_ap_fb_out.splitlines()[1:]), "keep my words")
check("the marker was still printed before that failed polish call",
      _ap_fb_out.splitlines()[0], p.PHASE_POLISHING)
check("a failed polish still copies the unpolished transcription", _copied, ["keep my words"])

# A polish switched off in config is a setting, not a failure: exit 0, the way a run without
# --polish would. Exiting 5 there would make dictate.lua alert after every dictation.
_copied[:] = []
_ap_off_code, _ap_off_out, _ap_off_err = finish_capturing(
    ["keep my words"], polish=True, copy=True, cfg=dict(POLISH_CFG, polish_enabled=False))
check("a polish switched off in config exits 0", _ap_off_code, 0)
check("no POLISHING marker when the polish CLI is never invoked",
      _ap_off_out, "keep my words\n")
check("the blocked polish says why on stderr", "[polish skipped]" in _ap_off_err, True)
check("the words are still copied when the polish is blocked", _copied, ["keep my words"])

# Enabled but unavailable IS a fallback: the user asked for a polish and did not get one.
_ap_gone_code, _ap_gone_out, _ = finish_capturing(
    ["keep my words"], polish=True,
    cfg=dict(POLISH_CFG, claude_bin=os.path.join(FAKE_BIN, "no-such-claude")))
check("a missing claude CLI is a polish fallback, not a crash",
      (_ap_gone_code, _ap_gone_out), (sw.EXIT_POLISH_FALLBACK, "keep my words\n"))

# A claude_bin that exists but cannot be executed used to raise PermissionError out of
# subprocess.run. In streaming nothing is persisted until after the polish, so that did not
# just skip the cleanup, it lost the dictation: no clipboard, no last-output.txt, no
# last-dict.txt. It must degrade to the same fallback as a missing CLI.
_unrunnable_claude = write_script("unrunnable-claude", "exit 0\n")
os.chmod(_unrunnable_claude, 0o644)
_copied[:] = []
sw.pipeline.copy_to_clipboard = lambda text: (_copied.append(text), True)[1]
_ap_unrunnable_code, _ap_unrunnable_out, _ = finish_capturing(
    ["keep my words"], polish=True, copy=True,
    cfg=dict(POLISH_CFG, claude_bin=_unrunnable_claude))
check("a claude_bin that cannot be executed is a fallback, not a crash",
      (_ap_unrunnable_code, _ap_unrunnable_out), (sw.EXIT_POLISH_FALLBACK, "keep my words\n"))
check("the dictation still reaches the clipboard when the CLI cannot be executed",
      _copied, ["keep my words"])

# Precedence: both flags given -> the optimizer runs and the polish is skipped entirely.
_saved_optimize_again = sw.pipeline.optimize_prompt
_precedence_calls = []


def _spy_optimize(text, cfg, target, status=None):
    _precedence_calls.append((text, target))
    return REWRITE


sw.pipeline.optimize_prompt = _spy_optimize
_both_code, _both_out, _both_err = finish_capturing(["  the parser  ", "takes both formats"],
                                                    optimize_for="opus", polish=True,
                                                    cfg=POLISH_CFG)
sw.pipeline.optimize_prompt = _saved_optimize_again
check("with both flags the optimizer still runs, on the merged text",
      _precedence_calls, [("the parser takes both formats", "opus")])
check("with both flags the polish is skipped: no POLISHING marker",
      p.PHASE_POLISHING in _both_out, False)
check("with both flags the rewrite is what gets emitted", (_both_code, _both_out),
      (0, REWRITE + "\n"))

# A stale clipboard outranks the polish fallback too: the caller must not paste at all.
sw.pipeline.copy_to_clipboard = lambda text: False
_ap_stale_code, _, _ = finish_capturing(["keep my words"], polish=True, copy=True,
                                        cfg=dict(POLISH_CFG, claude_bin=_polish_failing))
check("a failed clipboard copy wins over the polish fallback code too",
      _ap_stale_code, sw.EXIT_FAIL)
sw.pipeline.copy_to_clipboard = _saved_copy


# --- 6c3. a logged-out Claude CLI: the same fail-safe, one more specific exit code -----------
# Exit 6 is exit 4 or 5 with the cause named. What must not change is the contract they share:
# the words are still emitted and still copied, so the caller pastes exactly as it would on 0.
_logged_out_claude = write_script("logged-out-claude",
                                  'cat > /dev/null\n'
                                  'printf "Not logged in - Please run /login\\n"\n'
                                  'exit 1\n')
AUTH_CFG = dict(POLISH_CFG, claude_bin=_logged_out_claude)

_copied[:] = []
sw.pipeline.copy_to_clipboard = lambda text: (_copied.append(text), True)[1]
_auth_code, _auth_out, _auth_err = finish_capturing(["keep my words"], polish=True, copy=True,
                                                    cfg=AUTH_CFG)
check("a polish refused for want of a login returns the auth exit code",
      _auth_code, sw.EXIT_AUTH_NEEDED)
check("the worker and pipeline agree on the auth exit code",
      (sw.EXIT_AUTH_NEEDED, p.EXIT_AUTH_NEEDED), (6, 6))
check("a logged-out polish still emits the unpolished transcription",
      "\n".join(_auth_out.splitlines()[1:]), "keep my words")
check("a logged-out polish still copies the unpolished transcription", _copied, ["keep my words"])

_copied[:] = []
_auth_opt_code, _auth_opt_out, _ = finish_capturing(["keep my words"], optimize_for="opus",
                                                    copy=True, cfg=AUTH_CFG)
check("a rewrite refused for want of a login returns the auth exit code too",
      _auth_opt_code, sw.EXIT_AUTH_NEEDED)
check("a logged-out rewrite still copies the raw transcription", _copied, ["keep my words"])

# An ordinary failure must NOT be reported as a login problem, or the alert would send the
# user to fix something that is not broken.
_plain_fb_code, _, _ = finish_capturing(["keep my words"], polish=True,
                                        cfg=dict(POLISH_CFG, claude_bin=_polish_failing))
check("an ordinary polish failure keeps the plain fallback code",
      _plain_fb_code, sw.EXIT_POLISH_FALLBACK)
sw.pipeline.copy_to_clipboard = _saved_copy


# --- 6d. prompt mode: the flag plumbs from the command line into both run modes -------------
def plumbed_optimize_for(argv, attr):
    """Replace run_file/run_live with a spy, run main(argv), return what it was handed."""
    seen = {}

    def spy(source, cfg, copy=False, timings=False, optimize_for=None, polish=False):
        seen["source"], seen["optimize_for"], seen["polish"] = source, optimize_for, polish
        return 0

    saved = getattr(sw, attr)
    setattr(sw, attr, spy)
    try:
        seen["code"] = sw.main(argv)
    finally:
        setattr(sw, attr, saved)
    return seen


_HDR_WAV = os.path.join(FIXTURES, "hdr.wav")
_file_plumb = plumbed_optimize_for(["--from-file", _HDR_WAV, "--optimize-for", "sonnet"],
                                   "run_file")
check("--optimize-for reaches file mode", _file_plumb["optimize_for"], "sonnet")
check("file mode still gets its source and exit code",
      (_file_plumb["source"], _file_plumb["code"]), (_HDR_WAV, 0))
_live_plumb = plumbed_optimize_for(["--mic", "3", "--optimize-for", "fable"], "run_live")
check("--optimize-for reaches live mode", _live_plumb["optimize_for"], "fable")
check("live mode still gets its microphone index", _live_plumb["source"], "3")
check("omitting --optimize-for leaves the rewrite off",
      plumbed_optimize_for(["--from-file", _HDR_WAV], "run_file")["optimize_for"], None)

# --polish plumbs the same way, and the two flags may be given together: the worker applies the
# precedence itself rather than making argparse refuse the combination.
check("--polish reaches file mode",
      plumbed_optimize_for(["--from-file", _HDR_WAV, "--polish"], "run_file")["polish"], True)
check("--polish reaches live mode",
      plumbed_optimize_for(["--mic", "3", "--polish"], "run_live")["polish"], True)
check("omitting --polish leaves auto-polish off",
      plumbed_optimize_for(["--from-file", _HDR_WAV], "run_file")["polish"], False)
_both_flags = plumbed_optimize_for(
    ["--from-file", _HDR_WAV, "--polish", "--optimize-for", "fable"], "run_file")
check("both flags together are accepted and both reach the run",
      (_both_flags["polish"], _both_flags["optimize_for"]), (True, "fable"))

_stderr, sys.stderr = sys.stderr, io.StringIO()
check_raises("an unknown --optimize-for target is refused", SystemExit,
             sw.main, ["--from-file", _HDR_WAV, "--optimize-for", "gpt"])
_bad_target_err = sys.stderr.getvalue()
sys.stderr = _stderr
check("the unknown-target error lists the accepted targets",
      all(t in _bad_target_err for t in p.OPTIMIZE_TARGETS), True)


# --- 6e. phrases through the REAL streaming finish path (_finish), not just apply_phrases
# directly. This is what run_live/run_file actually call, so it is the closest this suite gets
# to proving the maintainer's own recording path (streaming) expands phrases, and exactly
# where relative to the optimizer/polish above.
#
# NOTE: ordering here was reversed after an earlier version of this section pinned the OPPOSITE
# behaviour (phrases expanding AFTER the optimizer). That version made the two recording paths
# disagree with each other, which HANDOVER.md calls the worst failure mode in this codebase:
# pipeline.py's batch path has always expanded phrases inside run(), before any --optimize-for
# rewrite (which only happens later, in __main__). The tests below now pin the corrected,
# shared order instead.
_saved_load_phrases = sw.pipeline.load_phrases
_saved_load_replacements = sw.pipeline.load_replacements

sw.pipeline.load_phrases = lambda: {
    "standard engagement caveat": "This engagement is subject to our standard terms."}
_ph_code, _ph_out, _ = finish_capturing(["please review the", "standard engagement caveat"])
check("the streaming finish path expands a phrase trigger, chunk seam included",
      (_ph_code, _ph_out),
      (0, "please review the This engagement is subject to our standard terms.\n"))

# Independence: a replacements entry must not rewrite text a phrase expansion just inserted.
# "Akme" is a configured mishearing fix (-> "Acme"); dictionary replacements run once, inside
# finalize_text, BEFORE the phrase is even looked up, and are never re-applied afterward. If
# phrase content came out garbled by a later replacements pass, this would fail.
sw.pipeline.load_replacements = lambda: {"Akme": "Acme"}
sw.pipeline.load_phrases = lambda: {
    "insert boilerplate": "Please contact Akme Corp for details."}
_indep_code, _indep_out, _ = finish_capturing(["insert boilerplate"])
check("phrase content is not touched by a dictionary replacement applied earlier in the chain",
      (_indep_code, _indep_out), (0, "Please contact Akme Corp for details.\n"))

# Ordering, prompt mode: the phrase expands BEFORE the optimizer's rewrite, matching batch.
# Proven by handing the optimizer stub the text and asserting it saw the EXPANDED phrase, not
# the bare trigger: a rewrite is free to reword, move, or drop a trigger word, so the optimizer
# must already have the real content to build a coherent prompt around.
sw.pipeline.load_replacements = lambda: {}
sw.pipeline.load_phrases = lambda: {"insert boilerplate": "EXPANDED BOILERPLATE TEXT"}
_order_calls = []
sw.pipeline.optimize_prompt = lambda text, cfg, target, status=None: (
    _order_calls.append(text), "REWRITTEN")[1]
_order_code, _order_out, _ = finish_capturing(["insert boilerplate"], optimize_for="opus")
check("the optimizer sees the EXPANDED phrase text, not the bare trigger",
      _order_calls, ["EXPANDED BOILERPLATE TEXT"])
check("the optimizer's rewrite is still what gets emitted",
      _order_out, "REWRITTEN\n")
sw.pipeline.optimize_prompt = _saved_optimize

# Ordering, polish: unchanged and pinned explicitly here so it cannot regress alongside the
# optimizer fix above. Phrases still expand AFTER polish: the polish CLI must never see, and
# so can never reshape, a phrase's own saved text.
_saved_polish_with_status = sw.pipeline.polish_with_status
_polish_order_calls = []
sw.pipeline.polish_with_status = lambda text, cfg, status=None: (
    _polish_order_calls.append(text), (text.upper(), True))[1]
_polish_order_code, _polish_order_out, _ = finish_capturing(
    ["insert boilerplate"], polish=True, cfg=POLISH_CFG)
check("the polish pass receives the bare trigger, never the expanded phrase text",
      _polish_order_calls, ["insert boilerplate"])
# apply_phrases is case-insensitive, so the trigger is still found (as "INSERT BOILERPLATE")
# inside the fake polish's uppercased output and expands there, AFTER polish ran.
check("phrases still expand after polish", _polish_order_out, "EXPANDED BOILERPLATE TEXT\n")
sw.pipeline.polish_with_status = _saved_polish_with_status

sw.pipeline.load_phrases = _saved_load_phrases
sw.pipeline.load_replacements = _saved_load_replacements


# --- 6f. regression guard: batch and streaming must AGREE on the prompt-mode order -----------
# The bug being guarded against: streaming once expanded phrases AFTER the optimizer while
# batch (pipeline.run()) has always expanded them before, since --optimize-for's rewrite in
# __main__ only happens after run() returns. Prove directly that both hand the optimizer the
# exact same, already-expanded text for the exact same dictation, so a future edit that makes
# them disagree again fails loudly here instead of only looking wrong to the maintainer.
_saved_p_transcribe = p.transcribe
_saved_p_load_phrases = p.load_phrases
_saved_p_load_replacements = p.load_replacements
p.transcribe = lambda wav_path, url, language, prompt: "insert boilerplate"
p.load_phrases = lambda: {"insert boilerplate": "EXPANDED BOILERPLATE TEXT"}
p.load_replacements = lambda: {}
# max_age=None: this reuses the synthetic hdr.wav fixture, which is not a fresh recording.
_batch_result = p.run(_HDR_WAV, "dict", CFG, max_age=None)
p.transcribe = _saved_p_transcribe
p.load_phrases = _saved_p_load_phrases
p.load_replacements = _saved_p_load_replacements
check("batch's run() has already expanded the phrase by the time __main__ calls the optimizer",
      _batch_result, "EXPANDED BOILERPLATE TEXT")

sw.pipeline.load_phrases = lambda: {"insert boilerplate": "EXPANDED BOILERPLATE TEXT"}
sw.pipeline.load_replacements = lambda: {}
_stream_optimizer_saw = []
sw.pipeline.optimize_prompt = lambda text, cfg, target, status=None: (
    _stream_optimizer_saw.append(text), "REWRITTEN")[1]
finish_capturing(["insert boilerplate"], optimize_for="opus")
sw.pipeline.optimize_prompt = _saved_optimize
sw.pipeline.load_phrases = _saved_load_phrases
sw.pipeline.load_replacements = _saved_load_replacements
check("streaming's _finish hands the optimizer the exact same text batch's run() would have",
      _stream_optimizer_saw, [_batch_result])


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
