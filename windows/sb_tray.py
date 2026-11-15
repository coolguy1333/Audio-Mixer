"""System tray icon (optional; needs pystray + Pillow). While it runs, closing the window only hides it, so
hotkeys, Stream Deck links and the phone companion keep working."""
import os
import sys
import threading

try:
    import pystray
    from PIL import Image
except Exception:                 # not installed, or no tray on this desktop
    pystray = None


def icon_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "icon.ico")


class Tray:
    def __init__(self, title, on_show, on_stop_all, on_quit):
        self.icon = None
        self.error = None
        if pystray is None:
            self.error = "pystray isn't installed"
            return
        try:
            image = Image.open(icon_path())
        except OSError:
            image = Image.new("RGBA", (64, 64), (53, 174, 245, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Show soundboard", lambda _i, _m: on_show(), default=True),
            pystray.MenuItem("Stop all sounds", lambda _i, _m: on_stop_all()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda _i, _m: on_quit()))
        try:
            self.icon = pystray.Icon("AudioMixerSoundboard", image, title, menu)
            threading.Thread(target=self.icon.run, daemon=True).start()     # fine off the main thread on Windows
        except Exception as e:
            self.icon = None
            self.error = str(e)

    @property
    def ok(self):
        return self.icon is not None

    def notify(self, text, title="Audio Mixer Soundboard"):
        if self.icon is not None:
            try:
                self.icon.notify(text, title)
            except Exception:
                pass

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
