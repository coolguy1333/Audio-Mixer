"""Audio Mixer Soundboard for Windows (also runs on Linux/macOS for development).

    AudioMixerSoundboard.exe            open the app window
    AudioMixerSoundboard.exe --browser  use the default web browser instead of the app window
    AudioMixerSoundboard.exe --tray     start hidden in the system tray (used by "Start with Windows")
    AudioMixerSoundboard.exe --selftest check that everything starts, then exit (used by the build)
"""
import io
import json
import math
import os
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.dirname(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import amx_sounds as snd  # noqa: E402
import sb_audio  # noqa: E402
import sb_autostart  # noqa: E402
import sb_hotkeys  # noqa: E402
import sb_library  # noqa: E402
import sb_server  # noqa: E402
import sb_tray  # noqa: E402

TITLE = "Audio Mixer Soundboard"
POPOUT_TITLE = "Soundboard"


def _log_to_file(lib_home):
    """A windowed .exe has no console: keep errors in a log file next to the settings."""
    if sys.stderr is None or getattr(sys, "frozen", False):
        try:
            f = open(os.path.join(lib_home, "log.txt"), "a", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout = f
            print(f"--- start {time.ctime()}")
        except OSError:
            pass


def _running(port, token=None):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "amx-soundboard"
    except Exception:
        return False


def _api(base, token, path, data=None, raw=None, headers=None):
    h = {"X-Token": token, **(headers or {})}
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=h, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        ct = r.headers.get_content_type()
        b = r.read()
    return json.loads(b) if ct == "application/json" else b


def _beep_wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 44100)))
                               for i in range(44100 // 2)))
    return buf.getvalue()


def selftest(base, token):
    assert _api(base, token, "/api/ping")["app"] == "amx-soundboard"
    st = _api(base, token, "/api/state")
    n0 = len(st["sounds"])
    r = _api(base, token, "/api/upload", raw=_beep_wav(), headers={"X-Filename": "selftest-beep.wav"})
    assert r["ok"], r
    st = _api(base, token, "/api/state")
    assert len(st["sounds"]) == n0 + 1 or r["skipped"], st
    sid = r["sound"] or st["sounds"][-1]["id"]
    assert abs(next(s for s in st["sounds"] if s["id"] == sid)["dur"] - 0.5) < 0.05
    assert len(_api(base, token, f"/api/peaks/{sid}")) > 10
    try:
        _api(base, token, f"/api/play/{sid}")
        print("play: ok")
    except urllib.error.HTTPError as e:              # CI machines have no sound card
        print("play: no output device:", json.load(e).get("error"))
    _api(base, token, "/api/stopall", data={})
    assert _api(base, token, "/api/export")[:2] == b"PK"
    assert _api(base, token, f"/api/delete/{sid}", data={})["ok"]
    try:
        urllib.request.urlopen(base + "/api/state", timeout=5)
        raise AssertionError("API answered without a token")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    print("devices:", json.dumps(sb_audio.devices()))
    print("hotkeys:", st["hotkeys"], "| ffmpeg:", st["ffmpeg"], "| yt-dlp:", st["ytdlp"],
          "| tray:", "available" if sb_tray.pystray is not None else "missing",
          "| autostart:", "supported" if sb_autostart.supported() else "n/a")
    print("SELFTEST OK")


def main(argv):
    lib = sb_library.Library()
    _log_to_file(lib.home)
    snd.CACHE = os.path.join(lib.cache, "web")
    port = int(lib.cfg.get("port") or 8788)
    test = "--selftest" in argv
    browser = "--browser" in argv
    if not test and _running(port):
        # already running (maybe hidden in the tray): bring that window back instead of starting a 2nd engine
        try:
            _api(f"http://127.0.0.1:{port}", lib.cfg["token"], "/api/show", data={})
        except Exception:
            _open_ui_plain(f"http://127.0.0.1:{port}/")
        return 0
    if sb_autostart.enabled():
        try:
            sb_autostart.set_enabled(True)           # keep the login entry pointing at this copy of the .exe
        except OSError:
            pass
    engine = sb_audio.Engine(lib)
    app = sb_server.App(lib, engine)
    app.hotkeys = sb_hotkeys.Hotkeys(lib, engine, lambda fn: threading.Thread(target=fn, daemon=True).start())
    srv, port = sb_server.serve(app, 0 if test else port)
    base = f"http://127.0.0.1:{port}"
    try:
        if test:
            selftest(base, lib.cfg["token"])
            return 0
        Shell(app, base + "/", browser, start_hidden="--tray" in argv).run()
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        app.hotkeys.stop()
        engine.close()
        srv.shutdown()
        srv.server_close()


def _open_ui_plain(url):
    import webbrowser
    webbrowser.open(url)


class Shell:
    """The app window (pywebview) or the browser, plus the tray icon. With the tray on, closing the window
    hides it and the soundboard keeps running (hotkeys, Stream Deck links); Quit is in the tray menu."""

    def __init__(self, app, url, browser, start_hidden=False):
        self.app, self.url, self.browser = app, url, browser
        self.start_hidden = start_hidden
        self.window = None
        self.quitting = False
        self.done = threading.Event()
        self.told = False
        app.on_quit = self.quit
        app.on_show = self.show
        app.window = None
        self.webview = None
        self.popout = None
        app.on_popout = self.toggle_popout
        self.tray = sb_tray.Tray(TITLE, self.show, app.engine.stop_all, self.quit, on_popout=self.toggle_popout)
        app.tray_ok = self.tray.ok

    def run(self):
        try:
            if not self.browser and self._run_window():
                return
            if not (self.start_hidden and self.tray.ok):
                _open_ui_plain(self.url)
            try:
                while not self.done.wait(0.5):
                    pass
            except KeyboardInterrupt:
                pass
        finally:
            self.tray.stop()

    def _api(self):
        """Python functions the page can call (window.pywebview.api.*), shared by both windows."""
        shell, app = self, self.app
        webview = self.webview

        class Api:
            def save_export(self_):
                r = shell.window.create_file_dialog(webview.SAVE_DIALOG, save_filename="soundboard.zip",
                                                    file_types=("Soundboard (*.zip)",))
                path = r[0] if isinstance(r, (list, tuple)) else r
                if not path:
                    return {"ok": False}
                app.lib.export_zip(path)
                return {"ok": True, "path": path}

            def open_folder(self_):
                if sys.platform == "win32":
                    os.startfile(app.lib.home)   # noqa: S606
                return True

            def popout(self_):
                shell.open_popout()
                return True

            def popout_on_top(self_, on):
                shell.set_on_top(bool(on))
                return bool(on)

            def show_main(self_):
                shell.show()
                return True
        return Api()

    def _run_window(self):
        try:
            import webview
        except Exception:
            return False
        self.webview = webview
        app = self.app
        try:
            self.window = webview.create_window(TITLE, self.url, width=1180, height=780, min_size=(200, 150),
                                                background_color="#1d1d1f", js_api=self._api(),
                                                hidden=self.start_hidden and self.tray.ok)
            app.window = self.window
            self.window.events.closing += self._closing
            if self._pcfg().get("open"):
                self.open_popout()                   # it was open last time
            # Edge WebView2 only: older engines can't run the page; the browser is better then
            webview.start(gui="edgechromium" if sys.platform == "win32" else None)
            return True
        except Exception:
            traceback.print_exc()                    # no WebView2 runtime, …: fall back to the browser
            self.window = app.window = None
            return False

    # ---- pop-out: just the pads, in a small window that can stay on top (like the Linux mixer's pop-out)
    def _pcfg(self):
        pc = self.app.lib.cfg.setdefault("popout", {})
        if pc.get("v") != 2:            # sizes saved before 2 were inflated by display scaling: start over
            for k in ("width", "height", "x", "y"):
                pc.pop(k, None)
            pc["v"] = 2
        return pc

    def open_popout(self):
        if self.popout is not None:
            try:
                self.popout.show()
                self.popout.restore()
            except Exception:
                pass
            return
        if self.webview is None:                     # browser mode: the page opens a popup window itself
            _open_ui_plain(self.url + "?mini=1")
            return
        pc = self._pcfg()
        kw = {}
        if isinstance(pc.get("x"), int) and isinstance(pc.get("y"), int) and pc["x"] > -10000 and pc["y"] > -10000:
            kw = {"x": pc["x"], "y": pc["y"]}
        w = self.webview.create_window(POPOUT_TITLE, self.url + "?mini=1", width=pc.get("width", 300),
                                       height=pc.get("height", 200), min_size=(100, 60), on_top=bool(pc.get("on_top")),
                                       background_color="#1d1d1f", js_api=self._api(), **kw)
        self.popout = w

        def scale():
            """resized/moved report physical pixels, create_window takes unscaled ones: at 150% display
            scaling, saving the raw size made the window 1.5x bigger every time it opened."""
            n = getattr(w, "native", None)
            for f in (lambda: n.DeviceDpi / 96, lambda: n.scale_factor):
                try:
                    v = float(f())
                    if v > 0:
                        return v
                except Exception:
                    pass
            return 1.0

        def resized(width, height):
            k = scale()
            width, height = round(width / k), round(height / k)
            if width >= 100 and height >= 60:
                pc["width"], pc["height"] = width, height

        def moved(x, y):
            k = scale()
            pc["x"], pc["y"] = round(x / k), round(y / k)

        def closed():
            if self.popout is w:
                self.popout = None
            if not self.quitting:                    # (quitting: keep "open", so it comes back next time)
                pc["open"] = False
            self.app.lib.save()
        w.events.resized += resized
        w.events.moved += moved
        w.events.closed += closed
        pc["open"] = True
        self.app.lib.save()

    def close_popout(self):
        if self.popout is not None:
            try:
                self.popout.destroy()
            except Exception:
                pass

    def toggle_popout(self):
        if self.popout is not None:
            self.close_popout()
        else:
            self.open_popout()

    def set_on_top(self, on):
        self._pcfg()["on_top"] = on
        self.app.lib.save()
        if self.popout is not None:
            try:
                self.popout.on_top = on
            except Exception:
                traceback.print_exc()

    def _closing(self):
        """Closing the window: hide it to the tray instead, unless quitting or the tray is off/unavailable."""
        if self.quitting or not self.tray.ok or not self.app.lib.cfg.get("close_to_tray", True):
            self.quitting = True
            return True
        threading.Timer(0.05, self.window.hide).start()   # not from inside the close handler
        if not self.told:
            self.told = True
            self.tray.notify("Still running in the tray, so hotkeys keep working. Right-click the icon to quit.")
        return False

    def show(self):
        if self.window is not None:
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass
        else:
            _open_ui_plain(self.url)

    def quit(self):
        self.quitting = True
        self.done.set()
        self.close_popout()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
        self.tray.stop()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
