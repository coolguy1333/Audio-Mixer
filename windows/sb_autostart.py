"""Start with Windows: a value under HKCU\\...\\Run that launches the soundboard in the tray at login."""
import os
import sys

try:
    import winreg
except ImportError:              # not Windows
    winreg = None

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "AudioMixerSoundboard"


def supported():
    return winreg is not None


def command():
    """What Windows runs at login: the .exe, or pythonw + main.py when running from source."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    main = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{pyw if os.path.exists(pyw) else exe}" "{main}" --tray'


def enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            val, _t = winreg.QueryValueEx(k, NAME)
            return bool(val)
    except OSError:
        return False


def set_enabled(on):
    if winreg is None:
        raise OSError("Start with Windows only works on Windows")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, NAME)
            except FileNotFoundError:
                pass
