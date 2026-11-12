#!/usr/bin/env python3
"""Audio Mixer - a Wave Link style software mixer for PipeWire/PulseAudio.

  Channel  a row: a virtual channel (apps get routed into it) or a hardware input.
  Mix      a column: an internal bus that is sent to any number of output devices,
           and can also be recorded by other apps ("<name> (mix)" input).
  Cell     one channel's level in one mix (can be switched off for that mix).

Graph:  app -> [channel sink] -> loopback -> [mix bus sink] -> loopback -> output device(s)
Everything is driven through pactl; faders are the volumes of the loopback streams.
"""
import collections
import copy
import glob
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from array import array

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amx_effects as fxm  # noqa: E402
import amx_extras as ext  # noqa: E402

APP_ID = "io.github.lawsonf.AudioMixer"
CONF = os.environ.get("AMX_CONFIG") or os.path.join(
    GLib.get_user_config_dir(), "audio-mixer", "config.json")
FULL = 65536  # pulse "100%"
# Buffer per loopback hop. Measured on PipeWire: every path stays bit-identical with no dropouts at
# 2-20 ms, even with all CPU cores busy. The two hops (channel -> mix -> output) add ~35 ms at 20,
# ~14 ms at 10, ~9 ms at 5 and ~4 ms at 3. A loopback asks for a graph period of a third of this
# (5 ms -> 64 frames), and the whole graph follows, so going lower raises the risk of crackles in
# busy apps. Override with "latency_ms" in config.json (then restart the mixer).
LINK_LATENCY_MS = 5
LINK_SETTLE = 0.4     # s a new loopback is held muted (it starts with a burst of garbage)


SAFETY_POLL_MS = 10000   # re-sync with the sound server at least this often, even without events
MIN_SYNC_S = 1.0         # ... and at most this often when events keep coming


def pactl(*args, js=False):
    cmd = ["pactl"] + (["-f", "json"] if js else []) + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    if js:
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None
    return r.stdout.strip()


def vol_of(d):
    try:
        return max(c["value"] for c in d.values())
    except (ValueError, KeyError, AttributeError):
        return 0


def pan_gains(pan):
    """(left, right) gain for a pan of -100 (hard left) .. 0 (centre) .. 100 (hard right).
    A balance law: the near side stays at unity, so a centred channel is untouched."""
    p = max(-100.0, min(100.0, float(pan or 0))) / 100
    return (1.0 - max(0.0, p), 1.0 + min(0.0, p))


def pan_label(pan):
    pan = round(pan or 0)
    return "C" if pan == 0 else f"L {-pan}" if pan < 0 else f"R {pan}"


def short_id():
    return uuid.uuid4().hex[:6]


ICON_HINTS = [
    ("game", "input-gaming-symbolic"), ("voice", "audio-headset-symbolic"),
    ("chat", "audio-headset-symbolic"), ("discord", "audio-headset-symbolic"),
    ("music", "audio-x-generic-symbolic"), ("spotify", "audio-x-generic-symbolic"),
    ("sfx", "audio-speakers-symbolic"), ("browser", "applications-internet-symbolic"),
    ("system", "computer-symbolic"), ("mic", "audio-input-microphone-symbolic"),
    ("speaker", "audio-speakers-symbolic"), ("headphone", "audio-headphones-symbolic"),
    ("headset", "audio-headset-symbolic"), ("stream", "network-wireless-symbolic"),
    ("record", "media-record-symbolic"), ("monitor", "video-display-symbolic"),
    ("tv", "video-display-symbolic"), ("hdmi", "video-display-symbolic"),
]
# Fallback names per icon, because themes differ (e.g. Breeze has no input-gaming-symbolic).
ICON_ALIASES = {
    "input-gaming-symbolic": ["input-gamepad-symbolic", "applications-games-symbolic", "input-gaming"],
    "audio-x-generic-symbolic": ["emblem-music-symbolic", "folder-music-symbolic", "audio-x-generic"],
    "video-display-symbolic": ["video-television-symbolic", "monitor-symbolic", "computer-symbolic"],
    "audio-speakers-symbolic": ["speaker-symbolic", "audio-card-symbolic"],
    "audio-headphones-symbolic": ["headphones-symbolic"],
    "camera-video-symbolic": ["camera-web-symbolic"],
    "call-start-symbolic": ["phone-symbolic"],
    "chat-symbolic": ["dialog-messages-symbolic", "mail-message-new-symbolic"],
    "smartphone-symbolic": ["phone-symbolic"],
    "notifications-symbolic": ["preferences-desktop-notification-symbolic"],
}
# What the icon picker offers (stored by these names; resolved through ICON_ALIASES when drawn).
PICKER_ICONS = [
    "audio-headphones-symbolic", "audio-speakers-symbolic", "audio-headset-symbolic",
    "audio-input-microphone-symbolic", "input-gaming-symbolic", "audio-x-generic-symbolic",
    "applications-internet-symbolic", "computer-symbolic", "video-display-symbolic",
    "chat-symbolic", "call-start-symbolic", "camera-video-symbolic",
    "network-wireless-symbolic", "media-record-symbolic", "notifications-symbolic",
    "smartphone-symbolic", "audio-card-symbolic", "multimedia-player-symbolic",
    "emblem-favorite-symbolic", "weather-clear-symbolic", "system-users-symbolic",
    "audio-volume-high-symbolic",
]


def guess_icon(name, kind):
    low = name.lower()
    for k, ic in ICON_HINTS:
        if k in low:
            return ic
    return "audio-input-microphone-symbolic" if kind == "hw" else "audio-volume-high-symbolic"


ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icons")
_icons_added = False


def add_app_icons():
    """Make the icons bundled in assets/icons (e.g. amx-plus-symbolic) available by name."""
    global _icons_added
    if not _icons_added and os.path.isdir(ICON_DIR):
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(ICON_DIR)
        _icons_added = True


def safe_icon(name, fallback="audio-volume-high-symbolic"):
    """The first of `name` and its aliases the current theme has, else the fallback."""
    add_app_icons()
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    for cand in ([name] + ICON_ALIASES.get(name, []) if name else []) + \
            ([fallback] + ICON_ALIASES.get(fallback, []) if fallback else []):
        if theme.has_icon(cand):
            return cand
    return fallback


def _kwin_call(obj, iface, method, args=None, ret=None):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync("org.kde.KWin", obj, iface, method, args,
                         GLib.VariantType(ret) if ret else None, Gio.DBusCallFlags.NONE, 2000, None)


def kwin_unload(name):
    try:
        _kwin_call("/Scripting", "org.kde.kwin.Scripting", "unloadScript", GLib.Variant("(s)", (name,)), "(b)")
    except GLib.Error:
        pass
    try:
        os.unlink(os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", name + ".js"))
    except OSError:
        pass


def kwin_script(js, name, keep=False):
    """Run a KWin (KDE Plasma) script. `keep` leaves it loaded (e.g. to keep reacting to signals)."""
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", name + ".js")
    try:
        with open(path, "w") as f:
            f.write(js)
        try:
            _kwin_call("/Scripting", "org.kde.kwin.Scripting", "unloadScript", GLib.Variant("(s)", (name,)), "(b)")
        except GLib.Error:
            pass
        sid = _kwin_call("/Scripting", "org.kde.kwin.Scripting", "loadScript",
                         GLib.Variant("(ss)", (path, name)), "(i)").unpack()[0]
        for obj in (f"/Scripting/Script{sid}", f"/{sid}"):    # Plasma 6 path, then Plasma 5
            try:
                _kwin_call(obj, "org.kde.kwin.Script", "run")
                break
            except GLib.Error:
                continue
        if not keep:
            GLib.timeout_add(3000, lambda: (kwin_unload(name), False)[1])
        return True
    except (GLib.Error, OSError):
        return False


def kwin_cleanup_stale():
    """Unload KWin scripts left behind by an earlier run that didn't exit cleanly."""
    run = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    try:
        names = [f[:-3] for f in os.listdir(run) if f.startswith(("amx-flags-", "amx-keepabove-",
                                                                   "amx-aspect-")) and f.endswith(".js")]
    except OSError:
        return
    for name in names:
        if f"-{os.getpid()}-" not in name + "-":
            kwin_unload(name)


def kwin_window_flags(caption, skip_taskbar=None, keep_above=None):
    """Set KWin window flags on the window with this title. True on success."""
    kwin_window_flags.n = getattr(kwin_window_flags, "n", 0) + 1
    sets = []
    if skip_taskbar is not None:
        v = "true" if skip_taskbar else "false"
        sets += [f"w.skipTaskbar = {v};", f"w.skipPager = {v};", f"w.skipSwitcher = {v};"]
    if keep_above is not None:
        sets.append(f"w.keepAbove = {'true' if keep_above else 'false'};")
    js = ("const want = %s;\n"
          "const list = workspace.windowList ? workspace.windowList() : workspace.clientList();\n"
          "for (const w of list) { if (w.caption === want) { %s } }\n" % (json.dumps(caption), " ".join(sets)))
    return kwin_script(js, f"amx-flags-{os.getpid()}-{kwin_window_flags.n}")


def kwin_keep_above(caption, on):
    """Ask KWin to keep the window with this title above others. True on success."""
    kwin_keep_above.n = getattr(kwin_keep_above, "n", 0) + 1
    js = ("const want = %s;\n"
          "const list = workspace.windowList ? workspace.windowList() : workspace.clientList();\n"
          "for (const w of list) { if (w.caption === want) { w.keepAbove = %s; } }\n"
          % (json.dumps(caption), "true" if on else "false"))
    return kwin_script(js, f"amx-keepabove-{os.getpid()}-{kwin_keep_above.n}")


_tinted_cache = {}


def tinted_icon(name, size, rgb):
    """A theme icon as a pixbuf, recoloured (symbolic SVGs) so it reads on the dark deck keys."""
    key = (name, size, rgb)
    if key in _tinted_cache:
        return _tinted_cache[key]
    if len(_tinted_cache) > 400:
        _tinted_cache.clear()
    pb = None
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    paint = theme.lookup_icon(safe_icon(name), None, size, 1, Gtk.TextDirection.NONE, 0)
    f = paint.get_file() if paint else None
    path = f.get_path() if f else None
    if path:
        try:
            if path.endswith(".svg"):
                import re
                hexcol = "#%02x%02x%02x" % tuple(int(c * 255) for c in rgb)
                svg = open(path, encoding="utf-8", errors="replace").read()
                svg = re.sub(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", hexcol, svg)
                loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
                loader.set_size(size, size)
                loader.write(svg.encode())
                loader.close()
                pb = loader.get_pixbuf()
            else:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
        except (GLib.Error, OSError):
            pb = None
    _tinted_cache[key] = pb
    return pb


# --------------------------------------------------------------------------- meters
class Meters:
    """One `parec` per channel; reports decaying peak levels (0..1, dB-scaled)."""

    def __init__(self):
        self.procs = {}   # ch_id -> (Popen, source, watch_id)
        self._dead = set()   # channels whose watch already ended (parec exited)
        self.raw = {}
        self.level = {}

    def sync(self, wanted):
        for cid, (p, src, _w) in list(self.procs.items()):
            if wanted.get(cid) != src or p.poll() is not None:
                self._stop(cid)
        for cid, src in wanted.items():
            if cid not in self.procs:
                self._start(cid, src)

    def _start(self, cid, src):
        try:
            p = subprocess.Popen(
                ["parec", "-d", src, "--format=s16le", "--rate=8000", "--channels=1",
                 "--latency-msec=40", "--client-name=amx-meter"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return
        os.set_blocking(p.stdout.fileno(), False)
        w = GLib.io_add_watch(p.stdout.fileno(), GLib.PRIORITY_DEFAULT,
                              GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_data, cid)
        self.procs[cid] = (p, src, w)

    def _stop(self, cid):
        p, _src, w = self.procs.pop(cid)
        if w and cid not in self._dead:
            GLib.source_remove(w)
        self._dead.discard(cid)
        p.kill()
        p.wait()
        p.stdout.close()
        self.level.pop(cid, None)
        self.raw.pop(cid, None)

    def _on_data(self, fd, cond, cid):
        try:
            data = os.read(fd, 16384)
        except BlockingIOError:
            return True
        except OSError:
            self._dead.add(cid)
            return False
        if not data:
            self._dead.add(cid)
            return False
        a = array("h")
        a.frombytes(data[:len(data) // 2 * 2])
        if a:
            self.raw[cid] = max(self.raw.get(cid, 0), max(max(a), -min(a)) / 32768)
        return True

    def tick(self):
        for cid in self.procs:
            r = self.raw.get(cid, 0)
            self.raw[cid] = 0
            self.level[cid] = max(r, self.level.get(cid, 0) * 0.85)

    def stop(self):
        for cid in list(self.procs):
            self._stop(cid)


class PaEvents:
    """`pactl subscribe`: calls on_change() when the sound server's devices, streams or modules change.
    Lets the mixer re-sync only when something happened instead of polling pactl every couple of seconds.
    Client events are ignored: every pactl call the mixer itself makes shows up as one."""
    WATCH = (b" on sink", b" on source", b" on module", b" on server", b" on card")

    def __init__(self, on_change):
        self.on_change = on_change
        self.proc = self.watch = None
        self.buf = b""
        self.stopped = False
        self._start()

    def _start(self):
        self.watch = None
        if self.stopped:
            return False
        try:
            self.proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL)
        except OSError:
            self.proc = None
            return False
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.watch = GLib.io_add_watch(self.proc.stdout.fileno(), GLib.PRIORITY_DEFAULT,
                                       GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_data)
        return False

    def _on_data(self, fd, _cond):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if not data:                    # the sound server went away (or restarted): reconnect
            self.watch = None           # returning False removes it
            self.buf = b""
            self._reap()
            GLib.timeout_add(2000, self._start)
            self.on_change()
            return False
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
        if any(w in ln for ln in lines for w in self.WATCH):
            self.on_change()
        return True

    def _reap(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
                self.proc.stdout.close()
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.proc = None

    def stop(self):
        self.stopped = True
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = None
        self._reap()


def db_level(p):
    if p <= 0.0005:
        return 0.0
    return max(0.0, min(1.0, (20 * math.log10(p) + 54) / 54))


# --------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sinks, self.sources, self.inputs = [], [], []
        self.default_sink = None
        self.sink_mods = {}   # ("ch"|"mix", id) -> (module_id, loaded_at)
        self.links = {}       # ("c", mix, ch) | ("o", mix, sink) -> (module_id, t, src, sink)
        self.input_by_mod = {}
        self._prev_default = None   # the user's real default output, restored on quit
        self._prev_default_src = None   # ... and default microphone
        self.recorders_raw, self.default_source = [], None
        self.duck = {}        # ch_id -> current ducking factor (1.0 = not ducked)
        # PipeWire's loopback plays a burst of uninitialised memory when it starts (a loud pop),
        # so every new link starts muted and is unmuted once settled; a link being replaced keeps
        # playing until its replacement is live (make-before-break, no gap).
        self.fresh = {}       # module id -> creation time (held muted for LINK_SETTLE seconds)
        self.retiring = {}    # module id -> (time to mute and unload it, its source)
        self._settle_timer = None
        self.on_external_change = None   # called when the config changed from outside (UI refresh)
        self.cleanup_stale()
        self.fx = fxm.FxHost()
        self.fx_src = {}      # fid -> source name of a running effect chain
        self.instruments = ext.Instruments()   # FluidSynth per channel with a MIDI instrument

    def cleanup_stale(self):
        # the json listing has no module ids, so use the short format: "<id>\t<name>\t<args>"
        for line in (pactl("list", "short", "modules") or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and "amx_" in parts[2] and parts[0].isdigit():
                pactl("unload-module", parts[0])

    def refresh(self):
        self.sinks = pactl("list", "sinks", js=True) or []
        self.sources = pactl("list", "sources", js=True) or []
        self.inputs = pactl("list", "sink-inputs", js=True) or []
        self.recorders_raw = pactl("list", "source-outputs", js=True) or []
        info = pactl("info", js=True) or {}
        self.default_sink = info.get("default_sink_name")
        self.default_source = info.get("default_source_name")
        self.input_by_mod = {}
        for i in self.inputs:
            om = i.get("owner_module")
            if om not in (None, "", "n/a"):
                try:
                    self.input_by_mod[int(om)] = i
                except ValueError:
                    pass

    @property
    def hw_sinks(self):
        return [s for s in self.sinks if not s["name"].startswith("amx_")]

    @property
    def hw_sources(self):
        return [s for s in self.sources
                if not s["name"].startswith("amx_") and not s["name"].endswith(".monitor")]

    def apps(self):
        sink_name = {s["index"]: s["name"] for s in self.sinks}
        out = []
        for i in self.inputs:
            if i.get("owner_module") not in (None, "", "n/a"):
                continue
            p = i.get("properties") or {}
            if (p.get("application.name") or "").startswith("amx-") or \
                    p.get("application.process.id") in self.instruments.pids():   # our soundboard / synths
                continue
            out.append({"idx": i["index"],
                        "name": p.get("application.name") or p.get("media.name") or "Unknown",
                        "media": p.get("media.name") or "",
                        "sink": sink_name.get(i.get("sink"), ""),
                        "paused": bool(i.get("corked")),
                        "binary": (p.get("application.process.binary") or "").lower()})
        return sorted(out, key=lambda a: (a["name"].lower(), a["idx"]))

    # ---- running desktop apps (anything with a launcher, whether or not it makes sound)
    _INTERPRETERS = ("python", "bash", "sh", "java", "node", "env", "electron")
    _desk_cache = (0.0, {})

    def _desktop_index(self):
        """{binary basename (lowercase): display name} from installed .desktop launchers."""
        t, idx = self._desk_cache
        if time.time() - t < 60 and idx:
            return idx
        home = os.path.expanduser("~")
        dirs = ["/usr/share/applications", "/usr/local/share/applications",
                f"{home}/.local/share/applications",
                "/var/lib/flatpak/exports/share/applications",
                f"{home}/.local/share/flatpak/exports/share/applications"]
        idx = {}
        for d in dirs:
            try:
                files = [f for f in os.listdir(d) if f.endswith(".desktop")]
            except OSError:
                continue
            for f in files:
                try:
                    name = exe = None
                    skip = False
                    with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                        in_entry = False
                        for line in fh:
                            line = line.strip()
                            if line.startswith("["):
                                if in_entry:
                                    break
                                in_entry = line == "[Desktop Entry]"
                            elif in_entry and "=" in line:
                                k, v = line.split("=", 1)
                                if k == "Name" and name is None:
                                    name = v
                                elif k == "Exec" and exe is None:
                                    exe = v
                                elif k in ("NoDisplay", "Hidden", "Terminal") and v.lower() == "true":
                                    skip = True
                                elif k == "Type" and v != "Application":
                                    skip = True
                    if skip or not name or not exe:
                        continue
                    toks = [t for t in exe.split() if not t.startswith("%") and "=" not in t.split("/")[0]]
                    while toks and os.path.basename(toks[0]).startswith(self._INTERPRETERS) and len(toks) > 1:
                        toks = [t for t in toks[1:] if not t.startswith("-")]
                    if toks:
                        idx.setdefault(os.path.basename(toks[0]).lower(), name)
                except OSError:
                    continue
        Engine._desk_cache = (time.time(), idx)
        return idx

    def installed_apps(self):
        """Display names of every installed desktop app."""
        return sorted(set(self._desktop_index().values()), key=str.lower)

    def running_apps(self):
        """Display names of running desktop apps owned by this user."""
        idx = self._desktop_index()
        me = os.getuid()
        names = {}
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return []
        for pid in pids:
            try:
                if os.stat(f"/proc/{pid}").st_uid != me:
                    continue
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    argv = fh.read().split(b"\0")[:3]
            except OSError:
                continue
            cands = []
            for a in argv:
                a = a.decode(errors="replace")
                if not a or a.startswith("-"):
                    continue
                cands.append(os.path.basename(a).lower())
                cands.append(os.path.basename(os.path.dirname(a)).lower())
            for c in cands:
                for key in (c, c.removesuffix(".asar"), c.removesuffix(".py")):
                    if key in idx:
                        names.setdefault(idx[key].lower(), idx[key])
                        break
        names.pop("audio mixer", None)
        return sorted(names.values(), key=str.lower)

    def rule_keys(self, rule):
        """All lowercase identifiers an assigned-app rule should match against a stream."""
        keys = {rule.lower()}
        for binary, name in self._desktop_index().items():
            if name.lower() == rule.lower():
                keys.add(binary)
        return keys

    _CLIENT_SKIP = ("pipewire", "wireplumber", "kwin", "plasmashell", "kded", "polkit",
                    "xdg-desktop-portal", "pactl", "pacat", "parec", "paplay", "pw-")

    def clients(self):
        """Apps connected to the sound server (even if silent), minus desktop/system helpers."""
        found = {}
        for c in pactl("list", "clients", js=True) or []:
            p = c.get("properties") or {}
            name = (p.get("application.name") or "").strip()
            binary = p.get("application.process.binary") or ""
            if (not name or name.startswith(("WirePlumber", "amx-")) or name in ("pipewire", "KWin")
                    or binary.startswith(self._CLIENT_SKIP)):
                continue
            found.setdefault(name.lower(), name)
        return sorted(found.values(), key=str.lower)

    def sink_desc(self, name):
        for s in self.sinks:
            if s["name"] == name:
                return s.get("description") or name
        return f"{name} (not connected)"

    # ---- config helpers
    def chan(self, ch):
        ch.setdefault("vol", 100)
        ch.setdefault("mute", False)
        ch.setdefault("pan", 0)
        ch.setdefault("apps", [])
        ch.setdefault("icon", guess_icon(ch["name"], ch["type"]))
        return ch

    def cell(self, mix, ch):
        c = mix["levels"].setdefault(ch["id"], {})
        c.setdefault("vol", 100)
        c.setdefault("mute", False)
        c.setdefault("on", ch["type"] == "virtual")
        return c

    def gain(self, mix, ch):
        c, ch = self.cell(mix, ch), self.chan(ch)
        if c["mute"] or ch["mute"] or not c["on"]:
            return 0.0
        return c["vol"] / 100 * ch["vol"] / 100 * self.duck.get(ch["id"], 1.0)

    def meter_sources(self):
        names = {s["name"] for s in self.sinks}
        srcs = {s["name"] for s in self.sources}
        out = {}
        for ch in self.cfg["channels"]:
            fsrc = self.fx_src.get(f"c{ch['id']}")
            if fsrc and fsrc in srcs:                  # meter what the effects put out
                out[ch["id"]] = fsrc
            elif ch["type"] == "virtual" and f"amx_ch_{ch['id']}" in names:
                out[ch["id"]] = f"amx_ch_{ch['id']}.monitor"
            elif ch["type"] == "hw" and ch["source"] in srcs:
                out[ch["id"]] = ch["source"]
        return out

    # ---- modules
    def _unload(self, mod_id):
        pactl("unload-module", mod_id)

    def reconcile(self):
        cfg = self.cfg
        self.refresh()
        sink_names = {s["name"] for s in self.sinks}
        hw_sink_names = {s["name"] for s in self.hw_sinks}
        src_names = {s["name"] for s in self.sources}
        now = time.time()

        src_names_now = {s["name"] for s in self.sources}
        for key, (mid, t) in list(self.sink_mods.items()):
            name = {"ch": f"amx_ch_{key[1]}", "mix": f"amx_mix_{key[1]}", "mic": f"amx_mic_{key[1]}"}[key[0]]
            present = name in (src_names_now if key[0] == "mic" else sink_names)
            if not present and now - t > 3:
                self._unload(mid)
                del self.sink_mods[key]
        for key, (mid, t, _s, _d) in list(self.links.items()):
            if mid not in self.input_by_mod and now - t > 3:
                self._unload(mid)
                del self.links[key]

        want_sinks = {}
        for ch in cfg["channels"]:
            if ch["type"] == "virtual":
                want_sinks[("ch", ch["id"])] = (f"amx_ch_{ch['id']}", ch["name"])
        for m in cfg["mixes"]:
            want_sinks[("mix", m["id"])] = (f"amx_mix_{m['id']}", f"{m['name']} (mix)")
        # a mix can also be offered to apps as a microphone (a real input device, not a monitor)
        want_mics = {("mic", m["id"]): (f"amx_mic_{m['id']}", f"{m['name']} (mic)")
                     for m in cfg["mixes"] if "mic" in m["outputs"]}

        changed = False
        for key, (mid, _t) in list(self.sink_mods.items()):
            if key not in want_sinks and key not in want_mics:
                self._unload(mid)
                del self.sink_mods[key]
                changed = True

        for key, (name, desc) in want_sinks.items():
            if key in self.sink_mods or name in sink_names:
                continue
            out = pactl("load-module", "module-null-sink", f"sink_name={name}",
                        f"sink_properties=\"device.description='{desc.replace(chr(39), '').replace(chr(34), '')}'\"")
            if out and out.isdigit():
                self.sink_mods[key] = (int(out), now)
                changed = True
        if changed:
            self.refresh()
            sink_names = {s["name"] for s in self.sinks}

        src_now = {s["name"] for s in self.sources}           # virtual microphones
        mic_made = False
        for key, (name, desc) in want_mics.items():
            bus = f"amx_mix_{key[1]}"
            if key in self.sink_mods or name in src_now or bus not in sink_names:
                continue
            out = pactl("load-module", "module-remap-source", f"master={bus}.monitor",
                        f"source_name={name}",
                        f"source_properties=\"device.description='{desc.replace(chr(39), '').replace(chr(34), '')}'\"")
            if out and out.isdigit():
                self.sink_mods[key] = (int(out), now)
                mic_made = True
        if mic_made:
            self.refresh()

        # effect chains sit between a channel (or a mix bus) and the loopbacks that carry it
        self.fx_src = self.sync_fx(sink_names, {s["name"] for s in self.sources})
        src_names = {s["name"] for s in self.sources}

        want_links = {}
        for m in cfg["mixes"]:
            bus = f"amx_mix_{m['id']}"
            for ch in cfg["channels"]:
                if not self.cell(m, ch)["on"]:
                    continue
                fsrc = self.fx_src.get(f"c{ch['id']}")
                if fsrc:
                    src, ok = fsrc, True
                elif ch["type"] == "virtual":
                    src, ok = f"amx_ch_{ch['id']}.monitor", True
                else:
                    src = ch["source"]
                    ok = src in src_names
                if ok:
                    want_links[("c", m["id"], ch["id"])] = (src, bus)
            out_src = self.fx_src.get(f"m{m['id']}") or f"{bus}.monitor"
            for out in m["outputs"]:
                if out in hw_sink_names:
                    want_links[("o", m["id"], out)] = (out_src, out)

        now = time.time()
        for key, (mid, _t, src, sink) in list(self.links.items()):
            if want_links.get(key) != (src, sink):
                del self.links[key]
                # replaced: keep playing until the new link is live; removed: fade out now
                if key in want_links:
                    self.retiring[mid] = (now + LINK_SETTLE, src)
                else:
                    self._mute_module(mid)
                    self.retiring[mid] = (now, src)

        created = False
        for key, (src, sink) in want_links.items():
            if key in self.links or sink not in sink_names:
                continue
            if src.endswith(".monitor") and src[:-8] not in sink_names:
                continue
            if src.startswith("amx_fx_") and src not in src_names:
                continue
            mid = self._new_link(src, sink)
            if mid is not None:
                self.links[key] = (mid, now, src, sink)
                created = True
        if created:
            time.sleep(0.15)
            self.refresh()
        self.apply_levels()
        self._schedule_settle()
        self.instruments.sync(cfg["channels"], sink_names)
        self.apply_system_default(sink_names)
        self.apply_default_mic()
        self.enforce_app_rules(sink_names)
        self.enforce_rec_rules()

    # ---- effects
    def fx_wanted(self):
        """fid -> chain description for every channel / mix whose effects are on."""
        out = {}
        for ch in self.cfg["channels"]:
            if fxm.active(ch):
                virt = ch["type"] == "virtual"
                nds = fxm.nodes(ch)
                mono = not virt and self.aux_mono(ch.get("source"))
                out[f"c{ch['id']}"] = {"nodes": nds, "shape": fxm.shape(ch) + ((("mono", "mono"),) if mono else ()),
                                       "mono": mono, "capture_sink": virt,
                                       "target": f"amx_ch_{ch['id']}" if virt else ch["source"],
                                       "desc": ch["name"]}
        for m in self.cfg["mixes"]:
            if fxm.active(m) and any(o != "mic" for o in m["outputs"]):
                out[f"m{m['id']}"] = {"nodes": fxm.nodes(m), "shape": fxm.shape(m), "capture_sink": True,
                                      "target": f"amx_mix_{m['id']}", "desc": m["name"]}
        return out

    def sync_fx(self, sink_names, src_names):
        wanted = {fid: w for fid, w in self.fx_wanted().items()
                  if w["target"] in (sink_names if w["capture_sink"] else src_names)}
        started = self.fx.sync(wanted, src_names)
        if started:                      # wait for the new chains' sources before linking them
            names = {self.fx.source(fid) for fid in started}
            deadline = time.time() + 2.5
            while time.time() < deadline:
                have = {ln.split("\t")[1] for ln in (pactl("list", "short", "sources") or "").splitlines()
                        if ln.count("\t") >= 1}
                if names <= have:
                    break
                time.sleep(0.05)
            self.refresh()
        have = {s["name"] for s in self.sources}
        return {fid: self.fx.source(fid) for fid in wanted
                if self.fx.running(fid) and self.fx.source(fid) in have}

    # ---- links (loopbacks)
    def aux_mono(self, name):
        """True for a one-channel source whose channel has an AUX position (a microphone on a
        "pro-audio" card profile). Such a channel isn't left, right or mono, so PipeWire links
        it to one side of a stereo stream only: the mic would play in one ear."""
        s = next((x for x in self.sources if x.get("name") == name), None)
        cmap = [c.strip() for c in str((s or {}).get("channel_map") or "").split(",") if c.strip()]
        return len(cmap) == 1 and cmap[0] != "mono"

    def _new_link(self, src, sink):
        # a one-channel AUX source gets a mono loopback, which plays on both sides of the
        # sink (apply_levels makes up the 6 dB the AUX -> mono conversion costs)
        mono = ("channels=1", "channel_map=mono") if self.aux_mono(src) else ()
        out = pactl("load-module", "module-loopback", f"source={src}", f"sink={sink}",
                    f"latency_msec={self.link_latency()}", "source_dont_move=true",
                    "sink_dont_move=true", *mono)
        if not (out and out.isdigit()):
            return None
        mid = int(out)
        self.fresh[mid] = time.time()
        for _ in range(20):                       # mute it before it starts streaming
            if self._mute_module(mid):
                break
            time.sleep(0.005)
        return mid

    @staticmethod
    def _mute_module(mid, mute=True):
        """Mute the sink input that loopback module `mid` owns. False if it isn't there yet."""
        for i in pactl("list", "sink-inputs", js=True) or []:
            if str(i.get("owner_module")) == str(mid):
                pactl("set-sink-input-mute", i["index"], int(mute))
                return True
        return False

    def _schedule_settle(self):
        if self._settle_timer is None and (self.fresh or self.retiring):
            self._settle_timer = GLib.timeout_add(int(LINK_SETTLE * 1000) + 30, self._settle)

    def _settle(self):
        """Unmute settled new links, then mute and unload the links they replace."""
        self._settle_timer = None
        now = time.time()
        self.fresh = {m: t for m, t in self.fresh.items() if now - t < LINK_SETTLE}
        self.refresh()
        due = [m for m, (t, _s) in self.retiring.items() if t <= now + 0.01]
        old = [i["index"] for i in self.inputs if str(i.get("owner_module")) in {str(m) for m in due}]
        self.apply_levels()                      # unmutes the settled new links ...
        for ix in old:                           # ... and the links they replace go quiet right after
            pactl("set-sink-input-mute", ix, 1)
        if due:
            def unload():
                for m in due:
                    self._unload(m)
                    self.retiring.pop(m, None)
                self.fx.reap({src for _m, _t, src, _s in self.links.values()} |
                             {src for _t, src in self.retiring.values()})
                self._schedule_settle()
                return False
            GLib.timeout_add(60, unload)
        else:
            self._schedule_settle()
        return False

    def apply_fx(self):
        """Apply effect edits: live values when the chains keep their shape, else rebuild.
        Returns True when the graph was rebuilt."""
        wanted = self.fx_wanted()
        procs = self.fx.procs
        if set(wanted) != set(procs) or any(procs[f]["shape"] != w["shape"] or not self.fx.running(f)
                                            for f, w in wanted.items()):
            self.reconcile()
            return True
        ids = self._fx_node_ids()
        if any(f not in ids for f in wanted):
            self.recorders_raw = pactl("list", "source-outputs", js=True) or []
            ids = self._fx_node_ids()
        for fid, w in wanted.items():
            self.fx.push(fid, {f"{n}:{k}": v for n, _l, c in w["nodes"] for k, v in c.items()}, ids.get(fid))
        return False

    def _fx_node_ids(self):
        by_name = {(o.get("properties") or {}).get("node.name"): (o.get("properties") or {}).get("object.id")
                   for o in self.recorders_raw}
        return {fid: by_name[self.fx.capture_name(fid)] for fid in self.fx.procs
                if self.fx.capture_name(fid) in by_name}

    def relink(self):
        """Drop every loopback and build them again (e.g. after a latency change)."""
        for mid, *_ in list(self.links.values()):
            self._mute_module(mid)
            self._unload(mid)
        self.links.clear()
        time.sleep(0.1)
        self.reconcile()

    def link_latency(self):
        try:
            return max(1, min(200, int(self.cfg.get("latency_ms") or LINK_LATENCY_MS)))
        except (TypeError, ValueError):
            return LINK_LATENCY_MS

    def apply_levels(self):
        for key, (mod, _t, src, _s) in self.links.items():
            inp = self.input_by_mod.get(mod)
            mix = next((m for m in self.cfg["mixes"] if m["id"] == key[1]), None)
            if not inp or not mix:
                continue
            if key[0] == "c":
                ch = next((c for c in self.cfg["channels"] if c["id"] == key[2]), None)
                if not ch:
                    continue
                want_gain = self.gain(mix, ch)
                want_mute = bool(self.cell(mix, ch)["mute"] or ch["mute"])
                pan = pan_gains(ch.get("pan", 0))
            else:                                   # mix -> one output device, with its own level
                ol = (mix.get("out_levels") or {}).get(key[2]) or {}
                want_mute = bool(mix["mute"] or ol.get("mute"))
                want_gain = 0.0 if want_mute else mix["vol"] / 100 * ol.get("vol", 100) / 100
                pan = (1.0, 1.0)
            if mod in self.fresh:                   # still settling: keep it silent
                want_mute = True
            vols = inp.get("volume") or {}
            if list(vols) == ["mono"] and self.aux_mono(src):
                # AUX -> mono conversion halves the level: +6 dB back (volumes are cubic)
                want_gain *= 2 ** (1 / 3)
            # one value per stream channel, in channel-map order; left/right ones follow the pan
            want = [round(want_gain * FULL * (pan[0] if "left" in pos else pan[1] if "right" in pos else 1.0))
                    for pos in vols] or [round(want_gain * FULL)]
            have = [v.get("value", 0) for v in vols.values()] or [0]
            if len(have) != len(want) or any(abs(h - w) > 200 for h, w in zip(have, want)):
                pactl("set-sink-input-volume", inp["index"], *want)
                for v, w in zip(vols.values(), want):
                    v["value"] = w
            if bool(inp.get("mute")) != want_mute:
                pactl("set-sink-input-mute", inp["index"], int(want_mute))
                inp["mute"] = want_mute
        self.apply_mic_levels()        # a mix's virtual mic follows its fader too
        self.apply_hw_mutes()

    # ---- hardware input mute
    def apply_hw_mutes(self, release_all=False):
        """Muting a hardware input channel mutes the device itself, so apps that record it
        directly (Discord, OBS, a browser) go silent too, not only the mixes. Only sources the
        mixer muted are unmuted again; they are remembered in the config so a crash can't leave
        a microphone muted for good."""
        ours = self.cfg.setdefault("_hw_muted", [])
        # off by default: muting a mic channel then only silences it in the mixes (each mix
        # can still mute it on its own), and apps recording the mic directly keep hearing it
        want = set() if release_all or not self.cfg.get("hw_mute_device") else {
            c.get("source") for c in self.cfg["channels"]
            if c.get("type") == "hw" and c.get("mute") and c.get("source")}
        for s in self.sources:
            name = s.get("name")
            if name in want and name in ours and not s.get("mute"):
                # we muted it and something else (the desktop's mic key, pavucontrol) unmuted
                # it: follow that instead of muting it again behind the user's back
                for c in self.cfg["channels"]:
                    if c.get("type") == "hw" and c.get("source") == name:
                        c["mute"] = False
                ours.remove(name)
                if self.on_external_change:
                    self.on_external_change()
            elif name in want and not s.get("mute"):
                if pactl("set-source-mute", name, 1) is not None:
                    s["mute"] = True
                    if name not in ours:
                        ours.append(name)
            elif name in ours and name not in want:
                if s.get("mute") and pactl("set-source-mute", name, 0) is not None:
                    s["mute"] = False
                ours.remove(name)
        # a muted source that is unplugged stays listed, so it is unmuted when it comes back

    # ---- virtual microphones
    def apply_mic_levels(self):
        """A mix's virtual microphone follows the mix's volume and mute."""
        for m in self.cfg["mixes"]:
            if "mic" not in m["outputs"]:
                continue
            src = next((x for x in self.sources if x["name"] == f"amx_mic_{m['id']}"), None)
            if not src:
                continue
            want = round((0 if m["mute"] else m["vol"]) / 100 * FULL)
            if abs(vol_of(src.get("volume")) - want) > 200:
                pactl("set-source-volume", src["name"], want)
                for v in (src.get("volume") or {}).values():
                    v["value"] = want
            if bool(src.get("mute")) != bool(m["mute"]):
                pactl("set-source-mute", src["name"], int(m["mute"]))
                src["mute"] = bool(m["mute"])

    def apply_default_mic(self):
        mix = next((m for m in self.cfg["mixes"] if m.get("default_mic") and "mic" in m["outputs"]), None)
        name = f"amx_mic_{mix['id']}" if mix else None
        if name and any(x["name"] == name for x in self.sources):
            if self.default_source != name:
                if self._prev_default_src is None and self.default_source \
                        and not self.default_source.startswith("amx_"):
                    self._prev_default_src = self.default_source
                pactl("set-default-source", name)
                self.default_source = name
        else:
            self.restore_default_source()

    def restore_default_source(self):
        prev = self._prev_default_src
        if prev and any(x["name"] == prev for x in self.sources):
            pactl("set-default-source", prev)
        self._prev_default_src = None

    def recorders(self):
        """Apps that are recording audio (microphone users), not our own streams."""
        src_name = {x["index"]: x["name"] for x in self.sources}
        out = []
        for r in self.recorders_raw:
            if r.get("owner_module") not in (None, "", "n/a"):
                continue
            p = r.get("properties") or {}
            name = p.get("application.name") or p.get("media.name") or "Unknown"
            if (name.startswith("amx-") or name in ("pactl", "pavucontrol")
                    or (p.get("node.name") or "").startswith("amx_")):
                continue
            out.append({"idx": r["index"], "name": name, "media": p.get("media.name") or "",
                        "source": src_name.get(r.get("source"), "")})
        return sorted(out, key=lambda a: (a["name"].lower(), a["idx"]))

    def input_options(self):
        """Inputs an app can record from: hardware inputs plus the mixes' virtual microphones."""
        mics = [(f"{m['name']} (mic)", f"amx_mic_{m['id']}") for m in self.cfg["mixes"]
                if "mic" in m["outputs"]]
        hw = [(s.get("description") or s["name"], s["name"]) for s in self.hw_sources]
        return mics + hw

    def enforce_rec_rules(self):
        rules = {k.lower(): v for k, v in (self.cfg.get("rec_rules") or {}).items()}
        if not rules:
            return
        names = {x["name"] for x in self.sources}
        for r in self.recorders():
            tgt = rules.get(r["name"].lower())
            if tgt and tgt in names and r["source"] != tgt:
                pactl("move-source-output", r["idx"], tgt)

    def system_channel(self):
        return next((c for c in self.cfg["channels"]
                     if c.get("system") and c["type"] == "virtual"), None)

    def _system_sink(self, sink_names):
        ch = self.system_channel()
        sink = f"amx_ch_{ch['id']}" if ch else None
        return sink if (self.cfg.get("system_default") and sink in sink_names) else None

    def apply_system_default(self, sink_names):
        """Make the System channel the default output so all system sounds land in it."""
        sink = self._system_sink(sink_names)
        if sink:
            if self.default_sink != sink:
                if self._prev_default is None and self.default_sink \
                        and not self.default_sink.startswith("amx_"):
                    self._prev_default = self.default_sink
                pactl("set-default-sink", sink)
                self.default_sink = sink
        else:
            self.restore_default()

    def restore_default(self):
        if self._prev_default:
            if any(s["name"] == self._prev_default for s in self.hw_sinks):
                pactl("set-default-sink", self._prev_default)
            self._prev_default = None

    def enforce_app_rules(self, sink_names):
        """Assigned apps go to their channel; everything else goes to System (unless bypassed)."""
        rules = []  # (identifiers, channel sink)
        for ch in self.cfg["channels"]:
            if ch["type"] == "virtual" and not ch.get("system"):
                for a in ch.get("apps", []):
                    rules.append((self.rule_keys(a), f"amx_ch_{ch['id']}"))
        system = self._system_sink(sink_names)
        bypass = {b.lower() for b in self.cfg.get("bypass", [])}
        if not rules and not system:
            return
        for app in self.apps():
            ids = {app["name"].lower(), app["binary"]} - {""}
            tgt = next((t for keys, t in rules if ids & keys), None)
            if tgt is None:
                if ids & bypass:
                    continue
                tgt = system
            if tgt and tgt in sink_names and app["sink"] != tgt:
                pactl("move-sink-input", app["idx"], tgt)

    # ---- direct actions
    def rename_mix_sink(self, mix):
        pactl("update-sink-proplist", f"amx_mix_{mix['id']}",
              f"device.description={mix['name']} (mix)")
        if "mic" in mix["outputs"]:
            pactl("update-source-proplist", f"amx_mic_{mix['id']}",
                  f"device.description={mix['name']} (mic)")

    def rename_channel_sink(self, ch):
        if ch["type"] == "virtual":
            pactl("update-sink-proplist", f"amx_ch_{ch['id']}",
                  f"device.description={ch['name']}")

    def move_app(self, idx, sink):
        pactl("move-sink-input", idx, sink)

    def set_default_sink(self, name):
        pactl("set-default-sink", name)

    def teardown(self):
        self.restore_default()
        self.restore_default_source()
        self.apply_hw_mutes(release_all=True)    # give the user their microphones back
        for mid, *_ in list(self.links.values()):
            self._unload(mid)
        for mid in list(self.retiring):
            self._unload(mid)
        self.retiring.clear()
        self.fx.stop_all()
        self.instruments.stop_all()
        for mid, _t in list(self.sink_mods.values()):
            self._unload(mid)
        self.links.clear()
        self.sink_mods.clear()


# --------------------------------------------------------------------------- config
def normalize(cfg):
    """Fill defaults and migrate older configs (single `output` -> `outputs` list)."""
    for m in cfg["mixes"]:
        if "outputs" not in m:
            old = m.pop("output", None)
            m["outputs"] = [old] if old and old != "virtual" else []
        m.setdefault("levels", {})
        m.setdefault("vol", 100)
        m.setdefault("mute", False)
        m.setdefault("default_mic", False)
        m.setdefault("icon", "audio-headphones-symbolic")
        if m["icon"] == "audio-headphones-symbolic" and not m.get("icon_set"):
            guess = guess_icon(m["name"], "mix")
            if guess != "audio-volume-high-symbolic":
                m["icon"] = guess
    for ch in cfg["channels"]:
        ch.setdefault("apps", [])
    cfg.setdefault("system_default", True)   # catch every unassigned sound in the System channel
    cfg.setdefault("hw_mute_device", False)  # muting a mic channel also mutes the device for every app
    cfg.setdefault("bypass", [])             # apps deliberately sent straight to a device
    cfg.setdefault("rec_rules", {})          # app name -> input it should record from
    if not cfg.get("_vmic_init"):            # once: mixes named like a mic become virtual mics
        for m in cfg["mixes"]:
            if "mic" in m["name"].lower() and "mic" not in m["outputs"]:
                m["outputs"].append("mic")
        cfg["_vmic_init"] = True
    if not any(c.get("system") for c in cfg["channels"]):
        for c in cfg["channels"]:
            if c["type"] == "virtual" and c["name"].lower() == "system":
                c["system"] = True
                c["apps"] = []
                break
    return cfg


def default_config(eng):
    hw = eng.hw_sinks
    out = next((s["name"] for s in hw if s["name"] == eng.default_sink),
               hw[0]["name"] if hw else None)
    return normalize({
        "channels": [{"id": short_id(), "name": n, "type": "virtual", "system": n == "System"}
                     for n in ("Games", "Voice", "Music", "SFX", "Browser", "System")],
        "mixes": [{"id": short_id(), "name": "My Mix", "outputs": [out] if out else []},
                  {"id": short_id(), "name": "Stream", "outputs": [],
                   "icon": "network-wireless-symbolic"}],
    })


CONFIG_PROBLEM = None   # set when config.json couldn't be read; shown to the user on start


def load_config():
    global CONFIG_PROBLEM
    try:
        with open(CONF) as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and cfg.get("channels") is not None and cfg.get("mixes"):
            return normalize(cfg)
        problem = "it has no channels or mixes"
    except FileNotFoundError:
        return None                           # first run
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        problem = str(e) or type(e).__name__
    # Unreadable: keep it instead of silently overwriting the user's setup with defaults.
    keep = CONF + time.strftime(".broken-%Y%m%d-%H%M%S")
    try:
        os.replace(CONF, keep)
        CONFIG_PROBLEM = (f"Your settings file couldn't be read ({problem}), so the mixer started "
                          f"with a fresh setup. The old file was kept as {keep}.")
    except OSError:
        CONFIG_PROBLEM = f"Your settings file couldn't be read ({problem}); started with a fresh setup."
    print(CONFIG_PROBLEM, file=sys.stderr)
    return None


def save_config(cfg):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONF)


# --------------------------------------------------------------------------- widgets
CSS = b"""
.sidebar-pane { background: #1a1a1a; }
.side-head { font-size: 15px; font-weight: 600; padding: 6px 4px; }
.navbtn { padding: 9px 12px; border-radius: 6px; background: transparent; }
.navbtn:hover { background: rgba(255,255,255,0.05); }
.navbtn:checked { background: rgba(255,255,255,0.09); box-shadow: inset 3px 0 0 #35aef5; }
.devbtn { padding: 8px 12px; border-radius: 6px; background: transparent; }
.devbtn:hover { background: rgba(255,255,255,0.05); }
.cell, .mixhead { background: #2b2b2b; border-radius: 6px; padding: 10px 12px; }
.mixhead:hover { background: #313131; }
.cell-empty { background: #262626; border-radius: 6px; min-height: 40px; }
.cell-empty:hover { background: #303030; }
.tile { background: #3a3a3a; border-radius: 8px; min-width: 56px; min-height: 56px; }
.tile:hover { background: #454545; }
.bigtile { background: #3a3a3a; border-radius: 28px; min-width: 220px; min-height: 220px; }
.dlg-name { font-size: 20px; font-weight: 600; min-height: 44px; }
.chname, .mixname { font-size: 15px; font-weight: 600; }
.sub { opacity: .6; font-size: 0.9em; }
.page-title { font-size: 34px; font-weight: 600; }
.mutebtn { background: transparent; border-radius: 6px; min-width: 30px; min-height: 30px; padding: 0; }
.mutebtn:checked { background: transparent; color: #ff6b6b; }
scale.mscale trough { background: transparent; min-height: 22px; border: none; box-shadow: none; }
scale.mscale trough highlight, scale.mscale trough fill { background: transparent; border: none; }
scale.mscale slider { min-width: 16px; min-height: 16px; border-radius: 50%; background: #35aef5;
  border: none; box-shadow: 0 0 0 3px rgba(53,174,245,0.28); margin: 0; }
scale.plain trough highlight, scale.plain trough fill { background: #35aef5; }
scale.plain slider { background: #35aef5; min-width: 18px; min-height: 18px; }
.deck-device { background: linear-gradient(180deg, #232326, #141416); border-radius: 30px;
  padding: 26px 26px 14px 26px; box-shadow: 0 14px 34px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.07);
  border: 1px solid #2c2c30; }
.deck-brand { font-size: 9px; letter-spacing: 4px; color: #55565c; font-weight: 700; }
.deck-page-name { font-weight: 600; }
.page-dot { min-width: 18px; min-height: 18px; padding: 0 2px; font-size: 10px; color: #35aef5; }
.swatch { min-width: 24px; min-height: 24px; padding: 3px; border-radius: 999px; }
.swatch:checked { box-shadow: inset 0 0 0 2px #35aef5; background: transparent; }
.deck-popout { background: #111113; }
.dimbtn { opacity: .45; }
.sb-pad { padding: 10px 12px; }
.sb-pad:hover { background: #313131; }
.dimbtn:hover { opacity: 1; }
"""


class Meter(Gtk.DrawingArea):
    def __init__(self):
        super().__init__(hexpand=True, valign=Gtk.Align.CENTER)
        self.set_content_height(22)
        self.level = 0.0
        self.set_draw_func(self._draw)

    def set_level(self, v):
        if abs(v - self.level) > 0.01 or (v == 0 and self.level != 0):
            self.level = v
            self.queue_draw()

    @staticmethod
    def _rr(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _draw(self, _a, cr, w, h):
        import cairo
        th = 6
        y = (h - th) / 2
        self._rr(cr, 0, y, w, th, th / 2)
        cr.set_source_rgb(0.28, 0.28, 0.28)
        cr.fill_preserve()
        cr.clip()
        if self.level > 0:
            g = cairo.LinearGradient(0, 0, w, 0)
            g.add_color_stop_rgb(0, 0.20, 0.72, 0.30)
            g.add_color_stop_rgb(0.65, 0.35, 0.78, 0.25)
            g.add_color_stop_rgb(0.9, 0.90, 0.80, 0.20)
            g.add_color_stop_rgb(1, 0.95, 0.45, 0.20)
            cr.set_source(g)
            cr.rectangle(0, y, w * self.level, th)
            cr.fill()


def fader(value, on_change, level_fn, refs):
    """Slider overlaid on a level meter."""
    meter = Meter()
    refs.append((meter, level_fn))
    sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
    sc.add_css_class("mscale")
    sc.set_draw_value(False)
    sc.set_value(value)
    sc.set_hexpand(True)
    sc.set_valign(Gtk.Align.CENTER)
    sc.connect("value-changed", lambda s: on_change(round(s.get_value())))
    ov = Gtk.Overlay(hexpand=True, valign=Gtk.Align.CENTER)
    ov.set_child(meter)
    ov.add_overlay(sc)
    return ov


def mute_button(active, on_toggle):
    b = Gtk.ToggleButton(css_classes=["mutebtn", "flat"], valign=Gtk.Align.CENTER)

    def upd():
        b.set_icon_name("audio-volume-muted-symbolic" if b.get_active()
                        else "audio-volume-high-symbolic")
    b.set_active(active)
    upd()

    def toggled(btn):
        upd()
        on_toggle(btn.get_active())
    b.connect("toggled", toggled)
    return b


def clear(box):
    while (c := box.get_first_child()):
        box.remove(c)


def dropdown(options, selected_value, on_change):
    dd = Gtk.DropDown.new_from_strings([o[0] for o in options])
    vals = [o[1] for o in options]
    dd.set_selected(vals.index(selected_value) if selected_value in vals
                    else Gtk.INVALID_LIST_POSITION)
    dd.connect("notify::selected", lambda d, _p: (
        d.get_selected() < len(vals) and on_change(vals[d.get_selected()])))
    return dd


def clickable(widget, fn):
    g = Gtk.GestureClick()
    g.connect("released", lambda *_: fn())
    widget.add_controller(g)
    widget.set_cursor_from_name("pointer")


def pill_button(icon, label):
    """Wide dim button used for 'Add output' / 'Add app' style actions."""
    row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
    row.append(Gtk.Image(icon_name=icon))
    row.append(Gtk.Label(label=label))
    return row


# --------------------------------------------------------------------------- tray
SNI_XML = """<node><interface name="org.kde.StatusNotifierItem">
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="WindowId" type="i" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="OverlayIconName" type="s" access="read"/>
<property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionIconName" type="s" access="read"/>
<property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
<signal name="NewStatus"><arg type="s"/></signal>
<signal name="NewIcon"/>
</interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
 <arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
 <arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/>
 <arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/>
 <arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/>
 <arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
<property name="Version" type="u" access="read"/>
<property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconThemePath" type="as" access="read"/>
</interface></node>"""


class Tray:
    """System tray icon via the StatusNotifierItem protocol (KDE Plasma, most DEs).

    Left click toggles the window; the right-click menu has Show / Quit.
    """

    MENU = [(1, "Show mixer"), (4, "Show / hide deck"), (2, None), (3, "Quit mixer (stops routing)")]

    def __init__(self, on_toggle, on_show, on_quit, on_deck=None):
        self.on_toggle, self.on_show, self.on_quit, self.on_deck = on_toggle, on_show, on_quit, on_deck
        self.ok = False
        self.conn = None
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    def start(self):
        Gio.bus_own_name(Gio.BusType.SESSION, self.name, Gio.BusNameOwnerFlags.NONE,
                         self._bus_acquired, None, None)

    # ---- plumbing
    def _bus_acquired(self, conn, _name):
        self.conn = conn
        conn.register_object("/StatusNotifierItem", Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0],
                             self._sni_call, self._sni_get, None)
        conn.register_object("/MenuBar", Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0],
                             self._menu_call, self._menu_get, None)
        # (re)register whenever the tray watcher appears, e.g. after a plasmashell restart
        Gio.bus_watch_name_on_connection(conn, "org.kde.StatusNotifierWatcher",
                                         Gio.BusNameWatcherFlags.NONE,
                                         self._watcher_up, self._watcher_down)

    def _watcher_up(self, conn, _name, _owner):
        conn.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                  "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                  self._registered)

    def _registered(self, conn, res):
        try:
            conn.call_finish(res)
            self.ok = True
        except GLib.Error:
            self.ok = False

    def _watcher_down(self, *_):
        self.ok = False

    # ---- StatusNotifierItem
    def _sni_call(self, conn, sender, path, iface, method, params, inv):
        if method == "Activate":
            self.on_toggle()
        elif method == "SecondaryActivate":
            self.on_show()
        inv.return_value(None)   # ContextMenu/Scroll: the host draws the dbusmenu itself

    def _sni_get(self, conn, sender, path, iface, prop):
        empty_px = GLib.Variant("a(iiay)", [])
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "audio-mixer"),
            "Title": GLib.Variant("s", "Audio Mixer"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", APP_ID),
            "IconPixmap": empty_px,
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": empty_px,
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": empty_px,
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (APP_ID, [], "Audio Mixer",
                                                     "Click to show or hide the mixer")),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/MenuBar"),
        }.get(prop)

    # ---- dbusmenu
    @staticmethod
    def _props(label):
        if label is None:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True)}

    def _menu_call(self, conn, sender, path, iface, method, params, inv):
        if method == "GetLayout":
            kids = [GLib.Variant("(ia{sv}av)", (i, self._props(lbl), [])) for i, lbl in self.MENU]
            root = (0, {"children-display": GLib.Variant("s", "submenu")}, kids)
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (1, root)))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            items = [(i, self._props(lbl)) for i, lbl in self.MENU if not ids or i in ids]
            inv.return_value(GLib.Variant("(a(ia{sv}))", (items,)))
        elif method == "GetProperty":
            inv.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
        elif method == "Event":
            item_id, event = params.unpack()[:2]
            if event == "clicked":
                self._click(item_id)
            inv.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _d, _t in params.unpack()[0]:
                if event == "clicked":
                    self._click(item_id)
            inv.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_value(None)

    def _click(self, item_id):
        if item_id == 1:
            GLib.idle_add(lambda: self.on_show() and False)
        elif item_id == 3:
            GLib.idle_add(lambda: self.on_quit() and False)
        elif item_id == 4 and self.on_deck:
            GLib.idle_add(lambda: self.on_deck() and False)

    def _menu_get(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("as", [])}.get(prop)


# --------------------------------------------------------------------------- remote control
class ControlServer:
    """Local JSON-lines control socket (used by the OpenDeck / Stream Deck plugin).

    Client -> server, one JSON object per line:
      {"cmd":"subscribe"}                       state now, then again on every change
      {"cmd":"state"}                           one-shot state
      {"cmd":"channel","id":C,["mix":M],...}    vol=N | delta=N, mute=true|false|"toggle", on=...
                                                pan=-100..100 | pan_delta=N (channel only, no mix)
      {"cmd":"mix","id":M,...}                  vol / delta / mute
      {"cmd":"system","enable":true|false|"toggle"}
      {"cmd":"window","action":"show"|"hide"|"toggle"}
      {"cmd":"output","mix":M,"output":SINK,...}    one output device of a mix: vol / delta / mute
      {"cmd":"fx","target":C|M,"effect":E,["on":...],["param":P,"value":V]}
                                                E: denoise gate lowcut eq deess comp limiter loud
      {"cmd":"fx","target":C,"rack":TYPE|UID,"on":...}   a Creative Rack unit (first of that type)
      {"cmd":"duck","id":C,"on":...}               channel ducking on / off
      {"cmd":"soundboard","action":"play"|"stop"|"stop_all","id":ID}
      {"cmd":"profile","name":NAME}
    Without "mix", a channel command changes the channel's master fader.
    Channels, mixes, sounds and profiles can be named by id or by (case-insensitive) name.
    """

    def __init__(self, app):
        self.app = app
        self.path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "audio-mixer.sock")
        self.lsock = None
        self.clients = {}   # fileno -> {"sock", "buf", "sub"}
        self._last = None

    def start(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(self.path)
            os.chmod(self.path, 0o600)
            s.listen(8)
            s.setblocking(False)
        except OSError:
            return
        self.lsock = s
        GLib.io_add_watch(s.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._accept)
        GLib.timeout_add(200, self._push)

    def stop(self):
        for fd in list(self.clients):
            self._drop(fd)
        if self.lsock:
            self.lsock.close()
            self.lsock = None
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def _accept(self, *_):
        try:
            conn, _a = self.lsock.accept()
        except OSError:
            return True
        conn.settimeout(0.3)
        fd = conn.fileno()
        self.clients[fd] = {"sock": conn, "buf": b"", "sub": False}
        self.clients[fd]["watch"] = GLib.io_add_watch(
            fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._readable, fd)
        return True

    def _drop(self, fd):
        c = self.clients.pop(fd, None)
        if c:
            if c.get("watch"):              # a watch left on a closed fd would spin the main loop
                GLib.source_remove(c["watch"])
                c["watch"] = 0
            try:
                c["sock"].close()
            except OSError:
                pass

    def _readable(self, _fd, _cond, fd):
        c = self.clients.get(fd)
        if not c:
            return False
        try:
            data = c["sock"].recv(65536)
        except (BlockingIOError, socket.timeout):
            return True
        except OSError:
            data = b""
        if not data:
            c["watch"] = 0                 # returning False removes this watch
            self._drop(fd)
            return False
        c["buf"] += data
        if len(c["buf"]) > 1 << 20:        # a client that never sends a newline
            c["watch"] = 0
            self._drop(fd)
            return False
        while b"\n" in c["buf"]:
            line, c["buf"] = c["buf"].split(b"\n", 1)
            if line.strip():
                try:
                    reply = self._handle(c, json.loads(line))
                except Exception as e:  # never let a bad client message take the app down
                    reply = {"error": str(e)}
                self._send(fd, reply)
                if fd not in self.clients:    # dropped while replying
                    return False
        return True

    def _send(self, fd, obj):
        c = self.clients.get(fd)
        if not c:
            return
        try:
            c["sock"].sendall(json.dumps(obj).encode() + b"\n")
        except OSError:
            self._drop(fd)

    def _push(self):
        if not self.clients:
            return True
        cur = json.dumps(self.state(), sort_keys=True)
        if cur != self._last:
            self._last = cur
            st = json.loads(cur)
            for fd, c in list(self.clients.items()):
                if c["sub"]:
                    self._send(fd, st)
        return True

    def state(self):
        eng = self.app.eng
        cfg = eng.cfg
        cells = {}
        for m in cfg["mixes"]:
            cells[m["id"]] = {}
            for ch in cfg["channels"]:
                c = eng.cell(m, ch)
                cells[m["id"]][ch["id"]] = {"vol": c["vol"], "mute": bool(c["mute"]), "on": bool(c["on"])}
        return {
            "event": "state",
            "channels": [{"id": c["id"], "name": c["name"], "type": c["type"],
                          "vol": eng.chan(c)["vol"], "mute": bool(c["mute"]), "pan": c.get("pan", 0),
                          "system": bool(c.get("system")), "icon": c.get("icon")}
                         for c in cfg["channels"]],
            "mixes": [{"id": m["id"], "name": m["name"], "vol": m["vol"], "mute": bool(m["mute"]),
                       "outputs": len(m["outputs"]), "icon": m.get("icon"), "mic": "mic" in m["outputs"],
                       "outs": [{"sink": o, "name": eng.sink_desc(o),
                                 "vol": ((m.get("out_levels") or {}).get(o) or {}).get("vol", 100),
                                 "mute": bool(((m.get("out_levels") or {}).get(o) or {}).get("mute"))}
                                for o in m["outputs"] if o != "mic"]} for m in cfg["mixes"]],
            "cells": cells,
            "system_default": bool(cfg.get("system_default")),
            "fx": self.fx_state(),
            "duck": {c["id"]: bool((c.get("duck") or {}).get("on")) for c in cfg["channels"]},
            "soundboard": [{"id": s["id"], "name": s["name"], "playing": self.app.board.is_playing(s["id"])}
                           for s in self.app.board.board["sounds"]] if self.app.board else [],
            "profiles": sorted(ext.profiles(cfg), key=str.lower),
            "profile": cfg.get("profile"),
        }

    def fx_state(self):
        out = {}
        for o in self.app.eng.cfg["channels"] + self.app.eng.cfg["mixes"]:
            fx = fxm.fx_of(o)
            out[o["id"]] = {
                "active": fxm.active(o),
                "std": {s["key"]: {"title": s["title"], "on": bool(fx[s["key"]]["on"])} for s in fxm.offered(o)},
                "rack": [{"uid": u["uid"], "type": u["type"], "title": fxm.RACK[u["type"]]["title"],
                          "on": bool(u["on"])} for u in fx.get("rack", [])]}
        return out

    @staticmethod
    def find(items, ident):
        if ident is None:
            return None
        ident = str(ident)
        return (next((x for x in items if x.get("id") == ident), None)
                or next((x for x in items if x.get("name", "").lower() == ident.lower()), None))

    def _handle(self, c, msg):
        cmd = msg.get("cmd")
        if cmd == "subscribe":
            c["sub"] = True
            return self.state()
        if cmd == "state":
            return self.state()
        return self.command(msg)

    def command(self, msg):
        """Apply a control command (used by the socket and by the on-screen deck)."""
        cmd = msg.get("cmd")
        win, eng = self.app.win, self.app.eng
        cfg = eng.cfg

        def new_vol(cur):
            v = msg["vol"] if "vol" in msg else cur + msg.get("delta", 0)
            return max(0, min(100, round(v)))

        def new_bool(cur, key):
            v = msg.get(key)
            return (not cur) if v == "toggle" else bool(v)

        structural = False
        if cmd in ("channel", "mix"):
            if cmd == "channel":
                tgt = self.find(cfg["channels"], msg.get("id"))
            else:
                tgt = self.find(cfg["mixes"], msg.get("id"))
            if tgt is None:
                return {"error": f"no such {cmd}"}
            if cmd == "channel" and msg.get("mix"):
                mix = self.find(cfg["mixes"], msg["mix"])
                if mix is None:
                    return {"error": "no such mix"}
                tgt = eng.cell(mix, tgt)   # fader / mute / on for this channel in this mix
                if "on" in msg:
                    tgt["on"] = new_bool(tgt["on"], "on")
                    structural = True
            elif cmd == "channel":
                eng.chan(tgt)
            if "vol" in msg or "delta" in msg:
                tgt["vol"] = new_vol(tgt["vol"])
            if "mute" in msg:
                tgt["mute"] = new_bool(tgt["mute"], "mute")
            if "pan" in msg or "pan_delta" in msg:
                if cmd != "channel" or msg.get("mix"):
                    return {"error": "pan is set per channel"}
                try:
                    v = float(msg["pan"]) if "pan" in msg else tgt.get("pan", 0) + float(msg["pan_delta"])
                except (TypeError, ValueError):
                    return {"error": "pan needs a number from -100 (left) to 100 (right)"}
                tgt["pan"] = max(-100, min(100, round(v)))
        elif cmd == "system":
            cfg["system_default"] = new_bool(cfg.get("system_default", True), "enable")
            structural = True
        elif cmd == "window":
            act = msg.get("action", "toggle")
            if act == "show":
                self.app.show_window()
            elif act == "hide":
                win.set_visible(False)
            else:
                self.app.toggle_window()
            return {"ok": True}
        elif cmd == "output":
            mix = self.find(cfg["mixes"], msg.get("mix"))
            if mix is None or msg.get("output") not in mix["outputs"]:
                return {"error": "no such mix output"}
            ol = mix.setdefault("out_levels", {}).setdefault(msg["output"], {"vol": 100, "mute": False})
            if "vol" in msg or "delta" in msg:
                ol["vol"] = new_vol(ol.get("vol", 100))
            if "mute" in msg:
                ol["mute"] = new_bool(ol.get("mute", False), "mute")
        elif cmd == "fx":
            obj = self.find(cfg["channels"], msg.get("target")) or self.find(cfg["mixes"], msg.get("target"))
            if obj is None:
                return {"error": "no such channel or mix"}
            fx = fxm.fx_of(obj)
            if msg.get("rack"):
                r = str(msg["rack"]).lower()
                unit = next((u for u in fx.get("rack", []) if u["uid"] == r), None) or \
                    next((u for u in fx.get("rack", []) if u["type"] == r or
                          fxm.RACK[u["type"]]["title"].lower() == r), None)
                if unit is None:
                    return {"error": "no such rack unit on that channel"}
                if "on" in msg:
                    unit["on"] = new_bool(unit["on"], "on")
                if "param" in msg:
                    try:
                        unit["p"][str(msg["param"])] = float(msg["value"])
                    except (KeyError, TypeError, ValueError):
                        return {"error": "param needs a numeric value"}
            else:
                key = str(msg.get("effect") or "").lower()
                key = {"noise": "denoise", "rnnoise": "denoise", "compressor": "comp", "deesser": "deess",
                       "ear": "loud", "lufs": "loud", "lowcut": "lowcut"}.get(key, key)
                if key not in fx or key == "rack":
                    return {"error": f"no effect {key!r} on that {('mix' if 'outputs' in obj else 'channel')}"}
                if "on" in msg:
                    fx[key]["on"] = new_bool(fx[key]["on"], "on")
                if "param" in msg:
                    try:
                        fx[key][str(msg["param"])] = float(msg["value"])
                    except (KeyError, TypeError, ValueError):
                        return {"error": "param needs a numeric value"}
            win.changed()
            win.schedule("fx", 60, win.apply_fx)
            win.schedule("dialog-fx", 200, win.refresh_fx_widgets)
            return {"ok": True}
        elif cmd == "duck":
            ch = self.find(cfg["channels"], msg.get("id"))
            if ch is None:
                return {"error": "no such channel"}
            d = ch.setdefault("duck", {})
            d["on"] = new_bool(d.get("on", False), "on")
        elif cmd == "soundboard":
            board, act = self.app.board, msg.get("action", "play")
            if act == "stop_all":
                board.stop_all()
                return {"ok": True}
            s = board.sound(msg.get("id"))
            if s is None:
                return {"error": "no such sound"}
            if act == "stop":
                board.stop(s["id"])
                return {"ok": True}
            if act == "toggle" and board.is_playing(s["id"]):
                board.stop(s["id"])
                return {"ok": True}
            return {"ok": board.play(s["id"])}
        elif cmd == "profile":
            name = next((p for p in ext.profiles(cfg) if p.lower() == str(msg.get("name", "")).lower()), None)
            if name is None:
                return {"error": "no such profile"}
            win.switch_profile(name)
            return {"ok": True}
        else:
            return {"error": f"unknown cmd {cmd!r}"}

        win.changed()
        win.schedule("levels", 30, eng.apply_levels)
        if structural:
            win.schedule("struct", 50, win.structure_changed)
        else:
            win.schedule("matrix", 150, win.build_matrix)   # refresh sliders on screen
        return {"ok": True}


# --------------------------------------------------------------------------- virtual deck
DECK_MODELS = [("Stream Deck MK.2  (5 × 3)", "mk2", 5, 3), ("Stream Deck Mini  (3 × 2)", "mini", 3, 2),
               ("Stream Deck XL  (8 × 4)", "xl", 8, 4), ("Custom size", "custom", None, None)]
DECK_ACTIONS = [
    ("Audio · Channel", "channel"), ("Audio · Mix", "mix"), ("Audio · System sounds", "system"),
    ("Audio · Show / hide mixer", "window"), ("Audio · Effect on / off", "fx"),
    ("Audio · Ducking on / off", "duck"), ("Audio · Switch profile", "profile"),
    ("Soundboard · Sound", "sound"),
    ("System · Stat", "stat"), ("Media · Control", "media"),
    ("Deck · Change page", "page"), ("Launch · App", "app"), ("Launch · Website", "url"),
    ("Launch · Command", "command"), ("Empty", "empty")]
DECK_MODES = [("Toggle mute", "mute"), ("Volume up", "up"), ("Volume down", "down"),
              ("Set volume to…", "set"), ("Send to / remove from mix", "route")]
STATS = [("CPU usage", "cpu"), ("CPU temperature", "cpu_temp"), ("RAM", "ram"), ("GPU usage", "gpu"),
         ("GPU temperature", "gpu_temp"), ("GPU memory", "vram"), ("Download speed", "net_down"),
         ("Upload speed", "net_up"), ("Disk usage", "disk"), ("Clock", "clock")]
STAT_LABEL = {"cpu": "CPU", "cpu_temp": "CPU TEMP", "ram": "RAM", "gpu": "GPU", "gpu_temp": "GPU TEMP",
              "vram": "VRAM", "net_down": "DOWNLOAD", "net_up": "UPLOAD", "disk": "DISK", "clock": ""}
STAT_COLOR = {"cpu": (0.21, 0.68, 0.96), "cpu_temp": (1.0, 0.6, 0.25), "ram": (0.72, 0.5, 1.0),
              "gpu": (0.45, 0.85, 0.35), "gpu_temp": (1.0, 0.6, 0.25), "vram": (0.45, 0.85, 0.35),
              "net_down": (0.3, 0.85, 0.85), "net_up": (0.95, 0.75, 0.3), "disk": (0.8, 0.8, 0.8),
              "clock": (0.9, 0.9, 0.9)}
MEDIA_MODES = [("Play / pause", "playpause"), ("Next track", "next"), ("Previous track", "previous"),
               ("Stop", "stop")]
PAGE_MODES = [("Next page", "next"), ("Previous page", "prev"), ("Go to page…", "goto")]
SOUND_MODES = [("Play (layers on repeat)", "play"), ("Play / stop", "toggle"), ("Stop", "stop"),
               ("Stop all sounds", "stop_all")]
NEW_ACTIONS = ("fx", "duck", "profile", "sound")
DECK_ACTION_IDS = {a for _label, a in DECK_ACTIONS} - {"empty"}


def fx_choices(obj):
    """(label, value) for every effect a deck key can switch on this channel / mix."""
    out = [(s["title"], s["key"]) for s in fxm.offered(obj)]
    seen = set()
    for u in (obj.get("fx") or {}).get("rack", []):
        if u["type"] in fxm.RACK and u["type"] not in seen:
            seen.add(u["type"])
            out.append((f"Rack: {fxm.RACK[u['type']]['title']}", "rack:" + u["type"]))
    return out
BLUE, RED, GREY = (0.208, 0.682, 0.961), (1.0, 0.42, 0.42), (0.55, 0.55, 0.55)
DECK_COLORS = [("Automatic", None), ("Blue", "#35aef5"), ("White", "#eeeeee"), ("Green", "#73d95a"),
               ("Yellow", "#fad94d"), ("Orange", "#ff9940"), ("Red", "#ff6b6b"), ("Pink", "#ff80bf"),
               ("Purple", "#b880ff")]
DECK_ICON_EXTRAS = [
    "amx-plus-symbolic", "amx-minus-symbolic", "audio-volume-high-symbolic",
    "audio-volume-medium-symbolic", "audio-volume-low-symbolic", "audio-volume-muted-symbolic",
    "media-playback-start-symbolic", "media-playback-pause-symbolic", "media-skip-forward-symbolic",
    "media-skip-backward-symbolic", "go-next-symbolic", "go-previous-symbolic", "go-home-symbolic",
    "utilities-terminal-symbolic", "folder-symbolic", "starred-symbolic", "system-run-symbolic",
    "mail-unread-symbolic", "preferences-system-symbolic", "view-grid-symbolic", "camera-photo-symbolic",
    "display-brightness-symbolic", "night-light-symbolic", "system-lock-screen-symbolic",
    "system-shutdown-symbolic", "edit-copy-symbolic", "document-save-symbolic", "web-browser-symbolic",
    "applications-graphics-symbolic", "applications-science-symbolic", "emoji-people-symbolic",
    "face-smile-symbolic", "help-about-symbolic", "dialog-warning-symbolic"]


def hex_rgb(h):
    if not h:
        return None
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def deck_icon_choices():
    """Icons offered for keys: the mixer set plus key-ish extras, de-duplicated, theme-available."""
    out, seen = [], set()
    for ic in DECK_ICON_EXTRAS[:6] + PICKER_ICONS + DECK_ICON_EXTRAS[6:]:   # +, − and volume first
        real = safe_icon(ic, "")
        if real and real not in seen:
            seen.add(real)
            out.append(ic)
    return out


_image_cache = {}


def key_image(path, size):
    """A user image for a key (cached; reloads when the file changes)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, mtime, size)
    if key not in _image_cache and len(_image_cache) > 100:
        _image_cache.clear()
    if key not in _image_cache:
        try:
            _image_cache[key] = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        except GLib.Error:
            _image_cache[key] = None
    return _image_cache[key]


def fmt_rate(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps / 1024 ** 2:.1f} MB/s"


class SysStats:
    """Samples CPU / RAM / GPU / network / disk once a second (only while a stat key is visible)."""

    def __init__(self):
        self.vals = {}
        self.hist = {k: collections.deque(maxlen=36) for _l, k in STATS}
        self._cpu = self._net = self._nv = None
        self._nv_busy = False
        self._has_nv = bool(shutil.which("nvidia-smi"))
        self._cpu_temp = self._find_cpu_temp()
        self._amd = None if self._has_nv else self._find_amd_gpu()

    @staticmethod
    def _read(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None

    def _find_cpu_temp(self):
        for name, label in (("k10temp", "Tctl"), ("zenpower", "Tdie"), ("coretemp", "Package id 0")):
            for h in glob.glob("/sys/class/hwmon/hwmon*"):
                if self._read(h + "/name") != name:
                    continue
                for lab in glob.glob(h + "/temp*_label"):
                    if self._read(lab) == label:
                        return lab.replace("_label", "_input")
                if os.path.exists(h + "/temp1_input"):
                    return h + "/temp1_input"
        p = "/sys/class/thermal/thermal_zone0/temp"
        return p if os.path.exists(p) else None

    def _find_amd_gpu(self):
        best, best_vram = None, -1
        for dev in glob.glob("/sys/class/drm/card*/device"):
            if os.path.exists(dev + "/gpu_busy_percent"):
                vram = int(self._read(dev + "/mem_info_vram_total") or 0)
                if vram > best_vram:
                    best, best_vram = dev, vram
        return best

    def _nv_query(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
            self._nv = tuple(float(x) for x in out.splitlines()[0].split(","))
        except Exception:
            pass
        finally:
            self._nv_busy = False

    def sample(self):
        v = {}
        try:                                                     # CPU usage
            nums = [int(x) for x in self._read("/proc/stat").splitlines()[0].split()[1:9]]
            idle, total = nums[3] + nums[4], sum(nums)
            if self._cpu and total > self._cpu[0]:
                p = 100 * (1 - (idle - self._cpu[1]) / (total - self._cpu[0]))
                v["cpu"] = (f"{p:.0f}%", f"{os.cpu_count()} threads", p / 100, p)
            self._cpu = (total, idle)
        except (AttributeError, ValueError, IndexError):
            pass
        t = self._read(self._cpu_temp) if self._cpu_temp else None   # CPU temperature
        if t and t.lstrip("-").isdigit():
            c = int(t) / 1000
            v["cpu_temp"] = (f"{c:.0f}°C", "", min(1, c / 100), c)
        try:                                                     # RAM
            mem = {ln.split(":")[0]: int(ln.split()[1]) for ln in self._read("/proc/meminfo").splitlines()}
            used, total = mem["MemTotal"] - mem["MemAvailable"], mem["MemTotal"]
            v["ram"] = (f"{used / 1048576:.1f} GB", f"of {total / 1048576:.0f} GB", used / total,
                        100 * used / total)
        except (AttributeError, KeyError, ValueError):
            pass
        if self._has_nv:                                         # GPU (NVIDIA)
            if not self._nv_busy:
                self._nv_busy = True
                threading.Thread(target=self._nv_query, daemon=True).start()
            if self._nv:
                u, temp, mu, mt = self._nv
                v["gpu"] = (f"{u:.0f}%", "NVIDIA", u / 100, u)
                v["gpu_temp"] = (f"{temp:.0f}°C", "", min(1, temp / 100), temp)
                v["vram"] = (f"{mu / 1024:.1f} GB", f"of {mt / 1024:.0f} GB", mu / mt if mt else 0,
                             100 * mu / mt if mt else 0)
        elif self._amd:                                          # GPU (AMD)
            u = int(self._read(self._amd + "/gpu_busy_percent") or 0)
            v["gpu"] = (f"{u}%", "AMD", u / 100, u)
            for h in glob.glob(self._amd + "/hwmon/hwmon*/temp1_input"):
                temp = int(self._read(h) or 0) / 1000
                v["gpu_temp"] = (f"{temp:.0f}°C", "", min(1, temp / 100), temp)
            mu = int(self._read(self._amd + "/mem_info_vram_used") or 0)
            mt = int(self._read(self._amd + "/mem_info_vram_total") or 0)
            if mt:
                v["vram"] = (f"{mu / 1024 ** 3:.1f} GB", f"of {mt / 1024 ** 3:.0f} GB", mu / mt,
                             100 * mu / mt)
        try:                                                     # network
            rx = tx = 0
            for ln in self._read("/proc/net/dev").splitlines()[2:]:
                name, data = ln.split(":", 1)
                if name.strip() == "lo":
                    continue
                f = data.split()
                rx, tx = rx + int(f[0]), tx + int(f[8])
            now = time.monotonic()
            if self._net:
                dt = max(1e-3, now - self._net[0])
                down, up = (rx - self._net[1]) / dt, (tx - self._net[2]) / dt
                v["net_down"] = (fmt_rate(down), "", None, down)
                v["net_up"] = (fmt_rate(up), "", None, up)
            self._net = (now, rx, tx)
        except (AttributeError, ValueError, IndexError):
            pass
        try:                                                     # disk
            st = os.statvfs("/")
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            pct = 100 * (1 - free / total)
            v["disk"] = (f"{pct:.0f}%", f"{free / 1024 ** 3:.0f} GB free", pct / 100, pct)
        except OSError:
            pass
        v["clock"] = (time.strftime("%H:%M"), time.strftime("%a %d %b"), None, None)
        for k, (val, sub, frac, num) in v.items():
            self.vals[k] = {"value": val, "sub": sub, "frac": frac}
            if num is not None:
                self.hist[k].append(num)


class Media:
    """Controls whichever MPRIS player is playing (or was last paused)."""
    IFACE = "org.mpris.MediaPlayer2.Player"

    def __init__(self):
        self.bus = None
        self.info = {"status": "Stopped", "title": "", "artist": "", "player": None}

    def _bus(self):
        if self.bus is None:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self.bus

    def _get(self, name, prop):
        try:
            return self._bus().call_sync(
                name, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (self.IFACE, prop)), GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE, 400, None).unpack()[0]
        except GLib.Error:
            return None

    def refresh(self):
        try:
            names = self._bus().call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
                None, GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 400, None).unpack()[0]
        except GLib.Error:
            names = []
        players = [n for n in names if n.startswith("org.mpris.MediaPlayer2.")]
        if any("firefox" in p for p in players):          # the KDE proxy duplicates the browser
            players = [p for p in players if "plasma-browser-integration" not in p]
        rank = {"Playing": 2, "Paused": 1}
        best, best_rank = None, -1
        for p in players:
            st = self._get(p, "PlaybackStatus") or "Stopped"
            if rank.get(st, 0) > best_rank:
                best, best_rank, best_st = p, rank.get(st, 0), st
        if not best:
            self.info = {"status": "Stopped", "title": "", "artist": "", "player": None}
            return
        md = self._get(best, "Metadata") or {}
        artist = md.get("xesam:artist") or ""
        self.info = {"status": best_st, "title": md.get("xesam:title") or "",
                     "artist": ", ".join(artist) if isinstance(artist, list) else artist,
                     "player": best}

    def command(self, mode):
        self.refresh()
        p = self.info["player"]
        method = {"playpause": "PlayPause", "next": "Next", "previous": "Previous", "stop": "Stop"}.get(mode)
        if not p or not method:
            return False
        self._bus().call(p, "/org/mpris/MediaPlayer2", self.IFACE, method, None, None,
                         Gio.DBusCallFlags.NONE, 1000, None, None, None)
        return True


_icon_cache = {}


def app_info(app_id):
    try:
        return Gio.DesktopAppInfo.new(app_id) if app_id else None
    except TypeError:
        return None


def app_pixbuf(app_id, size=48):
    key = (app_id, size)
    if key not in _icon_cache and len(_icon_cache) > 300:
        _icon_cache.clear()
    if key not in _icon_cache:
        pb = None
        info = app_info(app_id)
        if info and info.get_icon():
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            paint = theme.lookup_by_gicon(info.get_icon(), size, 1, Gtk.TextDirection.NONE, 0)
            f = paint.get_file() if paint else None
            if f and f.get_path():
                try:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_size(f.get_path(), size, size)
                except GLib.Error:
                    pb = None
        _icon_cache[key] = pb
    return _icon_cache[key]


def installed_app_options():
    apps = [a for a in Gio.AppInfo.get_all() if a.should_show() and a.get_id()]
    return sorted(((a.get_display_name(), a.get_id()) for a in apps), key=lambda x: x[0].lower())


def default_deck(cfg):
    """A Stream Deck MK.2 with three pages: Audio, Volume, System & media."""
    chans, mixes = cfg["channels"], cfg["mixes"]

    def kc(c, mode="mute"):
        return {"action": "channel", "channel": c["id"], "scope": "master", "mode": mode, "step": 5}
    seq = ([kc(c) for c in chans] + [{"action": "mix", "mix": m["id"], "mode": "mute"} for m in mixes] +
           [{"action": "system", "mode": "toggle"}, {"action": "window", "mode": "toggle"}])
    slots = [(r, c) for r in range(3) for c in range(5) if (r, c) != (2, 4)]
    audio = {f"{r},{c}": k for (r, c), k in zip(slots, seq)}
    audio["2,4"] = {"action": "page", "mode": "next"}
    vol = {}
    for i, c in enumerate(chans[:4]):
        vol[f"0,{i}"], vol[f"1,{i}"], vol[f"2,{i}"] = kc(c, "up"), kc(c, "mute"), kc(c, "down")
    vol["0,4"] = {"action": "page", "mode": "prev"}
    vol["1,4"] = {"action": "window", "mode": "toggle"}
    vol["2,4"] = {"action": "page", "mode": "next"}
    sysp = {}
    for i, s in enumerate(("cpu", "cpu_temp", "gpu", "gpu_temp", "clock")):
        sysp[f"0,{i}"] = {"action": "stat", "stat": s}
    for i, s in enumerate(("ram", "vram", "net_down", "net_up", "disk")):
        sysp[f"1,{i}"] = {"action": "stat", "stat": s}
    sysp["2,0"] = {"action": "page", "mode": "prev"}
    for i, m in enumerate(("previous", "playpause", "next")):
        sysp[f"2,{i + 1}"] = {"action": "media", "mode": m}
    sysp["2,4"] = {"action": "page", "mode": "next"}
    return {"model": "mk2", "cols": 5, "rows": 3, "page": 0,
            "pages": [{"name": "Audio", "keys": audio}, {"name": "Volume", "keys": vol},
                      {"name": "System & media", "keys": sysp}]}


def normalize_deck(cfg):
    d = cfg.get("deck")
    if d and "pages" not in d:     # older single-grid layout: keep its keys on their own pages
        old = [k for k in d.get("keys", []) if k]
        new = default_deck(cfg)
        per, cols = new["cols"] * new["rows"], new["cols"]
        for i in range(0, len(old), per):
            new["pages"].append({"name": "My keys" + (f" {i // per + 1}" if i else ""),
                                 "keys": {f"{j // cols},{j % cols}": k
                                          for j, k in enumerate(old[i:i + per])}})
        d = new
    if not d:
        d = default_deck(cfg)
    d.setdefault("model", "custom")
    d.setdefault("page", 0)
    if not d.get("pages"):
        d["pages"] = [{"name": "Page 1", "keys": {}}]
    d["page"] = max(0, min(int(d["page"]), len(d["pages"]) - 1))
    cfg["deck"] = d
    return d


def deck_msg(key, ticks=0):
    """The mixer control command an audio key press (or scroll tick) stands for."""
    act = key.get("action")
    step = int(key.get("step") or 5)
    if act == "system":
        m = key.get("mode") or "toggle"
        return {"cmd": "system", "enable": "toggle" if m == "toggle" else m == "on"}
    if act == "window":
        return {"cmd": "window", "action": key.get("mode") or "toggle"}
    if act in NEW_ACTIONS:
        if ticks:
            return None
        if act == "sound":
            mode = key.get("mode") or "play"
            return {"cmd": "soundboard", "action": mode, "id": key.get("sound")} \
                if mode == "stop_all" or key.get("sound") else None
        if act == "fx":
            eff = key.get("effect") or ""
            if not key.get("target") or not eff:
                return None
            sel = {"rack": eff[5:]} if eff.startswith("rack:") else {"effect": eff}
            return {"cmd": "fx", "target": key["target"], "on": "toggle", **sel}
        if act == "duck":
            return {"cmd": "duck", "id": key.get("channel"), "on": "toggle"} if key.get("channel") else None
        if act == "profile":
            return {"cmd": "profile", "name": key.get("profile")} if key.get("profile") else None
        return None
    if act == "mix":
        target = key.get("mix")
        base = {"cmd": "mix", "id": target}
    elif act == "channel":
        target = key.get("channel")
        base = {"cmd": "channel", "id": target}
        if (key.get("scope") or "master") != "master":
            base["mix"] = key["scope"]
    else:
        return None
    if not target:
        return None
    if ticks:
        return {**base, "delta": ticks * step}
    mode = key.get("mode") or "mute"
    if mode == "mute":
        return {**base, "mute": "toggle"}
    if mode == "up":
        return {**base, "delta": step}
    if mode == "down":
        return {**base, "delta": -step}
    if mode == "set":
        return {**base, "vol": int(50 if key.get("setTo") is None else key["setTo"])}
    if mode == "route" and act == "channel" and "mix" in base:
        return {**base, "on": "toggle"}
    return None


def deck_view(key, st, deck):
    """Everything a key needs to draw itself. None means an empty key."""
    if not key or key.get("action") not in DECK_ACTION_IDS:   # empty, or an action that was removed
        return None
    v = _deck_view(key, st, deck)
    if key.get("title"):
        v["lines"] = [key["title"]] + v["lines"][1:]
    v["icon_override"] = key.get("icon")
    v["image"] = key.get("image")
    v["color"] = hex_rgb(key.get("color"))
    return v


def _deck_view(key, st, deck):
    act = key["action"]
    if act == "window":
        return {"kind": "window", "lines": ["Mixer", "show / hide"]}
    if act == "system":
        on = bool(st.get("system_default"))
        sysch = next((c for c in st["channels"] if c.get("system")), None)
        return {"kind": "audio", "lines": [sysch["name"] if sysch else "System", "ON" if on else "OFF"],
                "vol": None, "muted": False, "on": on, "waves": 3 if on else 0}
    if act == "stat":
        s = key.get("stat") or "cpu"
        val = deck.win.stats.vals.get(s) or {"value": "…", "sub": "", "frac": None}
        return {"kind": "stat", "stat": s, "lines": [STAT_LABEL.get(s, s.upper())],
                "value": val["value"], "sub": val["sub"], "frac": val["frac"],
                "hist": tuple(deck.win.stats.hist.get(s, ()))}
    if act == "media":
        mode = key.get("mode") or "playpause"
        info = deck.win.media.info
        playing = info["status"] == "Playing"
        if mode == "playpause":
            lines = ([info["title"], info["artist"]] if info["title"] else
                     ["Play / pause", "nothing playing" if not info["player"] else info["status"].lower()])
        else:
            lines = [dict((m, lbl) for lbl, m in MEDIA_MODES)[mode].replace(" track", ""), ""]
        return {"kind": "media", "mode": mode, "playing": playing, "lines": lines}
    if act == "page":
        mode = key.get("mode") or "next"
        n, cur = len(deck.d["pages"]), deck.d["page"]
        target = {"next": (cur + 1) % n, "prev": (cur - 1) % n}.get(mode, int(key.get("page") or 0) % n)
        label = {"next": "Next page", "prev": "Previous page"}.get(mode, deck.d["pages"][target]["name"])
        return {"kind": "page", "mode": mode, "lines": [label, f"{target + 1} / {n}"]}
    if act == "app":
        info = app_info(key.get("app"))
        return {"kind": "app", "app": key.get("app"),
                "lines": [info.get_display_name() if info else "Pick an app", ""]}
    if act == "url":
        url = key.get("url") or ""
        host = url.split("://", 1)[-1].split("/", 1)[0].removeprefix("www.")
        return {"kind": "url", "lines": [host or "Website", ""]}
    if act == "command":
        cmd = key.get("command") or ""
        return {"kind": "command", "lines": ["Command", cmd or "not set"]}
    if act == "sound":
        mode = key.get("mode") or "play"
        if mode == "stop_all":
            return {"kind": "sound", "glyph": "stop", "lines": ["Stop all", "sounds"], "on": True}
        snd = next((x for x in st.get("soundboard", []) if x["id"] == key.get("sound")), None)
        if not snd:
            return {"kind": "sound", "glyph": "play", "lines": ["Pick", "a sound"], "on": False}
        playing = snd["playing"]
        glyph = "stop" if mode == "stop" or (mode == "toggle" and playing) else "play"
        return {"kind": "sound", "glyph": glyph, "lines": [snd["name"], "playing" if playing else snd["id"]],
                "on": True, "playing": playing}
    if act == "fx":
        obj = next((x for x in st["channels"] + st["mixes"] if x["id"] == key.get("target")), None)
        f = (st.get("fx") or {}).get(key.get("target")) or {}
        eff = key.get("effect") or ""
        if eff.startswith("rack:"):
            u = next((u for u in f.get("rack", []) if u["type"] == eff[5:]), None)
            title, on = (u["title"], u["on"]) if u else (fxm.RACK.get(eff[5:], {}).get("title", "?"), False)
        else:
            e = (f.get("std") or {}).get(eff)
            title, on = (e["title"], e["on"]) if e else ("Pick", False)
        if not obj:
            return {"kind": "fx", "lines": ["Pick a", "channel"], "on": False}
        return {"kind": "fx", "lines": [title, f"{obj['name']} · {'ON' if on else 'OFF'}"], "on": on}
    if act == "duck":
        ch = next((x for x in st["channels"] if x["id"] == key.get("channel")), None)
        on = bool((st.get("duck") or {}).get(key.get("channel")))
        return {"kind": "fx", "lines": [ch["name"] if ch else "Pick", "ducking " + ("ON" if on else "OFF")],
                "on": on}
    if act == "profile":
        name = key.get("profile")
        cur = name and name == st.get("profile")
        return {"kind": "profile", "lines": [name or "Pick a", "current" if cur else "profile"], "on": bool(cur)}
    step = int(key.get("step") or 5)                     # channel / mix
    if act == "mix":
        m = next((x for x in st["mixes"] if x["id"] == key.get("mix")), None)
        if not m:
            return {"kind": "audio", "lines": ["Pick", "a mix"], "vol": None, "muted": False, "on": False}
        name, vol, muted, on, icon = m["name"], m["vol"], m["mute"], True, m.get("icon")
    else:
        ch = next((x for x in st["channels"] if x["id"] == key.get("channel")), None)
        if not ch:
            return {"kind": "audio", "lines": ["Pick", "a channel"], "vol": None, "muted": False, "on": False}
        name, vol, muted, on, icon = ch["name"], ch["vol"], ch["mute"], True, ch.get("icon")
        scope = key.get("scope") or "master"
        if scope != "master":
            cell = st["cells"].get(scope, {}).get(ch["id"])
            if cell:
                vol, muted, on = cell["vol"], cell["mute"] or ch["mute"], cell["on"]
    mode = key.get("mode") or "mute"
    value = "Muted" if muted else (f"{vol}%" if on else "Off")
    value = {"up": f"+{step}%  ·  {value}", "down": f"-{step}%  ·  {value}"}.get(mode, value)
    return {"kind": "audio", "lines": [name, value], "vol": vol, "muted": muted, "on": on, "icon": icon}


# ---- drawing helpers (cairo)
def _rr(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _text(cr, s, cx, y, size, color, bold=True, maxw=None):
    import cairo
    if not s:
        return
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    ext = cr.text_extents(s)
    while maxw and ext.width > maxw and size > 8:
        size -= 0.5
        cr.set_font_size(size)
        ext = cr.text_extents(s)
    while maxw and ext.width > maxw and len(s) > 2:
        s = s[:-2].rstrip() + "…"
        ext = cr.text_extents(s)
    cr.set_source_rgb(*color)
    cr.move_to(cx - ext.width / 2 - ext.x_bearing, y)
    cr.show_text(s)


def draw_speaker(cr, color, waves=3, cross=False, dim=False):
    """The logo speaker, in a 144x144 box."""
    cr.save()
    cr.translate(72, 80)
    cr.scale(1.5, 1.5)
    cr.translate(-34.5, -37.5)
    cr.set_source_rgba(*color, 0.45 if dim else 1)
    cr.set_line_width(5)
    cr.set_line_join(1)
    cr.set_line_cap(1)
    cr.move_to(39.389, 13.769)
    for x, y in ((22.235, 28.606), (6, 28.606), (6, 47.699), (21.989, 47.699), (39.389, 62.75)):
        cr.line_to(x, y)
    cr.close_path()
    cr.fill_preserve()
    cr.stroke()
    if not cross:
        for i, (rad, ang) in enumerate(((19.5, 33.3), (30, 36.4), (38.8, 38.8))):
            if i < waves:
                cr.new_path()
                cr.arc(31.5, 38.3, rad, -math.radians(ang), math.radians(ang))
                cr.stroke()
    else:
        cr.set_source_rgb(*RED)
        cr.new_path()
        for x1, y1, x2, y2 in ((48, 26, 66, 50), (66, 26, 48, 50)):
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
        cr.stroke()
    cr.restore()


def _glyph(cr, kind, cx, cy, s, color):
    """Small vector glyphs: play, pause, next, previous, stop, chevrons, globe, terminal, window."""
    cr.save()
    cr.set_source_rgb(*color)
    cr.set_line_width(max(2, s * 0.09))
    cr.set_line_cap(1)
    cr.set_line_join(1)
    h = s / 2
    if kind == "power":
        cr.arc(cx, cy + h * 0.1, h * 0.75, -math.pi * 0.32, math.pi * 1.32)   # ring, open at the top
        cr.stroke()
        cr.move_to(cx, cy - h * 0.9)
        cr.line_to(cx, cy + h * 0.05)
        cr.stroke()
        cr.restore()
        return
    if kind == "fork":                                   # a tuning fork
        cr.move_to(cx - h * 0.35, cy - h * 0.9)
        cr.line_to(cx - h * 0.35, cy - h * 0.05)
        cr.arc_negative(cx, cy - h * 0.05, h * 0.35, math.pi, 0)
        cr.line_to(cx + h * 0.35, cy - h * 0.9)
        cr.move_to(cx, cy + h * 0.3)
        cr.line_to(cx, cy + h * 0.95)
        cr.stroke()
        cr.restore()
        return
    if kind == "play":
        cr.move_to(cx - h * 0.6, cy - h * 0.8)
        cr.line_to(cx + h * 0.8, cy)
        cr.line_to(cx - h * 0.6, cy + h * 0.8)
        cr.close_path()
        cr.fill()
    elif kind == "pause":
        for dx in (-h * 0.45, h * 0.15):
            _rr(cr, cx + dx, cy - h * 0.75, h * 0.3, h * 1.5, h * 0.08)
            cr.fill()
    elif kind in ("next", "previous"):
        d = 1 if kind == "next" else -1
        for off in (-h * 0.55, h * 0.1):
            o = off * d
            cr.move_to(cx + o - d * h * 0.0, cy - h * 0.6)
            cr.line_to(cx + o + d * h * 0.6, cy)
            cr.line_to(cx + o, cy + h * 0.6)
            cr.close_path()
            cr.fill()
        _rr(cr, cx + d * h * 0.72 - h * 0.08, cy - h * 0.6, h * 0.16, h * 1.2, h * 0.05)
        cr.fill()
    elif kind == "stop":
        _rr(cr, cx - h * 0.6, cy - h * 0.6, h * 1.2, h * 1.2, h * 0.12)
        cr.fill()
    elif kind in ("chev-right", "chev-left", "chev-goto"):
        d = -1 if kind == "chev-left" else 1
        for off in ((-h * 0.3, h * 0.25) if kind != "chev-goto" else (0,)):
            cr.move_to(cx + off * d - d * h * 0.3, cy - h * 0.6)
            cr.line_to(cx + off * d + d * h * 0.3, cy)
            cr.line_to(cx + off * d - d * h * 0.3, cy + h * 0.6)
            cr.stroke()
        if kind == "chev-goto":
            _rr(cr, cx - h * 0.9, cy - h * 0.75, h * 1.8, h * 1.5, h * 0.2)
            cr.stroke()
    elif kind == "globe":
        cr.arc(cx, cy, h * 0.8, 0, 2 * math.pi)
        cr.stroke()
        cr.save()
        cr.translate(cx, cy)
        cr.scale(0.4, 1)
        cr.arc(0, 0, h * 0.8, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()
        cr.move_to(cx - h * 0.8, cy)
        cr.line_to(cx + h * 0.8, cy)
        cr.stroke()
    elif kind == "terminal":
        _rr(cr, cx - h * 0.9, cy - h * 0.7, h * 1.8, h * 1.4, h * 0.18)
        cr.stroke()
        cr.move_to(cx - h * 0.5, cy - h * 0.25)
        cr.line_to(cx - h * 0.15, cy)
        cr.line_to(cx - h * 0.5, cy + h * 0.25)
        cr.stroke()
        cr.move_to(cx, cy + h * 0.3)
        cr.line_to(cx + h * 0.45, cy + h * 0.3)
        cr.stroke()
    elif kind == "window":
        _rr(cr, cx - h * 0.9, cy - h * 0.7, h * 1.8, h * 1.4, h * 0.15)
        cr.stroke()
        cr.move_to(cx - h * 0.9, cy - h * 0.3)
        cr.line_to(cx + h * 0.9, cy - h * 0.3)
        cr.stroke()
    cr.restore()


class DeckKey(Gtk.DrawingArea):
    SIZE = 100

    def __init__(self, deck, r, c, scalable=False):
        super().__init__()
        self.deck, self.r, self.c = deck, r, c
        self.view, self.down, self._rep = None, False, 0
        self.hover = self.dragging = False
        self._sc = 1.0
        if scalable:            # the pop-out window: keys grow and shrink with the window
            self.set_content_width(12)
            self.set_content_height(12)
            self.set_hexpand(True)
            self.set_vexpand(True)
        else:
            self.set_content_width(self.SIZE)
            self.set_content_height(self.SIZE)
        self.set_draw_func(self._draw)
        self.set_cursor_from_name("pointer")
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._pressed)
        click.connect("released", self._released)
        self.add_controller(click)
        # moving keys: drag one onto another slot (only in "Edit keys" mode)
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", self._drag_prepare)
        src.connect("drag-begin", self._drag_begin)
        src.connect("drag-end", self._drag_end)
        self.add_controller(src)
        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        tgt.connect("enter", self._drop_enter)
        tgt.connect("leave", lambda *_: self._set_hover(False))
        tgt.connect("drop", self._drop)
        self.add_controller(tgt)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)

    def key(self):
        return self.deck.key(self.r, self.c)

    # ---- drag & drop
    def _drag_prepare(self, _src, x, y):
        if not self.deck.edit_mode or not self.key():
            return None
        self._drag_at = (x, y)
        return Gdk.ContentProvider.new_for_value(f"{self.deck.d['page']},{self.r},{self.c}")

    def _drag_begin(self, src, _drag):
        self.dragging = True
        x, y = self._drag_at
        src.set_icon(Gtk.WidgetPaintable.new(self), int(x), int(y))
        self.queue_draw()

    def _drag_end(self, *_):
        self.dragging = False
        self.queue_draw()

    def _set_hover(self, on):
        if self.hover != on:
            self.hover = on
            self.queue_draw()

    def _drop_enter(self, *_):
        self._set_hover(True)
        return Gdk.DragAction.MOVE

    def _drop(self, _tgt, value, _x, _y):
        self._set_hover(False)
        try:
            sp, sr, sc = (int(x) for x in str(value).split(","))
        except ValueError:
            return False
        return self.deck.move_key(sp, sr, sc, self.deck.d["page"], self.r, self.c)

    # ---- pressing
    def _pressed(self, g, _n, _x, _y):
        if g.get_current_button() == 3:
            if self.deck.locked:
                self.deck.show_unlock(self)
            else:
                self.deck.edit_key(self)
            return
        if self.deck.edit_mode:       # open the editor on release, so a press can start a drag
            return
        self.down = True
        self.queue_draw()
        self.deck.press(self.r, self.c)
        key = self.key() or {}
        if key.get("mode") in ("up", "down") and key.get("action") in ("channel", "mix"):
            self._rep = GLib.timeout_add(400, self._start_repeat)   # hold to keep going

    def _start_repeat(self):
        self._rep = GLib.timeout_add(120, self._repeat) if self.down and self.get_mapped() else 0
        return False

    def _repeat(self):
        if not self.get_mapped() or self.deck.key(self.r, self.c) is None:
            self._rep = 0               # the key went away (page change / rebuild) mid-hold
            return False
        self.deck.press(self.r, self.c)
        return True

    def _released(self, g, *_):
        if self.deck.edit_mode and g.get_current_button() == 1 and not self.dragging:
            self.deck.edit_key(self)
        self.down = False
        self.queue_draw()
        if self._rep:
            GLib.source_remove(self._rep)
            self._rep = 0

    def _scroll(self, _c, _dx, dy):
        if (self.key() or {}).get("action") in ("channel", "mix"):
            self.deck.press(self.r, self.c, ticks=-1 if dy > 0 else 1)
            return True
        return False

    def _draw(self, _a, cr, w, h):
        import cairo
        side = min(w, h)                     # keys stay square; everything is laid out at 100x100
        cr.translate((w - side) / 2, (h - side) / 2)
        self._sc = side / self.SIZE
        cr.scale(self._sc, self._sc)
        w = h = self.SIZE
        v, edit = self.view, self.deck.edit_mode
        _rr(cr, 0, 0, w, h, 14)                          # bezel
        cr.set_source_rgb(0.02, 0.02, 0.025)
        cr.fill()
        ins = 4 if self.down else 3
        x0, y0, iw, ih = ins, ins, w - 2 * ins, h - 2 * ins
        _rr(cr, x0, y0, iw, ih, 11)                      # the key's screen
        g = cairo.LinearGradient(0, y0, 0, y0 + ih)
        top, bot = ((0.11, 0.11, 0.12), (0.07, 0.07, 0.08)) if v else ((0.06, 0.06, 0.065),) * 2
        g.add_color_stop_rgb(0, *top)
        g.add_color_stop_rgb(1, *bot)
        cr.set_source(g)
        cr.fill_preserve()
        cr.save()
        cr.clip()
        cr.translate(x0, y0)
        self._content(cr, iw, ih, v, edit)
        cr.restore()
        if self.down:
            _rr(cr, x0, y0, iw, ih, 11)
            cr.set_source_rgba(0, 0, 0, 0.3)
            cr.fill()
        if self.dragging:                       # the key being moved
            _rr(cr, 0, 0, w, h, 14)
            cr.set_source_rgba(0.02, 0.02, 0.025, 0.7)
            cr.fill()
        if self.hover:                          # drop target under the pointer
            _rr(cr, 1.5, 1.5, w - 3, h - 3, 13)
            cr.set_line_width(3)
            cr.set_source_rgb(*BLUE)
            cr.stroke()
            return
        if edit:
            _rr(cr, 1.5, 1.5, w - 3, h - 3, 13)
            cr.set_line_width(1.5)
            cr.set_source_rgba(*BLUE, 0.55 if v else 0.3)
            if not v:
                cr.set_dash([4, 4])
            cr.stroke()
            cr.set_dash([])

    def _content(self, cr, w, h, v, edit):
        import cairo
        white, dim = (0.93, 0.93, 0.93), (0.6, 0.6, 0.6)
        if v is None:
            if edit:
                _text(cr, "+", w / 2, h / 2 + 8, 24, (0.35, 0.35, 0.35), bold=False)
            return
        kind, accent = v["kind"], v.get("color")
        has_image = bool(v.get("image")) and self._image(cr, w, h, v["image"])
        if has_image and kind == "stat":   # keep the numbers readable over a picture
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.paint()
        if has_image:                    # full-key picture, like a real Stream Deck key image
            g = cairo.LinearGradient(0, h - 44, 0, h)
            g.add_color_stop_rgba(0, 0, 0, 0, 0)
            g.add_color_stop_rgba(1, 0, 0, 0, 0.8)
            cr.rectangle(0, h - 44, w, 44)
            cr.set_source(g)
            cr.fill()
        if kind == "stat":
            col = accent or STAT_COLOR.get(v["stat"], BLUE)
            if v["stat"] == "clock":
                _text(cr, v["value"], w / 2, h / 2 + 4, 26, white, maxw=w - 8)
                _text(cr, v["sub"], w / 2, h / 2 + 24, 10.5, dim, maxw=w - 8)
                return
            _text(cr, v["lines"][0], w / 2, 16, 9.5, col, maxw=w - 10)
            _text(cr, v["value"], w / 2, 45, 19, white, maxw=w - 8)
            _text(cr, v["sub"], w / 2, 59, 9, dim, bold=False, maxw=w - 8)
            hist = v["hist"]
            if len(hist) > 1:                            # sparkline
                top = max(hist) if v["frac"] is None else 100
                top = top or 1
                gx, gy, gw, gh = 6, h - 28, w - 12, 20
                pts = [(gx + gw * i / (len(hist) - 1), gy + gh - gh * min(1, x / top))
                       for i, x in enumerate(hist)]
                cr.move_to(gx, gy + gh)
                for x, y in pts:
                    cr.line_to(x, y)
                cr.line_to(gx + gw, gy + gh)
                cr.close_path()
                cr.set_source_rgba(*col, 0.22)
                cr.fill()
                cr.move_to(*pts[0])
                for x, y in pts[1:]:
                    cr.line_to(x, y)
                cr.set_source_rgb(*col)
                cr.set_line_width(1.8)
                cr.stroke()
            return
        tint = accent or BLUE
        if kind == "audio" and v["vol"] is not None:      # volume bar
            bx, by, bw, bh = 10, 8, w - 20, 5
            for frac, col in ((1.0, (0.25, 0.25, 0.25)),
                              (v["vol"] / 100 if v["on"] else 0, RED if v["muted"] else tint)):
                if frac > 0:
                    _rr(cr, bx, by, max(bh, bw * frac), bh, bh / 2)
                    cr.set_source_rgb(*col)
                    cr.fill()
        muted, on = bool(v.get("muted")), v.get("on", True)
        if has_image:
            if muted:
                self._slash(cr, w * 0.25, (h - 30) * 0.2 + 6, w * 0.5, (h - 30) * 0.6)
            return self._lines(cr, w, h, v)
        icon = v.get("icon_override") or (v.get("icon") if kind == "audio" else None)
        if icon:                                          # theme icon (custom, or the channel's own)
            pb = tinted_icon(icon, self._px(40), GREY if muted else tint)
            if pb is not None:
                ix, iy = (w - 40) / 2, (h - 30 - 40) / 2 + 6
                cr.push_group()
                self._pixbuf(cr, pb, ix, iy, 40)
                cr.pop_group_to_source()
                cr.paint_with_alpha(1 if on else 0.4)
                if muted:
                    self._slash(cr, ix, iy, 40, 40)
                return self._lines(cr, w, h, v)
        cy = (h - 30) / 2 + 2
        if kind == "audio":
            waves = v.get("waves")
            if waves is None:
                vol = v["vol"] or 0
                waves = 0 if not vol else 1 if vol <= 33 else 2 if vol <= 66 else 3
            gs = min(w, h - 40) * 1.05
            cr.save()
            cr.translate((w - gs) / 2, 4)
            cr.scale(gs / 144, gs / 144)
            draw_speaker(cr, GREY if muted else tint, waves=waves, cross=muted, dim=not on)
            cr.restore()
        elif kind == "media":
            g = {"playpause": "pause" if v["playing"] else "play"}.get(v["mode"], v["mode"])
            _glyph(cr, g, w / 2, cy, 34, accent or (0.95, 0.95, 0.95))
        elif kind == "page":
            g = {"next": "chev-right", "prev": "chev-left"}.get(v["mode"], "chev-goto")
            _glyph(cr, g, w / 2, cy, 34, tint)
        elif kind == "app":
            pb = app_pixbuf(v["app"], self._px(44))
            if pb is not None:
                self._pixbuf(cr, pb, (w - 44) / 2, (h - 30 - 44) / 2 + 2, 44)
            else:
                _glyph(cr, "window", w / 2, cy, 34, tint)
        elif kind in ("url", "command", "window"):
            _glyph(cr, {"url": "globe", "command": "terminal"}.get(kind, "window"), w / 2, cy, 36, tint)
        elif kind == "sound":
            _glyph(cr, v["glyph"], w / 2, cy, 34, (accent or BLUE) if v.get("playing") else
                   (accent or (0.95, 0.95, 0.95)) if v.get("on") else GREY)
        elif kind in ("fx", "profile"):
            _glyph(cr, "power" if kind == "fx" else "chev-goto", w / 2, cy, 36,
                   (accent or BLUE) if v.get("on") else GREY)
        self._lines(cr, w, h, v)

    def _pixbuf(self, cr, pb, x, y, size):
        """Paint a pixbuf (rendered at screen resolution) into a `size`-unit box."""
        cr.save()
        cr.translate(x, y)
        cr.scale(size / pb.get_width(), size / pb.get_height())
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        cr.restore()

    def _px(self, size):
        return max(16, int(size * self._sc * max(1, self.get_scale_factor())))

    @staticmethod
    def _slash(cr, x, y, w, h):
        cr.set_source_rgb(*RED)
        cr.set_line_width(4)
        cr.set_line_cap(1)
        cr.move_to(x - 2, y + h + 2)
        cr.line_to(x + w + 2, y - 2)
        cr.stroke()

    def _image(self, cr, w, h, path):
        pb = key_image(path, self._px(max(w, h)))
        if pb is None:
            return False
        sc = max(w / pb.get_width(), h / pb.get_height())       # cover the whole key
        cr.save()
        cr.translate((w - pb.get_width() * sc) / 2, (h - pb.get_height() * sc) / 2)
        cr.scale(sc, sc)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        cr.restore()
        return True

    @staticmethod
    def _lines(cr, w, h, v):
        lines = v["lines"]
        _text(cr, lines[0] if lines else "", w / 2, h - 19, 11, (0.93, 0.93, 0.93), maxw=w - 8)
        if len(lines) > 1:
            _text(cr, lines[1], w / 2, h - 6, 9.5, (0.6, 0.6, 0.6), maxw=w - 8)


class DeckView(Gtk.Box):
    """A virtual Stream Deck: a device with pages of keys."""

    def __init__(self, app, win, popped=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.app, self.win, self.popped = app, win, popped
        self.edit_mode, self.keys, self.locked, self.on_unlock = False, [], False, None
        self.d = normalize_deck(app.eng.cfg)
        if popped:              # pop-out window: nothing but the keys
            self.add_css_class("deck-popout")
            self.grid = Gtk.Grid(row_spacing=8, column_spacing=8, row_homogeneous=True,
                                 column_homogeneous=True, hexpand=True, vexpand=True,
                                 margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
            self.append(self.grid)
            self.pagebar = Gtk.Box()          # not shown; keeps the page code paths simple
            self.build()
            return

        bar = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER if popped else Gtk.Align.FILL)
        if not popped:
            bar.append(Gtk.Label(label="Device", css_classes=["sub"]))
            self.model_dd = dropdown([(m[0], m[1]) for m in DECK_MODELS], self.d["model"], self._set_model)
            bar.append(self.model_dd)
            self.size_box = Gtk.Box(spacing=6)
            self.cols = Gtk.SpinButton.new_with_range(1, 12, 1)
            self.rows = Gtk.SpinButton.new_with_range(1, 8, 1)
            self.cols.set_value(self.d["cols"])
            self.rows.set_value(self.d["rows"])
            self.cols.connect("value-changed", self._custom_size)
            self.rows.connect("value-changed", self._custom_size)
            self.size_box.append(self.cols)
            self.size_box.append(Gtk.Label(label="×", css_classes=["sub"]))
            self.size_box.append(self.rows)
            self.size_box.set_visible(self.d["model"] == "custom")
            bar.append(self.size_box)
        self.edit_btn = Gtk.ToggleButton(label="Edit keys",
                                         tooltip_text="Click a key to set it up (right-click works any time)")
        self.edit_btn.connect("toggled", self._toggle_edit)
        bar.append(self.edit_btn)
        if not popped:
            reset = Gtk.Button(label="Reset", tooltip_text="Replace every page with the default layout")
            reset.connect("clicked", self._reset)
            bar.append(reset)
            bar.append(Gtk.Box(hexpand=True))
            pop = Gtk.Button(tooltip_text="Open the deck in its own window")
            pop.set_child(pill_button("window-new-symbolic", "Pop out"))
            pop.connect("clicked", lambda *_: win.open_deck_window())
            bar.append(pop)
        self.append(bar)

        self.device = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              css_classes=["deck-device"], halign=Gtk.Align.CENTER)
        self.grid = Gtk.Grid(row_spacing=12, column_spacing=12, halign=Gtk.Align.CENTER)
        self.device.append(self.grid)
        self.device.append(Gtk.Label(label="AUDIO MIXER", css_classes=["deck-brand"]))
        self.append(self.device)
        self.pagebar = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.append(self.pagebar)
        self.build()

    # ---- model
    def page(self):
        return self.d["pages"][self.d["page"]]

    def key(self, r, c):
        return self.page()["keys"].get(f"{r},{c}")

    def set_key(self, r, c, k):
        if k:
            self.page()["keys"][f"{r},{c}"] = k
        else:
            self.page()["keys"].pop(f"{r},{c}", None)

    def uses(self, actions):
        """Does the visible page contain any key of these actions?"""
        return self.get_mapped() and any((k or {}).get("action") in actions
                                         for k in self.page()["keys"].values())

    def go(self, n):
        self.d["page"] = n % len(self.d["pages"])
        self.win.changed()
        self.win.deck_page_changed()

    def _set_model(self, model):
        m = next(x for x in DECK_MODELS if x[1] == model)
        self.d["model"] = model
        if m[2]:
            self.d["cols"], self.d["rows"] = m[2], m[3]
            self.cols.set_value(m[2])
            self.rows.set_value(m[3])
        self.size_box.set_visible(model == "custom")
        self.win.changed()
        self.win.deck_layout_changed()

    def _custom_size(self, *_):
        if self.d["model"] != "custom":
            return
        self.d["cols"], self.d["rows"] = int(self.cols.get_value()), int(self.rows.get_value())
        self.win.changed()
        self.win.deck_layout_changed()

    def _reset(self, *_):
        cfg = self.app.eng.cfg
        cfg["deck"] = default_deck(cfg)
        self.win.changed()
        for d in self.win.decks:
            d.d = cfg["deck"]
        if not self.popped:
            self.model_dd.set_selected(0)
        self.win.deck_layout_changed()

    def _toggle_edit(self, b):
        self.edit_mode = b.get_active()
        for k in self.keys:
            k.queue_draw()
        self.build_pagebar()

    # ---- widgets
    def build(self):
        clear(self.grid)
        self.keys = []
        for r in range(self.d["rows"]):
            for c in range(self.d["cols"]):
                k = DeckKey(self, r, c, scalable=self.popped)
                self.keys.append(k)
                self.grid.attach(k, c, r, 1, 1)
        self.build_pagebar()
        self.refresh(self.app.control.state())

    def show_unlock(self, widget):
        pop = Gtk.Popover()
        pop.set_parent(widget)
        pop.connect("closed", lambda *_: GLib.idle_add(pop.unparent))
        b = Gtk.Button(margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        b.set_child(pill_button("changes-allow-symbolic", "Unlock deck"))
        b.connect("clicked", lambda *_: (pop.popdown(), self.on_unlock and self.on_unlock()))
        pop.set_child(b)
        pop.popup()

    def build_pagebar(self):
        if self.popped:
            if getattr(self, "on_page", None):
                self.on_page()
            return
        clear(self.pagebar)
        cur = self.d["page"]

        def nav_button(icon, tip, target):
            btn = Gtk.Button(icon_name=icon, css_classes=["flat", "circular"], tooltip_text=tip)
            btn.connect("clicked", lambda *_: self.go(target))
            self._page_drop_target(btn, target, switch_on_hover=True)
            return btn
        self.pagebar.append(nav_button("go-previous-symbolic", "Previous page", cur - 1))
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        mid.append(Gtk.Label(label=self.page()["name"], css_classes=["deck-page-name"]))
        dots = Gtk.Box(spacing=2, halign=Gtk.Align.CENTER)
        for i, p in enumerate(self.d["pages"]):
            b = Gtk.Button(label="●" if i == cur else "○", css_classes=["flat", "page-dot"],
                           tooltip_text=p["name"] + ("" if not self.edit_mode else
                                                     "  ·  drop a key here to move it to this page"))
            b.connect("clicked", lambda *_, i=i: self.go(i))
            self._page_drop_target(b, i, switch_on_hover=True, drop_to_free_slot=True)
            dots.append(b)
        mid.append(dots)
        self.pagebar.append(mid)
        self.pagebar.append(nav_button("go-next-symbolic", "Next page", cur + 1))
        self.pagebar.append(self._page_menu())
        if self.edit_mode:
            self.pagebar.append(Gtk.Label(
                label="Drag keys to move them · hover a page dot to carry a key to another page",
                css_classes=["sub"], margin_start=10))

    def _page_menu(self):
        """⋯ menu: new, duplicate, rename, move, delete the current page."""
        mb = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["flat", "circular"],
                            tooltip_text="Page options")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6, width_request=230)
        pop.set_child(box)
        mb.set_popover(pop)
        n, cur = len(self.d["pages"]), self.d["page"]

        def item(icon, label, fn, sensitive=True, destructive=False):
            b = Gtk.Button(css_classes=["flat"] + (["error"] if destructive else []), sensitive=sensitive)
            r = Gtk.Box(spacing=10)
            r.append(Gtk.Image(icon_name=icon))
            r.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            b.set_child(r)
            b.connect("clicked", lambda *_: (pop.popdown(), fn()))
            box.append(b)

        item("list-add-symbolic", "New page", self.new_page)
        item("edit-copy-symbolic", "Duplicate page", self.duplicate_page)
        ren = Gtk.Entry(text=self.page()["name"], margin_top=4, margin_bottom=4,
                        placeholder_text="Page name", secondary_icon_name="document-edit-symbolic")

        def rename(e):
            t = e.get_text().strip()
            if t:
                self.page()["name"] = t
                self.win.changed()
                pop.popdown()
                self.win.deck_page_changed()
        ren.connect("activate", rename)
        box.append(ren)
        item("go-previous-symbolic", "Move page left", lambda: self.move_page(-1), cur > 0)
        item("go-next-symbolic", "Move page right", lambda: self.move_page(1), cur < n - 1)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item("user-trash-symbolic", "Delete page", self.delete_page, n > 1, destructive=True)
        return mb

    def _page_drop_target(self, widget, page, switch_on_hover=False, drop_to_free_slot=False):
        """Let a dragged key flip to `page` when hovered, or land in its first free slot."""
        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        state = {"timer": 0}

        def enter(*_):
            if switch_on_hover and page % len(self.d["pages"]) != self.d["page"]:
                state["timer"] = GLib.timeout_add(450, lambda: (self.go(page), False)[1])
            return Gdk.DragAction.MOVE

        def leave(*_):
            if state["timer"]:
                GLib.source_remove(state["timer"])
                state["timer"] = 0

        def drop(_t, value, _x, _y):
            leave()
            if not drop_to_free_slot:
                return False
            try:
                sp, sr, sc = (int(x) for x in str(value).split(","))
            except ValueError:
                return False
            return self.move_to_page(sp, sr, sc, page % len(self.d["pages"]))
        tgt.connect("enter", enter)
        tgt.connect("leave", leave)
        tgt.connect("drop", drop)
        widget.add_controller(tgt)

    # ---- page operations
    def new_page(self):
        cur = self.d["page"]
        self.d["pages"].insert(cur + 1, {"name": f"Page {len(self.d['pages']) + 1}", "keys": {}})
        self.go(cur + 1)

    def duplicate_page(self):
        cur = self.d["page"]
        dup = copy.deepcopy(self.page())
        dup["name"] = self.page()["name"] + " copy"
        self.d["pages"].insert(cur + 1, dup)
        self.go(cur + 1)

    def move_page(self, delta):
        cur = self.d["page"]
        new = cur + delta
        if 0 <= new < len(self.d["pages"]):
            pages = self.d["pages"]
            pages[cur], pages[new] = pages[new], pages[cur]
            self.go(new)

    def delete_page(self):
        if len(self.d["pages"]) <= 1:
            return
        cur, page = self.d["page"], self.page()

        def really_delete():
            if page in self.d["pages"]:
                self.d["pages"].remove(page)
                self.go(min(cur, len(self.d["pages"]) - 1))
        if not page["keys"]:
            really_delete()
            return
        dlg = Adw.AlertDialog(heading=f"Delete “{page['name']}”?",
                              body=f"Its {len(page['keys'])} key(s) will be removed. This can't be undone.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete page")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.connect("response", lambda _d, r: r == "delete" and really_delete())
        dlg.present(self.get_root())

    # ---- key operations
    def first_free(self, page_idx):
        keys = self.d["pages"][page_idx]["keys"]
        for r in range(self.d["rows"]):
            for c in range(self.d["cols"]):
                if f"{r},{c}" not in keys:
                    return r, c
        return None

    def move_key(self, sp, sr, sc, dp, dr, dc):
        """Move a key; if the destination is taken the two keys swap places."""
        pages = self.d["pages"]
        if not (0 <= sp < len(pages) and 0 <= dp < len(pages)):
            return False
        src, dst = pages[sp]["keys"], pages[dp]["keys"]
        a = src.pop(f"{sr},{sc}", None)
        if a is None:
            return False
        b = dst.pop(f"{dr},{dc}", None)
        dst[f"{dr},{dc}"] = a
        if b is not None:
            src[f"{sr},{sc}"] = b
        self.win.changed()
        self.win.deck_page_changed()
        return True

    def move_to_page(self, sp, sr, sc, dp):
        if sp == dp:
            return False
        slot = self.first_free(dp)
        if slot is None:
            self.win.toast(f"“{self.d['pages'][dp]['name']}” is full")
            return False
        return self.move_key(sp, sr, sc, dp, *slot)

    def duplicate_key(self, r, c):
        key = self.key(r, c)
        slot = self.first_free(self.d["page"])
        if not key or slot is None:
            self.win.toast("This page is full")
            return False
        self.page()["keys"][f"{slot[0]},{slot[1]}"] = copy.deepcopy(key)
        self.win.changed()
        self.win.refresh_decks()
        return True

    def refresh(self, st=None):
        st = st or self.app.control.state()
        for k in self.keys:
            nv = deck_view(k.key(), st, self)
            if nv != k.view:
                k.view = nv
                k.queue_draw()

    # ---- pressing keys
    def press_index(self, idx, ticks=0):
        r, c = divmod(idx, self.d["cols"])
        self.press(r, c, ticks)

    def press(self, r, c, ticks=0):
        key = self.key(r, c)
        if not key:
            return
        act = key.get("action")
        if act in ("channel", "mix", "system", "window") + NEW_ACTIONS:
            msg = deck_msg(key, ticks)
            if msg:
                self.app.control.command(msg)
        elif ticks:
            return
        elif act == "page":
            mode, cur = key.get("mode") or "next", self.d["page"]
            self.go({"next": cur + 1, "prev": cur - 1}.get(mode, int(key.get("page") or 0)))
            return
        elif act == "media":
            self.win.media.command(key.get("mode") or "playpause")
            GLib.timeout_add(350, lambda: (self.win.media.refresh(), self.win.refresh_decks()) and False)
        elif act == "app":
            info = app_info(key.get("app"))
            if info:
                try:
                    info.launch([], None)
                except GLib.Error:
                    pass
        elif act == "url" and key.get("url"):
            url = key["url"] if "://" in key["url"] else "https://" + key["url"]
            try:
                Gio.AppInfo.launch_default_for_uri(url, None)
            except GLib.Error:
                pass
        elif act == "command" and key.get("command"):
            subprocess.Popen(["sh", "-c", key["command"]], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.win.schedule("deckrefresh", 80, lambda: self.win.refresh_decks())

    # ---- key editor
    def edit_key(self, widget):
        r, c = widget.r, widget.c
        cfg = self.app.eng.cfg
        pop = Gtk.Popover()
        pop.set_parent(widget)
        pop.connect("closed", lambda *_: GLib.idle_add(pop.unparent))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12, width_request=330)
        pop.set_child(box)

        def update(**kw):
            k = dict(self.key(r, c) or {})
            k.update(kw)
            self.set_key(r, c, None if k.get("action") in (None, "empty") else k)
            self.win.changed()
            self.win.refresh_decks()

        def row(label, w):
            rb = Gtk.Box(spacing=8)
            rb.append(Gtk.Label(label=label, xalign=0, css_classes=["sub"], width_request=86))
            w.set_hexpand(True)
            rb.append(w)
            box.append(rb)

        def spin(key, lo, hi, default):
            sp = Gtk.SpinButton.new_with_range(lo, hi, 1)
            cur = (self.key(r, c) or {}).get(key)
            sp.set_value(default if cur is None else cur)
            sp.connect("value-changed", lambda s: update(**{key: int(s.get_value())}))
            return sp

        def entry(key, placeholder):
            e = Gtk.Entry(text=(self.key(r, c) or {}).get(key) or "", placeholder_text=placeholder)
            e.connect("changed", lambda e: update(**{key: e.get_text().strip() or None}))
            return e

        def fill():
            clear(box)
            k = self.key(r, c) or {}
            act = k.get("action") if k.get("action") in DECK_ACTION_IDS else "empty"

            def set_action(v):
                defaults = {
                    "channel": {"channel": cfg["channels"][0]["id"] if cfg["channels"] else None,
                                "scope": "master", "mode": "mute", "step": 5},
                    "mix": {"mix": cfg["mixes"][0]["id"] if cfg["mixes"] else None, "mode": "mute", "step": 5},
                    "system": {"mode": "toggle"}, "window": {"mode": "toggle"}, "stat": {"stat": "cpu"},
                    "media": {"mode": "playpause"}, "page": {"mode": "next"}, "app": {"app": None},
                    "url": {"url": ""}, "command": {"command": ""},
                    "sound": {"sound": next((x["id"] for x in self.app.board.board["sounds"]), None),
                              "mode": "play"},
                    "fx": {"target": cfg["channels"][0]["id"] if cfg["channels"] else None,
                           "effect": "denoise" if cfg["channels"] and cfg["channels"][0]["type"] == "hw"
                           else "comp"},
                    "duck": {"channel": cfg["channels"][0]["id"] if cfg["channels"] else None},
                    "profile": {"profile": next(iter(sorted(ext.profiles(cfg))), None)}}
                title = (self.key(r, c) or {}).get("title")
                self.set_key(r, c, None)
                update(action=v, title=title, **defaults.get(v, {}))
                fill()
            row("Action", dropdown(DECK_ACTIONS, act, set_action))
            if act == "channel":
                row("Channel", dropdown([(x["name"], x["id"]) for x in cfg["channels"]],
                                        k.get("channel"), lambda v: update(channel=v)))
                row("Applies to", dropdown([("Channel master (all mixes)", "master")] +
                                           [("Only in " + m["name"], m["id"]) for m in cfg["mixes"]],
                                           k.get("scope") or "master", lambda v: update(scope=v)))
            if act == "mix":
                row("Mix", dropdown([(m["name"], m["id"]) for m in cfg["mixes"]], k.get("mix"),
                                    lambda v: update(mix=v)))
            if act in ("channel", "mix"):
                modes = DECK_MODES if act == "channel" else DECK_MODES[:4]
                row("Key press", dropdown(modes, k.get("mode") or "mute", lambda v: (
                    update(mode=v, **({"setTo": k.get("setTo", 50)} if v == "set" else {})), fill())))
                row("Step (%)", spin("step", 1, 50, 5))
                if (k.get("mode") or "mute") == "set":
                    row("Set to (%)", spin("setTo", 0, 100, 50))
                box.append(Gtk.Label(label="Hold a volume key to repeat; scroll over a key to turn "
                                           "it like a dial.", wrap=True, xalign=0, css_classes=["sub"]))
            if act == "system":
                row("Key press", dropdown([("Toggle", "toggle"), ("Turn on", "on"), ("Turn off", "off")],
                                          k.get("mode") or "toggle", lambda v: update(mode=v)))
            if act == "window":
                row("Key press", dropdown([("Show / hide", "toggle"), ("Show", "show"), ("Hide", "hide")],
                                          k.get("mode") or "toggle", lambda v: update(mode=v)))
            if act == "stat":
                row("Show", dropdown(STATS, k.get("stat") or "cpu", lambda v: update(stat=v)))
            if act == "media":
                row("Key press", dropdown(MEDIA_MODES, k.get("mode") or "playpause",
                                          lambda v: update(mode=v)))
                box.append(Gtk.Label(label="Controls whichever player is playing (Spotify, Firefox, …).",
                                     wrap=True, xalign=0, css_classes=["sub"]))
            if act == "page":
                row("Key press", dropdown(PAGE_MODES, k.get("mode") or "next",
                                          lambda v: (update(mode=v), fill())))
                if k.get("mode") == "goto":
                    row("Page", dropdown([(p["name"], i) for i, p in enumerate(self.d["pages"])],
                                         int(k.get("page") or 0), lambda v: update(page=v)))
            if act == "app":
                dd = dropdown(installed_app_options(), k.get("app"), lambda v: update(app=v))
                dd.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
                dd.set_enable_search(True)
                row("App", dd)
            if act == "sound":
                snds = self.app.board.board["sounds"]
                if snds:
                    row("Sound", dropdown([(f"{x['name']}  ({x['id']})", x["id"]) for x in snds],
                                          k.get("sound"), lambda v: update(sound=v)))
                else:
                    box.append(Gtk.Label(label="Add sounds on the Soundboard page first.", xalign=0,
                                         css_classes=["sub"]))
                row("Key press", dropdown(SOUND_MODES, k.get("mode") or "play", lambda v: update(mode=v)))
            if act == "fx":
                objs = cfg["channels"] + cfg["mixes"]

                def set_target(v):
                    o = next((x for x in objs if x["id"] == v), None)
                    ch = fx_choices(o) if o else []
                    update(target=v, effect=ch[0][1] if ch else None)
                    fill()
                row("On", dropdown([(x["name"] + ("  (mix)" if "outputs" in x else ""), x["id"]) for x in objs],
                                   k.get("target"), set_target))
                o = next((x for x in objs if x["id"] == k.get("target")), None)
                if o:
                    row("Effect", dropdown(fx_choices(o), k.get("effect"), lambda v: update(effect=v)))
                box.append(Gtk.Label(label="Each press switches the effect on or off; its settings are kept.",
                                     wrap=True, xalign=0, css_classes=["sub"]))
            if act == "duck":
                row("Channel", dropdown([(x["name"], x["id"]) for x in cfg["channels"]], k.get("channel"),
                                        lambda v: update(channel=v)))
                box.append(Gtk.Label(label="Set what triggers the ducking in the channel's settings.",
                                     wrap=True, xalign=0, css_classes=["sub"]))
            if act == "profile":
                profs = sorted(ext.profiles(cfg), key=str.lower)
                if profs:
                    row("Profile", dropdown([(p_, p_) for p_ in profs], k.get("profile"),
                                            lambda v: update(profile=v)))
                else:
                    box.append(Gtk.Label(label="Save a profile in Settings first.", xalign=0,
                                         css_classes=["sub"]))
            if act == "url":
                row("Address", entry("url", "example.com"))
            if act == "command":
                row("Command", entry("command", "e.g. obs --startrecording"))
            if act != "empty":
                row("Title", entry("title", "(automatic)"))
                box.append(Gtk.Separator(margin_top=4, margin_bottom=2))
                box.append(Gtk.Label(label="Look", xalign=0, css_classes=["heading"]))
                # icon
                cur_icon = k.get("icon")
                ib = Gtk.MenuButton(always_show_arrow=True)
                il = Gtk.Box(spacing=8)
                il.append(Gtk.Image(icon_name=safe_icon(cur_icon) if cur_icon else "starred-symbolic"))
                il.append(Gtk.Label(label="Custom icon" if cur_icon else "Automatic"))
                ib.set_child(il)
                ipop = Gtk.Popover()
                ibox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8,
                               margin_bottom=8, margin_start=8, margin_end=8)
                autob = Gtk.Button(label="Automatic (match the action)")

                def set_icon(name):
                    ipop.popdown()
                    update(icon=name)
                    fill()
                autob.connect("clicked", lambda *_: set_icon(None))
                ibox.append(autob)
                igrid = Gtk.Grid(row_spacing=4, column_spacing=4)
                for n, name in enumerate(deck_icon_choices()):
                    b = Gtk.ToggleButton(css_classes=["flat"], active=name == cur_icon,
                                         tooltip_text=name.replace("-symbolic", "").replace("-", " "))
                    b.set_child(Gtk.Image(icon_name=safe_icon(name), pixel_size=24))
                    b.set_size_request(44, 44)
                    b.connect("clicked", lambda _b, name=name: set_icon(name))
                    igrid.attach(b, n % 8, n // 8, 1, 1)
                isw = Gtk.ScrolledWindow(max_content_height=320, propagate_natural_height=True,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER)
                isw.set_child(igrid)
                ibox.append(isw)
                ipop.set_child(ibox)
                ib.set_popover(ipop)
                row("Icon", ib)
                # image
                imrow = Gtk.Box(spacing=6)
                img_path = k.get("image")
                choose = Gtk.Button(label=os.path.basename(img_path) if img_path else "Choose image…",
                                    hexpand=True)
                choose.connect("clicked", lambda *_: self._choose_image(
                    widget, lambda path: (update(image=path), fill())))
                imrow.append(choose)
                rm = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Remove the image",
                                sensitive=bool(img_path))
                rm.connect("clicked", lambda *_: (update(image=None), fill()))
                imrow.append(rm)
                row("Image", imrow)
                # colour
                swr = Gtk.Box(spacing=3)
                for label, hexc in DECK_COLORS:
                    sb = Gtk.ToggleButton(css_classes=["flat", "swatch"], tooltip_text=label,
                                          active=k.get("color") == hexc)
                    da = Gtk.DrawingArea(content_width=16, content_height=16)

                    def draw_dot(_a, cr, dw, dh, hexc=hexc):
                        cr.arc(dw / 2, dh / 2, 7, 0, 2 * math.pi)
                        if hexc:
                            cr.set_source_rgb(*hex_rgb(hexc))
                            cr.fill()
                        else:                     # "automatic": an outlined dot with a slash
                            cr.set_source_rgb(0.7, 0.7, 0.7)
                            cr.set_line_width(1.5)
                            cr.stroke()
                            cr.move_to(dw / 2 - 4.5, dh / 2 + 4.5)
                            cr.line_to(dw / 2 + 4.5, dh / 2 - 4.5)
                            cr.stroke()
                    da.set_draw_func(draw_dot)
                    sb.set_child(da)
                    sb.connect("clicked", lambda _b, hexc=hexc: (update(color=hexc), fill()))
                    swr.append(sb)
                row("Color", swr)
                if act == "stat":
                    box.append(Gtk.Label(label="On stat keys the color tints the label and graph, "
                                               "and an image sits behind the numbers.",
                                         wrap=True, xalign=0, css_classes=["sub"]))
                box.append(Gtk.Separator(margin_top=4, margin_bottom=2))
                tools = Gtk.Box(spacing=6, homogeneous=True)
                dup = Gtk.Button(tooltip_text="Copy this key into the next free slot")
                dup.set_child(pill_button("edit-copy-symbolic", "Duplicate"))
                dup.connect("clicked", lambda *_: (pop.popdown(), self.duplicate_key(r, c)))
                tools.append(dup)
                mv = Gtk.MenuButton(tooltip_text="Send this key to another page")
                mv.set_child(pill_button("go-jump-symbolic", "Move to…"))
                mvpop = Gtk.Popover()
                mvbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                                margin_bottom=6, margin_start=6, margin_end=6)
                for i, pg in enumerate(self.d["pages"]):
                    if i == self.d["page"]:
                        continue
                    full = self.first_free(i) is None
                    pb = Gtk.Button(label=pg["name"] + ("  (full)" if full else ""),
                                    css_classes=["flat"], sensitive=not full)
                    pb.get_child().set_xalign(0)
                    pb.connect("clicked", lambda *_, i=i: (mvpop.popdown(), pop.popdown(),
                                                            self.move_to_page(self.d["page"], r, c, i)))
                    mvbox.append(pb)
                if len(self.d["pages"]) < 2:
                    mvbox.append(Gtk.Label(label="Add another page first", css_classes=["sub"]))
                mvpop.set_child(mvbox)
                mv.set_popover(mvpop)
                tools.append(mv)
                clr = Gtk.Button(css_classes=["destructive-action"], tooltip_text="Empty this key")
                clr.set_child(pill_button("edit-clear-symbolic", "Clear"))
                clr.connect("clicked", lambda *_: (pop.popdown(), update(action="empty")))
                tools.append(clr)
                box.append(tools)
        fill()
        pop.popup()

    def _choose_image(self, widget, done):
        """Pick an image file; it is copied into the config folder so the key keeps working."""
        dlg = Gtk.FileDialog(title="Choose a key image")
        flt = Gtk.FileFilter()
        flt.set_name("Images")
        for mt in ("image/png", "image/jpeg", "image/svg+xml", "image/webp", "image/gif", "image/bmp"):
            flt.add_mime_type(mt)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)

        def finished(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            src = f.get_path() if f else None
            if not src:
                return
            dest_dir = os.path.join(os.path.dirname(CONF), "deck-images")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, short_id() + os.path.splitext(src)[1].lower())
            try:
                shutil.copy2(src, dest)
            except OSError:
                dest = src
            done(dest)
        dlg.open(widget.get_root(), None, finished)


# --------------------------------------------------------------------------- window
class MixerWindow(Adw.ApplicationWindow):
    def __init__(self, app, eng):
        super().__init__(application=app, title="Audio Mixer",
                         default_width=1400, default_height=800)
        self.app, self.eng, self.cfg = app, eng, eng.cfg
        self.meters = Meters()
        self.refs = []
        self._timers = {}
        self._sigs = {}
        self._dialog_open = False
        self.decks, self._deck_win, self._deck_n = [], None, 0
        self.stats, self.media = SysStats(), Media()

        tv = Adw.ToolbarView()
        self._toasts = Adw.ToastOverlay()
        self._toasts.set_child(tv)
        self.set_content(self._toasts)
        tv.add_top_bar(Adw.HeaderBar(css_classes=["flat"]))
        split = Adw.OverlaySplitView(min_sidebar_width=230, max_sidebar_width=280,
                                     sidebar_width_fraction=0.2)
        tv.set_content(split)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       css_classes=["sidebar-pane"], margin_top=8, margin_bottom=8)
        side.append(Gtk.Label(label="Your devices", xalign=0, css_classes=["side-head"],
                              margin_start=12))
        self.dev_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6)
        side.append(self.dev_box)
        side.append(Gtk.Separator(margin_top=8, margin_bottom=8))
        side.append(Gtk.Label(label="Sound & Mixes", xalign=0, css_classes=["side-head"],
                              margin_start=12))
        self.stack = Gtk.Stack(hexpand=True, vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        first = None
        navbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6)
        for key, label, icon in (("mixes", "Mixes", "view-list-symbolic"),
                                 ("deck", "Deck", "view-grid-symbolic"),
                                 ("apps", "Apps", "applications-multimedia-symbolic"),
                                 ("soundboard", "Soundboard", "media-playlist-shuffle-symbolic"),
                                 ("settings", "Settings", "preferences-system-symbolic")):
            b = Gtk.ToggleButton(css_classes=["navbtn", "flat"])
            row = Gtk.Box(spacing=12)
            row.append(Gtk.Image(icon_name=icon))
            row.append(Gtk.Label(label=label))
            b.set_child(row)
            if first is None:
                first = b
                b.set_active(True)
            else:
                b.set_group(first)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and
                      self.stack.set_visible_child_name(k))
            navbox.append(b)
        side.append(navbox)
        side.append(Gtk.Box(vexpand=True))
        quit_b = Gtk.Button(css_classes=["navbtn", "flat"], margin_start=6, margin_end=6,
                            tooltip_text="Stops routing and removes the virtual devices")
        qrow = Gtk.Box(spacing=12)
        qrow.append(Gtk.Image(icon_name="application-exit-symbolic"))
        qrow.append(Gtk.Label(label="Quit mixer"))
        quit_b.set_child(qrow)
        quit_b.connect("clicked", lambda *_: app.shutdown())
        side.append(quit_b)
        split.set_sidebar(side)

        self.stack.add_named(self._build_mixes_page(), "mixes")
        self.stack.add_named(self._build_deck_page(), "deck")
        self.stack.add_named(self._build_apps_page(), "apps")
        self.soundboard_page = ext.SoundboardPage(self, app.board)
        self.stack.add_named(self.soundboard_page, "soundboard")
        self.settings_page = ext.SettingsPage(self)
        self.stack.add_named(self.settings_page, "settings")
        self.stack.connect("notify::visible-child-name", lambda st, _p: st.get_visible_child_name() == "settings"
                           and self.settings_page.rebuild())
        split.set_content(self.stack)
        self._fx_refreshers = []       # callbacks that redraw open effect editors after a remote change
        self._duck_until = {}

        self.connect("close-request", self._on_close)
        self.connect("notify::visible", lambda *_: self.sync_meters())   # meters only when someone looks
        self.rebuild_all()
        # Re-sync with the sound server when it reports a change (PaEvents), plus a slow safety poll.
        self._sync_due = 0                       # pending event-driven sync (timeout id)
        self._last_sync = time.monotonic()
        self.pa_events = PaEvents(self._on_pa_event)
        GLib.timeout_add(SAFETY_POLL_MS, self._tick)
        GLib.timeout_add(50, self._meter_tick)
        GLib.timeout_add(1000, self._stats_tick)

    def _on_close(self, *_):
        self.set_visible(False)  # keep routing alive in the background
        tray = getattr(self.app, "tray", None)
        if not (tray and tray.ok) and not self.cfg.get("bg_notice_shown"):
            # no tray icon to bring it back from: say once where it went
            self.cfg["bg_notice_shown"] = True
            self.changed()
            n = Gio.Notification.new("Audio Mixer is still running")
            n.set_body("It keeps your audio routed in the background. Open it again from the app "
                       "menu; use “Quit mixer” in the sidebar to stop it.")
            self.app.send_notification("background", n)
        return True

    # ---- page skeletons
    def _build_mixes_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=8,
                       margin_bottom=16, margin_start=20, margin_end=20)
        top = Gtk.Box(spacing=12)
        top.append(Gtk.Label(label="Mixes", xalign=0, hexpand=True, css_classes=["page-title"]))
        top.append(Gtk.Image(icon_name="audio-speakers-symbolic", css_classes=["sub"]))
        self.default_box = Gtk.Box()
        top.append(self.default_box)
        page.append(top)
        self.hint_box = Gtk.Box()
        page.append(self.hint_box)
        self.grid = Gtk.Grid(column_spacing=8, row_spacing=8, valign=Gtk.Align.START)
        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        sw.set_child(self.grid)
        page.append(sw)
        return page

    def _build_deck_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8,
                       margin_bottom=16, margin_start=20, margin_end=20)
        page.append(Gtk.Label(label="Deck", xalign=0, css_classes=["page-title"]))
        page.append(Gtk.Label(
            label="A virtual Stream Deck. Click a key to press it, hold a volume key to repeat, "
                  "scroll over a key to turn it like a dial. Switch pages with the arrows or dots "
                  "under the deck. Right-click a key (or use “Edit keys”) to change what it does.",
            xalign=0, wrap=True, css_classes=["sub"]))
        deck = DeckView(self.app, self)
        self.decks.append(deck)
        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        sw.set_child(deck)
        page.append(sw)
        return page

    DECK_WIN_TITLE = "Audio Mixer Deck"

    def open_deck_window(self):
        """Pop the keys out into their own resizable, lockable, optionally always-on-top window.
        It stays out of the taskbar; the tray menu has "Show / hide deck"."""
        if self._deck_win is not None:
            self._deck_win.present()
            return
        ws = self.cfg.setdefault("deck_window", {})
        width, height = ws.get("width", 560), ws.get("height", 380)
        if ws.get("locked"):                       # opening locked: no header, so no header height
            height = max(48, height - ws.get("header_h", 46))
        w = Adw.Window(title=self.DECK_WIN_TITLE, default_width=width, default_height=height)
        w.set_size_request(48, 48)     # libadwaita's default minimum is 360x200; let it go much smaller
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(css_classes=["flat"], decoration_layout=":close")   # compact, so it can get small
        title = Adw.WindowTitle(title="Deck")
        hb.set_title_widget(title)
        deck = DeckView(self.app, self, popped=True)
        prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Previous page")
        prev.connect("clicked", lambda *_: deck.go(deck.d["page"] - 1))
        nxt = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next page")
        nxt.connect("clicked", lambda *_: deck.go(deck.d["page"] + 1))
        hb.pack_start(prev)
        hb.pack_start(nxt)
        pin = Gtk.ToggleButton(icon_name="view-pin-symbolic", tooltip_text="Always on top",
                               active=bool(ws.get("on_top")))
        lock = Gtk.Button(icon_name="changes-prevent-symbolic",
                          tooltip_text="Lock: hide this bar, fix the size and stop editing; keys "
                                       "still work (right-click a key to unlock)")
        hb.pack_end(lock)
        hb.pack_end(pin)
        tv.add_top_bar(hb)
        tv.set_content(deck)
        w.set_content(tv)
        self.decks.append(deck)
        self._deck_win = w

        def show_page():
            title.set_subtitle(f"{deck.page()['name']}  ·  {deck.d['page'] + 1}/{len(deck.d['pages'])}")
        deck.on_page = show_page
        show_page()

        def kwin_rules():
            """Keep the deck out of the taskbar / pager / Alt+Tab, and apply always-on-top."""
            kwin_window_flags(self.DECK_WIN_TITLE, skip_taskbar=True, keep_above=bool(ws.get("on_top")))
            return False

        def set_on_top(on):
            ws["on_top"] = on
            self.changed()
            if not kwin_window_flags(self.DECK_WIN_TITLE, skip_taskbar=True, keep_above=on) and on:
                self.toast("Always on top needs KDE Plasma (KWin)")
                pin.set_active(False)
                return
            GLib.timeout_add(500, kwin_rules)

        def remember_size(*_):
            """While unlocked, keep track of the window's size (it's restored after unlocking)."""
            if not ws.get("locked") and w.get_mapped():
                dw, dh = w.get_default_size()
                if dw > 0 and dh > 0:
                    ws["width"], ws["height"] = dw, dh
                if hb.get_parent() is not None and hb.get_height() > 0:
                    ws["header_h"] = hb.get_height()
        w.connect("notify::default-width", remember_size)
        w.connect("notify::default-height", remember_size)

        def set_locked(locked):
            if locked:
                remember_size()
            ws["locked"] = locked
            deck.locked = locked
            if locked:
                had_header = hb.get_parent() is not None
                if had_header:
                    tv.remove(hb)
                if had_header and w.get_mapped():   # shrink by exactly the header: no empty band
                    w.set_default_size(ws.get("width", w.get_width()),
                                       max(48, ws.get("height", w.get_height()) - ws.get("header_h", 46)))
                GLib.timeout_add(300, lambda: (ws.get("locked") and w.set_resizable(False), False)[1])
            else:
                w.set_resizable(True)
                if hb.get_parent() is None:
                    tv.add_top_bar(hb)
                # GTK re-measures when a window becomes resizable again; put the old size back
                GLib.timeout_add(150, lambda: (w.set_default_size(ws.get("width", 560),
                                                                  ws.get("height", 380)), False)[1])
            self.changed()
            GLib.timeout_add(400, kwin_rules)    # re-assert after the window's decorations change
        deck.on_unlock = lambda: set_locked(False)
        pin.connect("toggled", lambda b: set_on_top(b.get_active()))
        lock.connect("clicked", lambda *_: set_locked(True))

        def closed(*_):
            remember_size()                   # (no-op while locked: the unlocked size is kept)
            ws["open"] = False
            self.changed()
            if deck in self.decks:
                self.decks.remove(deck)
            self._deck_win = None
            return False
        w.connect("close-request", closed)
        ws["open"] = True
        self.changed()
        if ws.get("locked"):
            deck.locked = True
            tv.remove(hb)
            w.set_resizable(False)
        w.present()
        GLib.timeout_add(300, kwin_rules)
        GLib.timeout_add(1200, kwin_rules)       # once more, in case the window mapped late

    def toggle_deck_window(self):
        if self._deck_win is not None:
            self._deck_win.close()
        else:
            self.open_deck_window()

    # ---- effects, profiles, tools
    def apply_fx(self):
        if self.eng.apply_fx():
            self.sync_meters()
            self.build_matrix()

    def fx_edit(self):
        """Callback for effect editors: save and apply (debounced)."""
        self.changed()
        self.schedule("fx", 60, self.apply_fx)

    def refresh_fx_widgets(self):
        for fn in list(self._fx_refreshers):
            try:
                fn()
            except Exception:
                self._fx_refreshers.remove(fn)
        self.build_matrix()

    def switch_profile(self, name):
        self.app.board.stop_all()
        if ext.switch_profile(self.cfg, name):
            self.toast(f"Profile: {name}")
            self.structure_changed()

    def replace_config(self, new):
        """Load a whole imported configuration in place of the current one."""
        self.app.board.stop_all()
        try:
            new = normalize(copy.deepcopy(new))
        except (KeyError, TypeError, AttributeError):
            self.toast("That configuration is damaged")
            return
        self.cfg.clear()
        self.cfg.update(new)
        normalize_deck(self.cfg)
        self.structure_changed()
        self.deck_layout_changed()
        self.soundboard_page.rebuild()
        self.toast("Configuration imported")

    def open_rack_window(self, obj):
        """The Creative Rack as its own window, to keep open next to whatever you play into."""
        w = Adw.Window(title=f"{obj['name']} — Creative Rack", default_width=520, default_height=760)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(css_classes=["flat"]))
        holder = Gtk.Box(margin_top=6, margin_bottom=12, margin_start=12, margin_end=12)
        tv.set_content(holder)
        w.set_content(tv)

        def build():
            clear(holder)
            ed = fxm.rack_editor(obj, self.fx_edit, self.fx_edit, self.toast, holder)
            ed.set_hexpand(True)
            holder.append(ed)
        build()
        self._fx_refreshers.append(build)
        w.connect("close-request", lambda *_: (build in self._fx_refreshers and
                                               self._fx_refreshers.remove(build), False)[1])
        w.present()

    def _duck_tick(self):
        """Ducking: lower a channel while any of its trigger channels is active."""
        now = time.monotonic()
        moved = False
        for ch in self.cfg["channels"]:
            d = ch.get("duck") or {}
            cur = self.eng.duck.get(ch["id"], 1.0)
            target = 1.0
            if d.get("on") and d.get("by"):
                thr = 10 ** (d.get("threshold", -40) / 20)
                if any(self.meters.level.get(t, 0) > thr for t in d["by"] if t != ch["id"]):
                    self._duck_until[ch["id"]] = now + d.get("hold", 500) / 1000
                if now < self._duck_until.get(ch["id"], 0):
                    target = 10 ** (-d.get("amount", 12) / 20)
            if target < cur:
                new = max(target, cur * 0.55)                     # duck fast
            else:
                new = min(target, cur + (1 - cur) * 0.12 + 0.004)   # recover smoothly
            if abs(new - cur) > 1e-4:
                self.eng.duck[ch["id"]] = new
                moved = True
        if moved:
            self.eng.apply_levels()

    def toast(self, text):
        if getattr(self, "_toasts", None) is not None:
            self._toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def notice(self, heading, body):
        """Something the user has to read (not a 2-second toast)."""
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    def deck_page_changed(self):
        for d in self.decks:
            d.build_pagebar()
            d.refresh()

    def deck_layout_changed(self):
        for d in self.decks:
            d.d = self.eng.cfg["deck"]
            d.build()
            if getattr(d, "on_layout", None):
                d.on_layout()

    def _stats_tick(self):
        self.sync_meters()        # follows ducking settings and dead meters within a second
        visible = [d for d in self.decks if d.get_mapped()]
        if any(d.uses(("stat",)) for d in visible):
            self.stats.sample()
        if any(d.uses(("media",)) for d in visible):
            self.media.refresh()
        if visible:
            self.refresh_decks()
        return True

    def refresh_decks(self):
        shown = [d for d in self.decks if d.get_mapped()]
        if not shown or not self.app.control:
            return                                # don't build the whole state for nobody
        st = self.app.control.state()
        for d in shown:
            d.refresh(st)

    def _build_apps_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8,
                       margin_bottom=16, margin_start=20, margin_end=20)
        page.append(Gtk.Label(label="Apps", xalign=0, css_classes=["page-title"]))
        page.append(Gtk.Label(
            label="Send each playing app to a channel; the app is remembered and moved "
                  "there every time it plays. You can also edit this from a channel's "
                  "window (click a channel).",
            xalign=0, wrap=True, css_classes=["sub"]))
        sw = Gtk.ScrolledWindow(vexpand=True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.append(Gtk.Label(label="Playing", xalign=0, css_classes=["heading"]))
        self.apps_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    css_classes=["boxed-list"], valign=Gtk.Align.START)
        inner.append(self.apps_box)
        inner.append(Gtk.Label(label="Recording", xalign=0, css_classes=["heading"], margin_top=12))
        inner.append(Gtk.Label(
            label="Apps using a microphone. Choose what each one records — e.g. a mix's virtual "
                  "microphone. The choice is remembered.", xalign=0, wrap=True, css_classes=["sub"]))
        self.rec_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                   css_classes=["boxed-list"], valign=Gtk.Align.START)
        inner.append(self.rec_box)
        sw.set_child(inner)
        page.append(sw)
        return page

    # ---- helpers
    def schedule(self, key, ms, fn):
        if key in self._timers:
            return

        def run():
            self._timers.pop(key, None)
            fn()
            return False
        self._timers[key] = GLib.timeout_add(ms, run)

    def changed(self):
        self.schedule("save", 400, lambda: save_config(self.cfg))

    def structure_changed(self):
        self.changed()
        self.eng.reconcile()
        self.sync_meters()
        self.rebuild_all()

    def sync_meters(self):
        want = self.eng.meter_sources()
        comp = self.app.companion
        if not (self.get_visible() or (comp and comp.viewers)):
            # nobody is looking: keep only the meters that drive ducking
            need = {t for ch in self.cfg["channels"] if (ch.get("duck") or {}).get("on")
                    for t in (ch.get("duck") or {}).get("by", []) if t != ch["id"]}
            want = {k: v for k, v in want.items() if k in need}
        self.meters.sync(want)

    def output_options(self):
        return [(s.get("description") or s["name"], s["name"]) for s in self.eng.hw_sinks]

    def route_options(self):
        opts = [(f"{c['name']}  (channel)", f"amx_ch_{c['id']}")
                for c in self.cfg["channels"] if c["type"] == "virtual"]
        return opts + self.output_options()

    def assign_app(self, app_name, channel):
        """Make `channel` (or nobody) the home of an app, removing it from other channels."""
        for ch in self.cfg["channels"]:
            ch["apps"] = [a for a in ch.get("apps", []) if a.lower() != app_name.lower()]
        if channel is not None:
            channel["apps"].append(app_name)
        self.changed()

    # ---- builders
    def rebuild_all(self):
        self.build_devices()
        self.build_default()
        self.build_matrix()
        self.build_apps()

    def build_devices(self):
        clear(self.dev_box)
        used = {c.get("source") for c in self.cfg["channels"]}
        n = 0
        for s in self.eng.hw_sources:
            if s["name"] in used:
                continue
            b = Gtk.Button(css_classes=["devbtn", "flat"], tooltip_text="Add as a channel")
            row = Gtk.Box(spacing=12)
            row.append(Gtk.Image(icon_name="audio-input-microphone-symbolic"))
            row.append(Gtk.Label(label=s.get("description") or s["name"], xalign=0,
                                 ellipsize=3, hexpand=True, max_width_chars=22))
            row.append(Gtk.Image(icon_name="list-add-symbolic", css_classes=["sub"]))
            b.set_child(row)

            def add(_b, s=s):
                self.cfg["channels"].append({
                    "id": short_id(), "type": "hw", "source": s["name"],
                    "name": s.get("description") or s["name"]})
                self.structure_changed()
            b.connect("clicked", add)
            self.dev_box.append(b)
            n += 1
        if not n:
            self.dev_box.append(Gtk.Label(label="All inputs added", xalign=0, margin_start=6,
                                          css_classes=["sub"]))

    def build_default(self):
        clear(self.default_box)
        sysch = self.eng.system_channel()
        row = Gtk.Box(spacing=10)
        row.append(Gtk.Label(label=f"All system sounds → {sysch['name'] if sysch else 'System'}",
                             css_classes=["sub"]))
        sw = Gtk.Switch(active=bool(self.cfg.get("system_default")) and sysch is not None,
                        sensitive=sysch is not None, valign=Gtk.Align.CENTER,
                        tooltip_text="Route every sound that isn't assigned to another "
                                     "channel into the System channel")

        def toggled(s, _p):
            self.cfg["system_default"] = s.get_active()
            self.changed()
            self.eng.reconcile()
        sw.connect("notify::active", toggled)
        row.append(sw)
        self.default_box.append(row)

        hrow = Gtk.Box(spacing=10, margin_start=18)
        hrow.append(Gtk.Label(label="Mic mute → all apps", css_classes=["sub"]))
        hsw = Gtk.Switch(active=bool(self.cfg.get("hw_mute_device")), valign=Gtk.Align.CENTER,
                         tooltip_text="Off: muting a microphone channel only silences it in "
                                      "the mixes, and each mix can mute it on its own. On: it "
                                      "also mutes the device, so apps that record the mic "
                                      "directly (Discord, OBS, a browser) go silent too")

        def htoggled(s, _p):
            self.cfg["hw_mute_device"] = s.get_active()
            self.changed()
            self.eng.apply_hw_mutes()
        hsw.connect("notify::active", htoggled)
        hrow.append(hsw)
        self.default_box.append(hrow)

    def build_hint(self):
        """First-run help, until an app has been given a channel (or it is dismissed)."""
        clear(self.hint_box)
        if self.cfg.get("hint_dismissed") or any(c.get("apps") for c in self.cfg["channels"]):
            return
        bar = Gtk.Box(spacing=12, css_classes=["cell"], hexpand=True)
        bar.append(Gtk.Image(icon_name="dialog-information-symbolic", valign=Gtk.Align.START))
        bar.append(Gtk.Label(
            label="<b>Getting started</b>\n"
                  "1. Play something, then open <b>Apps</b> and send each app to a channel "
                  "(Games, Music, …). Anything you don't assign plays through <b>System</b>.\n"
                  "2. Click a mix's name to choose where it plays: headphones, speakers, or a "
                  "virtual microphone for Discord / OBS.\n"
                  "3. Each row's slider is that channel's level in that mix. Click a channel for "
                  "effects, panning and ducking. Add a microphone from <b>Your devices</b>.",
            use_markup=True, wrap=True, xalign=0, hexpand=True))
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"],
                           valign=Gtk.Align.START, tooltip_text="Hide this")

        def dismiss(_b):
            self.cfg["hint_dismissed"] = True
            self.changed()
            self.build_hint()
        close.connect("clicked", dismiss)
        bar.append(close)
        self.hint_box.append(bar)

    def build_matrix(self):
        self.build_hint()
        clear(self.grid)
        self.refs = []
        eng, cfg = self.eng, self.cfg
        mixes, chans = cfg["mixes"], cfg["channels"]

        for ci, m in enumerate(mixes):
            self.grid.attach(self.mix_header(m), ci + 1, 0, 1, 1)
        plus = Gtk.Button(icon_name="list-add-symbolic", css_classes=["cell-empty", "flat"],
                          tooltip_text="Add mix", valign=Gtk.Align.FILL)
        plus.set_size_request(52, -1)

        def add_mix(_b):
            m = normalize({"channels": [], "mixes": [
                {"id": short_id(), "name": f"Mix {len(mixes) + 1}", "outputs": []}]})["mixes"][0]
            mixes.append(m)
            self.structure_changed()
            self.open_dialog("mix", m)
        plus.connect("clicked", add_mix)
        self.grid.attach(plus, len(mixes) + 1, 0, 1, 1)

        for ri, ch in enumerate(chans):
            eng.chan(ch)
            self.grid.attach(self.channel_card(ch), 0, ri + 1, 1, 1)
            for ci, m in enumerate(mixes):
                self.grid.attach(self.cell_widget(m, ch), ci + 1, ri + 1, 1, 1)

        self.grid.attach(self.create_channel_button(), 0, len(chans) + 1, 1, 1)

    def create_channel_button(self):
        mb = Gtk.MenuButton(css_classes=["cell", "flat"], always_show_arrow=False)
        mb.set_child(pill_button("list-add-symbolic", "Create channel"))
        mb.set_size_request(400, 48)
        pop = Gtk.Popover()
        box = Gtk.Box(spacing=6, margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        entry = Gtk.Entry(placeholder_text="Channel name, e.g. Podcast", width_chars=24)
        go = Gtk.Button(label="Add", css_classes=["suggested-action"])
        box.append(entry)
        box.append(go)
        pop.set_child(box)
        mb.set_popover(pop)

        def add(*_):
            n = entry.get_text().strip()
            if n:
                ch = {"id": short_id(), "name": n, "type": "virtual"}
                self.cfg["channels"].append(ch)
                entry.set_text("")
                pop.popdown()
                self.structure_changed()
                self.open_dialog("chan", ch)
        go.connect("clicked", add)
        entry.connect("activate", add)
        return mb

    def channel_card(self, ch):
        card = Gtk.Box(spacing=14, css_classes=["cell"])
        card.set_size_request(400, -1)
        tile = Gtk.Box(css_classes=["tile"], halign=Gtk.Align.START, valign=Gtk.Align.CENTER,
                       tooltip_text="Click to edit")
        tile.append(Gtk.Image(icon_name=safe_icon(ch["icon"]), pixel_size=30, margin_start=13,
                              margin_end=13, margin_top=13, margin_bottom=13))
        clickable(tile, lambda: self.open_dialog("chan", ch))
        card.append(tile)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        head = Gtk.Box()
        n_apps = len(ch["apps"])
        subtxt = ("Hardware input" if ch["type"] == "hw"
                  else "System sounds" if ch.get("system")
                  else f"{n_apps} app{'s' if n_apps != 1 else ''}")
        if fxm.active(ch):
            subtxt += "  ·  FX"
        if (ch.get("duck") or {}).get("on"):
            subtxt += "  ·  ducking"
        head.append(Gtk.Label(label=ch["name"], xalign=0, hexpand=True, css_classes=["chname"],
                              ellipsize=3))
        head.append(Gtk.Label(label=subtxt, css_classes=["sub"]))
        clickable(head, lambda: self.open_dialog("chan", ch))
        vb.append(head)
        row = Gtk.Box(spacing=6)

        def set_mute(v):
            ch["mute"] = v
            self.changed()
            self.eng.apply_levels()

        def set_vol(v):
            ch["vol"] = v
            self.changed()
            self.schedule("levels", 40, self.eng.apply_levels)
        mb = mute_button(ch["mute"], set_mute)
        mb.set_tooltip_text("Mute this microphone everywhere: in every mix and in apps that use it "
                            "directly (Discord, OBS, …)" if ch["type"] == "hw"
                            else "Mute this channel in every mix")
        row.append(mb)
        row.append(fader(ch["vol"], set_vol,
                         lambda: 0.0 if ch["mute"] else db_level(self.meters.level.get(ch["id"], 0)),
                         self.refs))
        more = Gtk.Button(icon_name="view-more-symbolic", css_classes=["flat", "dimbtn"],
                          valign=Gtk.Align.CENTER, tooltip_text="Edit channel")
        more.connect("clicked", lambda *_: self.open_dialog("chan", ch))
        row.append(more)
        vb.append(row)
        card.append(vb)
        return card

    def cell_widget(self, mix, ch):
        eng = self.eng
        cell = eng.cell(mix, ch)
        if not cell["on"]:
            b = Gtk.Button(css_classes=["cell-empty", "flat"], valign=Gtk.Align.FILL,
                           tooltip_text=f"Send {ch['name']} to {mix['name']}")
            b.set_child(Gtk.Image(icon_name="list-add-symbolic", css_classes=["dimbtn"]))
            b.set_size_request(290, -1)

            def enable(_b):
                cell["on"] = True
                self.structure_changed()
            b.connect("clicked", enable)
            return b

        box = Gtk.Box(spacing=6, css_classes=["cell"], valign=Gtk.Align.FILL)
        box.set_size_request(290, -1)

        def set_mute(v):
            cell["mute"] = v
            self.changed()
            eng.apply_levels()

        def set_vol(v):
            cell["vol"] = v
            self.changed()
            self.schedule("levels", 40, eng.apply_levels)
        box.append(mute_button(cell["mute"], set_mute))
        box.append(fader(cell["vol"], set_vol, lambda: self._cell_level(mix, ch), self.refs))
        off = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "dimbtn"],
                         valign=Gtk.Align.CENTER, tooltip_text="Remove from this mix")

        def disable(_b):
            cell["on"] = False
            self.structure_changed()
        off.connect("clicked", disable)
        box.append(off)
        return box

    def _cell_level(self, mix, ch):
        return db_level(self.meters.level.get(ch["id"], 0) * self.eng.gain(mix, ch))

    def mix_header(self, mix):
        b = Gtk.Button(css_classes=["mixhead", "flat"], tooltip_text="Click to edit")
        b.set_size_request(290, -1)
        row = Gtk.Box(spacing=12)
        row.append(Gtk.Image(icon_name=safe_icon(mix["icon"], "audio-headphones-symbolic"),
                             pixel_size=30))
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START)
        vb.append(Gtk.Label(label=mix["name"], xalign=0, css_classes=["mixname"]))
        n = len(mix["outputs"])
        vb.append(Gtk.Label(label="No outputs" if n == 0 else f"{n} Output{'s' if n != 1 else ''}",
                            xalign=0, css_classes=["sub"]))
        row.append(vb)
        b.set_child(row)
        b.connect("clicked", lambda *_: self.open_dialog("mix", mix))
        return b

    # ---- edit dialog (mix or channel)
    def open_dialog(self, kind, obj):
        if self._dialog_open:
            return
        self._dialog_open = True
        eng, cfg = self.eng, self.cfg
        is_mix = kind == "mix"
        dlg = Adw.Dialog(content_width=940, content_height=620)
        dlg.set_title("Edit mix" if is_mix else "Edit channel")
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=20,
                       margin_bottom=20, margin_start=24, margin_end=24)
        dlg.set_child(root)

        name = Gtk.Entry(text=obj["name"], halign=Gtk.Align.START, width_chars=28,
                         css_classes=["dlg-name"])

        def on_name(e):
            t = e.get_text().strip()
            if t:
                obj["name"] = t
                self.changed()
        name.connect("changed", on_name)
        root.append(name)

        body = Gtk.Box(spacing=28, vexpand=True)
        root.append(body)

        # left: icon + volume
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER,
                       width_request=300)
        body.append(left)
        big = Gtk.Box(css_classes=["bigtile"], halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                      tooltip_text="Click to change the icon", margin_top=36)
        default_ic = "audio-headphones-symbolic" if is_mix else "audio-volume-high-symbolic"
        img = Gtk.Image(icon_name=safe_icon(obj.get("icon"), default_ic), pixel_size=96,
                        margin_start=62, margin_end=62, margin_top=62, margin_bottom=62)
        big.append(img)
        left.append(big)
        picker = Gtk.MenuButton(halign=Gtk.Align.CENTER, css_classes=["flat"])
        picker.set_child(pill_button("document-edit-symbolic", "Change icon"))
        left.append(picker)

        ppop = Gtk.Popover()
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin_top=10, margin_bottom=10,
                        margin_start=10, margin_end=10)
        shown = 0
        for ic in PICKER_ICONS:
            real = safe_icon(ic, "")
            if not real:
                continue
            ib = Gtk.ToggleButton(css_classes=["flat"], active=obj.get("icon") == ic)
            ib.set_child(Gtk.Image(icon_name=real, pixel_size=28))
            ib.set_size_request(52, 52)

            def pick(_b, ic=ic):
                if not _b.get_active():
                    return
                obj["icon"] = ic
                obj["icon_set"] = True          # chosen by hand: don't guess from the name again
                img.set_from_icon_name(safe_icon(ic, default_ic))
                self.changed()
                ppop.popdown()
            ib.connect("toggled", pick)
            grid.attach(ib, shown % 6, shown // 6, 1, 1)
            shown += 1
        ppop.set_child(grid)
        picker.set_popover(ppop)
        clickable(big, picker.popup)

        left.append(Gtk.Label(label="Mix volume" if is_mix else "Channel volume",
                              css_classes=["sub"], margin_top=8))
        vrow = Gtk.Box(spacing=8)

        def set_mute(v):
            obj["mute"] = v
            self.changed()
            eng.apply_levels()
        vrow.append(mute_button(obj["mute"], set_mute))
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        sc.add_css_class("plain")
        sc.set_hexpand(True)
        sc.set_draw_value(True)
        sc.set_value(obj["vol"])

        def set_vol(s):
            obj["vol"] = round(s.get_value())
            self.changed()
            self.schedule("levels", 40, eng.apply_levels)
        sc.connect("value-changed", set_vol)
        vrow.append(sc)
        left.append(vrow)

        # right: tabs
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["cell"],
                        hexpand=True, margin_bottom=4)
        body.append(right)
        tabs = Gtk.Box(css_classes=["linked"], halign=Gtk.Align.START)
        stack = Gtk.Stack(vexpand=True)
        right.append(tabs)
        right.append(stack)

        def add_tab(key, label, widget, group):
            b = Gtk.ToggleButton(label=label)
            if group[0] is None:
                group[0] = b
                b.set_active(True)
            else:
                b.set_group(group[0])
            b.connect("toggled", lambda btn: btn.get_active() and stack.set_visible_child_name(key))
            tabs.append(b)
            stack.add_named(widget, key)
            return b

        def list_page(empty_title, empty_sub):
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                              css_classes=["boxed-list"], valign=Gtk.Align.START)
            sw.set_child(lst)
            page.append(sw)
            return page, lst

        def popover_button(icon, label):
            mb = Gtk.MenuButton(css_classes=["cell-empty", "flat"], always_show_arrow=False)
            mb.set_child(pill_button(icon, label))
            pop = Gtk.Popover()
            pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8,
                           margin_bottom=8, margin_start=8, margin_end=8, width_request=340)
            psw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     max_content_height=380, propagate_natural_height=True)
            psw.set_child(pbox)
            pop.set_child(psw)
            mb.set_popover(pop)
            return mb, pop, pbox

        group = [None]
        fx_builds = []
        if is_mix:
            page, lst = list_page("", "")
            addb, pop, pbox = popover_button("list-add-symbolic", "Add output")
            page.append(addb)

            def fill_outputs():
                clear(lst)
                if not obj["outputs"]:
                    lst.append(Adw.ActionRow(
                        title="No outputs",
                        subtitle=f"Apps can still record “{obj['name']} (mix)” as an input"))
                for o in list(obj["outputs"]):
                    if o == "mic":
                        r = Adw.ActionRow(
                            title="Virtual microphone",
                            subtitle=GLib.markup_escape_text(
                                f"“{obj['name']} (mic)” – pick it as the microphone in Discord, OBS, …"))
                        r.add_prefix(Gtk.Image(icon_name="audio-input-microphone-symbolic"))
                    else:
                        r = Adw.ActionRow(title=GLib.markup_escape_text(eng.sink_desc(o)))
                        r.add_prefix(Gtk.Image(icon_name="audio-speakers-symbolic"))
                        ol = obj.setdefault("out_levels", {}).setdefault(o, {"vol": 100, "mute": False})

                        def set_omute(v, ol=ol):
                            ol["mute"] = v
                            self.changed()
                            eng.apply_levels()

                        def set_ovol(sc_, ol=ol):
                            ol["vol"] = round(sc_.get_value())
                            self.changed()
                            self.schedule("levels", 40, eng.apply_levels)
                        r.add_suffix(mute_button(ol.get("mute", False), set_omute))
                        osc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
                        osc.add_css_class("plain")
                        osc.set_size_request(150, -1)
                        osc.set_valign(Gtk.Align.CENTER)
                        osc.set_value(ol.get("vol", 100))
                        osc.set_tooltip_text("This output's own level (on top of the mix volume)")
                        osc.connect("value-changed", set_ovol)
                        r.add_suffix(osc)
                    rm = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"],
                                    valign=Gtk.Align.CENTER, tooltip_text="Remove output")

                    def remove(_b, o=o):
                        obj["outputs"].remove(o)
                        if o == "mic":
                            obj["default_mic"] = False
                        self.changed()
                        eng.reconcile()
                        fill_outputs()
                        fill_settings()
                    rm.connect("clicked", remove)
                    r.add_suffix(rm)
                    lst.append(r)

            def fill_pop(*_):
                clear(pbox)
                if "mic" not in obj["outputs"]:
                    vb = Gtk.Button(css_classes=["flat"])
                    vrow = Gtk.Box(spacing=10)
                    vrow.append(Gtk.Image(icon_name="audio-input-microphone-symbolic"))
                    vl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    vl.append(Gtk.Label(label="Virtual microphone", xalign=0))
                    vl.append(Gtk.Label(label="An input device apps can record this mix from",
                                        xalign=0, css_classes=["sub"]))
                    vrow.append(vl)
                    vb.set_child(vrow)

                    def add_mic(_b):
                        obj["outputs"].append("mic")
                        self.changed()
                        pop.popdown()
                        eng.reconcile()
                        fill_outputs()
                        fill_settings()
                    vb.connect("clicked", add_mic)
                    pbox.append(vb)
                    pbox.append(Gtk.Separator(margin_top=2, margin_bottom=2))
                free = [s for s in eng.hw_sinks if s["name"] not in obj["outputs"]]
                for s in free:
                    b = Gtk.Button(label=s.get("description") or s["name"], css_classes=["flat"])
                    b.get_child().set_halign(Gtk.Align.START)

                    def add(_b, s=s):
                        obj["outputs"].append(s["name"])
                        self.changed()
                        pop.popdown()
                        eng.reconcile()
                        fill_outputs()
                    b.connect("clicked", add)
                    pbox.append(b)
                if not free:
                    pbox.append(Gtk.Label(label="All outputs already added", css_classes=["sub"]))
            pop.connect("show", fill_pop)
            fill_outputs()
            add_tab("outputs", "Audio outputs", page, group)
            add_tab("effects", "Effects", self._fx_tab(obj, "effects", fx_builds), group)

            sp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

            def fill_settings():
                clear(sp)
                r = Gtk.Box(spacing=12)
                r.append(Gtk.Label(label="Mute mix", hexpand=True, xalign=0))
                sw_mute = Gtk.Switch(active=obj["mute"], valign=Gtk.Align.CENTER)
                sw_mute.connect("notify::active", lambda s, _p: set_mute(s.get_active()))
                r.append(sw_mute)
                sp.append(r)
                has_mic = "mic" in obj["outputs"]
                r2 = Gtk.Box(spacing=12)
                lb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
                lb.append(Gtk.Label(label="Default microphone", xalign=0))
                lb.append(Gtk.Label(label="Apps that use the system default record this mix"
                                    if has_mic else "Add the “Virtual microphone” output first",
                                    xalign=0, css_classes=["sub"]))
                r2.append(lb)
                sw_def = Gtk.Switch(active=bool(obj.get("default_mic")) and has_mic,
                                    sensitive=has_mic, valign=Gtk.Align.CENTER)

                def set_default_mic(s, _p):
                    on = s.get_active()
                    for m in cfg["mixes"]:
                        m["default_mic"] = False
                    obj["default_mic"] = on
                    self.changed()
                    eng.reconcile()
                sw_def.connect("notify::active", set_default_mic)
                r2.append(sw_def)
                sp.append(r2)
                sp.append(Gtk.Label(
                    label=(f"Apps can pick “{obj['name']} (mic)” as their microphone." if has_mic else
                           f"Apps can record “Monitor of {obj['name']} (mix)”; add the Virtual microphone "
                           "output to get a proper microphone that every app lists."),
                    xalign=0, wrap=True, css_classes=["sub"]))
            fill_settings()
            add_tab("settings", "Mix settings", sp, group)
        else:
            if obj["type"] == "virtual" and obj.get("system"):
                page, lst = list_page("", "")
                page.prepend(Gtk.Label(
                    label="Catches all system sounds and every app that isn't assigned to "
                          "another channel.", xalign=0, wrap=True, css_classes=["sub"]))

                def fill_live():
                    clear(lst)
                    here = [a for a in eng.apps() if a["sink"] == f"amx_ch_{obj['id']}"]
                    if not here:
                        lst.append(Adw.ActionRow(title="Nothing playing here right now",
                                                 subtitle="System sounds appear here as they happen"))
                    for a in here:
                        lst.append(Adw.ActionRow(
                            title=GLib.markup_escape_text(a["name"]),
                            subtitle="paused" if a["paused"] else "playing"))
                eng.refresh()
                fill_live()
                add_tab("apps", "Playing here", page, group)
            elif obj["type"] == "virtual":
                page, lst = list_page("", "")
                addb, pop, pbox = popover_button("list-add-symbolic", "Add app")
                page.append(addb)

                def fill_apps():
                    clear(lst)
                    if not obj["apps"]:
                        lst.append(Adw.ActionRow(
                            title="No apps",
                            subtitle="Add an app and it will always play through this channel"))
                    running = {a["name"].lower() for a in eng.apps()}
                    for a in list(obj["apps"]):
                        r = Adw.ActionRow(
                            title=GLib.markup_escape_text(a),
                            subtitle="Playing now" if a.lower() in running else "Not playing")
                        rm = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"],
                                        valign=Gtk.Align.CENTER, tooltip_text="Remove app")

                        def remove(_b, a=a):
                            self.assign_app(a, None)
                            fill_apps()
                        rm.connect("clicked", remove)
                        r.add_suffix(rm)
                        lst.append(r)

                def assign(app_name):
                    self.assign_app(app_name, obj)
                    pop.popdown()
                    eng.reconcile()
                    fill_apps()

                def fill_pop(*_):
                    clear(pbox)
                    eng.refresh()  # live data: the background refresh pauses while a dialog is open
                    mine = {x.lower() for x in obj["apps"]}
                    chan_of = {f"amx_ch_{c['id']}": c["name"] for c in cfg["channels"]}
                    streams = [a for a in eng.apps() if a["name"].lower() not in mine]
                    stream_ids = {x for a in eng.apps() for x in (a["name"].lower(), a["binary"])}
                    running = eng.running_apps() + eng.clients()
                    installed = eng.installed_apps()

                    search = Gtk.SearchEntry(placeholder_text="Search installed apps…")
                    pbox.append(search)
                    results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                    pbox.append(results)

                    def app_row(app_name, state):
                        b = Gtk.Button(css_classes=["flat"])
                        bl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                        bl.append(Gtk.Label(label=app_name, xalign=0))
                        bl.append(Gtk.Label(label=state, xalign=0, css_classes=["sub"],
                                            ellipsize=3, max_width_chars=40))
                        b.set_child(bl)
                        b.connect("clicked", lambda _b: assign(app_name))
                        results.append(b)

                    def header(text):
                        results.append(Gtk.Label(label=text, xalign=0, css_classes=["sub"],
                                                 margin_start=4, margin_top=6))

                    def render(*_):
                        q = search.get_text().strip().lower()
                        clear(results)
                        shown = set()
                        count = 0

                        def want(name):
                            k = name.lower()
                            return (k not in mine and k not in shown and (not q or q in k))

                        def take(name):
                            shown.add(name.lower())
                            return True

                        first = True
                        for a in streams:
                            if not want(a["name"]):
                                continue
                            if first:
                                header("Active apps")
                                first = False
                            take(a["name"])
                            where = chan_of.get(a["sink"]) or eng.sink_desc(a["sink"])
                            app_row(a["name"], ("paused" if a["paused"] else "playing") + " · " + where)
                            count += 1
                        # running apps that are not already listed as an audio stream
                        first = True
                        for n in sorted(set(running), key=str.lower):
                            if not want(n) or (eng.rule_keys(n) & stream_ids):
                                continue
                            if first:
                                header("Running")
                                first = False
                            take(n)
                            app_row(n, "running")
                            count += 1
                        # everything installed (only while searching, to keep the default list short)
                        if q:
                            first = True
                            for n in installed:
                                if count >= 60:
                                    break
                                if not want(n) or (eng.rule_keys(n) & stream_ids):
                                    continue
                                if first:
                                    header("Installed")
                                    first = False
                                take(n)
                                app_row(n, "installed")
                                count += 1
                        if not count:
                            results.append(Gtk.Label(
                                label=("No installed app matches “" + q + "”" if q else
                                       "No other apps are running right now.\nSearch to find "
                                       "any installed app."),
                                css_classes=["sub"], margin_top=6, margin_bottom=6))
                    search.connect("search-changed", render)
                    if os.environ.get("AMX_DEBUG_SEARCH"):  # testing aid
                        search.set_text(os.environ["AMX_DEBUG_SEARCH"])
                    render()
                    GLib.idle_add(lambda: (search.grab_focus(), False)[1])   # one-shot (grab_focus returns True)

                    pbox.append(Gtk.Separator(margin_top=4, margin_bottom=4))
                    er = Gtk.Box(spacing=6)
                    ent = Gtk.Entry(placeholder_text="Or type an app name (e.g. Spotify)",
                                    hexpand=True)
                    go = Gtk.Button(label="Add", css_classes=["suggested-action"])

                    def typed(*_):
                        t = ent.get_text().strip()
                        if t:
                            assign(t)
                    go.connect("clicked", typed)
                    ent.connect("activate", typed)
                    er.append(ent)
                    er.append(go)
                    pbox.append(er)
                pop.connect("show", fill_pop)
                if os.environ.get("AMX_DEBUG_POPUP"):  # testing aid
                    GLib.timeout_add(700, lambda: addb.popup() and False)
                    if os.environ.get("AMX_DEBUG_SNAP"):  # dump the popover's visible text
                        def dump():
                            def walk(w, out):
                                if isinstance(w, Gtk.Label) and w.get_label():
                                    out.append(w.get_label().replace("\n", " / "))
                                c = w.get_first_child()
                                while c:
                                    walk(c, out)
                                    c = c.get_next_sibling()
                                return out
                            print("POPOVER:", " | ".join(walk(pop.get_child(), [])), flush=True)
                        GLib.timeout_add(1800, lambda: dump() and False)
                fill_apps()
                add_tab("apps", "Apps", page, group)

            add_tab("effects", "Effects", self._fx_tab(obj, "effects", fx_builds), group)
            add_tab("creative", "Creative", self._fx_tab(obj, "creative", fx_builds), group)
            sp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            r = Gtk.Box(spacing=12)
            r.append(Gtk.Label(label="Mute channel", hexpand=True, xalign=0))
            sw_mute = Gtk.Switch(active=obj["mute"], valign=Gtk.Align.CENTER)
            sw_mute.connect("notify::active", lambda s, _p: set_mute(s.get_active()))
            r.append(sw_mute)
            sp.append(r)
            if obj["type"] == "virtual":
                sr = Gtk.Box(spacing=12)
                sr.append(Gtk.Label(label="Catch all system sounds", hexpand=True, xalign=0))
                ssw = Gtk.Switch(active=bool(obj.get("system")), valign=Gtk.Align.CENTER)

                def set_system(sw, _p):
                    if sw.get_active():
                        for c in cfg["channels"]:
                            c["system"] = False
                        obj["system"] = True
                        obj["apps"] = []
                    else:
                        obj["system"] = False
                    self.changed()
                    eng.reconcile()
                ssw.connect("notify::active", set_system)
                sr.append(ssw)
                sp.append(sr)
            if obj["type"] == "hw":
                desc = next((x.get("description") for x in eng.sources if x["name"] == obj["source"]),
                            None) or f"{obj['source']} (not connected)"
                sp.append(Gtk.Label(label=f"Hardware input: {desc}",
                                    xalign=0, wrap=True, css_classes=["sub"]))
            else:
                sp.append(Gtk.Label(
                    label="Apps assigned to this channel are moved into it automatically. "
                          "You can also set the channel as the Default output to catch "
                          "everything else.", xalign=0, wrap=True, css_classes=["sub"]))
            sp.append(self._pan_box(obj))
            sp.append(self._duck_box(obj))
            if obj["type"] == "virtual":
                sp.append(self._midi_box(obj))
            ssw_ = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            ssw_.set_child(sp)
            add_tab("settings", "Channel settings", ssw_, group)

        # always open on the first tab
        first_tab = tabs.get_first_child()
        if first_tab is not None:
            first_tab.set_active(True)
            stack.set_visible_child(stack.get_first_child())

        # bottom row
        bottom = Gtk.Box(spacing=12)
        delete = Gtk.Button(css_classes=["destructive-action"])
        delete.set_child(pill_button("user-trash-symbolic", "Delete mix" if is_mix else "Delete channel"))
        if is_mix and len(cfg["mixes"]) <= 1:
            delete.set_sensitive(False)
            delete.set_tooltip_text("You need at least one mix")

        def do_delete(_b):
            if is_mix:
                cfg["mixes"] = [x for x in cfg["mixes"] if x["id"] != obj["id"]]
            else:
                cfg["channels"] = [c for c in cfg["channels"] if c["id"] != obj["id"]]
                for mm in cfg["mixes"]:
                    mm["levels"].pop(obj["id"], None)
            dlg.close()
        delete.connect("clicked", do_delete)
        bottom.append(delete)
        bottom.append(Gtk.Box(hexpand=True))
        close = Gtk.Button(label="Close window", css_classes=["suggested-action"])
        close.connect("clicked", lambda *_: dlg.close())
        bottom.append(close)
        root.append(bottom)

        def on_closed(_d):
            self._dialog_open = False
            for fn in fx_builds:
                if fn in self._fx_refreshers:
                    self._fx_refreshers.remove(fn)
            if is_mix:
                eng.rename_mix_sink(obj)
            else:
                eng.rename_channel_sink(obj)
            self.structure_changed()
        dlg.connect("closed", on_closed)
        dlg.present(self)

    def _fx_tab(self, obj, which, registry):
        """A tab holding the effects (or Creative Rack) editor, rebuilt after remote changes."""
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)

        def build():
            clear(holder)
            if which == "effects":
                ed = fxm.effects_editor(obj, self.fx_edit)
            else:
                ed = fxm.rack_editor(obj, self.fx_edit, self.fx_edit, self.toast, holder)
                pop = Gtk.Button(label="Open the rack in its own window", css_classes=["flat"],
                                 halign=Gtk.Align.END)
                pop.connect("clicked", lambda *_: self.open_rack_window(obj))
                holder.append(pop)
            ed.set_vexpand(True)
            holder.append(ed)
            if not self.eng.fx.ok:
                holder.prepend(Gtk.Label(label=f"Effects are unavailable: {self.eng.fx.msg}", wrap=True,
                                         xalign=0, css_classes=["sub"]))
        build()
        registry.append(build)
        self._fx_refreshers.append(build)
        return holder

    def _pan_box(self, obj):
        """Left / right pan for the channel, applied in every mix."""
        self.eng.chan(obj)
        grp = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        row = Adw.ActionRow(title="Pan", subtitle="Place this channel left or right in every mix")
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        sc.set_value(obj.get("pan", 0))
        sc.set_draw_value(False)
        sc.add_css_class("plain")
        sc.set_size_request(230, -1)
        sc.set_valign(Gtk.Align.CENTER)
        sc.add_mark(0, Gtk.PositionType.BOTTOM, None)
        lab = Gtk.Label(label=pan_label(obj.get("pan", 0)), width_chars=6, xalign=1,
                        css_classes=["sub", "numeric"])

        def changed(s):
            v = round(s.get_value())
            if abs(v) <= 2:                       # snap to centre
                v = 0
            lab.set_label(pan_label(v))
            if v != obj.get("pan", 0):
                obj["pan"] = v
                self.eng.apply_levels()
                self.changed()
        sc.connect("value-changed", changed)
        g = Gtk.GestureClick()                    # double-click the value to centre
        g.connect("pressed", lambda _g, n, *_: n == 2 and sc.set_value(0))
        lab.add_controller(g)
        lab.set_tooltip_text("Double-click to centre")
        row.add_suffix(Gtk.Label(label="L", css_classes=["sub"]))
        row.add_suffix(sc)
        row.add_suffix(Gtk.Label(label="R", css_classes=["sub"]))
        row.add_suffix(lab)
        grp.append(row)
        return grp

    def _duck_box(self, obj):
        d = obj.setdefault("duck", {})
        d.setdefault("on", False)
        d.setdefault("by", [])
        d.setdefault("amount", 12)
        d.setdefault("threshold", -40)
        d.setdefault("hold", 500)
        grp = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        exp = Adw.ExpanderRow(title="Ducking", subtitle="Turn this channel down while other channels are "
                                                        "active (e.g. music while you or your friends talk)",
                              show_enable_switch=True, enable_expansion=bool(d["on"]))

        def toggled(e, _p):
            d["on"] = e.get_enable_expansion()
            self.changed()
        exp.connect("notify::enable-expansion", toggled)
        for c in self.cfg["channels"]:
            if c["id"] == obj["id"]:
                continue
            row = Adw.SwitchRow(title=f"When “{GLib.markup_escape_text(c['name'])}” is active",
                                active=c["id"] in d["by"])

            def pick(r, _p, cid=c["id"]):
                d["by"] = [x for x in d["by"] if x != cid] + ([cid] if r.get_active() else [])
                self.changed()
            row.connect("notify::active", pick)
            exp.add_row(row)
        for key, p in (("amount", fxm.P("a", "Turn down by", 0, 40, 12, "dB")),
                       ("threshold", fxm.P("t", "Trigger level", -70, -10, -40, "dB")),
                       ("hold", fxm.P("h", "Hold", 0, 3000, 500, "ms"))):
            def setv(v, key=key):
                d[key] = v
                self.changed()
            exp.add_row(fxm.param_row(p, float(d[key]), setv))
        grp.append(exp)
        return grp

    def _midi_box(self, obj):
        m = obj.setdefault("midi", {})
        grp = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        have = shutil.which("fluidsynth") is not None

        def resync():
            self.eng.instruments.sync(self.cfg["channels"], {s["name"] for s in self.eng.sinks})
        exp = Adw.ExpanderRow(title="MIDI instrument",
                              subtitle="Play a SoundFont from any MIDI keyboard into this channel (FluidSynth)"
                              if have else "Install fluidsynth to use MIDI instruments",
                              show_enable_switch=True, enable_expansion=bool(m.get("on")) and have,
                              sensitive=have)
        sf = Adw.ActionRow(title="SoundFont", subtitle=GLib.markup_escape_text(
            os.path.basename(m["sf2"]) if m.get("sf2") else "None chosen (.sf2 / .sf3)"))
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)

        def chosen(path):
            m["sf2"] = path
            sf.set_subtitle(GLib.markup_escape_text(os.path.basename(path)))
            self.changed()
            resync()
        choose.connect("clicked", lambda *_: ext.file_dialog(choose, "Choose a SoundFont", done=chosen,
                                                             patterns=["SoundFonts", "*.sf2", "*.sf3"]))
        sf.add_suffix(choose)
        exp.add_row(sf)

        def setg(v):
            m["gain"] = v
            self.changed()
            self.schedule("midi", 600, resync)
        exp.add_row(fxm.param_row(fxm.P("g", "Synth level", 0.1, 2.0, 0.6, "", 2), float(m.get("gain", 0.6)), setg))

        def toggled(e, _p):
            m["on"] = e.get_enable_expansion()
            self.changed()
            resync()
        exp.connect("notify::enable-expansion", toggled)
        grp.append(exp)
        return grp

    def build_apps(self):
        eng = self.eng
        clear(self.apps_box)
        opts = self.route_options()
        apps = eng.apps()
        if not apps:
            self.apps_box.append(Adw.ActionRow(
                title="Nothing playing", subtitle="Apps show up here while they make sound"))
        for a in apps:
            row = Adw.ActionRow(title=GLib.markup_escape_text(a["name"]),
                                subtitle=GLib.markup_escape_text(a["media"]))

            def route(v, a=a):
                ch = next((c for c in self.cfg["channels"]
                           if f"amx_ch_{c['id']}" == v), None)
                bypass = self.cfg.setdefault("bypass", [])
                bypass[:] = [b for b in bypass if b.lower() != a["name"].lower()]
                if ch is None:           # a physical device: skip the System catch-all
                    bypass.append(a["name"])
                # the System channel is the catch-all, so it never holds a manual assignment
                self.assign_app(a["name"], None if (ch is None or ch.get("system")) else ch)
                eng.move_app(a["idx"], v)
            dd = dropdown(opts, a["sink"], route)
            dd.set_valign(Gtk.Align.CENTER)
            row.add_suffix(dd)
            self.apps_box.append(row)

        clear(self.rec_box)
        recs = eng.recorders()
        if not recs:
            self.rec_box.append(Adw.ActionRow(
                title="No apps are recording", subtitle="Apps show up here while they use a microphone"))
        in_opts = eng.input_options()
        for r in recs:
            row = Adw.ActionRow(title=GLib.markup_escape_text(r["name"]),
                                subtitle=GLib.markup_escape_text(r["media"]))
            row.add_prefix(Gtk.Image(icon_name="audio-input-microphone-symbolic"))

            def pick_input(v, r=r):
                self.cfg.setdefault("rec_rules", {})[r["name"]] = v
                self.changed()
                pactl("move-source-output", r["idx"], v)
            dd = dropdown(in_opts, r["source"], pick_input)
            dd.set_valign(Gtk.Align.CENTER)
            row.add_suffix(dd)
            self.rec_box.append(row)

    # ---- periodic
    def signature(self):
        e = self.eng
        return {
            "apps": (tuple((a["idx"], a["name"], a["media"], a["sink"]) for a in e.apps()),
                     tuple((r["idx"], r["name"], r["source"]) for r in e.recorders()),
                     tuple(o[1] for o in e.input_options())),
            "default": (e.default_sink, tuple(s["name"] for s in e.hw_sinks)),
            "src": tuple(s["name"] for s in e.hw_sources),
        }

    def refresh_dynamic(self):
        sig = self.signature()
        old, self._sigs = self._sigs, sig
        if old.get("src") != sig["src"]:
            self.build_devices()
        if old.get("default") != sig["default"]:
            self.build_default()
        if old.get("apps") != sig["apps"]:
            self.build_apps()
        self.sync_meters()

    def _on_pa_event(self):
        if self._sync_due:
            return
        # coalesce bursts, and sync at most every MIN_SYNC_S (ducking and fader moves cause events too)
        wait = max(0.25, MIN_SYNC_S - (time.monotonic() - self._last_sync))
        self._sync_due = GLib.timeout_add(int(wait * 1000), self._event_sync)

    def _event_sync(self):
        if self._timers or self._dialog_open:
            return True                          # busy: try again shortly (same timer)
        self._sync_due = 0
        self._sync()
        return False

    def _sync(self):
        self._last_sync = time.monotonic()
        self.eng.reconcile()
        self.refresh_dynamic()

    def _tick(self):
        if not (self._timers or self._dialog_open or self._sync_due) and \
                time.monotonic() - self._last_sync >= SAFETY_POLL_MS / 1000 - 0.1:
            self._sync()
        return True

    def _meter_tick(self):
        self.meters.tick()
        self._duck_tick()
        self._deck_n = (self._deck_n + 1) % 4
        if self._deck_n == 0:
            self.refresh_decks()
        if self.get_visible():
            for meter, fn in self.refs:
                meter.set_level(fn())
        return True


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.win = None
        self.eng = None
        self.tray = None
        self.control = None
        self.board = None
        self.companion = None
        self.start_hidden = False
        self._wait_win = self._wait_page = self._wait_timer = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        Gtk.Window.set_default_icon_name(APP_ID)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS + fxm.RACK_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        q = Gio.SimpleAction.new("quit", None)
        q.connect("activate", lambda *_: self.shutdown())
        self.add_action(q)
        self.hold()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                from gi.repository import GLibUnix
                GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, self.shutdown)
            except ImportError:
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.shutdown)

    def do_activate(self):
        if not self.win:
            if not self._audio_ready():
                return
            eng = Engine({"channels": [], "mixes": []})
            eng.refresh()
            cfg = load_config() or default_config(eng)
            eng.cfg = cfg
            self.eng = eng
            eng.reconcile()
            save_config(cfg)
            self.control = ControlServer(self)
            self.board = ext.Soundboard(self)
            self.companion = ext.Companion(self)
            kwin_cleanup_stale()
            self.win = MixerWindow(self, eng)
            eng.on_external_change = lambda w=self.win: (w.changed(),
                                                         w.schedule("matrix", 150, w.build_matrix))
            self.win.sync_meters()
            if CONFIG_PROBLEM:
                GLib.timeout_add(800, lambda: self.win.notice("Settings were reset", CONFIG_PROBLEM)
                                 and False)
            if (cfg.get("deck_window") or {}).get("open"):
                GLib.idle_add(lambda: (self.win.open_deck_window(), False)[1])
            self.tray = Tray(self.toggle_window, self.show_window, self.shutdown,
                             lambda: self.win and self.win.toggle_deck_window())
            self.tray.start()
            self.control.start()
            cc = cfg.get("companion") or {}
            if cc.get("autostart"):
                self.companion.start(cc.get("port", 8787))
            page = os.environ.get("AMX_DEBUG_PAGE")  # testing aids: open a page / save the window
            if page:
                GLib.timeout_add(600, lambda: self.win.stack.set_visible_child_name(page) and False)
            dpage = os.environ.get("AMX_DEBUG_DECKPAGE")
            if dpage:
                GLib.timeout_add(900, lambda: self.win.decks[0].go(int(dpage)) and False)
            presses = os.environ.get("AMX_DEBUG_PRESS")  # testing aid: "idx:ticks,idx:ticks"
            if presses:
                def do_presses():
                    for item in presses.split(","):
                        idx, ticks = (item.split(":") + ["0"])[:2]
                        self.win.decks[0].press_index(int(idx), int(ticks))
                    return False
                GLib.timeout_add(2500, do_presses)
            out = os.environ.get("AMX_DEBUG_WINSNAP")
            if out:
                def snap():
                    w = self.win                        # whole window: dialogs too
                    sn = Gtk.Snapshot()
                    from gi.repository import Graphene
                    bg = Gdk.RGBA()
                    bg.parse("#242424")                 # the window background isn't in the paintable
                    sn.append_color(bg, Graphene.Rect().init(0, 0, w.get_width(), w.get_height()))
                    Gtk.WidgetPaintable.new(w).snapshot(sn, w.get_width(), w.get_height())
                    tex = self.win.get_native().get_renderer().render_texture(sn.to_node(), None)
                    tex.save_to_png(out)
                    print("SNAP saved", w.get_width(), w.get_height(), flush=True)
                GLib.timeout_add(int(os.environ.get("AMX_DEBUG_WINSNAP_MS", 2500)),
                                 lambda: snap() and False)
            dbg = os.environ.get("AMX_DEBUG_DIALOG")  # testing aid: open a dialog on start
            if dbg in ("mix", "chan"):
                GLib.timeout_add(1500, lambda: self.win.open_dialog(
                    dbg, cfg["mixes"][0] if dbg == "mix" else cfg["channels"][2]) and False)
            if self.start_hidden:           # first launch only; launching again opens the window
                self.start_hidden = False
                return
        self.win.present()

    # ---- waiting for the audio server (e.g. started at login before PipeWire is up)
    def _audio_ready(self):
        """True when the sound server answers. Otherwise show why (unless starting hidden) and
        retry every second; activation continues by itself once it is up."""
        if shutil.which("pactl") and pactl("info") is not None:
            if self._wait_win:
                self._wait_win.destroy()
                self._wait_win = None
            return True
        if not shutil.which("pactl"):
            title, desc = ("pactl is missing",
                           "The mixer controls your sound through pactl. Install it (on Arch: "
                           "sudo pacman -S libpulse), then start the mixer again.")
        else:
            title, desc = ("Waiting for the sound server…",
                           "PipeWire (with pipewire-pulse) or PulseAudio isn't answering yet. "
                           "The mixer starts as soon as it does.\n\nIf this doesn't go away, "
                           "check that pipewire-pulse is installed and running:\n"
                           "systemctl --user status pipewire-pulse")
        if not self.start_hidden:
            if not self._wait_win:
                self._wait_win = Adw.ApplicationWindow(application=self, title="Audio Mixer",
                                                       default_width=560, default_height=420)
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                box.append(Adw.HeaderBar())
                self._wait_page = Adw.StatusPage(icon_name="audio-speakers-symbolic", vexpand=True)
                box.append(self._wait_page)
                self._wait_win.set_content(box)
                self._wait_win.connect("close-request", lambda *_: (self.quit(), False)[1])
            self._wait_page.set_title(title)
            self._wait_page.set_description(desc)
            self._wait_win.present()
        if not self._wait_timer:
            def retry():
                if shutil.which("pactl") and pactl("info") is not None:
                    self._wait_timer = None
                    self.activate()
                    return False
                return True
            self._wait_timer = GLib.timeout_add(1000, retry)
        return False

    def show_window(self):
        if self.win:
            self.win.present()

    def toggle_window(self):
        if os.environ.get("AMX_DEBUG_TRAY"):  # testing aid
            print("TOGGLE visible=%s active=%s" % (self.win.get_visible(), self.win.is_active()),
                  flush=True)
        if self.win and self.win.get_visible() and self.win.is_active():
            self.win.set_visible(False)
        else:
            self.show_window()

    def shutdown(self):
        if self.eng:
            try:
                save_config(self.eng.cfg)     # don't lose a change made just before quitting
            except OSError:
                pass
        if self.control:
            self.control.stop()
        if self.companion:
            self.companion.stop()
        if self.board:
            self.board.stop_all()
        if self.win:
            self.win.meters.stop()
            self.win.pa_events.stop()
        if self.eng:
            self.eng.teardown()
            try:
                save_config(self.eng.cfg)     # teardown released the microphones it had muted
            except OSError:
                pass
        self.quit()
        return False


CTL_HELP = """usage: {ctl} <command> [args]   (talks to the running mixer)

  state                              print the mixer state as JSON
  play <sound>  |  stop <sound>  |  stop-all          soundboard (sound = 4-digit id or name)
  toggle <sound>                     play, or stop if it is playing
  fx <channel|mix> <effect> [on|off|toggle]           effect: denoise gate lowcut eq deess comp
                                                      limiter loud, or a rack unit (reverb, delay, ...)
  fx-set <channel|mix> <effect> <param> <value>       e.g. fx-set Mic gate Threshold -50
  mute <channel> [mix] [on|off|toggle]
  vol <channel> <0-100|+N|-N> [mix]
  pan <channel> <L N|R N|C|-100..100>                e.g. pan Music L30, pan Mic C
  mix-vol <mix> <0-100|+N|-N>   |  mix-mute <mix> [on|off|toggle]
  duck <channel> [on|off|toggle]
  profile <name>
  show | hide | toggle               the mixer window
  json '<object>'                    send a raw command (see ControlServer)
"""


def ctl_main(args):
    def onoff(v):
        return {"on": True, "off": False, "toggle": "toggle"}.get((v or "toggle").lower(), "toggle")

    def vol(v):
        return {"delta": int(v)} if v[:1] in "+-" else {"vol": int(v)}
    a = args
    try:
        c = a[0] if a else "help"
        if c == "state":
            msg = {"cmd": "state"}
        elif c in ("play", "stop") or (c == "toggle" and len(a) > 1):
            msg = {"cmd": "soundboard", "action": c, "id": " ".join(a[1:])}
        elif c == "stop-all":
            msg = {"cmd": "soundboard", "action": "stop_all"}
        elif c == "fx":
            rack = a[2].lower() in fxm.RACK or a[2].lower() in {d["title"].lower() for d in fxm.RACK.values()}
            msg = {"cmd": "fx", "target": a[1], ("rack" if rack else "effect"): a[2],
                   "on": onoff(a[3] if len(a) > 3 else None)}
        elif c == "fx-set":
            rack = a[2].lower() in fxm.RACK
            msg = {"cmd": "fx", "target": a[1], ("rack" if rack else "effect"): a[2], "param": a[3],
                   "value": float(a[4])}
        elif c == "mute":
            rest = a[2:]
            state = rest.pop() if rest and rest[-1].lower() in ("on", "off", "toggle") else None
            msg = {"cmd": "channel", "id": a[1], "mute": onoff(state)}
            if rest:
                msg["mix"] = rest[0]
        elif c == "vol":
            msg = dict({"cmd": "channel", "id": a[1]}, **vol(a[2]))
            if len(a) > 3:
                msg["mix"] = a[3]
        elif c == "pan":
            v = "".join(a[2:]).strip().upper()
            if v in ("C", "CENTER", "CENTRE"):
                pan = 0
            elif v[:1] in "LR" and v[1:].isdigit():
                pan = int(v[1:]) * (-1 if v[0] == "L" else 1)
            else:
                pan = int(v)
            msg = {"cmd": "channel", "id": a[1], "pan": pan}
        elif c == "mix-vol":
            msg = dict({"cmd": "mix", "id": a[1]}, **vol(a[2]))
        elif c == "mix-mute":
            msg = {"cmd": "mix", "id": a[1], "mute": onoff(a[2] if len(a) > 2 else None)}
        elif c == "duck":
            msg = {"cmd": "duck", "id": a[1], "on": onoff(a[2] if len(a) > 2 else None)}
        elif c == "profile":
            msg = {"cmd": "profile", "name": " ".join(a[1:])}
        elif c in ("show", "hide", "toggle"):
            msg = {"cmd": "window", "action": c}
        elif c == "json":
            msg = json.loads(a[1])
        else:
            print(CTL_HELP.format(ctl=ext.ctl_hint()), end="")
            return 0 if c in ("help", "-h", "--help") else 2
    except (IndexError, ValueError):
        print(CTL_HELP.format(ctl=ext.ctl_hint()), end="")
        return 2
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "audio-mixer.sock")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(path)
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        print(f"audio mixer is not running ({e})", file=sys.stderr)
        return 1
    reply = json.loads(buf.split(b"\n")[0] or b"{}")
    if c == "state":
        print(json.dumps(reply, indent=2))
    elif reply.get("error"):
        print(reply["error"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ctl":
        sys.exit(ctl_main(sys.argv[2:]))
    app = App()
    if "--hidden" in sys.argv[1:]:          # start in the tray (used by the login autostart entry)
        sys.argv.remove("--hidden")
        app.start_hidden = True
    sys.exit(app.run(sys.argv))
