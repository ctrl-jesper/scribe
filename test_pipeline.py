#!/usr/bin/env python3
"""Tests for the deterministic stages of pipeline.py (config loading, dictionary, loop
collapser) plus the SCRIBE_HOME config contract.

All fixture strings are synthetic and self-contained: none of them depend on a real user's
dictionary.json (the shipped one ships empty) and none of them are read from any file outside
the throwaway SCRIBE_HOME this test creates.

Run: python3 test_pipeline.py
"""
import importlib.util, json, os, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------------------
# Isolated SCRIBE_HOME: created and populated BEFORE pipeline.py is imported, so the module's
# import-time path resolution (see pipeline.py's _resolve_paths()) already points here. This
# test never reads or writes a real user's ~/.config/scribe.
# --------------------------------------------------------------------------------------
SCRIBE_HOME = tempfile.mkdtemp(prefix="scribe-test-home-")
os.environ["SCRIBE_HOME"] = SCRIBE_HOME

CONFIG = {
    "language": "en",
    "mic_name": "Test Microphone",
    "hotkey_keycode": 61,
    "hotkey_flag": "alt",
    "server_port": 8090,
    "model_file": "ggml-test-model.bin",
    "vocabulary": ["Acme", "Northlight"],
    "speaker_note": "Speaker is a non-native English speaker dictating short technical notes.",
    "mode": "dict",
    "polish_enabled": False,
    "claude_bin": os.path.join(SCRIBE_HOME, "no-such-claude"),  # deliberately absent
    "claude_model": "claude-haiku-4-5-20251001",
}
with open(os.path.join(SCRIBE_HOME, "config.json"), "w") as f:
    json.dump(CONFIG, f)
# The dictionary a fresh install ships with: present, but empty. Tests below define their own
# replacement pairs inline rather than relying on any entry existing here.
with open(os.path.join(SCRIBE_HOME, "dictionary.json"), "w") as f:
    json.dump({"replacements": {}}, f)

spec = importlib.util.spec_from_file_location("pipeline", os.path.join(HERE, "pipeline.py"))
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)

cases = []
def check(name, got, want):
    cases.append((name, got == want, got, want))


# --- config: reads every schema key from config.json -----------------------------------
cfg = p.load_config()
check("config reads language", cfg["language"], "en")
check("config reads mic_name", cfg["mic_name"], "Test Microphone")
check("config reads hotkey_keycode", cfg["hotkey_keycode"], 61)
check("config reads hotkey_flag", cfg["hotkey_flag"], "alt")
check("config reads server_port", cfg["server_port"], 8090)
check("config reads model_file", cfg["model_file"], "ggml-test-model.bin")
check("config reads vocabulary", cfg["vocabulary"], ["Acme", "Northlight"])
check("config reads mode", cfg["mode"], "dict")
check("config reads polish_enabled", cfg["polish_enabled"], False)
check("config reads claude_model", cfg["claude_model"], "claude-haiku-4-5-20251001")

# --- config: derived values built from the schema keys, not stored directly ------------
check("config derives server_url from server_port",
      cfg["server_url"], "http://127.0.0.1:8090/inference")
check("config derives prompt from vocabulary",
      cfg["prompt"], "Glossary of names that may occur: Acme, Northlight.")

# --- config: missing keys fall back to built-in defaults --------------------------------
PARTIAL_HOME = tempfile.mkdtemp(prefix="scribe-test-partial-")
with open(os.path.join(PARTIAL_HOME, "config.json"), "w") as f:
    json.dump({"mode": "full"}, f)   # only one key set; everything else must default
_saved_home = os.environ["SCRIBE_HOME"]
os.environ["SCRIBE_HOME"] = PARTIAL_HOME
partial_cfg = p.load_config()
os.environ["SCRIBE_HOME"] = _saved_home
check("config falls back to default language when key is missing", partial_cfg["language"], "en")
check("config falls back to default hotkey_keycode when key is missing",
      partial_cfg["hotkey_keycode"], 61)
check("config keeps the one key that was set", partial_cfg["mode"], "full")

# --- config: empty vocabulary produces an empty prompt (no dangling glossary header) ----
check("empty vocabulary yields empty prompt", p.build_prompt([]), "")
check("single-name vocabulary still reads as a sentence",
      p.build_prompt(["Acme"]), "Glossary of names that may occur: Acme.")

# --- config: dictionary.json ships empty on a fresh install -----------------------------
check("fresh-install dictionary.json has no replacements", p.load_replacements(), {})

# --- config: polish is blocked by default, and by a missing claude binary ---------------
check("polish blocked when polish_enabled is false",
      p.polish_blocked_reason(cfg) is not None, True)
enabled_missing_bin = dict(cfg, polish_enabled=True)   # claude_bin still points nowhere
check("polish blocked when claude_bin does not exist",
      p.polish_blocked_reason(enabled_missing_bin) is not None, True)
real_bin = os.path.join(SCRIBE_HOME, "fake-claude")
open(real_bin, "w").close()
enabled_real_bin = dict(cfg, polish_enabled=True, claude_bin=real_bin)
check("polish unblocked once enabled with an existing claude_bin",
      p.polish_blocked_reason(enabled_real_bin), None)


# --- dictionary: happy path, inline replacement pairs (never from a shipped dictionary) --
REPS = {
    "Akme": "Acme",
    "Nortlite": "Northlight",
    "Sorensen": "Sørensen",   # non-ASCII replacement, used by the word-boundary test below
}

check("dict fixes a garbled name",
      p.apply_dictionary("the Akme and Nortlite deal", REPS),
      "the Acme and Northlight deal")

# --- dictionary: edge, must not touch a substring inside a longer word -------------------
# Equivalent of the diacritic-boundary property in the original tool's private tests: a
# same-prefix longer word must be left untouched while the standalone word is corrected,
# including a plain-ASCII -> non-ASCII replacement.
check("dict respects word boundary across a diacritic replacement",
      p.apply_dictionary("Sorensenberg is not Sorensen", REPS),
      "Sorensenberg is not Sørensen")

# --- dictionary: a second garble of the same brand corrects the same way -----------------
check("dict fixes a second garble of the same name",
      p.apply_dictionary("as founder of Nortlite we grew", REPS),
      "as founder of Northlight we grew")


# --- segment join: mid-word split must reunite, word-boundary split must keep its space ---
check("join reunites mid-word split",
      p.join_segments(" including asset serv\nicing, billing"),
      "including asset servicing, billing")
check("join keeps boundary space",
      p.join_segments(" Hello there.\n Next sentence."),
      "Hello there. Next sentence.")


# --- collapser: happy path, runaway word-loop ---------------------------------------------
check("collapse word-loop",
      p.collapse_repetitions("who are the people who are the people who are the people who will win"),
      "who are the people who will win")

# --- collapser: happy path, single-word stutter -------------------------------------------
check("collapse single-word stutter",
      p.collapse_repetitions("this this this is important"),
      "this is important")

# --- collapser: happy path, exact duplicate sentence --------------------------------------
check("collapse duplicate sentence",
      p.collapse_repetitions("That is a partner. That is a partner."),
      "That is a partner.")

# --- collapser: edge, must NOT collapse a word said only twice (legit emphasis) -----------
check("keep double (not a loop)",
      p.collapse_repetitions("no no it is fine"),
      "no no it is fine")

# --- collapser: edge, must NOT touch non-repeating text ------------------------------------
check("keep normal prose",
      p.collapse_repetitions("run multiple agendas at the customer this quarter"),
      "run multiple agendas at the customer this quarter")

# --- collapser: edge, near-duplicate sentences differ, keep both --------------------------
check("keep near-duplicate sentences",
      p.collapse_repetitions("Run the agenda. Run the agenda at the customer."),
      "Run the agenda. Run the agenda at the customer.")


fails = [c for c in cases if not c[1]]
for name, ok, got, want in cases:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got:  {got!r}\n        want: {want!r}")
print(f"\n{len(cases) - len(fails)}/{len(cases)} passed")
raise SystemExit(1 if fails else 0)
