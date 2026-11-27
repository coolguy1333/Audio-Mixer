"""Audio Mixer extras: Soundboard, Web Companion, Profiles, MIDI instruments, Settings page."""
import copy
import hashlib
import http.server
import json
import math
import os
import queue
import random
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import zipfile
from array import array

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import amx_sounds as snd  # noqa: E402

CONF_DIR = os.path.join(GLib.get_user_config_dir(), "audio-mixer")
SOUNDS_DIR = os.path.join(CONF_DIR, "sounds")
PCM_CACHE = os.path.join(GLib.get_user_cache_dir(), "audio-mixer", "pcm")   # decoded clips, read on play
RATE = 48000
FRAME = 8              # bytes per frame: float32 stereo
PEAK_RES = 2048        # waveform buckets kept in memory per clip


def _clear(box):
    while (c := box.get_first_child()):
        box.remove(c)


def file_dialog(parent, title, save=False, multiple=False, done=None, name=None, patterns=None):
    dlg = Gtk.FileDialog(title=title)
    if name:
        dlg.set_initial_name(name)
    if patterns:
        f = Gtk.FileFilter()
        f.set_name(patterns[0])
        for p in patterns[1:]:
            f.add_pattern(p)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)
    win = parent.get_root() if parent is not None else None

    def fin(d, res):
        try:
            if save:
                r = d.save_finish(res)
            elif multiple:
                r = d.open_multiple_finish(res)
            else:
                r = d.open_finish(res)
        except GLib.Error:
            return
        if multiple:
            paths = [r.get_item(i).get_path() for i in range(r.get_n_items())]
            done([p for p in paths if p])
        elif r and r.get_path():
            done(r.get_path())
    if save:
        dlg.save(win, None, fin)
    elif multiple:
        dlg.open_multiple(win, None, fin)
    else:
        dlg.open(win, None, fin)


def _write(win, path, fn, ok_msg):
    """Write a file from a dialog callback, reporting failure instead of raising."""
    try:
        with open(path, "w") as f:
            fn(f)
    except OSError as e:
        win.toast(f"Couldn't save: {e.strerror or e}")
        return
    win.toast(ok_msg)


try:                       # resolved before fork (see amx_effects._pdeathsig)
    import ctypes as _ct
    import signal as _sig
    _prctl = _ct.CDLL(None).prctl
except (OSError, AttributeError):
    _prctl = None


def _pdeathsig():
    """Child side of fork: a synth must not outlive the mixer."""
    if _prctl is not None:
        _prctl(1, int(_sig.SIGTERM))


# =========================================================================== soundboard
class Soundboard:
    """Pads that play audio clips into a mixer channel. State lives in cfg["soundboard"]."""
    AUDIO = ["Audio files", "*.wav", "*.mp3", "*.ogg", "*.oga", "*.opus", "*.flac", "*.m4a", "*.aac",
             "*.webm", "*.mp4", "*.mkv"]

    def __init__(self, app):
        self.app = app
        # sid -> {"key", "path", "frames", "peaks"}. The decoded audio itself stays on disk (PCM_CACHE),
        # so memory holds only a small peak array per clip, not the whole clip.
        self._meta = {}
        self._peaks = {}      # (sid, n, buckets) -> normalised list for drawing
        self.playing = {}     # sid -> [(proc, t0, dur)]
        self.skipped = []     # names the last add/import left out because that audio was already on the board
        self._lock = threading.Lock()

    @property
    def board(self):
        b = self.app.eng.cfg.setdefault("soundboard", {})
        b.setdefault("channel", None)
        b.setdefault("volume", 100)
        b.setdefault("sounds", [])
        for s in b["sounds"]:
            s.setdefault("start", 0.0)
            s.setdefault("end", None)
            s.setdefault("vol", 100)
        return b

    def sound(self, sid):
        sid = str(sid)
        return next((s for s in self.board["sounds"] if s["id"] == sid or s["name"].lower() == sid.lower()), None)

    # ---- audio data
    @staticmethod
    def _decode_to(path, dest):
        """Decode `path` to raw float32 stereo at RATE in file `dest`. True on success."""
        if shutil.which("ffmpeg"):
            try:
                r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-f", "f32le",
                                    "-ac", "2", "-ar", str(RATE), dest], timeout=120,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if r.returncode == 0 and os.path.getsize(dest) > 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:                                   # plain 16-bit WAV without ffmpeg
            import wave
            with wave.open(path) as w:
                if w.getsampwidth() != 2:
                    return False
                raw = array("h")
                raw.frombytes(w.readframes(w.getnframes()))
                ch, sr = w.getnchannels(), w.getframerate()
            out = array("f")
            step = sr / RATE
            frames = len(raw) // ch
            i = 0.0
            while int(i) < frames:
                j = int(i) * ch
                left = raw[j] / 32768
                right = raw[j + 1] / 32768 if ch > 1 else left
                out.append(left)
                out.append(right)
                i += step
            with open(dest, "wb") as fh:
                out.tofile(fh)
            return len(out) > 0
        except Exception:
            return False

    @staticmethod
    def _scan_peaks(path, frames):
        """Max |sample| per bucket, PEAK_RES buckets (fewer for very short clips), read in chunks."""
        per = max(1, frames // PEAK_RES)
        n = min(PEAK_RES, frames // per)
        pk = array("f")
        step = max(1, (1 << 18) // (per * FRAME)) * per          # whole buckets per read, ~256 KB
        with open(path, "rb") as fh:
            while len(pk) < n:
                buf = array("f")
                buf.frombytes(fh.read(min(step, (n - len(pk)) * per) * FRAME))
                if not buf:
                    break
                for i in range(0, len(buf), per * 2):
                    seg = buf[i:i + per * 2]
                    pk.append(max(max(seg), -min(seg)))
        return pk

    def _entry(self, s):
        """The decoded clip's cache entry, decoding it on first use. None if it can't be decoded."""
        try:
            st = os.stat(s["file"])
        except OSError:
            return None
        key = (s["file"], st.st_mtime_ns)
        with self._lock:
            hit = self._meta.get(s["id"])
        if hit and hit["key"] == key:
            return hit
        os.makedirs(PCM_CACHE, exist_ok=True)
        stem = hashlib.sha1(os.path.abspath(s["file"]).encode()).hexdigest()[:20]
        base = os.path.join(PCM_CACHE, f"{stem}-{st.st_mtime_ns}")
        pcm, pkf = base + ".f32", base + ".peaks"
        try:
            if not os.path.exists(pcm):
                tmp = f"{pcm}.{os.getpid()}.{threading.get_ident()}.tmp"
                ok = self._decode_to(s["file"], tmp)
                if not ok:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    return None
                os.replace(tmp, pcm)
                for f in os.listdir(PCM_CACHE):        # older decodes of the same file
                    if f.startswith(stem + "-") and not f.startswith(os.path.basename(base)):
                        try:
                            os.unlink(os.path.join(PCM_CACHE, f))
                        except OSError:
                            pass
            frames = os.path.getsize(pcm) // FRAME
            pk = array("f")
            try:
                with open(pkf, "rb") as fh:
                    pk.frombytes(fh.read())
            except OSError:
                pk = self._scan_peaks(pcm, frames)
                with open(pkf + ".tmp", "wb") as fh:
                    pk.tofile(fh)
                os.replace(pkf + ".tmp", pkf)
        except OSError:
            return None
        e = {"key": key, "path": pcm, "frames": frames, "peaks": pk}
        with self._lock:
            self._meta[s["id"]] = e
        return e

    def prune_cache(self):
        """Delete cached decodes of files no longer on the board."""
        keep = {hashlib.sha1(os.path.abspath(s["file"]).encode()).hexdigest()[:20] for s in self.board["sounds"]}
        try:
            names = os.listdir(PCM_CACHE)
        except OSError:
            return
        for f in names:
            if f.split("-", 1)[0] not in keep and not f.endswith(".tmp"):
                try:
                    os.unlink(os.path.join(PCM_CACHE, f))
                except OSError:
                    pass

    def preload(self, sounds, done):
        def work():
            if sounds is self.board["sounds"]:
                self.prune_cache()          # a full preload (startup): drop leftovers
            for s in sounds:
                self._entry(s)
                # one-shot: an idle callback that returns anything truthy is re-run forever
                GLib.idle_add(lambda sid=s["id"]: (done(sid), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def duration(self, s):
        e = self._meta.get(s["id"])
        return e["frames"] / RATE if e else 0.0

    def peaks(self, s, n):
        e = self._meta.get(s["id"])
        if not e or not e["peaks"]:
            return None
        src = e["peaks"]
        m = len(src)
        key = (s["id"], n, m, e["key"])
        if key in self._peaks:
            return self._peaks[key]
        if m <= n:
            out = list(src)
        else:
            out = [max(src[i * m // n:max((i + 1) * m // n, i * m // n + 1)]) for i in range(n)]
        top = max(out)
        out = [x / top if top > 0 else 0 for x in out]
        if len(self._peaks) > 256:
            self._peaks.clear()
        self._peaks[key] = out
        return out

    # ---- playback
    def target(self):
        cid = self.board.get("channel")
        ch = next((c for c in self.app.eng.cfg["channels"] if c["id"] == cid and c["type"] == "virtual"), None)
        return f"amx_ch_{ch['id']}" if ch else None

    def play(self, sid):
        s = self.sound(sid)
        if not s:
            return False
        e = self._entry(s)
        if e is None:
            return False
        frames = e["frames"]
        a = max(0, min(frames, int((s.get("start") or 0) * RATE)))
        b = frames if s.get("end") is None else max(a, min(frames, int(s["end"] * RATE)))
        g = s.get("vol", 100) / 100 * self.board.get("volume", 100) / 100
        cmd = ["paplay", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=2",
               "--client-name=amx-soundboard", f"--stream-name={s['name']}", "--latency-msec=20"]
        tgt = self.target()
        if tgt:
            cmd += ["-d", tgt]
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False

        def feed():                    # stream the trimmed range from the cache, a second at a time
            try:
                with open(e["path"], "rb") as fh:
                    fh.seek(a * FRAME)
                    left = (b - a) * FRAME
                    while left > 0:
                        chunk = fh.read(min(left, RATE * FRAME))
                        if not chunk:
                            break
                        left -= len(chunk)
                        if g != 1:
                            buf = array("f")
                            buf.frombytes(chunk)
                            chunk = array("f", map(g.__mul__, buf)).tobytes()
                        p.stdin.write(chunk)
                p.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()
        self.playing.setdefault(s["id"], []).append((p, time.monotonic(), (b - a) / RATE))
        return True

    def _prune(self):
        for sid in list(self.playing):
            self.playing[sid] = [x for x in self.playing[sid] if x[0].poll() is None]
            if not self.playing[sid]:
                del self.playing[sid]

    def is_playing(self, sid):
        self._prune()
        return bool(self.playing.get(sid))

    def progress(self, sid):
        self._prune()
        now = time.monotonic()
        return [min(1.0, (now - t0) / dur) if dur > 0 else 1.0 for _p, t0, dur in self.playing.get(sid, [])]

    def stop(self, sid):
        s = self.sound(sid)
        for p, *_ in self.playing.pop(s["id"] if s else str(sid), []):
            p.kill()

    def stop_all(self):
        for sid in list(self.playing):
            for p, *_ in self.playing.pop(sid, []):
                p.kill()

    # ---- editing
    def new_id(self):
        used = {s["id"] for s in self.board["sounds"]}
        while True:
            i = str(random.randint(1000, 9999))
            if i not in used:
                return i

    def digest(self, s):
        """SHA-1 of a sound's file, cached in its entry. None if the file is gone."""
        if not s.get("sha1"):
            try:
                s["sha1"] = _sha1(s["file"])
            except OSError:
                return None
        return s["sha1"]

    def has_src(self, url):
        return any(s.get("src") == url for s in self.board["sounds"])

    def add_files(self, paths, names=None, srcs=None):
        """Copy files onto the board. `names` (optional, same order) sets the pad names and file names; `srcs`
        records where a download came from. A file whose audio is already on the board is skipped and its name
        put in self.skipped."""
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        added, self.skipped = [], []
        known = {self.digest(s) for s in self.board["sounds"]}
        for i, p in enumerate(paths):
            base = os.path.basename(p)
            src = srcs[i] if srcs else None
            try:
                dig = _sha1(p)
            except OSError:
                continue
            if dig in known or (src and self.has_src(src)):
                self.skipped.append(names[i] if names and names[i] else os.path.splitext(base)[0])
                continue
            known.add(dig)
            if names and names[i]:
                base = _safe_name(names[i]) + os.path.splitext(base)[1]
            dest = os.path.join(SOUNDS_DIR, base)
            n = 1
            while os.path.exists(dest) and not os.path.samefile(dest, p):
                stem, ext = os.path.splitext(base)
                dest = os.path.join(SOUNDS_DIR, f"{stem}-{n}{ext}")
                n += 1
            if not os.path.exists(dest):
                shutil.copy2(p, dest)
            nm = names[i].strip() if names and names[i] else os.path.splitext(base)[0]
            s = {"id": self.new_id(), "name": nm[:40], "file": dest,
                 "start": 0.0, "end": None, "vol": 100, "sha1": dig}
            if src:
                s["src"] = src
            self.board["sounds"].append(s)
            added.append(s)
        return added

    def remove(self, sid):
        s = self.sound(sid)
        if not s:
            return
        self.stop(s["id"])
        self.board["sounds"] = [x for x in self.board["sounds"] if x["id"] != s["id"]]
        if os.path.dirname(os.path.abspath(s["file"])) == SOUNDS_DIR and \
                not any(x["file"] == s["file"] for x in self.board["sounds"]):
            try:
                os.unlink(s["file"])
            except OSError:
                pass
        e = self._meta.pop(s["id"], None)
        if e and not any(x["file"] == s["file"] for x in self.board["sounds"]):
            for f in (e["path"], e["path"][:-4] + ".peaks"):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    def export_zip(self, path):
        b = copy.deepcopy(self.board)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for s in b["sounds"]:
                arc = f"sounds/{s['id']}-{os.path.basename(s['file'])}"
                if os.path.exists(s["file"]):
                    z.write(s["file"], arc)
                s["file"] = arc
            b.pop("channel", None)
            z.writestr("soundboard.json", json.dumps(b, indent=2))

    def import_zip(self, path):
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        self.skipped = []
        known = {self.digest(x) for x in self.board["sounds"]}
        with zipfile.ZipFile(path) as z:
            b = json.loads(z.read("soundboard.json"))
            n = 0
            for s in b.get("sounds", []):
                arc = s.get("file", "")
                if not arc.startswith("sounds/") or ".." in arc or arc not in z.namelist():
                    continue
                h = hashlib.sha1()
                with z.open(arc) as src:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                dig = h.hexdigest()
                if dig in known:                         # this audio is already on the board
                    self.skipped.append(str(s.get("name", "Sound")))
                    continue
                known.add(dig)
                dest = os.path.join(SOUNDS_DIR, os.path.basename(arc))
                stem, ext = os.path.splitext(dest)
                k = 1
                while os.path.exists(dest):              # never overwrite a sound already on the board
                    dest = f"{stem}-{k}{ext}"
                    k += 1
                with z.open(arc) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                used = {x["id"] for x in self.board["sounds"]}
                sid = str(s.get("id")) if str(s.get("id")) not in used else self.new_id()
                e = {"id": sid, "name": str(s.get("name", "Sound"))[:40], "file": dest,
                     "start": float(s.get("start") or 0), "end": s.get("end"), "vol": int(s.get("vol", 100)),
                     "sha1": dig}
                if isinstance(s.get("src"), str):
                    e["src"] = s["src"]
                self.board["sounds"].append(e)
                n += 1
        return n


class Waveform(Gtk.DrawingArea):
    """A clip's waveform, with its trim region and play position(s)."""

    def __init__(self, board, s, height=46, editable=False, on_trim=None):
        super().__init__(hexpand=True)
        self.set_content_height(height)
        self.board, self.s, self.editable, self.on_trim = board, s, editable, on_trim
        self.set_draw_func(self._draw)
        self._drag = None
        if editable:
            g = Gtk.GestureDrag()
            g.connect("drag-begin", self._begin)
            g.connect("drag-update", self._update)
            g.connect("drag-end", lambda *_: setattr(self, "_drag", None))
            self.add_controller(g)
            self.set_cursor_from_name("col-resize")

    def _pos(self, t, w):
        dur = self.board.duration(self.s) or 1
        return w * t / dur

    def _begin(self, _g, x, _y):
        w = self.get_width()
        dur = self.board.duration(self.s)
        if not dur:
            return
        a = self._pos(self.s.get("start") or 0, w)
        b = self._pos(self.s["end"] if self.s.get("end") is not None else dur, w)
        self._drag = ("start" if abs(x - a) <= abs(x - b) else "end", x)
        self._update(None, 0, 0)

    def _update(self, _g, dx, _dy):
        if not self._drag:
            return
        which, x0 = self._drag
        dur = self.board.duration(self.s)
        t = max(0.0, min(dur, (x0 + dx) / max(1, self.get_width()) * dur))
        end = self.s["end"] if self.s.get("end") is not None else dur
        if which == "start":
            self.s["start"] = round(min(t, end - 0.05), 3)
        else:
            self.s["end"] = round(max(t, (self.s.get("start") or 0) + 0.05), 3)
        self.queue_draw()
        if self.on_trim:
            self.on_trim()

    def _draw(self, _a, cr, w, h):
        dur = self.board.duration(self.s)
        n = max(20, int(w / 3))
        pk = self.board.peaks(self.s, n)
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if not pk:
            cr.set_source_rgba(1, 1, 1, 0.3)
            cr.set_font_size(10)
            cr.move_to(6, h / 2 + 4)
            cr.show_text("loading…" if os.path.exists(self.s["file"]) else "file missing")
            return
        a = self._pos(self.s.get("start") or 0, w)
        b = self._pos(self.s["end"] if self.s.get("end") is not None else dur, w)
        bw = w / len(pk)
        for i, v in enumerate(pk):
            x = i * bw
            inside = a <= x <= b
            cr.set_source_rgba(0.21, 0.68, 0.96, 0.95 if inside else 0.25)
            hh = max(1, v * (h - 4))
            cr.rectangle(x, (h - hh) / 2, max(1, bw - 1), hh)
            cr.fill()
        if self.editable:
            for x in (a, b):
                cr.set_source_rgb(1, 0.85, 0.3)
                cr.rectangle(x - 1.5, 0, 3, h)
                cr.fill()
        span = max(1e-6, (b - a))
        for f in self.board.progress(self.s["id"]):
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(a + span * f, 0, 2, h)
            cr.fill()


def stop_all_shortcut(window, board, when=None):
    """Esc in this window stops every soundboard sound (only while when() is true, if given).
    Focused widgets see the key first, so Esc still closes popovers and dialogs."""
    def act(*_):
        if when is not None and not when():
            return False
        board.stop_all()
        return True
    sc = Gtk.ShortcutController()
    sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                 action=Gtk.CallbackAction.new(act)))
    window.add_controller(sc)


class SoundboardPage(Gtk.Box):
    """The Soundboard page, or (popped=True) just its pads, for the pop-out window."""

    def __init__(self, win, board, popped=False):
        m = 0 if popped else 20       # (pop-out: the margin goes inside, so the dark background fills the window)
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=0 if popped else 8,
                         margin_bottom=0 if popped else 16, margin_start=m, margin_end=m)
        self.win, self.board, self.popped = win, board, popped
        self.locked, self.on_unlock = False, None
        self.waves = []
        if popped:              # pop-out window: nothing but the pads
            sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, margin_top=8,
                                    margin_bottom=8, margin_start=8, margin_end=8, max_children_per_line=16,
                                    min_children_per_line=1, row_spacing=6, column_spacing=6,
                                    valign=Gtk.Align.START)
            sw.set_child(self.flow)
            self.append(sw)
            unlock = Gtk.GestureClick(button=3)      # locked window has no header: right-click anywhere unlocks
            unlock.connect("pressed", lambda *_: self.locked and self.on_unlock and self.on_unlock())
            self.add_controller(unlock)
            self.rebuild()
            board.preload(board.board["sounds"], lambda _sid: self.redraw_all())
            GLib.timeout_add(60, self._tick)
            return
        top = Gtk.Box(spacing=10)
        top.append(Gtk.Label(label="Soundboard", xalign=0, hexpand=True, css_classes=["page-title"]))
        stop = Gtk.Button(css_classes=["destructive-action"], valign=Gtk.Align.CENTER,
                          tooltip_text="Stop every sound (Esc)")
        stop.set_child(Gtk.Label(label="Stop all"))
        stop.connect("clicked", lambda *_: board.stop_all())
        add = Gtk.Button(css_classes=["suggested-action"], valign=Gtk.Align.CENTER)
        add.set_child(Gtk.Label(label="+ Add sounds"))
        add.connect("clicked", lambda *_: file_dialog(self, "Add sounds", multiple=True, done=self._add,
                                                      patterns=Soundboard.AUDIO))
        self.web = Gtk.Button(valign=Gtk.Align.CENTER,
                              tooltip_text="Search MyInstants, Freesound, Openverse and Wikimedia Commons, or paste "
                                           "a link")
        self.web.set_child(Gtk.Label(label="Find sounds online"))
        self.web.connect("clicked", lambda *_: self.open_web())
        pop = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Open just the pads in their own small window")
        prow = Gtk.Box(spacing=6)
        prow.append(Gtk.Image(icon_name="window-new-symbolic"))
        prow.append(Gtk.Label(label="Pop out"))
        pop.set_child(prow)
        pop.connect("clicked", lambda *_: win.open_soundboard_window())
        top.append(pop)
        top.append(stop)
        top.append(self.web)
        top.append(add)
        self.append(top)
        self.append(Gtk.Label(
            label="Click a pad to play it (again to layer another copy); right-click or ⋯ to trim it. Esc stops "
                  "every sound. Drop audio "
                  "files here to add them. Each sound "
                  "has a 4-digit id for Stream Deck buttons and the command line: "
                  f"{ctl_hint()} play <id>.", xalign=0, wrap=True, css_classes=["sub"]))
        self.routing = Gtk.Box(spacing=12, css_classes=["cell"])
        self.append(self.routing)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, max_children_per_line=6,
                                min_children_per_line=1, row_spacing=10, column_spacing=10,
                                valign=Gtk.Align.START)
        sw.set_child(self.flow)
        self.append(sw)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)     # drag audio files onto the page
        drop.connect("drop", self._drop)
        self.add_controller(drop)
        self.rebuild()
        board.preload(board.board["sounds"], lambda _sid: self.redraw_all())
        GLib.timeout_add(60, self._tick)

    def pages(self):
        """Every soundboard view that's open (this page and the pop-out), so a change shows in all of them."""
        return getattr(self.win, "soundboards", None) or [self]

    def rebuild_all(self):
        for p in self.pages():
            p.rebuild()

    def redraw_all(self):
        for p in self.pages():
            for w in p.waves:
                w.queue_draw()

    def _tick(self):
        if self.popped and self not in self.pages():     # its pop-out window was closed: stop this timer
            return False
        if self.get_mapped() and (self.board.playing or self._was_playing):
            for w in self.waves:
                w.queue_draw()
        self._was_playing = bool(self.board.playing)
        return True
    _was_playing = False

    def open_web(self):
        if self.win.cfg.get("web_sounds"):
            WebSoundsDialog(self).present(self.win)
        else:
            ask_enable_web(self.win, lambda: WebSoundsDialog(self).present(self.win))

    def _drop(self, _t, value, _x, _y):
        exts = {p.lower() for p in Soundboard.AUDIO[1:]}
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        ok = [p for p in paths if "*" + os.path.splitext(p)[1].lower() in exts]
        if len(ok) < len(paths):
            self.win.toast("Only audio files can go on the board")
        if ok:
            self._add(ok)
        return bool(ok)

    def _add(self, paths, names=None, srcs=None):
        added = self.board.add_files(paths, names, srcs)
        if self.board.skipped:
            sk = self.board.skipped
            self.win.toast(f"Already on the board: {sk[0][:40]}" + (f" and {len(sk) - 1} more" if len(sk) > 1 else ""))
        if added:
            self.win.changed()
            self.rebuild_all()
            self.board.preload(added, lambda _sid: self.redraw_all())
        return added

    def build_routing(self):
        r = self.routing
        _clear(r)
        cfg = self.win.cfg
        b = self.board.board
        chans = [c for c in cfg["channels"] if c["type"] == "virtual"]
        opts = [("Default output", None)] + [(c["name"], c["id"]) for c in chans] + [("+ New “Soundboard” channel", "new")]
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vb.append(Gtk.Label(label="Plays on", xalign=0, css_classes=["sub"]))
        dd = Gtk.DropDown.new_from_strings([o[0] for o in opts])
        vals = [o[1] for o in opts]
        dd.set_selected(vals.index(b.get("channel")) if b.get("channel") in vals else 0)

        def pick(d, _p):
            v = vals[d.get_selected()]
            if v == "new":
                ch = {"id": os.urandom(3).hex(), "name": "Soundboard", "type": "virtual",
                      "icon": "audio-speakers-symbolic"}
                cfg["channels"].append(ch)
                for m in cfg["mixes"]:     # heard in every mix that has outputs or a mic, at full level
                    m["levels"][ch["id"]] = {"vol": 100, "mute": False, "on": True}
                b["channel"] = ch["id"]
                self.win.structure_changed()
            else:
                b["channel"] = v
                self.win.changed()
            GLib.idle_add(lambda: (self.build_routing(), False)[1])
        dd.connect("notify::selected", pick)
        vb.append(dd)
        r.append(vb)
        vb2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        vb2.append(Gtk.Label(label="Board volume", xalign=0, css_classes=["sub"]))
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        sc.add_css_class("plain")
        sc.set_value(b.get("volume", 100))
        sc.connect("value-changed", lambda s: (b.__setitem__("volume", round(s.get_value())), self.win.changed()))
        vb2.append(sc)
        r.append(vb2)
        ch = next((c for c in chans if c["id"] == b.get("channel")), None)
        if ch:   # which mixes hear the board, and how loud: this is the channel's row in the mixer
            mixes = Gtk.Box(spacing=6)
            for m in cfg["mixes"]:
                cell = self.win.eng.cell(m, ch)
                tb = Gtk.ToggleButton(active=cell["on"], tooltip_text=f"Send the soundboard to {m['name']}")
                tb.set_child(Gtk.Label(label=m["name"]))

                def tog(btn, cell=cell):
                    cell["on"] = btn.get_active()
                    self.win.structure_changed()
                tb.connect("toggled", tog)
                mixes.append(tb)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(Gtk.Label(label="Heard in (levels: the channel's row on the Mixes page)", xalign=0,
                                 css_classes=["sub"]))
            box.append(mixes)
            r.append(box)

    def rebuild(self):
        if not self.popped:
            self.build_routing()
        _clear(self.flow)
        self.waves = []
        self.name_labels = {}     # sound id -> its pad's name label, to rename without a rebuild
        self.cards = {}           # sound id -> its pad, to remove one without a rebuild
        sounds = self.board.board["sounds"]
        if not sounds:
            self.flow.append(Gtk.Label(label="No sounds yet." if self.popped else
                                       "No sounds yet. Drop audio files here, use “+ Add sounds”, or "
                                       "“Find sounds online”.", css_classes=["sub"], margin_top=12 if self.popped else 40))
            return
        for s in sounds:
            self.flow.append(self.mini_pad(s) if self.popped else self.pad(s))

    def mini_pad(self, s):
        """A pop-out pad: name and waveform. Click plays (again to layer); right-click stops it."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["cell", "sb-pad"],
                       tooltip_text=f"{s['name']}  ·  {s['id']}\nClick to play (again to layer), right-click to stop.\n"
                                    "Esc stops every sound.")
        card.set_size_request(110, -1)
        self.cards[s["id"]] = card
        lbl = Gtk.Label(label=s["name"], xalign=0, ellipsize=3, css_classes=["chname"])
        self.name_labels[s["id"]] = lbl
        card.append(lbl)
        w = Waveform(self.board, s, height=28)
        self.waves.append(w)
        card.append(w)
        g1 = Gtk.GestureClick(button=1)
        g1.connect("released", lambda *_: self.board.play(s["id"]) or self.win.toast("Couldn't play that file"))
        card.add_controller(g1)
        g3 = Gtk.GestureClick(button=3)
        g3.connect("pressed", lambda *_: self.locked or self.board.stop(s["id"]))   # (locked: right-click unlocks)
        card.add_controller(g3)
        return card

    def pad(self, s):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["cell", "sb-pad"])
        card.set_size_request(190, -1)
        self.cards[s["id"]] = card
        head = Gtk.Box(spacing=6)
        lbl = Gtk.Label(label=s["name"], xalign=0, hexpand=True, ellipsize=3, css_classes=["chname"])
        self.name_labels[s["id"]] = lbl
        head.append(lbl)
        head.append(Gtk.Label(label=s["id"], css_classes=["sub", "numeric"], tooltip_text="Sound id"))
        more = Gtk.Button(icon_name="view-more-symbolic", css_classes=["flat", "dimbtn"], tooltip_text="Edit")
        more.connect("clicked", lambda *_: self.edit(s))
        head.append(more)
        card.append(head)
        w = Waveform(self.board, s)
        self.waves.append(w)
        card.append(w)
        row = Gtk.Box(spacing=6, homogeneous=True)
        play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play (layers on repeat)")
        play.connect("clicked", lambda *_: self.board.play(s["id"]) or self.win.toast("Couldn't play that file"))
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        stop.connect("clicked", lambda *_: self.board.stop(s["id"]))
        row.append(play)
        row.append(stop)
        card.append(row)
        g = Gtk.GestureClick(button=3)
        g.connect("pressed", lambda *_: self.edit(s))
        card.add_controller(g)
        g1 = Gtk.GestureClick(button=1)
        g1.connect("released", lambda _g, _n, x, y: card.pick(x, y, Gtk.PickFlags.DEFAULT) in (w, card) and
                   (self.board.play(s["id"]) or self.win.toast("Couldn't play that file")))
        card.add_controller(g1)
        return card

    def edit(self, s):
        dlg = Adw.Dialog(content_width=640, title="Edit sound")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=20, margin_bottom=20,
                      margin_start=22, margin_end=22)
        dlg.set_child(box)
        name = Gtk.Entry(text=s["name"], css_classes=["dlg-name"])
        name.connect("changed", lambda e: e.get_text().strip() and (s.__setitem__("name", e.get_text().strip()[:40]),
                                                                    self.win.changed()))
        box.append(name)
        info = Gtk.Label(xalign=0, css_classes=["sub"])
        box.append(info)

        def upd_info():
            dur = self.board.duration(s)
            end = s["end"] if s.get("end") is not None else dur
            info.set_label(f"Id {s['id']}  ·  plays {s.get('start', 0):.2f}s – {end:.2f}s of {dur:.2f}s  ·  "
                           f"drag the yellow handles to trim")
        wave = Waveform(self.board, s, height=120, editable=True, on_trim=lambda: (upd_info(), self.win.changed()))
        box.append(wave)
        upd_info()
        ctl = Gtk.Box(spacing=10)
        play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Preview")
        play.connect("clicked", lambda *_: self.board.play(s["id"]))
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        stop.connect("clicked", lambda *_: self.board.stop(s["id"]))
        reset = Gtk.Button(label="Reset trim", css_classes=["flat"])

        def do_reset(*_):
            s["start"], s["end"] = 0.0, None
            wave.queue_draw()
            upd_info()
            self.win.changed()
        reset.connect("clicked", do_reset)
        ctl.append(play)
        ctl.append(stop)
        ctl.append(reset)
        ctl.append(Gtk.Label(label="Clip volume", css_classes=["sub"], margin_start=12))
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 1)
        sc.add_css_class("plain")
        sc.set_hexpand(True)
        sc.set_draw_value(True)
        sc.set_value(s.get("vol", 100))
        sc.connect("value-changed", lambda x: (s.__setitem__("vol", round(x.get_value())), self.win.changed()))
        ctl.append(sc)
        box.append(ctl)
        t = GLib.timeout_add(60, lambda: (wave.queue_draw(), True)[1])
        bottom = Gtk.Box(spacing=10)
        rm = Gtk.Button(label="Delete sound", css_classes=["destructive-action"])

        def delete(*_):
            self.board.remove(s["id"])
            self.win.changed()
            dlg.close()
        rm.connect("clicked", delete)
        bottom.append(rm)
        bottom.append(Gtk.Box(hexpand=True))
        close = Gtk.Button(label="Done", css_classes=["suggested-action"])
        close.connect("clicked", lambda *_: dlg.close())
        bottom.append(close)
        box.append(bottom)

        def closed(*_):
            GLib.source_remove(t)
            # Update the pads in place rather than rebuilding them all: a rebuild throws away the pad that has
            # focus, and the page jumps back to the top.
            gone = s not in self.board.board["sounds"]
            for p in self.pages():
                card = p.cards.get(s["id"])
                if not card:
                    continue
                if not gone:
                    p.name_labels[s["id"]].set_label(s["name"])
                elif self.board.board["sounds"]:
                    p.waves = [w for w in p.waves if w.get_ancestor(Gtk.FlowBoxChild) is not card.get_parent()]
                    p.flow.remove(card.get_parent())
                    del p.cards[s["id"]], p.name_labels[s["id"]]
                else:
                    p.rebuild()          # the last sound: show the "No sounds yet" hint
            self.redraw_all()
        dlg.connect("closed", closed)
        dlg.present(self.win)
        if not self.board.duration(s):
            self.board.preload([s], lambda _i: (wave.queue_draw(), upd_info()))


# =========================================================================== online sounds (optional)
# Off until the user turns it on (the first click on "Find sounds online" asks; Settings ▸ Soundboard turns it
# off again). The sources and downloads live in amx_sounds.
_safe_name = snd.safe_name


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class WebPreview:
    """Plays one downloaded clip on the default output (the System channel), not the soundboard's channel."""
    def __init__(self):
        self.procs = []

    def play(self, path):
        self.stop()
        try:
            dec = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-f", "f32le", "-ac", "2",
                                    "-ar", str(RATE), "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        try:
            out = subprocess.Popen(["paplay", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=2",
                                    "--client-name=amx-soundboard-preview"], stdin=dec.stdout,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            dec.kill()
            return False
        dec.stdout.close()
        self.procs = [dec, out]
        return True

    def playing(self):
        return any(p.poll() is None for p in self.procs)

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        self.procs = []


def ask_enable_web(win, then):
    """The online search is opt-in: nothing is sent to any site until the user says yes here."""
    dlg = Adw.AlertDialog(heading="Find sounds online?",
                          body="Search MyInstants, Freesound, Openverse and Wikimedia Commons, or paste a link, and "
                               "add sounds to the board with one click. Your searches are sent to the site you pick; "
                               "nothing is sent until you turn this on. Turn it off any time in Settings ▸ Soundboard.")
    dlg.add_response("cancel", "Not now")
    dlg.add_response("on", "Turn on")
    dlg.set_response_appearance("on", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("on")

    def done(_d, resp):
        if resp == "on":
            win.cfg["web_sounds"] = True
            win.changed()
            then()
    dlg.connect("response", done)
    dlg.present(win)


class WebSoundsDialog(Adw.Dialog):
    def __init__(self, page):
        super().__init__(content_width=660, content_height=680, title="Find sounds online")
        self.page, self.win = page, page.win
        self.gen = 0                 # bumps on every new search, so late answers to old ones are dropped
        self._queries, self._cur_src = {}, None
        self.pg = 1
        self.preview = WebPreview()
        self.prev_btn = None
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6, margin_bottom=16,
                      margin_start=16, margin_end=16)
        tv.set_content(box)
        bar = Gtk.Box(spacing=8)
        self.src = Gtk.DropDown.new_from_strings([n for n, _f, _p in snd.SOURCES])
        self.src.set_tooltip_text("Where to search")
        self.src.set_selected(min(int(self.win.cfg.get("web_source", 0) or 0), len(snd.SOURCES) - 1))
        self.entry = Gtk.SearchEntry(hexpand=True, search_delay=500)
        self.entry.connect("search-changed", lambda *_: self.search())
        self.entry.connect("activate", lambda *_: self.search())
        bar.append(self.src)
        bar.append(self.entry)
        box.append(bar)
        # Freesound's free API key, shown only while Freesound is picked
        self.keybox = Gtk.Box(spacing=8)
        self.key = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True, placeholder_text="Freesound API key",
                                     text=self.win.cfg.get("freesound_key") or "")
        self.key.connect("changed", self._key_changed)
        self.keybox.append(self.key)
        self.keybox.append(Gtk.LinkButton(uri=snd.FREESOUND_KEY_URL, label="Get a free key"))
        box.append(self.keybox)
        self.status = Gtk.Label(xalign=0, wrap=True, css_classes=["sub"])
        box.append(self.status)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, margin_top=24, visible=False)
        box.append(self.spinner)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"], visible=False)
        self.more = Gtk.Button(label="Load more", halign=Gtk.Align.CENTER, visible=False)
        self.more.connect("clicked", lambda *_: self.search(self.pg + 1))
        inner.append(self.list)
        inner.append(self.more)
        sw.set_child(inner)
        box.append(sw)
        box.append(Gtk.Label(label="▶ previews on the System channel, + adds the sound to the board. Freesound, "
                                   "Openverse and Commons sounds carry the licence shown: credit the creator where "
                                   "it asks for it.", xalign=0, wrap=True, css_classes=["sub"]))
        self.src.connect("notify::selected", self._src_changed)
        self._src_changed()
        self.tick = GLib.timeout_add(250, self._tick)
        self.connect("closed", self._closed)

    closed = False

    def _closed(self, *_):
        self.closed = True
        self.gen += 1
        self.preview.stop()
        GLib.source_remove(self.tick)

    def _source(self):
        return snd.SOURCES[self.src.get_selected()]

    def _src_changed(self, *_):
        cur = self.src.get_selected()           # each source keeps its own search text
        if self._cur_src is not None:
            self._queries[self._cur_src] = self.entry.get_text()
            self.entry.set_text(self._queries.get(cur, ""))
        self._cur_src = cur
        name, fn, placeholder = self._source()
        if self.win.cfg.get("web_source") != self.src.get_selected():
            self.win.cfg["web_source"] = self.src.get_selected()
            self.win.changed()
        self.entry.set_placeholder_text(placeholder)
        self.keybox.set_visible(fn is snd.search_freesound)
        self.entry.grab_focus()
        self.search()

    def _key_changed(self, e):
        self.win.cfg["freesound_key"] = e.get_text().strip()
        self.win.changed()
        self.search()

    def _tick(self):
        if self.prev_btn and not self.preview.playing():
            self.prev_btn.set_icon_name("media-playback-start-symbolic")
            self.prev_btn = None
        return True

    def search(self, pg=1):
        self.gen += 1
        gen, q = self.gen, self.entry.get_text().strip()
        name, fn, _p = self._source()
        cfg = {"freesound_key": self.win.cfg.get("freesound_key")}
        self.pg = pg
        self.more.set_visible(False)
        if pg == 1:
            _clear(self.list)
            self.list.set_visible(False)
        self.status.set_label(f"Searching {name}…" if q else f"Loading {name}…")
        self.spinner.set_visible(pg == 1)
        self.spinner.start()

        def work():
            try:
                res, more = fn(q, pg, cfg)
                err = None
            except Exception as e:          # network, HTTP or parse error: report it, never crash the mixer
                res, more, err = [], False, snd.friendly_error(e)
            GLib.idle_add(lambda: (self._show(gen, q, res, more, err), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _show(self, gen, q, res, more, err):
        if gen != self.gen:
            return
        self.spinner.stop()
        self.spinner.set_visible(False)
        if err:
            self.status.set_label(err)
            return
        for r in res:
            self.list.append(self._row(r))
        has = self.list.get_first_child() is not None
        self.list.set_visible(has)
        self.status.set_label("Nothing found. Try other words or another source." if not has else
                              f"Results for “{q}”" if q else "Popular sounds")
        self.more.set_visible(more)

    def _row(self, r):
        row = Adw.ActionRow(title=GLib.markup_escape_text(r["name"]), subtitle=GLib.markup_escape_text(r["info"]),
                            title_lines=1, subtitle_lines=1, tooltip_text=r["name"])
        pv = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                        tooltip_text="Preview")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                         tooltip_text="Add to the soundboard")
        board = self.page.board

        def fetch(then, btn):
            btn.set_sensitive(False)
            icon = btn.get_icon_name()
            btn.set_child(Gtk.Spinner(spinning=True))
            gen = self.gen

            def work():
                try:
                    path, err = snd.fetch(r["url"]), None
                except Exception as e:
                    path, err = None, snd.friendly_error(e)

                def done():
                    btn.set_icon_name(icon)
                    btn.set_sensitive(True)
                    if path:
                        then(path)
                    elif self.gen == gen:
                        self.win.toast(f"Couldn't download it: {err}")
                    return False
                GLib.idle_add(done)
            threading.Thread(target=work, daemon=True).start()

        def do_preview(btn):
            if self.prev_btn is btn:
                self.preview.stop()
                return

            def go(path):
                if self.closed:
                    return
                if self.preview.play(path):
                    if self.prev_btn:
                        self.prev_btn.set_icon_name("media-playback-start-symbolic")
                    self.prev_btn = btn
                    btn.set_icon_name("media-playback-stop-symbolic")
                else:
                    self.win.toast("Preview needs ffmpeg and paplay")
            fetch(go, btn)

        def mark_added():
            add.set_icon_name("object-select-symbolic")
            add.set_tooltip_text("Already on the soundboard")
            add.set_sensitive(False)

        def do_add(btn):
            if board.has_src(r["url"]):
                mark_added()
                return

            def go(path):
                if self.page._add([path], [r["name"]], [r["url"]]):
                    self.win.toast(f"Added “{r['name'][:40]}”")
                mark_added()                 # added now, or the same audio was already there
            fetch(go, btn)
        pv.connect("clicked", do_preview)
        add.connect("clicked", do_add)
        row.add_suffix(pv)
        row.add_suffix(add)
        if board.has_src(r["url"]):
            mark_added()
        return row


# =========================================================================== profiles
PROFILE_KEYS = ("channels", "mixes", "system_default", "bypass", "rec_rules")


def profiles(cfg):
    return cfg.setdefault("profiles", {})


def save_profile(cfg, name):
    profiles(cfg)[name] = {k: copy.deepcopy(cfg.get(k)) for k in PROFILE_KEYS if k in cfg}
    cfg["profile"] = name


def switch_profile(cfg, name):
    p = profiles(cfg).get(name)
    if not p:
        return False
    cur = cfg.get("profile")
    if cur and cur in profiles(cfg):          # remember the edits made to the profile we leave
        save_profile(cfg, cur)
    for k in PROFILE_KEYS:
        if k in p:
            cfg[k] = copy.deepcopy(p[k])
    cfg["profile"] = name
    return True


# =========================================================================== MIDI instruments
class Instruments:
    """One FluidSynth per channel that has a MIDI instrument; it plays into that channel and
    listens to every MIDI input (ALSA sequencer, auto-connected)."""

    def __init__(self):
        self.procs = {}     # ch_id -> (Popen, sf2, sink)

    def pids(self):
        return {str(p.pid) for p, *_ in self.procs.values() if p.poll() is None}

    def sync(self, channels, sinks):
        want = {}
        for ch in channels:
            m = ch.get("midi") or {}
            sink = f"amx_ch_{ch['id']}"
            if ch["type"] == "virtual" and m.get("on") and m.get("sf2") and os.path.exists(m["sf2"]) \
                    and sink in sinks:
                want[ch["id"]] = (m["sf2"], sink, float(m.get("gain", 0.6)))
        for cid in list(self.procs):
            p, sf2, sink, gain = self.procs[cid]
            if want.get(cid) != (sf2, sink, gain) or p.poll() is not None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                del self.procs[cid]
        if not shutil.which("fluidsynth"):
            return
        for cid, (sf2, sink, gain) in want.items():
            if cid in self.procs:
                continue
            try:
                p = subprocess.Popen(
                    ["fluidsynth", "-i", "-s", "-a", "pulseaudio", "-m", "alsa_seq", "-g", f"{gain:.2f}",
                     "-r", str(RATE), "-o", "midi.autoconnect=1", "-o", f"audio.pulseaudio.device={sink}",
                     "-o", "audio.period-size=128", "-o", "audio.periods=2", sf2],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, preexec_fn=_pdeathsig)
            except OSError:
                continue
            self.procs[cid] = (p, sf2, sink, gain)

    def stop_all(self):
        self.sync([], set())


# =========================================================================== how to run the mixer
def installed():
    """True when installed with an `audio-mixer` command: the AUR package (/usr/share/audio-mixer) or the
    release's install.sh (~/.local/share/audio-mixer)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return here in ("/usr/share/audio-mixer", os.path.join(GLib.get_user_data_dir(), "audio-mixer")) \
        and bool(shutil.which("audio-mixer"))


def run_command():
    """The command that starts this mixer, for showing to the user and for autostart."""
    if installed():
        path = shutil.which("audio-mixer")
        if path.startswith("/usr/"):
            return "audio-mixer"
        return '"{}"'.format(path)        # ~/.local/bin isn't on PATH in every login session
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_mixer.py")
    return 'python3 "{}"'.format(script.replace("\\", "\\\\").replace('"', '\\"'))


def ctl_hint():
    """Short form of the control command for help texts."""
    return "audio-mixer ctl" if installed() else "python3 audio_mixer.py ctl"


# =========================================================================== start at login
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", "io.github.lawsonf.AudioMixer.desktop")


def autostart_enabled():
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(on, hidden=True):
    """Add or remove the XDG autostart entry. hidden: start in the tray instead of opening the window."""
    if not on:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass
        return
    cmd = run_command()
    if hidden:
        cmd += " --hidden"
    os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
    with open(AUTOSTART_FILE + ".tmp", "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Audio Mixer\n"
                "Comment=Wave Link style software mixer for PipeWire\n"
                f"Exec={cmd}\nIcon=io.github.lawsonf.AudioMixer\nTerminal=false\n"
                "X-GNOME-Autostart-enabled=true\n")
    os.replace(AUTOSTART_FILE + ".tmp", AUTOSTART_FILE)


# =========================================================================== web companion
COMPANION_PAGE =os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "companion.html")


def companion_html():
    try:
        with open(COMPANION_PAGE, "rb") as f:
            return f.read()
    except OSError:
        return b"<!doctype html><title>Audio Mixer</title><p>web/companion.html is missing.</p>"

MANIFEST = json.dumps({"name": "Audio Mixer Console", "short_name": "Mixer", "display": "standalone", "start_url": "/",
                       "background_color": "#151517", "theme_color": "#151517", "icons": []})


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Companion:
    """A phone / tablet remote served over HTTP: page + JSON commands + Server-Sent Events.
    No password: anyone who can reach the port can drive the mixer. Off unless switched on."""

    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.state = "{}"
        self.meters = "{}"
        self.version = 0
        self.mversion = 0
        self.timer = None
        self.error = None
        self.viewers = 0        # open /api/events streams; with none, state is refreshed only slowly

    @property
    def running(self):
        return self.httpd is not None

    def start(self, port):
        if self.httpd:
            return True
        comp = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                b = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send(200, companion_html(), "text/html; charset=utf-8")
                elif self.path == "/manifest.json":
                    self._send(200, MANIFEST, "application/manifest+json")
                elif self.path == "/api/state":
                    self._send(200, comp.state, "application/json")
                elif self.path == "/api/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    sv = mv = -1
                    last = time.monotonic()
                    comp.viewer(+1)
                    try:
                        while comp.httpd is not None:
                            out = b""
                            if comp.version != sv:
                                sv = comp.version
                                out += b"event: state\ndata: " + comp.state.encode() + b"\n\n"
                            if comp.mversion != mv:
                                mv = comp.mversion
                                out += b"event: meters\ndata: " + comp.meters.encode() + b"\n\n"
                            if not out and time.monotonic() - last > 15:
                                out = b": ping\n\n"
                            if out:
                                self.wfile.write(out)
                                self.wfile.flush()
                                last = time.monotonic()
                            time.sleep(0.05)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        comp.viewer(-1)
                else:
                    self._send(404, "not found", "text/plain")

            def do_POST(self):
                if self.path != "/api/cmd":
                    return self._send(404, "not found", "text/plain")
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 65536)
                    msg = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(msg, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, '{"error":"bad json"}', "application/json")
                q = queue.Queue()

                def run():
                    try:
                        q.put(comp.app.control.command(msg))
                    except Exception as e:      # never let a remote command take the mixer down
                        q.put({"error": str(e)})
                    return False
                GLib.idle_add(run)
                try:
                    res = q.get(timeout=5)
                except queue.Empty:
                    res = {"error": "timeout"}
                self._send(200, json.dumps(res), "application/json")

        try:
            self.httpd = _Server(("0.0.0.0", int(port)), H)
        except OSError as e:
            self.error = str(e)
            self.httpd = None
            return False
        self.error = None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.timer = GLib.timeout_add(100, self._update)
        self._update()
        return True

    def stop(self):
        h, self.httpd = self.httpd, None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        if h:
            threading.Thread(target=lambda: (h.shutdown(), h.server_close()), daemon=True).start()

    _n = 0

    def viewer(self, d):
        """Called from server threads when a phone connects / leaves."""
        def apply():
            self.viewers = max(0, self.viewers + d)
            if self.viewers == (1 if d > 0 else 0) and self.app.win:
                self.app.win.sync_meters()        # start / stop the meters it shows
            return False
        GLib.idle_add(apply)

    def _update(self):
        if not self.httpd:
            return False
        self._n += 1
        if not self.viewers and self._n % 10:
            return True                            # no phone connected: refresh once a second
        try:
            st = json.dumps(self.app.control.state(), sort_keys=True)
        except Exception:
            return True
        if st != self.state:
            self.state = st
            self.version += 1
        win = self.app.win
        if win and self.viewers:
            lv = {}
            for cid, v in win.meters.level.items():
                lv[cid] = round(max(0.0, min(1.0, (20 * math.log10(v) + 54) / 54)) if v > 0.0005 else 0.0, 3)
            m = json.dumps(lv)
            if m != self.meters:
                self.meters = m
                self.mversion += 1
        return True

    @staticmethod
    def urls(port):
        out = []
        try:
            r = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[1] != "lo":
                    out.append(f"http://{parts[3].split('/')[0]}:{port}")
        except (OSError, subprocess.TimeoutExpired):
            pass
        out.append(f"http://{socket.gethostname()}.local:{port}")
        return out


# =========================================================================== settings page
LATENCIES = [("Lowest (3 ms buffers, ~4 ms)", 3), ("Low (5 ms buffers, ~9 ms) — default", 5),
             ("Relaxed (10 ms buffers, ~14 ms)", 10), ("Safe (20 ms buffers, ~35 ms)", 20),
             ("Very safe (40 ms buffers)", 40)]


class SettingsPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8, margin_bottom=16,
                         margin_start=20, margin_end=20)
        self.win = win
        self.append(Gtk.Label(label="Settings", xalign=0, css_classes=["page-title"]))
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.inner = Adw.Clamp(maximum_size=860)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.inner.set_child(self.body)
        sw.set_child(self.inner)
        self.append(sw)
        self.rebuild()

    def group(self, title, desc=None):
        g = Adw.PreferencesGroup(title=title, description=desc)
        self.body.append(g)
        return g

    def rebuild(self):
        _clear(self.body)
        win, cfg, app = self.win, self.win.cfg, self.win.app

        # ---- startup
        g = self.group("Startup")
        hidden = cfg.get("autostart_hidden", True)
        at = Adw.SwitchRow(title="Start at login", subtitle="Open the mixer automatically when you log in",
                           active=autostart_enabled())
        th = Adw.SwitchRow(title="Start in the tray", subtitle="At login, run in the system tray without "
                                                              "opening the window",
                           active=hidden, sensitive=at.get_active())

        def apply_autostart(*_a):
            cfg["autostart_hidden"] = th.get_active()
            win.changed()
            th.set_sensitive(at.get_active())
            try:
                set_autostart(at.get_active(), th.get_active())
            except OSError as e:
                win.toast(f"Couldn't change autostart: {e}")
        at.connect("notify::active", apply_autostart)
        th.connect("notify::active", apply_autostart)
        g.add(at)
        g.add(th)

        # ---- audio
        g = self.group("Audio")
        vals = [v for _l, v in LATENCIES]
        cur = win.eng.link_latency()
        lat = Adw.ComboRow(title="Latency", subtitle="Buffer per routing hop. Lower is snappier; raise it if you "
                                                     "hear crackles.",
                           model=Gtk.StringList.new([l for l, _v in LATENCIES]),
                           selected=vals.index(cur) if cur in vals else 1)

        def set_lat(r, _p):
            cfg["latency_ms"] = vals[r.get_selected()]
            win.changed()
            win.eng.relink()
            win.toast(f"Latency set to {cfg['latency_ms']} ms buffers")
        lat.connect("notify::selected", set_lat)
        g.add(lat)
        fxh = win.eng.fx
        g.add(Adw.ActionRow(
            title="Effects engine",
            subtitle=("Ready" + (f" — {fxh.msg}" if fxh.msg else "") if fxh.ok else f"Unavailable: {fxh.msg}"),
            subtitle_lines=3))

        # ---- profiles
        g = self.group("Profiles", "A profile is a whole mixer setup: channels, levels, mutes, routing and "
                                   "effects. Switching keeps the edits you made to the profile you leave.")
        er = Adw.EntryRow(title="Save the current setup as…", show_apply_button=True)

        def save(e):
            n = e.get_text().strip()
            if n:
                save_profile(cfg, n)
                win.changed()
                win.toast(f"Saved profile “{n}”")
                GLib.idle_add(lambda: (self.rebuild(), False)[1])
        er.connect("apply", save)
        g.add(er)
        for name in sorted(profiles(cfg), key=str.lower):
            r = Adw.ActionRow(title=GLib.markup_escape_text(name),
                              subtitle="Current" if cfg.get("profile") == name else "")
            if cfg.get("profile") != name:
                b = Gtk.Button(label="Switch", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda _b, n=name: (win.switch_profile(n), self.rebuild()))
                r.add_suffix(b)
            ren = Gtk.MenuButton(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                 tooltip_text="Rename")
            pop = Gtk.Popover()
            ent = Gtk.Entry(text=name, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)

            def rename(e, n=name, pop=pop):
                new = e.get_text().strip()
                if new and new != n and new not in profiles(cfg):
                    profiles(cfg)[new] = profiles(cfg).pop(n)
                    if cfg.get("profile") == n:
                        cfg["profile"] = new
                    win.changed()
                pop.popdown()
                GLib.idle_add(lambda: (self.rebuild(), False)[1])
            ent.connect("activate", rename)
            pop.set_child(ent)
            ren.set_popover(pop)
            r.add_suffix(ren)
            ex = Gtk.Button(icon_name="document-save-as-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Export")

            def export(_b, n=name):
                def done(path):
                    _write(win, path, lambda f: json.dump({"audio_mixer_profile": n, "data": profiles(cfg)[n]},
                                                          f, indent=2), "Profile exported")
                file_dialog(self, "Export profile", save=True, done=done, name=f"{n}.json")
            ex.connect("clicked", export)
            r.add_suffix(ex)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Delete")

            def delete(_b, n=name):
                profiles(cfg).pop(n, None)
                if cfg.get("profile") == n:
                    cfg.pop("profile", None)
                win.changed()
                self.rebuild()
            rm.connect("clicked", delete)
            r.add_suffix(rm)
            g.add(r)
        if hasattr(Adw, "ButtonRow"):           # libadwaita 1.6+
            imp = Adw.ButtonRow(title="Import a profile…")
        else:
            imp = Adw.ActionRow(title="Import a profile…", activatable=True)
            imp.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))

        def do_import(*_):
            def done(path):
                try:
                    with open(path) as f:
                        d = json.load(f)
                    n, data = d["audio_mixer_profile"], d["data"]
                    if not isinstance(data, dict) or "channels" not in data:
                        raise ValueError
                except (OSError, ValueError, KeyError, TypeError):
                    win.toast("That file isn't an Audio Mixer profile")
                    return
                while n in profiles(cfg):
                    n += " (imported)"
                profiles(cfg)[n] = data
                win.changed()
                self.rebuild()
                win.toast(f"Imported profile “{n}”")
            file_dialog(self, "Import profile", done=done, patterns=["Profiles", "*.json"])
        imp.connect("activated", do_import)
        g.add(imp)

        # ---- companion
        comp = app.companion
        cc = cfg.setdefault("companion", {"port": 8787, "autostart": False})
        g = self.group("Web Companion",
                       "Control the mixer and the soundboard from a phone or tablet on your network, in a browser. "
                       "There is no password: anyone who can reach this computer on the port can change your mixer "
                       "while it runs. Leave it off on networks you don't control.")
        on = Adw.SwitchRow(title="Run the companion server", active=comp.running,
                           subtitle=f"Couldn't start: {comp.error}" if comp.error and not comp.running else "")

        def toggle(r, _p):
            if r.get_active():
                if not comp.start(cc.get("port", 8787)):
                    win.toast(f"Couldn't start: {comp.error}")
            else:
                comp.stop()
            GLib.idle_add(lambda: (self.rebuild(), False)[1])
        on.connect("notify::active", toggle)
        g.add(on)
        port = Adw.SpinRow.new_with_range(1024, 65535, 1)
        port.set_title("Port")
        port.set_value(cc.get("port", 8787))
        port.set_sensitive(not comp.running)
        port.connect("notify::value", lambda r, _p: (cc.__setitem__("port", int(r.get_value())), win.changed()))
        g.add(port)
        auto = Adw.SwitchRow(title="Start it with the mixer", active=bool(cc.get("autostart")))
        auto.connect("notify::active", lambda r, _p: (cc.__setitem__("autostart", r.get_active()), win.changed()))
        g.add(auto)
        if comp.running:
            for u in comp.urls(cc.get("port", 8787)):
                r = Adw.ActionRow(title=u, title_selectable=True)
                cp = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Copy")
                cp.connect("clicked", lambda _b, u=u: (Gdk.Display.get_default().get_clipboard().set(u),
                                                       win.toast("Copied")))
                r.add_suffix(cp)
                g.add(r)

        # ---- soundboard
        g = self.group("Soundboard")
        ws = Adw.SwitchRow(title="Online sound search",
                           subtitle="“Find sounds online” on the Soundboard page searches MyInstants, Freesound, "
                                    "Openverse and Wikimedia Commons. Off, the mixer never contacts them.",
                           active=bool(cfg.get("web_sounds")))

        def set_ws(r, _p):
            cfg["web_sounds"] = r.get_active()
            win.changed()
        ws.connect("notify::active", set_ws)
        g.add(ws)

        # ---- backups
        g = self.group("Backups")
        r = Adw.ActionRow(title="Whole mixer configuration", subtitle="Channels, mixes, deck, profiles, settings")
        for label, fn in (("Export…", self.export_cfg), ("Import…", self.import_cfg)):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_, fn=fn: fn())
            r.add_suffix(b)
        g.add(r)
        r = Adw.ActionRow(title="Soundboard", subtitle="All sounds with their trims and levels, as one .zip")
        for label, fn in (("Export…", self.export_board), ("Import…", self.import_board)):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_, fn=fn: fn())
            r.add_suffix(b)
        g.add(r)

    def export_cfg(self):
        def done(path):
            _write(self.win, path, lambda f: json.dump(self.win.cfg, f, indent=2), "Configuration exported")
        file_dialog(self, "Export configuration", save=True, done=done, name="audio-mixer-config.json")

    def import_cfg(self):
        def done(path):
            try:
                with open(path) as f:
                    d = json.load(f)
                if not isinstance(d, dict) or not d.get("mixes") or d.get("channels") is None:
                    raise ValueError
            except (OSError, ValueError):
                self.win.toast("That file isn't an Audio Mixer configuration")
                return
            self.win.replace_config(d)
            self.rebuild()
        file_dialog(self, "Import configuration", done=done, patterns=["Configuration", "*.json"])

    def export_board(self):
        def done(path):
            try:
                self.win.app.board.export_zip(path)
            except OSError as e:
                self.win.toast(f"Couldn't export: {e.strerror or e}")
                return
            self.win.toast("Soundboard exported")
        file_dialog(self, "Export soundboard", save=True, done=done, name="soundboard.zip")

    def import_board(self):
        def done(path):
            try:
                n = self.win.app.board.import_zip(path)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                self.win.toast("That file isn't a soundboard export")
                return
            self.win.changed()
            self.win.soundboard_page.rebuild_all()
            sk = len(self.win.app.board.skipped)
            self.win.toast(f"Imported {n} sound{'s' if n != 1 else ''}" +
                           (f" ({sk} already on the board)" if sk else ""))
        file_dialog(self, "Import soundboard", done=done, patterns=["Soundboard", "*.zip"])
