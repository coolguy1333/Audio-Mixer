"""Effects for the Audio Mixer: definitions, PipeWire filter-chain hosting and the editors.

Every channel (and every mix, for its device outputs) can carry an effect chain.  A chain runs
as its own `pipewire -c <conf>` process hosting libpipewire-module-filter-chain with the
plugins from fx/amx_fx.so.  It captures the channel (the monitor of its sink, or the hardware
input) and publishes the processed audio as a virtual source, `amx_fx_<id>`, which the mixer's
loopbacks then use instead of the raw signal.

Knob moves are sent live with `pw-cli set-param` (no restart, no gap).  Only a change of the
chain's *shape* - turning the first effect on, the last one off, or adding / removing /
reordering a Creative Rack module - restarts the process.
"""
import cmath
import copy
import ctypes
import json
import math
import os
import signal
import subprocess
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FX_DIR = os.path.join(HERE, "fx")
FX_SRC = os.path.join(FX_DIR, "amx_fx.c")
FX_SO = os.path.join(FX_DIR, "amx_fx.so")
if not os.access(FX_DIR, os.W_OK):
    # System install (e.g. /usr/share/audio-mixer): use the packaged build if there is one,
    # otherwise compile into the user's cache directory.
    if not os.path.exists(FX_SO):
        FX_SO = os.path.join(GLib.get_user_cache_dir(), "audio-mixer", "amx_fx.so")
        os.makedirs(os.path.dirname(FX_SO), exist_ok=True)
RUN_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "audio-mixer")
PRESETS = os.path.join(GLib.get_user_config_dir(), "audio-mixer", "rack-presets.json")


# --------------------------------------------------------------------------- build
def ensure_built():
    """Compile fx/amx_fx.so if it is missing or older than its source. Returns (ok, message)."""
    try:
        if os.path.exists(FX_SO) and (os.path.getmtime(FX_SO) >= os.path.getmtime(FX_SRC)
                                      or not os.access(os.path.dirname(FX_SO), os.W_OK)):
            return True, ""
    except OSError:
        return os.path.exists(FX_SO), "effects source missing"
    cc = next((c for c in ("cc", "gcc", "clang") if GLib.find_program_in_path(c)), None)
    if not cc:
        return os.path.exists(FX_SO), "no C compiler found (install gcc) - effects unavailable"
    base = [cc, "-O2", "-fPIC", "-shared", "-o", FX_SO + ".tmp", FX_SRC]
    for extra, note in ((["-lrnnoise", "-lm"], ""),
                        (["-DAMX_NO_RNNOISE", "-lm"], "built without RNNoise (install rnnoise for noise suppression)")):
        try:
            r = subprocess.run(base + extra, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"compiling effects failed: {e}"
        if r.returncode == 0:
            os.replace(FX_SO + ".tmp", FX_SO)
            return True, note
    return False, "compiling effects failed: " + (r.stderr.strip().splitlines() or ["?"])[-1]


# --------------------------------------------------------------------------- definitions
# A parameter: (port, label, lo, hi, default, unit, digits[, kind])   kind: "toggle" | "int"
def P(port, label, lo, hi, default, unit="", digits=0, kind=None, choices=None):
    return {"port": port, "label": label, "lo": lo, "hi": hi, "def": default, "unit": unit,
            "digits": digits, "kind": kind, "choices": choices}


def C(port, label, choices, default=0):
    """A pick-one parameter; the plugin sees the index."""
    return P(port, label, 0, len(choices) - 1, default, kind="choice", choices=choices)


# Standard chain, in signal order.  "where": which objects offer it (hw input, app channel, mix)
STANDARD = [
    {"key": "denoise", "title": "Noise suppression", "plugin": "amx_denoise", "where": "hv",
     "sub": "RNNoise neural noise removal, at full strength", "params": []},
    {"key": "gate", "title": "Noise gate", "plugin": "amx_gate", "where": "hv",
     "sub": "Silence the input between words", "params": [
        P("Threshold", "Threshold", -90, 0, -45, "dB"), P("Range", "Reduction", -90, 0, -60, "dB"),
        P("Attack", "Attack", 0.1, 100, 2, "ms", 1), P("Hold", "Hold", 0, 1000, 120, "ms"),
        P("Release", "Release", 5, 2000, 150, "ms")]},
    {"key": "lowcut", "title": "Low cut", "plugin": "amx_lowcut", "where": "hv",
     "sub": "Remove rumble, hum and plosive thumps", "params": [
        P("Frequency", "Frequency", 20, 500, 80, "Hz")]},
    {"key": "eq", "title": "Equalizer", "plugin": None, "where": "hvm",
     "sub": "Three tone sliders, or ten parametric bands", "params": []},
    {"key": "deess", "title": "De-esser", "plugin": "amx_deess", "where": "hv",
     "sub": "Tame harsh “s” and “t” sounds", "params": [
        P("Frequency", "Frequency", 2000, 12000, 6000, "Hz"), P("Threshold", "Threshold", -60, 0, -30, "dB"),
        P("Amount", "Max reduction", 0, 24, 8, "dB")]},
    {"key": "comp", "title": "Compressor", "plugin": "amx_comp", "where": "hvm",
     "sub": "Even out loud and quiet parts", "params": [
        P("Threshold", "Threshold", -60, 0, -18, "dB"), P("Ratio", "Ratio", 1, 20, 3, ":1", 1),
        P("Attack", "Attack", 0.1, 200, 10, "ms", 1), P("Release", "Release", 10, 2000, 150, "ms"),
        P("Knee", "Knee", 0, 12, 6, "dB"), P("Makeup", "Makeup gain", -12, 24, 0, "dB", 1),
        P("Auto Makeup", "Automatic makeup gain", 0, 1, 1, kind="toggle")]},
    # (the Creative Rack is inserted here)
    {"key": "limiter", "title": "Limiter", "plugin": "amx_limiter", "where": "hvm",
     "sub": "A hard ceiling: nothing gets louder than this", "params": [
        P("Ceiling", "Ceiling", -24, 0, -1, "dB", 1), P("Release", "Release", 10, 1000, 80, "ms")]},
    {"key": "loud", "title": "Ear protection", "plugin": "amx_loud", "where": "vm",
     "sub": "LUFS loudness limiter: a sudden loud video can't blast your ears", "params": [
        P("Target", "Max loudness", -40, -5, -16, "LUFS"), P("Max Cut", "Max reduction", 0, 40, 24, "dB")]},
]
STD = {s["key"]: s for s in STANDARD}
RACK_AT = [s["key"] for s in STANDARD].index("limiter")

EQ3 = [P("Bass", "Bass", -15, 15, 0, "dB", 1), P("Mid", "Mid", -15, 15, 0, "dB", 1),
       P("Treble", "Treble", -15, 15, 0, "dB", 1)]
PEQ_TYPES = ["Off", "Bell", "Low shelf", "High shelf", "Low cut", "High cut", "Notch"]
PEQ_N = 10
DEFAULT_BANDS = [{"type": 1, "freq": f, "gain": 0.0, "q": 1.0}
                 for f in (32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)]


def _bands(*spec):
    """Preset helper: (type, freq, gain, q) tuples, padded with flat bells."""
    out = [{"type": t, "freq": f, "gain": g, "q": q} for t, f, g, q in spec]
    for b in DEFAULT_BANDS[len(out):]:
        out.append(dict(b, type=0))
    return out


EQ_PRESETS = {
    "Flat": [dict(b) for b in DEFAULT_BANDS],
    "Voice clarity": _bands((4, 80, 0, 0.7), (1, 250, -3, 1.0), (1, 3000, 3, 1.0), (3, 10000, 2, 0.7)),
    "Broadcast voice": _bands((4, 70, 0, 0.7), (1, 120, 3, 0.8), (1, 400, -2.5, 1.2), (1, 5000, 4, 1.0),
                              (3, 12000, 2, 0.7)),
    "Warm podcast": _bands((4, 60, 0, 0.7), (2, 150, 3, 0.7), (1, 2500, -1.5, 1.5), (3, 8000, -2, 0.7)),
    "Reduce harshness": _bands((1, 3500, -4, 2.0), (1, 6500, -3, 2.0)),
    "Bass boost": _bands((2, 100, 6, 0.7)),
    "Treble boost": _bands((3, 6000, 5, 0.7)),
    "Telephone": _bands((4, 400, 0, 0.7), (5, 3400, 0, 0.7), (1, 1500, 5, 1.0)),
}

# Creative Rack modules: type -> definition
RACK = {
    "ceq": {"title": "Creative EQ", "cat": "Tone and drive", "plugin": "amx_ceq", "params": [
        P("Gain", "Gain", 0, 10, 0, "", 1), P("Bass", "Bass", 0, 10, 5, "", 1), P("Middle", "Middle", 0, 10, 5, "", 1),
        P("Treble", "Treble", 0, 10, 5, "", 1), P("Presence", "Presence", 0, 10, 5, "", 1),
        P("Master", "Master", -24, 12, 0, "dB", 1)]},
    "overdrive": {"title": "Overdrive", "cat": "Tone and drive", "plugin": "amx_overdrive", "params": [
        P("Drive", "Drive", 0, 10, 5, "", 1), P("Tone", "Tone", 0, 10, 5, "", 1), P("Level", "Level", -24, 6, -6, "dB", 1)]},
    "tape": {"title": "Tape Saturator", "cat": "Tone and drive", "plugin": "amx_tape", "params": [
        P("Drive", "Drive", 0, 24, 9, "dB", 1), P("Warmth", "Warmth", 0, 1, 0.5, "", 2), P("Mix", "Mix", 0, 1, 1, "", 2)]},
    "crush": {"title": "Bitcrusher", "cat": "Tone and drive", "plugin": "amx_crush", "params": [
        P("Bits", "Bits", 1, 16, 8, "", 0, "int"), P("Downsample", "Downsample", 1, 50, 4, "×", 0, "int"),
        P("Mix", "Mix", 0, 1, 1, "", 2)]},
    "chorus": {"title": "Chorus", "cat": "Modulation", "plugin": "amx_chorus", "params": [
        P("Rate", "Rate", 0.05, 5, 0.8, "Hz", 2), P("Depth", "Depth", 0, 10, 3, "ms", 1), P("Mix", "Mix", 0, 1, 0.6, "", 2)]},
    "flanger": {"title": "Flanger", "cat": "Modulation", "plugin": "amx_flanger", "params": [
        P("Rate", "Rate", 0.02, 5, 0.25, "Hz", 2), P("Depth", "Depth", 0, 8, 2, "ms", 1),
        P("Feedback", "Feedback", -0.95, 0.95, 0.5, "", 2), P("Mix", "Mix", 0, 1, 1, "", 2)]},
    "phaser": {"title": "Phaser", "cat": "Modulation", "plugin": "amx_phaser", "params": [
        P("Rate", "Rate", 0.02, 5, 0.5, "Hz", 2), P("Depth", "Depth", 0, 1, 0.7, "", 2),
        P("Feedback", "Feedback", 0, 0.95, 0.4, "", 2), P("Mix", "Mix", 0, 1, 1, "", 2)]},
    "tremolo": {"title": "Tremolo", "cat": "Modulation", "plugin": "amx_tremolo", "params": [
        P("Rate", "Rate", 0.5, 20, 5, "Hz", 1), P("Depth", "Depth", 0, 1, 0.6, "", 2),
        P("Stereo", "Stereo (auto-pan)", 0, 1, 0, kind="toggle")]},
    "ring": {"title": "Ring Modulator", "cat": "Modulation", "plugin": "amx_ring", "params": [
        P("Frequency", "Frequency", 1, 2000, 440, "Hz"), P("Mix", "Mix", 0, 1, 1, "", 2)]},
    "envfilter": {"title": "Envelope Filter", "cat": "Modulation", "plugin": "amx_envfilter", "params": [
        P("Sensitivity", "Sensitivity", 0, 1, 0.5, "", 2), P("Resonance", "Resonance", 0.5, 12, 4, "", 1),
        P("Low Freq", "Low frequency", 80, 1000, 250, "Hz"), P("Range", "Range", 1, 6, 4, "oct", 1),
        P("Mix", "Mix", 0, 1, 1, "", 2)]},
    "pitch": {"title": "Pitch Shifter", "cat": "Pitch and time", "plugin": "amx_pitch", "params": [
        P("Semitones", "Semitones", -12, 12, 0, "st", 1), P("Mix", "Mix", 0, 1, 1, "", 2)]},
    "delay": {"title": "Delay", "cat": "Pitch and time", "plugin": "amx_delay", "params": [
        P("Time", "Time", 1, 2000, 350, "ms"), P("Feedback", "Feedback", 0, 0.95, 0.4, "", 2),
        P("Damping", "Damping", 0, 1, 0.3, "", 2), P("Ping-Pong", "Ping-pong", 0, 1, 0, kind="toggle"),
        P("Mix", "Mix", 0, 1, 0.35, "", 2)]},
    "revdelay": {"title": "Reverse Delay", "cat": "Pitch and time", "plugin": "amx_revdelay", "params": [
        P("Time", "Time", 50, 2000, 500, "ms"), P("Feedback", "Feedback", 0, 0.9, 0.3, "", 2),
        P("Mix", "Mix", 0, 1, 0.5, "", 2)]},
    "reverb": {"title": "Reverb", "cat": "Pitch and time", "plugin": "amx_reverb", "params": [
        P("Size", "Size", 0, 1, 0.6, "", 2), P("Damping", "Damping", 0, 1, 0.4, "", 2),
        P("Predelay", "Predelay", 0, 200, 20, "ms"), P("Mix", "Mix", 0, 1, 0.3, "", 2)]},
    # The EasyEffects line-up (its gate, compressor, limiter, de-esser, equalizer, noise reduction
    # and loudness live in the standard chain; pitch, delay and reverb are above)
    "autogain": {"title": "Auto Gain", "cat": "Dynamics", "plugin": "amx_autogain", "params": [
        P("Target", "Target loudness", -36, -6, -18, "LUFS"), P("Max Gain", "Max boost", 0, 30, 12, "dB"),
        P("Max Cut", "Max cut", 0, 30, 12, "dB"), P("Speed", "Speed", 0.5, 20, 3, "s", 1)]},
    "expander": {"title": "Expander", "cat": "Dynamics", "plugin": "amx_expander", "params": [
        P("Threshold", "Threshold", -80, 0, -40, "dB"), P("Ratio", "Ratio", 1, 10, 2, ":1", 1),
        P("Max Reduction", "Max reduction", 0, 90, 40, "dB"), P("Attack", "Attack", 0.1, 100, 5, "ms", 1),
        P("Release", "Release", 10, 2000, 200, "ms")]},
    "mbcomp": {"title": "Multiband Compressor", "cat": "Dynamics", "plugin": "amx_mbcomp", "params": [
        P("Low Crossover", "Low / mid split", 40, 1000, 200, "Hz"),
        P("High Crossover", "Mid / high split", 1000, 12000, 3000, "Hz"),
        P("Low Threshold", "Low threshold", -60, 0, -24, "dB"), P("Low Ratio", "Low ratio", 1, 20, 3, ":1", 1),
        P("Mid Threshold", "Mid threshold", -60, 0, -24, "dB"), P("Mid Ratio", "Mid ratio", 1, 20, 3, ":1", 1),
        P("High Threshold", "High threshold", -60, 0, -24, "dB"), P("High Ratio", "High ratio", 1, 20, 3, ":1", 1),
        P("Attack", "Attack", 0.1, 200, 10, "ms", 1), P("Release", "Release", 10, 2000, 150, "ms"),
        P("Makeup", "Makeup gain", -12, 24, 0, "dB", 1)]},
    "mbgate": {"title": "Multiband Gate", "cat": "Dynamics", "plugin": "amx_mbgate", "params": [
        P("Low Crossover", "Low / mid split", 40, 1000, 200, "Hz"),
        P("High Crossover", "Mid / high split", 1000, 12000, 3000, "Hz"),
        P("Low Threshold", "Low threshold", -90, 0, -50, "dB"), P("Mid Threshold", "Mid threshold", -90, 0, -50, "dB"),
        P("High Threshold", "High threshold", -90, 0, -50, "dB"), P("Range", "Reduction", -90, 0, -40, "dB"),
        P("Attack", "Attack", 0.1, 100, 2, "ms", 1), P("Hold", "Hold", 0, 1000, 100, "ms"),
        P("Release", "Release", 5, 2000, 150, "ms")]},
    "maximizer": {"title": "Maximizer", "cat": "Dynamics", "plugin": "amx_maximizer", "params": [
        P("Threshold", "Threshold", -30, 0, -6, "dB", 1), P("Ceiling", "Ceiling", -12, 0, -0.5, "dB", 1),
        P("Release", "Release", 1, 1000, 60, "ms")]},
    "filter": {"title": "Filter", "cat": "Tone and drive", "plugin": "amx_filter", "params": [
        C("Type", "Type", ["Low pass", "High pass", "Band pass", "Notch"]),
        P("Frequency", "Frequency", 20, 20000, 1000, "Hz"), P("Resonance", "Resonance (Q)", 0.1, 10, 0.71, "", 2),
        C("Slope", "Slope", ["12 dB/oct", "24 dB/oct", "48 dB/oct"])]},
    "bassenh": {"title": "Bass Enhancer", "cat": "Tone and drive", "plugin": "amx_bassenh", "params": [
        P("Amount", "Amount", 0, 1, 0.4, "", 2), P("Frequency", "Below", 40, 250, 100, "Hz"),
        P("Harmonics", "Harmonics", 0, 10, 5, "", 1),
        P("Include Fundamental", "Add the deep bass too", 0, 1, 0, kind="toggle")]},
    "bassloud": {"title": "Bass Loudness", "cat": "Tone and drive", "plugin": "amx_bassloud", "params": [
        P("Amount", "Amount", 0, 18, 6, "dB", 1), P("Bass Frequency", "Bass below", 40, 250, 100, "Hz"),
        P("Treble", "Treble share", 0, 1, 0.35, "", 2), P("Output", "Output", -24, 6, -3, "dB", 1)]},
    "exciter": {"title": "Exciter", "cat": "Tone and drive", "plugin": "amx_exciter", "params": [
        P("Amount", "Amount", 0, 1, 0.3, "", 2), P("Frequency", "Above", 1000, 12000, 4000, "Hz"),
        P("Harmonics", "Harmonics", 0, 10, 5, "", 1)]},
    "crystal": {"title": "Crystalizer", "cat": "Tone and drive", "plugin": "amx_crystal", "params": [
        P("Intensity", "Intensity", 0, 10, 2, "", 1), P("Output", "Output", -24, 6, -3, "dB", 1)]},
    "stereo": {"title": "Stereo Tools", "cat": "Stereo", "plugin": "amx_stereo", "params": [
        P("Balance", "Balance", -1, 1, 0, "", 2), P("Width", "Width", 0, 2, 1, "×", 2),
        P("Mono", "Mono", 0, 1, 0, kind="toggle"), P("Swap", "Swap left / right", 0, 1, 0, kind="toggle"),
        P("Invert L", "Invert left phase", 0, 1, 0, kind="toggle"),
        P("Invert R", "Invert right phase", 0, 1, 0, kind="toggle")]},
    "crossfeed": {"title": "Crossfeed", "cat": "Stereo", "plugin": "amx_crossfeed", "params": [
        P("Cutoff", "Cutoff", 300, 2000, 700, "Hz"), P("Feed", "Feed level", 1, 15, 4.5, "dB", 1)]},
}
RACK_CATS = ["Dynamics", "Tone and drive", "Modulation", "Pitch and time", "Stereo"]


def kind_of(obj):
    """'h' hardware input channel, 'v' app (virtual) channel, 'm' mix."""
    if "outputs" in obj:
        return "m"
    return "h" if obj.get("type") == "hw" else "v"


def offered(obj):
    k = kind_of(obj)
    return [s for s in STANDARD if k in s["where"]]


def fx_of(obj):
    """The object's effect settings, with defaults filled in."""
    fx = obj.setdefault("fx", {})
    for s in offered(obj):
        m = fx.setdefault(s["key"], {})
        m.setdefault("on", False)
        for p in s["params"]:
            m.setdefault(p["port"], p["def"])
    eq = fx.get("eq")
    if eq is not None:
        eq.setdefault("mode", "simple")
        for p in EQ3:
            eq.setdefault(p["port"], 0.0)
        eq.setdefault("Output", 0.0)
        bands = eq.setdefault("bands", [])
        while len(bands) < PEQ_N:
            bands.append(dict(DEFAULT_BANDS[len(bands)]))
    if kind_of(obj) != "m":
        rack = fx.get("rack")
        if not isinstance(rack, list):
            rack = fx["rack"] = []
        rack[:] = [m for m in rack if isinstance(m, dict) and m.get("type") in RACK]
        for m in rack:
            if not isinstance(m.get("p"), dict):
                m["p"] = {}
            d = RACK.get(m.get("type"))
            m.setdefault("uid", os.urandom(3).hex())
            m.setdefault("on", True)
            ps = m.setdefault("p", {})
            for p in (d["params"] if d else []):
                ps.setdefault(p["port"], p["def"])
        rack[:] = [m for m in rack if m.get("type") in RACK]
    return fx


def active(obj):
    fx = obj.get("fx") or {}
    if any((fx.get(s["key"]) or {}).get("on") for s in offered(obj)):
        return True
    return bool(fx.get("rack")) and kind_of(obj) != "m"


def nodes(obj):
    """[(node name, plugin label, {control: value})] in signal order."""
    fx = fx_of(obj)
    out, rack_done = [], False
    for s in offered(obj):
        if not rack_done and STANDARD.index(s) >= RACK_AT:
            out += _rack_nodes(obj, fx)
            rack_done = True
        out += _std_nodes(s, fx[s["key"]])
    if not rack_done:
        out += _rack_nodes(obj, fx)
    return out


def _std_nodes(s, m):
    on = 1.0 if m.get("on") else 0.0
    if s["key"] == "eq":
        simple = m.get("mode", "simple") == "simple"
        c3 = {"Enabled": on if simple else 0.0}
        for p in EQ3:
            c3[p["port"]] = float(m.get(p["port"], 0.0))
        cp = {"Enabled": 0.0 if simple else on, "Output": float(m.get("Output", 0.0))}
        for i, b in enumerate(m.get("bands", DEFAULT_BANDS)[:PEQ_N], 1):
            cp[f"Type {i}"] = float(b.get("type", 0))
            cp[f"Freq {i}"] = float(b.get("freq", 1000))
            cp[f"Gain {i}"] = float(b.get("gain", 0))
            cp[f"Q {i}"] = float(b.get("q", 1))
        return [("s_eq3", "amx_eq3", c3), ("s_peq", "amx_peq", cp)]
    c = {"Enabled": on}
    for p in s["params"]:
        c[p["port"]] = float(m.get(p["port"], p["def"]))
    return [("s_" + s["key"], s["plugin"], c)]


def _rack_nodes(obj, fx):
    if kind_of(obj) == "m":
        return []
    out = []
    for m in fx.get("rack", []):
        d = RACK[m["type"]]
        c = {"Enabled": 1.0 if m.get("on") else 0.0}
        for p in d["params"]:
            c[p["port"]] = float(m["p"].get(p["port"], p["def"]))
        out.append((f"r_{m['uid']}", d["plugin"], c))
    return out


def controls(obj):
    return {f"{n}:{k}": v for n, _l, c in nodes(obj) for k, v in c.items()}


def shape(obj):
    """What forces a restart when it changes: the node list (names + plugins)."""
    return tuple((n, l) for n, l, _c in nodes(obj))


def _q(s):
    return '"' + str(s).replace("\\", "").replace('"', "'") + '"'


def make_conf(tag, nds, target, capture_sink, desc, mono=False):
    """mono: the target is a one-channel source with an AUX position (a "pro-audio" profile
    microphone). A stereo capture would take that channel on one side only, so the chain
    captures it as MONO and a splitter feeds it to both inputs of the first effect. The
    splitter also gives back the 6 dB that PipeWire drops when converting AUX0 to MONO."""
    lines = []
    for n, label, c in nds:
        ctl = " ".join(f"{_q(k)} = {v:.6g}" for k, v in c.items())
        lines.append(f"          {{ type = ladspa name = {n} plugin = {_q(FX_SO)} label = {label} "
                     f"control = {{ {ctl} }} }}")
    links = []
    for (a, _l1, _c1), (b, _l2, _c2) in zip(nds, nds[1:]):
        links.append(f'          {{ output = "{a}:Out L" input = "{b}:In L" }}')
        links.append(f'          {{ output = "{a}:Out R" input = "{b}:In R" }}')
    first, last = nds[0][0], nds[-1][0]
    if mono:
        lines.insert(0, '          { type = builtin name = amx_split label = linear '
                        'control = { "Mult" = 2.0 "Add" = 0.0 } }')
        links.append(f'          {{ output = "amx_split:Out" input = "{first}:In L" }}')
        links.append(f'          {{ output = "amx_split:Out" input = "{first}:In R" }}')
        inputs = '[ "amx_split:In" ]'
        chans = "audio.channels = 1\n      audio.position = [ MONO ]"
        play_fmt = " audio.channels = 2 audio.position = [ FL FR ]"
    else:
        inputs = f'[ "{first}:In L" "{first}:In R" ]'
        chans = "audio.channels = 2\n      audio.position = [ FL FR ]"
        play_fmt = ""
    cap = (f'node.name = "amx_fxin_{tag}" node.description = {_q(desc + " (effects input)")} '
           f'target.object = {_q(target)} node.dont-reconnect = true node.dont-fallback = true '
           f'node.dont-move = true media.role = "DSP"'
           + (" stream.capture.sink = true" if capture_sink else ""))
    play = (f'node.name = "amx_fx_{tag}" node.description = {_q(desc + " (effects)")} '
            f'media.class = Audio/Source' + play_fmt)
    return f"""# generated by Audio Mixer - do not edit
context.properties = {{ log.level = 1 }}
context.spa-libs = {{
  audio.convert.* = audioconvert/libspa-audioconvert
  support.*       = support/libspa-support
}}
context.modules = [
  {{ name = libpipewire-module-rt args = {{ nice.level = -11 }} flags = [ ifexists nofail ] }}
  {{ name = libpipewire-module-protocol-native }}
  {{ name = libpipewire-module-client-node }}
  {{ name = libpipewire-module-adapter }}
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = {_q(desc + " effects")}
      filter.graph = {{
        nodes = [
{chr(10).join(lines)}
        ]
        links = [
{chr(10).join(links)}
        ]
        inputs = {inputs}
        outputs = [ "{last}:Out L" "{last}:Out R" ]
      }}
      {chans}
      capture.props = {{ {cap} }}
      playback.props = {{ {play} }}
    }}
  }}
]
"""


# --------------------------------------------------------------------------- process host
try:                       # resolved here, not in the forked child (no dlopen after fork)
    _prctl = ctypes.CDLL(None).prctl
except (OSError, AttributeError):
    _prctl = None


def _pdeathsig():
    """Child side of fork: get SIGTERM when the mixer dies, so no chain outlives it."""
    if _prctl is not None:
        _prctl(1, int(signal.SIGTERM))      # PR_SET_PDEATHSIG


def _orphan(pid):
    """True unless the process's parent is a running Audio Mixer (never kill a live mixer's chains)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            ppid = int(f.read().rsplit(b")", 1)[1].split()[1])
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            parent = f.read()
    except (OSError, ValueError, IndexError):
        return True
    return ppid <= 1 or b"audio_mixer" not in parent


class FxHost:
    """Runs one filter-chain process per active effect chain."""

    def __init__(self):
        self.procs = {}       # fid -> {"proc", "shape", "target", "tag", "conf", "sent", "t"}
        self.old = []         # replaced chains, still playing until the mixer has switched over
        self.gen = 0          # generation counter: every (re)start gets fresh node names
        self.fails = {}       # fid -> (shape, consecutive crashes)
        self.ok, self.msg = ensure_built()
        self.cleanup_stale()

    @staticmethod
    def cleanup_stale():
        """Kill chains left running by an earlier mixer that crashed."""
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            if len(argv) >= 3 and os.path.basename(argv[0]) == b"pipewire" and \
                    RUN_DIR.encode() + b"/fx-" in argv[2] and _orphan(pid):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass

    def running(self, fid):
        p = self.procs.get(fid)
        return bool(p) and p["proc"].poll() is None

    def sync(self, wanted, sources=None):
        """wanted: fid -> {"nodes", "shape", "target", "capture_sink", "desc"}.
        sources: the server's current source names. A chain that is running but whose source is
        missing (e.g. PipeWire restarted under it: the process lives on, disconnected) is restarted.
        Starts / restarts / stops processes. Returns the fids that were (re)started.
        A chain being replaced is not stopped here: it keeps playing (in self.old) until the
        mixer has switched its links over to the new one and calls reap()."""
        started = []
        now = time.time()
        for fid in list(self.procs):
            p, w = self.procs[fid], wanted.get(fid)
            published = sources is not None and f"amx_fx_{p['tag']}" in sources
            lost = sources is not None and not published and now - p["t"] > 4
            if published and now - p["t"] > 30:
                self.fails.pop(fid, None)             # healthy for a while: forget old failures
            if p["proc"].poll() is not None or lost:  # crashed / cut off: count it against this shape
                shp, n = self.fails.get(fid, (None, 0))
                self.fails[fid] = (p["shape"], n + 1 if shp == p["shape"] else 1)
                self._stop(fid)
            elif w is None or w["shape"] != p["shape"] or w["target"] != p["target"]:
                self.old.append(self.procs.pop(fid))
        if not self.ok:
            return started
        for fid, w in wanted.items():
            if fid in self.procs:
                continue
            shp, n = self.fails.get(fid, (None, 0))
            if shp == w["shape"] and n >= 3:          # keeps crashing: wait for a different chain
                continue
            self.gen += 1
            tag = f"{fid}-{self.gen}"
            os.makedirs(RUN_DIR, exist_ok=True)
            conf = os.path.join(RUN_DIR, f"fx-{tag}.conf")
            with open(conf, "w") as f:
                f.write(make_conf(tag, w["nodes"], w["target"], w["capture_sink"], w["desc"],
                                  w.get("mono", False)))
            try:
                proc = subprocess.Popen(["pipewire", "-c", conf], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        preexec_fn=_pdeathsig)
            except OSError:
                continue
            self.procs[fid] = {"proc": proc, "shape": w["shape"], "target": w["target"], "tag": tag,
                               "conf": conf,
                               "sent": {n + ":" + k: v for n, _l, c in w["nodes"] for k, v in c.items()},
                               "t": time.time()}
            started.append(fid)
        return started

    def source(self, fid):
        """Name of the source a running chain publishes."""
        p = self.procs.get(fid)
        return f"amx_fx_{p['tag']}" if p else None

    def capture_name(self, fid):
        p = self.procs.get(fid)
        return f"amx_fxin_{p['tag']}" if p else None

    def reap(self, in_use):
        """Stop replaced chains whose source no longer feeds anything (in_use: source names)."""
        keep = []
        for p in self.old:
            if f"amx_fx_{p['tag']}" in in_use and p["proc"].poll() is None:
                keep.append(p)
            else:
                self._kill(p)
        self.old = keep

    @staticmethod
    def _kill(p):
        try:
            p["proc"].terminate()
            p["proc"].wait(timeout=2)
        except subprocess.TimeoutExpired:
            p["proc"].kill()
            p["proc"].wait()
        except OSError:
            pass
        try:
            os.unlink(p["conf"])
        except (OSError, KeyError):
            pass

    def _stop(self, fid):
        p = self.procs.pop(fid, None)
        if p:
            self._kill(p)

    def stop_all(self):
        for fid in list(self.procs):
            self._stop(fid)
        self.reap(set())

    def push(self, fid, ctrls, node_id):
        """Send changed control values to a running chain."""
        p = self.procs.get(fid)
        if not p or node_id is None:
            return
        diff = {k: v for k, v in ctrls.items() if p["sent"].get(k) != v}
        if not diff:
            return
        body = " ".join(f"{json.dumps(k)} {v:.6g}" for k, v in diff.items())
        try:
            r = subprocess.run(["pw-cli", "set-param", str(node_id), "Props", f"{{ params = [ {body} ] }}"],
                               capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return
        if r.returncode == 0:
            p["sent"].update(diff)


# --------------------------------------------------------------------------- shared widgets
def fmt(p, v):
    if p.get("kind") == "toggle":
        return "On" if v >= 0.5 else "Off"
    if p.get("kind") == "choice":
        return p["choices"][min(max(int(round(v)), 0), len(p["choices"]) - 1)]
    d = p.get("digits", 0)
    s = f"{v:.{d}f}" if d else f"{round(v)}"
    if p["unit"] == "Hz" and v >= 1000:
        s = f"{v / 1000:.2f} k"
    u = p["unit"]
    return f"{s} {u}".strip() if u not in (":1", "×") else f"{s}{u}"


def param_row(p, value, on_change, compact=False):
    """A labelled slider (or switch) for one parameter."""
    if p.get("kind") == "toggle":
        row = Adw.SwitchRow(title=p["label"], active=value >= 0.5)
        row.connect("notify::active", lambda r, _x: on_change(1.0 if r.get_active() else 0.0))
        return row
    if p.get("kind") == "choice":
        row = Adw.ComboRow(title=p["label"], model=Gtk.StringList.new(p["choices"]))
        row.set_selected(min(max(int(round(value)), 0), len(p["choices"]) - 1))
        row.connect("notify::selected", lambda r, _x: on_change(float(r.get_selected())))
        return row
    row = Adw.ActionRow(title=p["label"])
    lo, hi = p["lo"], p["hi"]
    log = p["unit"] == "Hz" and lo > 0 and hi / lo > 20
    step = 1 if p.get("kind") == "int" or not p.get("digits") else 10 ** -p["digits"]
    if log:
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.001)
        sc.set_value(math.log(max(lo, value) / lo) / math.log(hi / lo))
    else:
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        sc.set_value(value)
    sc.set_draw_value(False)
    sc.add_css_class("plain")
    sc.set_size_request(150 if compact else 230, -1)
    sc.set_valign(Gtk.Align.CENTER)
    lab = Gtk.Label(label=fmt(p, value), width_chars=9, xalign=1, css_classes=["sub", "numeric"])

    def changed(s):
        v = s.get_value()
        if log:
            v = lo * (hi / lo) ** v
            v = round(v) if v >= 100 else round(v, 1)
        elif p.get("kind") == "int":
            v = round(v)
        else:
            v = round(v, p.get("digits", 0) or 0)
        lab.set_label(fmt(p, v))
        on_change(float(v))
    sc.connect("value-changed", changed)
    # double-click the value to reset
    g = Gtk.GestureClick()
    g.connect("pressed", lambda _g, n, *_: n == 2 and sc.set_value(
        math.log(p["def"] / lo) / math.log(hi / lo) if log else p["def"]))
    lab.add_controller(g)
    lab.set_tooltip_text("Double-click to reset")
    row.add_suffix(sc)
    row.add_suffix(lab)
    return row


# --------------------------------------------------------------------------- EQ curve
def _bq_coefs(t, f, g, q, sr=48000.0):
    import math as m
    if t == 0:
        return None
    f = min(max(f, 5.0), sr * 0.49)
    q = max(q, 0.05)
    A = 10 ** (g / 40)
    w = 2 * m.pi * f / sr
    cw, sw = m.cos(w), m.sin(w)
    al = sw / (2 * q)
    if t == 1:
        b = (1 + al * A, -2 * cw, 1 - al * A); a = (1 + al / A, -2 * cw, 1 - al / A)
    elif t in (2, 3):
        sA = 2 * m.sqrt(A) * al
        if t == 2:
            b = (A * ((A + 1) - (A - 1) * cw + sA), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sA))
            a = ((A + 1) + (A - 1) * cw + sA, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sA)
        else:
            b = (A * ((A + 1) + (A - 1) * cw + sA), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sA))
            a = ((A + 1) - (A - 1) * cw + sA, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sA)
    elif t == 4:
        b = ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2); a = (1 + al, -2 * cw, 1 - al)
    elif t == 5:
        b = ((1 - cw) / 2, 1 - cw, (1 - cw) / 2); a = (1 + al, -2 * cw, 1 - al)
    else:
        b = (1, -2 * cw, 1); a = (1 + al, -2 * cw, 1 - al)
    return b, a


def eq_response(bands, freqs, out_db=0.0, sr=48000.0):
    cs = [c for c in (_bq_coefs(int(b["type"]), b["freq"], b["gain"], b["q"], sr) for b in bands) if c]
    res = []
    for f in freqs:
        z = cmath.exp(-1j * 2 * math.pi * f / sr)
        h = 1.0
        for b, a in cs:
            h *= (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
        res.append(20 * math.log10(max(abs(h), 1e-6)) + out_db)
    return res


class EqCurve(Gtk.DrawingArea):
    """Response curve with a draggable handle per band. Scroll over a handle to change its Q."""
    F_LO, F_HI, DB = 20.0, 20000.0, 24.0
    COLORS = [(0.21, 0.68, 0.96), (0.45, 0.85, 0.35), (0.98, 0.85, 0.30), (1.0, 0.6, 0.25), (1.0, 0.42, 0.42),
              (1.0, 0.5, 0.75), (0.72, 0.5, 1.0), (0.3, 0.85, 0.85), (0.85, 0.85, 0.85), (0.6, 0.75, 1.0)]

    def __init__(self, eq, on_change, on_select):
        super().__init__(hexpand=True)
        self.set_content_height(210)
        self.eq, self.on_change, self.on_select = eq, on_change, on_select
        self.sel, self.drag = 0, None
        self.set_draw_func(self._draw)
        d = Gtk.GestureDrag()
        d.connect("drag-begin", self._begin)
        d.connect("drag-update", self._update)
        d.connect("drag-end", lambda *_: setattr(self, "drag", None))
        self.add_controller(d)
        s = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        s.connect("scroll", self._scroll)
        self.add_controller(s)

    def fx(self, f, w):
        return w * math.log(f / self.F_LO) / math.log(self.F_HI / self.F_LO)

    def xf(self, x, w):
        return self.F_LO * (self.F_HI / self.F_LO) ** min(1, max(0, x / max(1, w)))

    def gy(self, g, h):
        return h / 2 - g / self.DB * (h / 2 - 8)

    def yg(self, y, h):
        return max(-self.DB, min(self.DB, (h / 2 - y) / (h / 2 - 8) * self.DB))

    def _handle_y(self, b, h):
        return self.gy(b["gain"], h) if b["type"] in (1, 2, 3) else h / 2

    def _begin(self, g, x, y):
        w, h = self.get_width(), self.get_height()
        best, bd = None, 18 ** 2
        for i, b in enumerate(self.eq["bands"]):
            if b["type"] == 0:
                continue
            d = (self.fx(b["freq"], w) - x) ** 2 + (self._handle_y(b, h) - y) ** 2
            if d < bd:
                best, bd = i, d
        if best is not None:
            self.drag = (best, x, y)
            self.sel = best
            self.on_select(best)
            self.queue_draw()

    def _update(self, g, dx, dy):
        if not self.drag:
            return
        i, x0, y0 = self.drag
        w, h = self.get_width(), self.get_height()
        b = self.eq["bands"][i]
        b["freq"] = round(self.xf(x0 + dx, w), 1)
        if b["type"] in (1, 2, 3):
            b["gain"] = round(self.yg(y0 + dy, h), 1)
        self.on_change(i)
        self.queue_draw()

    def _scroll(self, _c, _dx, dy):
        b = self.eq["bands"][self.sel]
        b["q"] = round(max(0.1, min(18, b["q"] * (0.9 if dy > 0 else 1.1))), 2)
        self.on_change(self.sel)
        self.queue_draw()
        return True

    def _draw(self, _a, cr, w, h):
        cr.set_source_rgb(0.12, 0.12, 0.13)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.07)
        for f in (50, 100, 200, 500, 1000, 2000, 5000, 10000):
            x = self.fx(f, w)
            cr.move_to(x, 0)
            cr.line_to(x, h)
        for g in (-18, -12, -6, 6, 12, 18):
            y = self.gy(g, h)
            cr.move_to(0, y)
            cr.line_to(w, y)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.18)
        cr.move_to(0, h / 2)
        cr.line_to(w, h / 2)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_font_size(9)
        for f, t in ((100, "100"), (1000, "1k"), (10000, "10k")):
            cr.move_to(self.fx(f, w) + 3, h - 4)
            cr.show_text(t)
        n = max(60, int(w / 3))
        freqs = [self.xf(i * w / (n - 1), w) for i in range(n)]
        resp = eq_response(self.eq["bands"], freqs, self.eq.get("Output", 0.0))
        cr.move_to(0, self.gy(resp[0], h))
        for i, r in enumerate(resp):
            cr.line_to(i * w / (n - 1), self.gy(max(-self.DB, min(self.DB, r)), h))
        path = cr.copy_path()
        cr.set_source_rgb(0.21, 0.68, 0.96)
        cr.set_line_width(2)
        cr.stroke()
        cr.append_path(path)
        cr.line_to(w, h / 2)
        cr.line_to(0, h / 2)
        cr.close_path()
        cr.set_source_rgba(0.21, 0.68, 0.96, 0.12)
        cr.fill()
        for i, b in enumerate(self.eq["bands"]):
            if b["type"] == 0:
                continue
            x, y = self.fx(b["freq"], w), self._handle_y(b, h)
            col = self.COLORS[i % len(self.COLORS)]
            cr.arc(x, y, 8 if i == self.sel else 6, 0, 2 * math.pi)
            cr.set_source_rgb(*col)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.set_line_width(1.5)
            cr.stroke()
            cr.set_source_rgb(0.05, 0.05, 0.05)
            cr.set_font_size(8)
            cr.move_to(x - (5 if i >= 9 else 2.5), y + 3)
            cr.show_text(str(i + 1))


# --------------------------------------------------------------------------- editors
def _clear(box):
    while (c := box.get_first_child()):
        box.remove(c)


def effects_editor(obj, changed):
    """The standard chain for a channel or mix: one expander per effect.
    `changed()` is called after every edit (the caller applies it)."""
    fx = fx_of(obj)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_end=6)
    sw.set_child(box)
    outer.append(sw)
    group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    box.append(group)
    for s in offered(obj):
        m = fx[s["key"]]
        exp = Adw.ExpanderRow(title=s["title"], subtitle=s["sub"], show_enable_switch=True,
                              enable_expansion=bool(m["on"]), expanded=False)

        def toggled(e, _p, m=m):
            if bool(m["on"]) != e.get_enable_expansion():
                m["on"] = e.get_enable_expansion()
                changed()
        exp.connect("notify::enable-expansion", toggled)
        if s["key"] == "eq":
            exp.add_row(_eq_editor(m, changed))
        for p in s["params"]:
            def setp(v, m=m, k=p["port"]):
                m[k] = v
                changed()
            exp.add_row(param_row(p, float(m.get(p["port"], p["def"])), setp))
        if s["key"] == "denoise":
            exp.add_row(Adw.ActionRow(title="No settings", subtitle="Adds 10 ms of delay while on"))
        group.append(exp)
    if kind_of(obj) == "m":
        box.append(Gtk.Label(label="These effects apply to this mix's device outputs (not its virtual "
                                   "microphone).", xalign=0, wrap=True, css_classes=["sub"]))
    return outer


def _eq_editor(m, changed):
    wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                   margin_start=12, margin_end=12)
    modes = Gtk.Box(css_classes=["linked"], halign=Gtk.Align.START)
    b_simple = Gtk.ToggleButton(label="Simple")
    b_adv = Gtk.ToggleButton(label="Advanced (10-band)", group=b_simple)
    modes.append(b_simple)
    modes.append(b_adv)
    wrap.append(modes)
    stack = Gtk.Stack(vhomogeneous=False)
    wrap.append(stack)

    simple = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    for p in EQ3:
        def setp(v, k=p["port"]):
            m[k] = v
            changed()
        simple.append(param_row(p, float(m.get(p["port"], 0)), setp))
    stack.add_named(simple, "simple")

    adv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    band_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    state = {"sel": 0, "building": False}

    def band_changed(i):
        changed()
        if i == state["sel"] and not state["building"]:
            GLib.idle_add(lambda: (show_band(i, keep_curve=True), False)[1])

    curve = EqCurve(m, band_changed, lambda i: show_band(i))
    top = Gtk.Box(spacing=8)
    presets = Gtk.DropDown.new_from_strings(["Presets…"] + list(EQ_PRESETS))

    def preset(d, _p):
        i = d.get_selected()
        if i > 0:
            name = list(EQ_PRESETS)[i - 1]
            m["bands"] = copy.deepcopy(EQ_PRESETS[name])
            curve.eq = m
            changed()
            curve.queue_draw()
            show_band(state["sel"])
            d.set_selected(0)
    presets.connect("notify::selected", preset)
    top.append(Gtk.Label(label="Drag a dot to move a band, scroll on it to change its width.",
                         xalign=0, hexpand=True, wrap=True, css_classes=["sub"]))
    top.append(presets)
    adv.append(top)
    frame = Gtk.Frame()
    frame.set_child(curve)
    adv.append(frame)
    sel_row = Gtk.Box(spacing=4)
    btns = []
    for i in range(PEQ_N):
        b = Gtk.ToggleButton(label=str(i + 1), css_classes=["flat"])
        if btns:
            b.set_group(btns[0])
        b.connect("toggled", lambda bb, i=i: bb.get_active() and state["sel"] != i and show_band(i))
        btns.append(b)
        sel_row.append(b)
    adv.append(sel_row)
    adv.append(band_box)
    out_p = P("Output", "Output gain", -24, 24, 0, "dB", 1)

    def set_out(v):
        m["Output"] = v
        changed()
        curve.queue_draw()
    out_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    out_list.append(param_row(out_p, float(m.get("Output", 0)), set_out))
    adv.append(out_list)
    stack.add_named(adv, "advanced")

    def show_band(i, keep_curve=False):
        state["sel"] = i
        state["building"] = True
        curve.sel = i
        if not btns[i].get_active():
            btns[i].set_active(True)
        _clear(band_box)
        b = m["bands"][i]
        tr = Adw.ComboRow(title=f"Band {i + 1}", model=Gtk.StringList.new(PEQ_TYPES), selected=int(b["type"]))

        def set_type(r, _p):
            b["type"] = r.get_selected()
            changed()
            curve.queue_draw()
            show_band(i)
        tr.connect("notify::selected", set_type)
        band_box.append(tr)
        for key, p in (("freq", P("f", "Frequency", 20, 20000, 1000, "Hz")),
                       ("gain", P("g", "Gain", -24, 24, 0, "dB", 1)),
                       ("q", P("q", "Q (width)", 0.1, 18, 1, "", 2))):
            if key == "gain" and b["type"] not in (1, 2, 3):
                continue

            def setv(v, key=key):
                b[key] = v
                changed()
                curve.queue_draw()
            band_box.append(param_row(p, float(b[key]), setv))
        state["building"] = False
        if not keep_curve:
            curve.queue_draw()

    def set_mode(adv_on):
        mode = "advanced" if adv_on else "simple"
        stack.set_visible_child_name(mode)
        if m.get("mode") != mode:
            m["mode"] = mode
            changed()
    b_simple.connect("toggled", lambda b: set_mode(not b.get_active()))
    if m.get("mode") == "advanced":
        b_adv.set_active(True)
        stack.set_visible_child_name("advanced")
    else:
        b_simple.set_active(True)
        stack.set_visible_child_name("simple")
    show_band(0)
    return wrap


# ---- Creative Rack
def load_presets():
    try:
        with open(PRESETS) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_presets(d):
    os.makedirs(os.path.dirname(PRESETS), exist_ok=True)
    with open(PRESETS + ".tmp", "w") as f:
        json.dump(d, f, indent=2)
    os.replace(PRESETS + ".tmp", PRESETS)


RACK_CSS = b"""
.rack-unit { background: linear-gradient(180deg, #2e2e33, #232327); border-radius: 10px;
  border: 1px solid #3a3a40; padding: 8px 12px 10px 8px; }
.rack-unit.off { opacity: .55; }
.rack-grip { opacity: .5; }
.rack-grip:hover { opacity: 1; }
.rack-title { font-weight: 700; letter-spacing: 1px; }
.rack-cat { font-size: 9px; letter-spacing: 2px; opacity: .5; }
.rack-drop { box-shadow: 0 -3px 0 #35aef5; }
"""


def rack_editor(obj, changed, structural, toast=None, parent=None):
    """The Creative Rack: an ordered list of effect units. `structural()` is called when the
    unit list or order changes (the chain is rebuilt), `changed()` on knob moves."""
    fx = fx_of(obj)
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    bar = Gtk.Box(spacing=8)
    root.append(bar)
    add = Gtk.MenuButton(css_classes=["suggested-action"])
    add.set_child(Gtk.Label(label="+ Add effect"))
    bar.append(add)
    bar.append(Gtk.Box(hexpand=True))
    pre = Gtk.MenuButton(label="Presets", css_classes=["flat"])
    bar.append(pre)
    sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
    units = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_end=6)
    sw.set_child(units)
    root.append(sw)

    pop = Gtk.Popover()
    pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=8, margin_bottom=8,
                   margin_start=8, margin_end=8)
    for cat in RACK_CATS:
        pbox.append(Gtk.Label(label=cat.upper(), xalign=0, css_classes=["rack-cat"], margin_top=6))
        for t, d in RACK.items():
            if d["cat"] != cat:
                continue
            b = Gtk.Button(label=d["title"], css_classes=["flat"])
            b.get_child().set_halign(Gtk.Align.START)

            def do_add(_b, t=t):
                pop.popdown()
                fx["rack"].append({"uid": os.urandom(3).hex(), "type": t, "on": True,
                                   "p": {p["port"]: p["def"] for p in RACK[t]["params"]}})
                structural()
                rebuild()
            b.connect("clicked", do_add)
            pbox.append(b)
    psw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                             max_content_height=520)
    psw.set_child(pbox)
    pop.set_child(psw)
    add.set_popover(pop)

    ppop = Gtk.Popover()
    ppbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8, margin_bottom=8,
                    margin_start=8, margin_end=8, width_request=260)
    ppop.set_child(ppbox)
    pre.set_popover(ppop)

    def fill_presets(*_):
        _clear(ppbox)
        er = Gtk.Box(spacing=6)
        ent = Gtk.Entry(placeholder_text="Save rack as…", hexpand=True)
        sv = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Save this rack as a preset")

        def save(*_):
            n = ent.get_text().strip()
            if not n:
                return
            d = load_presets()
            d[n] = copy.deepcopy(fx["rack"])
            save_presets(d)
            ppop.popdown()
            toast and toast(f"Saved preset “{n}”")
        sv.connect("clicked", save)
        ent.connect("activate", save)
        er.append(ent)
        er.append(sv)
        ppbox.append(er)
        d = load_presets()
        if d:
            ppbox.append(Gtk.Label(label="LOAD", xalign=0, css_classes=["rack-cat"], margin_top=6))
        for n in sorted(d, key=str.lower):
            r = Gtk.Box(spacing=4)
            b = Gtk.Button(label=n, css_classes=["flat"], hexpand=True)
            b.get_child().set_halign(Gtk.Align.START)

            def load(_b, n=n):
                rack = copy.deepcopy(load_presets().get(n) or [])
                rack = [u for u in rack if isinstance(u, dict)] if isinstance(rack, list) else []
                for u in rack:
                    u["uid"] = os.urandom(3).hex()
                fx["rack"] = rack
                fx_of(obj)
                ppop.popdown()
                structural()
                rebuild()
            b.connect("clicked", load)
            rm = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], tooltip_text="Delete preset")

            def delete(_b, n=n):
                dd = load_presets()
                dd.pop(n, None)
                save_presets(dd)
                fill_presets()
            rm.connect("clicked", delete)
            r.append(b)
            r.append(rm)
            ppbox.append(r)
        ppbox.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        io = Gtk.Box(spacing=6, homogeneous=True)
        imp = Gtk.Button(label="Import…", css_classes=["flat"])
        exp = Gtk.Button(label="Export…", css_classes=["flat"])
        imp.connect("clicked", lambda *_: (ppop.popdown(), _import_presets(parent or root, toast)))
        exp.connect("clicked", lambda *_: (ppop.popdown(), _export_presets(parent or root, toast)))
        io.append(imp)
        io.append(exp)
        ppbox.append(io)
    ppop.connect("show", fill_presets)

    def move(uid, before_uid):
        rack = fx["rack"]
        u = next((x for x in rack if x["uid"] == uid), None)
        if u is None or uid == before_uid:
            return
        rack.remove(u)
        idx = next((i for i, x in enumerate(rack) if x["uid"] == before_uid), len(rack))
        rack.insert(idx, u)
        structural()
        rebuild()

    def unit(u):
        d = RACK[u["type"]]
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["rack-unit"])
        if not u["on"]:
            card.add_css_class("off")
        head = Gtk.Box(spacing=8)
        grip = Gtk.Label(label="⋮⋮", css_classes=["rack-grip"], tooltip_text="Drag to reorder")
        grip.set_cursor_from_name("grab")
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(u["uid"]))
        grip.add_controller(src)
        head.append(grip)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        tb.append(Gtk.Label(label=d["cat"].upper(), xalign=0, css_classes=["rack-cat"]))
        tb.append(Gtk.Label(label=d["title"].upper(), xalign=0, css_classes=["rack-title"]))
        head.append(tb)
        reset = Gtk.Button(label="RESET", css_classes=["flat"], valign=Gtk.Align.CENTER,
                           tooltip_text="Restore this unit's knobs")

        def do_reset(_b):
            u["p"] = {p["port"]: p["def"] for p in d["params"]}
            changed()
            rebuild()
        reset.connect("clicked", do_reset)
        head.append(reset)
        pw = Gtk.Switch(active=u["on"], valign=Gtk.Align.CENTER, tooltip_text="Power (I/O)")

        def power(s, _p):
            u["on"] = s.get_active()
            (card.remove_css_class if u["on"] else card.add_css_class)("off")
            changed()
        pw.connect("notify::active", power)
        head.append(pw)
        rm = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                        tooltip_text="Remove from the rack")

        def remove(_b):
            fx["rack"] = [x for x in fx["rack"] if x["uid"] != u["uid"]]
            structural()
            rebuild()
        rm.connect("clicked", remove)
        head.append(rm)
        card.append(head)
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for p in d["params"]:
            def setp(v, k=p["port"]):
                u["p"][k] = v
                changed()
            lst.append(param_row(p, float(u["p"].get(p["port"], p["def"])), setp, compact=True))
        card.append(lst)
        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        tgt.connect("enter", lambda *_: (card.add_css_class("rack-drop"), Gdk.DragAction.MOVE)[1])
        tgt.connect("leave", lambda *_: card.remove_css_class("rack-drop"))
        tgt.connect("drop", lambda _t, v, *_: (card.remove_css_class("rack-drop"),
                                                GLib.idle_add(lambda: (move(v, u["uid"]), False)[1]), True)[2])
        card.add_controller(tgt)
        return card

    def rebuild():
        _clear(units)
        if not fx["rack"]:
            units.append(Gtk.Label(label="The rack is empty.\nAdd effects with “+ Add effect”; drag a unit "
                                         "by its grip to change the order the audio goes through them.",
                                   css_classes=["sub"], margin_top=30, justify=Gtk.Justification.CENTER))
            return
        for u in fx["rack"]:
            units.append(unit(u))
        end = Gtk.Box(height_request=30)             # drop here = move to the end
        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        tgt.connect("drop", lambda _t, v, *_: (GLib.idle_add(lambda: (move(v, None), False)[1]), True)[1])
        end.add_controller(tgt)
        units.append(end)
    rebuild()
    return root


def _file_dialog(parent, title, save, done, name=None):
    dlg = Gtk.FileDialog(title=title)
    if name:
        dlg.set_initial_name(name)
    f = Gtk.FileFilter()
    f.set_name("JSON")
    f.add_pattern("*.json")
    from gi.repository import Gio
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(f)
    dlg.set_filters(filters)
    win = parent.get_root() if parent is not None else None

    def fin(d, res):
        try:
            fl = d.save_finish(res) if save else d.open_finish(res)
        except GLib.Error:
            return
        if fl and fl.get_path():
            done(fl.get_path())
    (dlg.save if save else dlg.open)(win, None, fin)


def _import_presets(parent, toast):
    def done(path):
        try:
            with open(path) as f:
                new = json.load(f)
            if not isinstance(new, dict):
                raise ValueError
        except (OSError, ValueError):
            toast and toast("That file isn't a rack preset export")
            return
        d = load_presets()
        d.update(new)
        save_presets(d)
        toast and toast(f"Imported {len(new)} preset{'s' if len(new) != 1 else ''}")
    _file_dialog(parent, "Import rack presets", False, done)


def _export_presets(parent, toast):
    def done(path):
        try:
            with open(path, "w") as f:
                json.dump(load_presets(), f, indent=2)
        except OSError as e:
            toast and toast(f"Couldn't export: {e.strerror}")
            return
        toast and toast("Presets exported")
    _file_dialog(parent, "Export rack presets", True, done, "rack-presets.json")
