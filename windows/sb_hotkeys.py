"""Global hotkeys (work while another app, e.g. a game, has focus). Uses pynput; optional.

Hotkeys are stored in pynput's format, e.g. "<ctrl>+<alt>+1" or "<f13>". The UI records them from key presses
and converts them with the same rules as to_pynput() below.
"""
try:
    from pynput import keyboard
except Exception:                # not installed, or no display on Linux
    keyboard = None

MODS = {"Control": "<ctrl>", "Alt": "<alt>", "Shift": "<shift>", "Meta": "<cmd>"}
NAMED = {"Enter": "<enter>", "Escape": "<esc>", "Tab": "<tab>", "Backspace": "<backspace>", "Delete": "<delete>",
         "Insert": "<insert>", "Home": "<home>", "End": "<end>", "PageUp": "<page_up>", "PageDown": "<page_down>",
         "ArrowUp": "<up>", "ArrowDown": "<down>", "ArrowLeft": "<left>", "ArrowRight": "<right>",
         " ": "<space>", "Pause": "<pause>", "ScrollLock": "<scroll_lock>", "PrintScreen": "<print_screen>",
         "NumLock": "<num_lock>", "CapsLock": "<caps_lock>", "MediaPlayPause": "<media_play_pause>",
         "MediaTrackNext": "<media_next>", "MediaTrackPrevious": "<media_previous>"}


def to_pynput(mods, key):
    """(["Control", "Alt"], "1") -> "<ctrl>+<alt>+1". Returns None for a bare modifier."""
    if key in MODS or not key:
        return None
    if len(key) > 1 and key[0] == "F" and key[1:].isdigit():
        k = f"<f{key[1:]}>"
    elif key in NAMED:
        k = NAMED[key]
    elif len(key) == 1:
        k = key.lower()
    else:
        return None
    order = ["Control", "Alt", "Shift", "Meta"]
    return "+".join([MODS[m] for m in order if m in mods] + [k])


def valid(combo):
    if keyboard is None or not combo:
        return False
    try:
        keyboard.HotKey.parse(combo)
        return True
    except Exception:
        return False


class Hotkeys:
    def __init__(self, lib, engine, call_soon):
        """call_soon(fn) runs fn off the keyboard hook's thread (the hook must return quickly)."""
        self.lib, self.engine, self.call_soon = lib, engine, call_soon
        self.listener = None
        self.status = "unavailable (install pynput)" if keyboard is None else "off"
        self.reload()

    def reload(self):
        if keyboard is None:
            return
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        mapping, bad = {}, []
        for s in self.lib.sounds:
            hk = s.get("hotkey")
            if hk:
                if valid(hk) and hk not in mapping:
                    mapping[hk] = (lambda sid=s["id"]: self.call_soon(lambda: self.engine.toggle(sid)))
                else:
                    bad.append(s["name"])
        sk = self.lib.cfg.get("stop_key")
        if sk and valid(sk) and sk not in mapping:
            mapping[sk] = lambda: self.call_soon(self.engine.stop_all)
        if not mapping:
            self.status = "no hotkeys set"
            return
        try:
            self.listener = keyboard.GlobalHotKeys(mapping)
            self.listener.daemon = True
            self.listener.start()
            self.status = f"{len(mapping)} active" + (f"; ignored for: {', '.join(bad)}" if bad else "")
        except Exception as e:
            self.listener = None
            self.status = f"unavailable: {e}"

    def stop(self):
        if self.listener is not None:
            self.listener.stop()
