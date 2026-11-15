"""Audio engine: mixes playing sounds into the chosen output (e.g. VB-Cable for Discord / OBS), a monitor
output (your headphones), and optionally passes a microphone through into the main output, so the people you
talk to hear your voice and the sounds on one virtual mic."""
import collections
import os
import sys
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except (ImportError, OSError):       # no PortAudio: the app still runs, it just can't play
    sd = None
if os.environ.get("AMXSB_NO_AUDIO"):  # tests
    sd = None

RATE = 48000
BLOCK = 480                           # 10 ms


def _hostapi_ok(d):
    """On Windows list each device once, through WASAPI (full names, low latency)."""
    if sys.platform != "win32" or sd is None:
        return True
    try:
        return "WASAPI" in sd.query_hostapis(d["hostapi"])["name"]
    except Exception:
        return True


def devices(rescan=False):
    """{"outputs": [names], "inputs": [names], "error": str|None}. A rescan restarts PortAudio, which kills
    open streams: only Engine.rescan() may ask for one."""
    if sd is None:
        return {"outputs": [], "inputs": [], "error": "PortAudio isn't available"}
    try:
        if rescan:
            sd._terminate()
            sd._initialize()
        devs = [d for d in sd.query_devices() if _hostapi_ok(d)]
    except Exception as e:
        return {"outputs": [], "inputs": [], "error": str(e)}
    outs = [d["name"] for d in devs if d["max_output_channels"] > 0]
    ins = [d["name"] for d in devs if d["max_input_channels"] > 0]
    return {"outputs": list(dict.fromkeys(outs)), "inputs": list(dict.fromkeys(ins)), "error": None}


def _wasapi():
    """Index of the WASAPI host API, or None (not Windows, or not available)."""
    if sys.platform != "win32" or sd is None:
        return None
    for i, h in enumerate(sd.query_hostapis()):
        if "WASAPI" in h["name"]:
            return i
    return None


def _candidates(name, kind):
    """Ways to open a device, best first: [(device index or None, extra_settings or None)].

    On Windows the first choice is WASAPI with auto_convert, so Windows resamples to the device's rate. The
    fallback is the same device through another host API (MME resamples by itself). WASAPI settings are only
    ever given to a WASAPI device: anything else fails with "Incompatible host API specific stream info"."""
    if sd is None:
        return []
    key = "max_output_channels" if kind == "output" else "max_input_channels"
    default = not name or name == "default"
    devs = sd.query_devices()
    w = _wasapi()
    out = []
    if w is not None:
        try:
            ws = sd.WasapiSettings(auto_convert=True)
        except TypeError:                        # older sounddevice
            ws = None
        if default:
            idx = sd.query_hostapis(w)["default_output_device" if kind == "output" else "default_input_device"]
            if idx >= 0:
                out.append((idx, ws))
        else:
            out += [(i, ws) for i, d in enumerate(devs) if d["hostapi"] == w and d["name"] == name and d[key] > 0]
    if default:
        out.append((None, None))                 # PortAudio's own default (MME on Windows)
    else:
        # other host APIs; MME cuts names to 31 characters
        out += [(i, None) for i, d in enumerate(devs) if d[key] > 0 and d["hostapi"] != w and
                (d["name"] == name or (len(d["name"]) >= 20 and name.startswith(d["name"])))]
    if not out:
        raise LookupError(f"“{name}” isn't connected")
    return out


def _open(name, kind, make):
    """make(device, extra_settings) -> started stream. Tries each candidate until one opens."""
    err = None
    for idx, extra in _candidates(name, kind):
        try:
            return make(idx, extra)
        except Exception as e:                   # PortAudioError, ValueError (bad channel count), …
            err = e
    raise err


class Voice:
    __slots__ = ("sid", "data", "pos", "end", "gain", "start")

    def __init__(self, sid, data, start, end, gain):
        self.sid, self.data, self.pos, self.end, self.gain, self.start = sid, data, start, end, gain, start


class Output:
    """One output stream. Voices are handed over through a queue, so the audio callback never waits on a lock."""

    def __init__(self, name, gain=1.0, mic=None):
        self.name, self.gain, self.mic = name, gain, mic
        self.voices = []                  # owned by the callback
        self.cmds = collections.deque()   # ("add", Voice) | ("stop", sid) | ("stopall", None)
        self.progress = {}                # sid -> [fraction, …], written by the callback
        self.error = None
        self.stream = None
        def make(idx, extra):
            st = sd.OutputStream(samplerate=RATE, channels=2, dtype="float32", blocksize=BLOCK, latency="low",
                                 device=idx, callback=self._cb, extra_settings=extra)
            st.start()
            return st
        self.stream = _open(name, "output", make)

    def _cb(self, out, frames, _time, status):
        while self.cmds:
            op, arg = self.cmds.popleft()
            if op == "add":
                self.voices.append(arg)
            elif op == "stop":
                self.voices = [v for v in self.voices if v.sid != arg]
            else:
                self.voices = []
        out.fill(0)
        prog = {}
        keep = []
        for v in self.voices:
            n = min(frames, v.end - v.pos)
            if n > 0:
                out[:n] += v.data[v.pos:v.pos + n] * v.gain
                v.pos += n
            if v.pos < v.end:
                keep.append(v)
                prog.setdefault(v.sid, []).append((v.pos - v.start) / max(1, v.end - v.start))
        self.voices = keep
        self.progress = prog
        if self.mic is not None:
            self.mic.mix_into(out, frames)
        if self.gain != 1.0:
            out *= self.gain
        np.clip(out, -1.0, 1.0, out=out)

    def add(self, voice):
        self.cmds.append(("add", voice))

    def stop(self, sid=None):
        self.cmds.append(("stop", sid) if sid is not None else ("stopall", None))

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


class MicPassthrough:
    """Captures a microphone into a short buffer that the main output mixes in."""
    MAX_FRAMES = RATE // 10               # keep at most 100 ms: older audio is dropped (clock drift)

    def __init__(self, name, gain=1.0):
        self.gain = gain
        self.buf = collections.deque()
        self.frames = 0
        self.lock = threading.Lock()
        def make(idx, extra):
            info = sd.query_devices(idx if idx is not None else sd.default.device[0])
            ch = 2 if info["max_input_channels"] >= 2 else 1
            st = sd.InputStream(samplerate=RATE, channels=ch, dtype="float32", blocksize=BLOCK, latency="low",
                                device=idx, callback=self._cb, extra_settings=extra)
            st.start()
            return st
        self.stream = _open(name, "input", make)

    def _cb(self, data, frames, _time, status):
        block = np.repeat(data, 2, axis=1) if data.shape[1] == 1 else data[:, :2]
        with self.lock:
            self.buf.append(block.copy())
            self.frames += len(block)
            while self.frames > self.MAX_FRAMES and self.buf:
                self.frames -= len(self.buf.popleft())

    def mix_into(self, out, frames):
        got = 0
        with self.lock:
            while got < frames and self.buf:
                b = self.buf[0]
                n = min(frames - got, len(b))
                out[got:got + n] += b[:n] * self.gain
                got += n
                if n == len(b):
                    self.buf.popleft()
                else:
                    self.buf[0] = b[n:]
                self.frames -= n

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class Engine:
    def __init__(self, lib):
        self.lib = lib
        self.main = self.monitor = self.preview = self.mic = None
        self.errors = {}                  # "output" | "monitor" | "mic" | "preview" -> message
        self.lock = threading.Lock()
        self.apply()

    # ---- streams
    def apply(self):
        """(Re)open streams to match the settings. Safe to call any time."""
        c = self.lib.cfg
        with self.lock:
            for o in (self.main, self.monitor, self.mic):
                if o is not None:
                    o.close()
            self.main = self.monitor = self.mic = None
            self.errors = {}
            if sd is None:
                self.errors["output"] = "Audio isn't available (PortAudio missing)"
                return
            if c.get("mic"):
                try:
                    self.mic = MicPassthrough(c["mic"], c.get("mic_volume", 100) / 100)
                except Exception as e:
                    self.errors["mic"] = f"Microphone: {e}"
            try:
                self.main = Output(c.get("output"), c.get("volume", 100) / 100, self.mic)
            except Exception as e:
                self.errors["output"] = f"Output: {e}"
            mon = c.get("monitor")
            if mon and mon != c.get("output"):
                try:
                    self.monitor = Output(None if mon == "default" else mon, c.get("monitor_volume", 100) / 100)
                except Exception as e:
                    self.errors["monitor"] = f"Monitor: {e}"

    def rescan(self):
        """Pick up devices plugged in since start: close every stream, restart PortAudio, reopen."""
        with self.lock:
            for o in (self.main, self.monitor, self.mic, self.preview):
                if o is not None:
                    o.close()
            self.main = self.monitor = self.mic = self.preview = None
            d = devices(rescan=True)
        self.apply()
        return d

    def set_gains(self):
        c = self.lib.cfg
        if self.main:
            self.main.gain = c.get("volume", 100) / 100
        if self.monitor:
            self.monitor.gain = c.get("monitor_volume", 100) / 100
        if self.mic:
            self.mic.gain = c.get("mic_volume", 100) / 100

    def close(self):
        with self.lock:
            for o in (self.main, self.monitor, self.mic, self.preview):
                if o is not None:
                    o.close()
            self.main = self.monitor = self.mic = self.preview = None

    # ---- playing
    def play(self, sid):
        """Start a sound (layers on top of copies already playing). Returns an error message or None."""
        s = self.lib.sound(sid)
        if not s:
            return "No such sound"
        data, st, en = self.lib.segment(s)
        if data is None:
            return f"Couldn't read “{s['name']}”"
        if en <= st:
            return None
        gain = s.get("vol", 100) / 100
        outs = [o for o in (self.main, self.monitor) if o is not None]
        if not outs:
            return self.errors.get("output") or "No output device"
        for o in outs:
            o.add(Voice(s["id"], data, st, en, gain))
        return None

    def stop(self, sid):
        s = self.lib.sound(sid)
        for o in (self.main, self.monitor):
            if o is not None:
                o.stop(s["id"] if s else str(sid))

    def stop_all(self):
        for o in (self.main, self.monitor, self.preview):
            if o is not None:
                o.stop()

    def toggle(self, sid):
        s = self.lib.sound(sid)
        if s and self.progress().get(s["id"]):
            self.stop(sid)
            return None
        return self.play(sid)

    def progress(self):
        o = self.main or self.monitor
        return dict(o.progress) if o else {}

    # ---- previews of online sounds: your headphones (the monitor device), never the main output
    def preview_file(self, data):
        c = self.lib.cfg
        dev = c.get("monitor") if c.get("monitor") not in (None, "default") else None
        with self.lock:
            if self.preview is None or self.preview.name != dev:
                if self.preview is not None:
                    self.preview.close()
                self.preview = None
                if sd is None:
                    return "Audio isn't available"
                try:
                    self.preview = Output(dev)
                except Exception as e:
                    return f"Preview: {e}"
            self.preview.stop()
            self.preview.add(Voice("preview", data, 0, len(data), 1.0))
        return None

    def preview_playing(self):
        return bool(self.preview and self.preview.progress)

    def stop_preview(self):
        if self.preview:
            self.preview.stop()


def wait_idle(engine, timeout=5.0):
    """Tests: wait until nothing plays."""
    t = time.monotonic() + timeout
    while engine.progress() and time.monotonic() < t:
        time.sleep(0.02)
