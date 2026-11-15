"""Audio Mixer Soundboard for Windows (also runs on Linux/macOS for development).

    AudioMixerSoundboard.exe            open the app window
    AudioMixerSoundboard.exe --browser  use the default web browser instead of the app window
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
import sb_hotkeys  # noqa: E402
import sb_library  # noqa: E402
import sb_server  # noqa: E402

TITLE = "Audio Mixer Soundboard"


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
    print("hotkeys:", st["hotkeys"], "| ffmpeg:", st["ffmpeg"], "| yt-dlp:", st["ytdlp"])
    print("SELFTEST OK")


def main(argv):
    lib = sb_library.Library()
    _log_to_file(lib.home)
    snd.CACHE = os.path.join(lib.cache, "web")
    port = int(lib.cfg.get("port") or 8788)
    test = "--selftest" in argv
    if not test and _running(port):                     # already open: show it again instead of a 2nd engine
        _open_ui(f"http://127.0.0.1:{port}/", "--browser" in argv, None)
        return 0
    engine = sb_audio.Engine(lib)
    app = sb_server.App(lib, engine)
    app.hotkeys = sb_hotkeys.Hotkeys(lib, engine, lambda fn: threading.Thread(target=fn, daemon=True).start())
    srv, port = sb_server.serve(app, 0 if test else port)
    base = f"http://127.0.0.1:{port}"
    done = threading.Event()

    def quit_app():
        done.set()
        if app.window is not None:
            try:
                app.window.destroy()
            except Exception:
                pass
    app.window = None
    app.on_quit = quit_app
    try:
        if test:
            selftest(base, lib.cfg["token"])
            return 0
        _open_ui(base + "/", "--browser" in argv, app, done)
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        app.hotkeys.stop()
        engine.close()
        srv.shutdown()
        srv.server_close()


def _open_ui(url, browser, app, done=None):
    if not browser:
        try:
            import webview
        except Exception:
            webview = None
        if webview is not None:
            class Api:
                def save_export(self):
                    r = app.window.create_file_dialog(webview.SAVE_DIALOG, save_filename="soundboard.zip",
                                                      file_types=("Soundboard (*.zip)",))
                    path = r[0] if isinstance(r, (list, tuple)) else r
                    if not path:
                        return {"ok": False}
                    app.lib.export_zip(path)
                    return {"ok": True, "path": path}

                def open_folder(self):
                    if sys.platform == "win32":
                        os.startfile(app.lib.home)   # noqa: S606
                    return True
            try:
                w = webview.create_window(TITLE, url, width=1180, height=780, min_size=(560, 420),
                                          background_color="#1d1d1f", js_api=Api() if app else None)
                if app:
                    app.window = w
                # Edge WebView2 only: older engines can't run the page, the browser is better then
                webview.start(gui="edgechromium" if sys.platform == "win32" else None)
                return
            except Exception:
                traceback.print_exc()           # no WebView2 runtime, …: fall back to the browser
    import webbrowser
    webbrowser.open(url)
    if done is not None:
        try:
            while not done.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
