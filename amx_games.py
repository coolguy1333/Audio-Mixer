"""Game auto-detection: decides from /proc whether a sound stream's process is a game.

Signals, most reliable first:
  1. Steam/Proton environment variables (SteamAppId, SteamGameId, STEAM_COMPAT_APP_ID)
     - set for every game Steam launches, native, Proton or inside pressure-vessel.
  2. Ancestry: the process (or a parent) is Wine/Proton, or a parent is a game launcher
     (Steam's reaper, Lutris, Heroic, Bottles, umu, gamescope).
  3. The app's .desktop launcher lists Categories=Game (passed in by the caller).

No GTK and no sound server needed, so it can be unit-tested against a fake /proc.
"""
import os

STEAM_ENV = (b"SteamAppId", b"SteamGameId", b"STEAM_COMPAT_APP_ID")

# The process itself being one of these means it runs under Wine/Proton.
WINE_NAMES = {"wine", "wine64", "wine-preloader", "wine64-preloader", "wineserver", "proton"}
# A parent being one of these means it was started by a game launcher.
LAUNCHER_NAMES = WINE_NAMES | {"reaper", "pressure-vessel-wrap", "pv-bwrap", "lutris", "heroic",
                               "legendary", "gogdl", "bottles", "umu-run", "gamescope"}
# Never games, even when their parent is a launcher (Steam's own UI, launcher front-ends).
NOT_GAMES = {"steam", "steamwebhelper", "steam-runtime-launcher-service", "lutris", "heroic",
             "bottles", "gamescope", "mangohud", "discord", "obs"}

MAX_DEPTH = 32


class Detector:
    def __init__(self, proc="/proc"):
        self.proc = proc
        self._cache = {}   # pid -> (starttime, verdict); starttime guards against pid reuse

    # ---- /proc readers (each returns None when the process is gone or unreadable)
    def _stat(self, pid):
        """(comm, ppid, starttime) from /proc/<pid>/stat."""
        try:
            with open(f"{self.proc}/{pid}/stat", "rb") as fh:
                data = fh.read().decode(errors="replace")
        except OSError:
            return None
        lp, rp = data.find("("), data.rfind(")")
        if lp < 0 or rp < lp:
            return None
        rest = data[rp + 2:].split()
        try:
            return data[lp + 1:rp], int(rest[1]), rest[19]
        except (IndexError, ValueError):
            return None

    def _exe(self, pid):
        try:
            return os.path.basename(os.readlink(f"{self.proc}/{pid}/exe"))
        except OSError:
            return ""

    def _environ(self, pid):
        try:
            with open(f"{self.proc}/{pid}/environ", "rb") as fh:
                return fh.read().split(b"\0")
        except OSError:
            return []

    def _names(self, pid, comm):
        return {n.lower() for n in (comm, self._exe(pid)) if n}

    # ---- detection
    def _steam_game(self, pid):
        for kv in self._environ(pid):
            k, _, v = kv.partition(b"=")
            if k in STEAM_ENV and v.strip() not in (b"", b"0"):
                return True
        return False

    def _detect(self, pid, st, binary, name):
        comm = st[0]
        own = self._names(pid, comm) | {n for n in (binary, name.lower()) if n}
        if own & NOT_GAMES:
            return False
        if own & WINE_NAMES or any(n.endswith(".exe") for n in own):
            return True
        if self._steam_game(pid):
            return True
        ppid, seen = st[1], {pid}
        for _ in range(MAX_DEPTH):
            if ppid <= 1 or ppid in seen:
                break
            seen.add(ppid)
            pst = self._stat(ppid)
            if not pst:
                break
            if self._names(ppid, pst[0]) & LAUNCHER_NAMES:
                return True
            ppid = pst[1]
        return False

    def is_game(self, pid, binary="", name="", desktop_games=()):
        """True when the process looks like a game. `desktop_games` holds lowercase binaries and
        display names of apps whose launcher says Categories=Game."""
        keys = {k for k in (binary, name.lower()) if k}
        if keys & NOT_GAMES:
            return False
        if keys & set(desktop_games):
            return True
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        st = self._stat(pid)
        if not st:
            return False
        hit = self._cache.get(pid)
        if hit and hit[0] == st[2]:
            return hit[1]
        verdict = self._detect(pid, st, binary, name)
        self._cache[pid] = (st[2], verdict)
        return verdict

    def forget_others(self, live_pids):
        """Drop cached verdicts for processes that no longer play sound."""
        live = set()
        for p in live_pids:
            try:
                live.add(int(p))
            except (TypeError, ValueError):
                pass
        for p in list(self._cache):
            if p not in live:
                del self._cache[p]
