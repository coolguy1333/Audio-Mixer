"""The soundboard's sounds: storage, duplicate check, decoding and waveforms. No audio devices, no UI.

Config and sounds live in %APPDATA%\\AudioMixerSoundboard (Windows) or ~/.config/audio-mixer-soundboard;
set AMXSB_HOME to use another folder (tests do).
"""
import hashlib
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import threading
import zipfile

import numpy as np

try:
    import miniaudio
except ImportError:                     # ffmpeg alone still works
    miniaudio = None

RATE = 48000
PEAK_RES = 1000
MAX_SECONDS = 20 * 60                   # longer files are refused: this is a soundboard
AUDIO_EXT = (".wav", ".mp3", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".webm", ".weba", ".mp4", ".wma")


def data_dirs():
    home = os.environ.get("AMXSB_HOME")
    if home:
        return home, os.path.join(home, "cache")
    if sys.platform == "win32":
        roam = os.environ.get("APPDATA") or os.path.expanduser("~")
        local = os.environ.get("LOCALAPPDATA") or roam
        return os.path.join(roam, "AudioMixerSoundboard"), os.path.join(local, "AudioMixerSoundboard", "cache")
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cfg, "audio-mixer-soundboard"), os.path.join(cache, "audio-mixer-soundboard")


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name):
    keep = "".join(c if c.isalnum() or c in " -_.()!?',&" else "_" for c in name).strip(" ._")
    return keep[:60] or "sound"


def ffmpeg_path():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    for cand in (os.path.join(here, "ffmpeg.exe"), os.path.join(here, "ffmpeg")):
        if os.path.exists(cand):
            return cand
    return None


class DecodeError(Exception):
    pass


def decode(path):
    """The whole file as float32 stereo at RATE, shape (frames, 2)."""
    err = None
    if miniaudio is not None:
        try:
            d = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=2,
                                      sample_rate=RATE)
            a = np.frombuffer(d.samples, dtype=np.float32).reshape(-1, 2)
            if len(a):
                return a.copy()
        except Exception as e:          # unsupported format (m4a, opus, webm, …): try ffmpeg
            err = e
    ff = ffmpeg_path()
    if ff:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            r = subprocess.run([ff, "-nostdin", "-v", "error", "-i", path, "-t", str(MAX_SECONDS), "-f", "f32le",
                                "-ac", "2", "-ar", str(RATE), "pipe:1"], capture_output=True, timeout=180,
                               creationflags=flags)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DecodeError(f"ffmpeg failed: {e}") from None
        if r.returncode == 0 and r.stdout:
            return np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2).copy()
        raise DecodeError(r.stderr.decode("utf-8", "replace").strip()[-200:] or "ffmpeg couldn't read it")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".m4a", ".aac", ".opus", ".webm", ".weba", ".mp4", ".wma"):
        raise DecodeError(f"{ext} files need ffmpeg (install it and restart)")
    raise DecodeError(str(err) if err else "unsupported file")


class Library:
    """Sounds on the board + settings, saved as JSON. Thread-safe for the HTTP server's threads."""

    DEFAULTS = {"volume": 100, "output": None, "monitor": None, "monitor_volume": 100, "mic": None,
                "mic_volume": 100, "web_sounds": False, "web_source": 0, "freesound_key": "",
                "lan": False, "port": 8788, "stop_key": None}

    def __init__(self):
        self.home, self.cache = data_dirs()
        self.sounds_dir = os.path.join(self.home, "sounds")
        self.conf_path = os.path.join(self.home, "config.json")
        os.makedirs(self.sounds_dir, exist_ok=True)
        os.makedirs(self.cache, exist_ok=True)
        self.lock = threading.RLock()
        self.problem = None      # set by _load when the settings file was damaged
        self.cfg = self._load()
        self._pcm = {}           # sid -> (key, array)
        self._peaks = {}         # sid -> (key, list)
        self.skipped = []

    # ---- config
    def _load(self):
        cfg = {}
        try:
            with open(self.conf_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            cfg = {}
        except (OSError, ValueError) as e:
            keep = self.conf_path + ".broken"
            try:
                os.replace(self.conf_path, keep)
            except OSError:
                pass
            self.problem = f"Settings couldn't be read ({e}); started fresh. The old file is {keep}."
            cfg = {}
        for k, v in self.DEFAULTS.items():
            cfg.setdefault(k, v)
        cfg.setdefault("sounds", [])
        cfg.setdefault("token", secrets.token_urlsafe(16))
        for s in cfg["sounds"]:
            s.setdefault("start", 0.0)
            s.setdefault("end", None)
            s.setdefault("vol", 100)
            s.setdefault("hotkey", None)
            s.setdefault("color", None)
        return cfg

    def save(self):
        with self.lock:
            tmp = self.conf_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            os.replace(tmp, self.conf_path)

    @property
    def sounds(self):
        return self.cfg["sounds"]

    def sound(self, sid):
        sid = str(sid)
        with self.lock:
            return next((s for s in self.sounds if s["id"] == sid or s["name"].lower() == sid.lower()), None)

    # ---- adding / removing
    def new_id(self):
        used = {s["id"] for s in self.sounds}
        while True:
            i = str(random.randint(1000, 9999))
            if i not in used:
                return i

    def digest(self, s):
        if not s.get("sha1"):
            try:
                s["sha1"] = sha1_file(s["file"])
            except OSError:
                return None
        return s["sha1"]

    def has_src(self, url):
        return any(s.get("src") == url for s in self.sounds)

    def add_file(self, path, name=None, src=None):
        """Copy one file onto the board. Returns the new sound, or None when that audio is already there
        (its name goes to self.skipped). Raises DecodeError when it isn't playable audio."""
        with self.lock:
            dig = sha1_file(path)
            if dig in {self.digest(s) for s in self.sounds} or (src and self.has_src(src)):
                self.skipped.append(name or os.path.splitext(os.path.basename(path))[0])
                return None
            pcm = decode(path)                       # refuse files we can't play before copying them
            if len(pcm) > MAX_SECONDS * RATE:
                raise DecodeError("longer than 20 minutes")
            ext = os.path.splitext(path)[1].lower() or ".mp3"
            nm = (name or os.path.splitext(os.path.basename(path))[0]).strip() or "Sound"
            base = safe_name(nm)
            dest = os.path.join(self.sounds_dir, base + ext)
            k = 1
            while os.path.exists(dest):
                dest = os.path.join(self.sounds_dir, f"{base}-{k}{ext}")
                k += 1
            shutil.copyfile(path, dest)
            s = {"id": self.new_id(), "name": nm[:40], "file": dest, "start": 0.0, "end": None, "vol": 100,
                 "sha1": dig, "hotkey": None, "color": None}
            if src:
                s["src"] = src
            self.sounds.append(s)
            self._pcm[s["id"]] = ((dest, os.path.getmtime(dest)), pcm)
            self.save()
            return s

    def add_bytes(self, data, filename, name=None, src=None):
        tmpdir = os.path.join(self.cache, "upload")
        os.makedirs(tmpdir, exist_ok=True)
        ext = os.path.splitext(filename)[1].lower()
        tmp = os.path.join(tmpdir, safe_name(os.path.splitext(os.path.basename(filename))[0]) + ext)
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            return self.add_file(tmp, name or os.path.splitext(os.path.basename(filename))[0], src)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def update(self, sid, **kw):
        with self.lock:
            s = self.sound(sid)
            if not s:
                return None
            if "name" in kw and str(kw["name"]).strip():
                s["name"] = str(kw["name"]).strip()[:40]
            if "vol" in kw:
                s["vol"] = max(0, min(200, int(kw["vol"])))
            dur = self.duration(s)
            if "start" in kw:
                s["start"] = max(0.0, min(float(kw["start"] or 0), dur or 1e9))
            if "end" in kw:
                e = kw["end"]
                s["end"] = None if e is None or (dur and float(e) >= dur - 1e-3) else max(s["start"], float(e))
            if "hotkey" in kw:
                s["hotkey"] = kw["hotkey"] or None
            if "color" in kw:
                s["color"] = kw["color"] or None
            self.save()
            return s

    def reorder(self, ids):
        with self.lock:
            pos = {sid: i for i, sid in enumerate(ids)}
            self.sounds.sort(key=lambda s: pos.get(s["id"], len(pos)))
            self.save()

    def remove(self, sid):
        with self.lock:
            s = self.sound(sid)
            if not s:
                return False
            self.cfg["sounds"] = [x for x in self.sounds if x["id"] != s["id"]]
            self._pcm.pop(s["id"], None)
            self._peaks.pop(s["id"], None)
            if os.path.dirname(os.path.abspath(s["file"])) == os.path.abspath(self.sounds_dir):
                try:
                    os.unlink(s["file"])
                except OSError:
                    pass
            self.save()
            return True

    # ---- audio data
    def pcm(self, s):
        """Decoded audio, kept in memory (clips are short). None if the file is gone or unreadable."""
        try:
            key = (s["file"], os.path.getmtime(s["file"]))
        except OSError:
            return None
        hit = self._pcm.get(s["id"])
        if hit and hit[0] == key:
            return hit[1]
        try:
            a = decode(s["file"])
        except DecodeError:
            return None
        self._pcm[s["id"]] = (key, a)
        return a

    def duration(self, s):
        a = self.pcm(s)
        return len(a) / RATE if a is not None else 0.0

    def segment(self, s):
        """(audio, first frame, end frame) after trimming."""
        a = self.pcm(s)
        if a is None:
            return None, 0, 0
        n = len(a)
        st = max(0, min(n, int((s.get("start") or 0) * RATE)))
        en = n if s.get("end") is None else max(st, min(n, int(s["end"] * RATE)))
        return a, st, en

    def peaks(self, s):
        a = self.pcm(s)
        if a is None:
            return []
        hit = self._peaks.get(s["id"])
        if hit and hit[0] is a:
            return hit[1]
        mono = np.abs(a).max(axis=1)
        n = min(PEAK_RES, len(mono)) or 1
        edges = np.linspace(0, len(mono), n + 1).astype(int)
        pk = [float(mono[edges[i]:max(edges[i + 1], edges[i] + 1)].max()) if len(mono) else 0.0 for i in range(n)]
        top = max(pk) if pk else 0
        pk = [round(x / top, 3) if top > 0 else 0 for x in pk]
        self._peaks[s["id"]] = (a, pk)
        return pk

    # ---- backups
    def export_zip(self, path):
        with self.lock:
            b = {"sounds": []}
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for s in self.sounds:
                    arc = f"sounds/{s['id']}-{os.path.basename(s['file'])}"
                    if os.path.exists(s["file"]):
                        z.write(s["file"], arc)
                    e = {k: s.get(k) for k in ("id", "name", "start", "end", "vol", "src", "hotkey", "color")}
                    e["file"] = arc
                    b["sounds"].append(e)
                z.writestr("soundboard.json", json.dumps(b, indent=2))

    def import_zip(self, path):
        """Add the sounds of an export (this app's or the Linux mixer's). Returns how many were added."""
        n = 0
        self.skipped = []
        tmpdir = os.path.join(self.cache, "import")
        os.makedirs(tmpdir, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            b = json.loads(z.read("soundboard.json"))
            names = set(z.namelist())
            for e in b.get("sounds", []):
                arc = str(e.get("file", ""))
                if not arc.startswith("sounds/") or ".." in arc or arc not in names:
                    continue
                tmp = os.path.join(tmpdir, os.path.basename(arc))
                with z.open(arc) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                try:
                    s = self.add_file(tmp, str(e.get("name") or "Sound"), e.get("src") if isinstance(e.get("src"), str)
                                      else None)
                except DecodeError:
                    s = None
                finally:
                    os.unlink(tmp)
                if s:
                    self.update(s["id"], start=e.get("start") or 0, end=e.get("end"), vol=e.get("vol", 100))
                    if e.get("hotkey") and isinstance(e["hotkey"], str):
                        self.update(s["id"], hotkey=e["hotkey"])
                    n += 1
        return n
