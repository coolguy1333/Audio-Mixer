"""Tests for game auto-detection against a fake /proc. No GTK or sound server needed:
    python3 -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import amx_games  # noqa: E402


class FakeProc:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="amx-proc-")

    def add(self, pid, comm, ppid, exe=None, env=None, start="100"):
        d = os.path.join(self.root, str(pid))
        os.makedirs(d, exist_ok=True)
        # fields after "(comm)": state ppid pgrp session tty tpgid flags minflt cminflt majflt
        # cmajflt utime stime cutime cstime priority nice threads itrealvalue starttime
        rest = ["S", str(ppid)] + ["0"] * 17 + [start]
        with open(os.path.join(d, "stat"), "w") as fh:
            fh.write(f"{pid} ({comm}) " + " ".join(rest) + "\n")
        with open(os.path.join(d, "environ"), "wb") as fh:
            fh.write(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()))
        link = os.path.join(d, "exe")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(exe or f"/usr/bin/{comm}", link)

    def remove(self, pid):
        shutil.rmtree(os.path.join(self.root, str(pid)))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Detection(unittest.TestCase):
    def setUp(self):
        self.p = FakeProc()
        self.addCleanup(self.p.cleanup)
        self.p.add(1, "systemd", 0)
        self.p.add(10, "plasmashell", 1)
        self.d = amx_games.Detector(self.p.root)

    def test_plain_app_is_not_a_game(self):
        self.p.add(20, "spotify", 10)
        self.assertFalse(self.d.is_game(20, "spotify", "Spotify"))

    def test_steam_env_marks_native_game(self):
        self.p.add(30, "hl2_linux", 10, env={"SteamAppId": "220", "HOME": "/home/x"})
        self.assertTrue(self.d.is_game(30, "hl2_linux", "Half-Life 2"))

    def test_steam_env_zero_is_ignored(self):
        self.p.add(31, "someapp", 10, env={"SteamAppId": "0"})
        self.assertFalse(self.d.is_game(31, "someapp", "Some App"))

    def test_steam_client_is_never_a_game(self):
        self.p.add(40, "steam", 10, env={"SteamAppId": "7"})
        self.p.add(41, "steamwebhelper", 40)
        self.assertFalse(self.d.is_game(40, "steam", "Steam"))
        self.assertFalse(self.d.is_game(41, "steamwebhelper", "Steam"))

    def test_steamvr_is_not_a_game(self):
        self.p.add(45, "vrserver", 10, env={"SteamAppId": "250820"})
        self.p.add(46, "vrcompositor", 10, env={"SteamAppId": "250820"})
        self.assertFalse(self.d.is_game(45, "vrserver", "vrserver"))
        self.assertFalse(self.d.is_game(46, "vrcompositor", "SteamVR"))

    def test_child_of_reaper_is_a_game(self):
        self.p.add(50, "steam", 10)
        self.p.add(51, "reaper", 50)
        self.p.add(52, "factorio", 51)
        self.assertTrue(self.d.is_game(52, "factorio", "Factorio"))

    def test_wine_exe_is_a_game(self):
        self.p.add(60, "Game.exe", 10, exe="/opt/proton/files/bin/wine64-preloader")
        self.assertTrue(self.d.is_game(60, "wine64-preloader", "Game.exe"))

    def test_child_of_lutris(self):
        self.p.add(70, "lutris", 10)
        self.p.add(71, "bash", 70)
        self.p.add(72, "openmw", 71)
        self.assertTrue(self.d.is_game(72, "openmw", "OpenMW"))
        self.assertFalse(self.d.is_game(70, "lutris", "Lutris"))

    def test_desktop_category(self):
        self.p.add(80, "supertuxkart", 10)
        self.assertTrue(self.d.is_game(80, "supertuxkart", "SuperTuxKart",
                                       desktop_games={"supertuxkart"}))

    def test_missing_process(self):
        self.assertFalse(self.d.is_game(999, "x", "X"))
        self.assertFalse(self.d.is_game("", "x", "X"))

    def test_cache_and_pid_reuse(self):
        self.p.add(90, "Game.exe", 10, exe="/usr/bin/wine64-preloader", start="500")
        self.assertTrue(self.d.is_game(90, "", "Game.exe"))
        # same pid, new process (different start time): verdict recomputed
        self.p.add(90, "firefox", 10, exe="/usr/lib/firefox/firefox", start="900")
        self.assertFalse(self.d.is_game(90, "firefox", "Firefox"))
        self.d.forget_others([])
        self.assertEqual(self.d._cache, {})

    def test_parent_loop_terminates(self):
        self.p.add(100, "a", 101)
        self.p.add(101, "b", 100)
        self.assertFalse(self.d.is_game(100, "a", "A"))

    def test_comm_with_parens_and_spaces(self):
        self.p.add(110, "weird (name) x", 10, env={"SteamGameId": "12"})
        self.assertTrue(self.d.is_game(110, "", "Weird"))


if __name__ == "__main__":
    unittest.main()
