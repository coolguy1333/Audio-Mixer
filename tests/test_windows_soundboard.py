"""Tests for the Windows soundboard (windows/). They run on any OS without sound devices or network:
    python -m unittest discover -s tests
Skipped when numpy isn't installed (pip install -r windows/requirements.txt)."""
import io
import json
import math
import os
import struct
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
import urllib.request
import wave
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "windows"))
sys.path.insert(0, ROOT)
os.environ["AMXSB_NO_AUDIO"] = "1"
try:
    import numpy  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


def wav_bytes(freq=440, secs=0.5, rate=44100, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) * channels
                          for i in range(int(rate * secs)))
        w.writeframes(frames)
    return buf.getvalue()


@unittest.skipUnless(HAVE, "numpy not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["AMXSB_HOME"] = self.tmp.name
        import sb_library
        self.L = sb_library
        self.lib = sb_library.Library()

    def file(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p


class Library(Base):
    def test_add_decode_and_duplicates(self):
        s = self.lib.add_file(self.file("a.wav", wav_bytes(440)))
        self.assertAlmostEqual(self.lib.duration(s), 0.5, places=2)
        self.assertEqual(self.lib.pcm(s).shape[1], 2)
        self.assertIsNone(self.lib.add_file(self.file("copy.wav", wav_bytes(440))))
        self.assertEqual(self.lib.skipped, ["copy"])
        self.assertIsNotNone(self.lib.add_file(self.file("b.wav", wav_bytes(660))))
        self.assertEqual(len(self.lib.sounds), 2)

    def test_not_audio_is_refused(self):
        with self.assertRaises(self.L.DecodeError):
            self.lib.add_file(self.file("x.mp3", b"<html>nope</html>" * 50))
        self.assertEqual(self.lib.sounds, [])

    def test_trim_volume_hotkey_and_order(self):
        a = self.lib.add_file(self.file("a.wav", wav_bytes(440)))
        b = self.lib.add_file(self.file("b.wav", wav_bytes(660)))
        self.lib.update(a["id"], start=0.1, end=0.3, vol=250, hotkey="<ctrl>+1", name="  Boom ")
        s = self.lib.sound(a["id"])
        self.assertEqual((s["start"], s["end"], s["vol"], s["hotkey"], s["name"]), (0.1, 0.3, 200, "<ctrl>+1", "Boom"))
        _d, st, en = self.lib.segment(s)
        self.assertEqual((st, en), (4800, 14400))
        self.lib.update(a["id"], end=99)                  # past the end = no end trim
        self.assertIsNone(self.lib.sound(a["id"])["end"])
        self.lib.reorder([b["id"], a["id"]])
        self.assertEqual([x["id"] for x in self.lib.sounds], [b["id"], a["id"]])
        self.assertEqual(self.lib.sound("boom")["id"], a["id"])          # by name too

    def test_settings_persist(self):
        self.lib.add_file(self.file("a.wav", wav_bytes()))
        self.lib.cfg["volume"] = 70
        self.lib.save()
        again = self.L.Library()
        self.assertEqual(again.cfg["volume"], 70)
        self.assertEqual(len(again.sounds), 1)
        self.assertEqual(again.cfg["token"], self.lib.cfg["token"])

    def test_key_is_saved_on_first_run(self):
        again = self.L.Library()                      # nothing was changed in between
        self.assertEqual(again.cfg["token"], self.lib.cfg["token"])

    def test_broken_config_is_kept(self):
        with open(self.lib.conf_path, "w") as f:
            f.write("{broken")
        again = self.L.Library()
        self.assertTrue(again.problem)
        self.assertTrue(os.path.exists(self.lib.conf_path + ".broken"))

    def test_export_import_and_linux_export(self):
        a = self.lib.add_file(self.file("a.wav", wav_bytes(440)))
        self.lib.update(a["id"], start=0.2)
        z = os.path.join(self.tmp.name, "b.zip")
        self.lib.export_zip(z)
        self.assertEqual(self.lib.import_zip(z), 0)                        # all already there
        self.lib.remove(a["id"])
        self.assertEqual(self.lib.import_zip(z), 1)
        self.assertEqual(self.lib.sounds[0]["start"], 0.2)
        lz = os.path.join(self.tmp.name, "linux.zip")                      # the Linux mixer's format
        with zipfile.ZipFile(lz, "w") as zz:
            zz.writestr("sounds/1234-horn.wav", wav_bytes(880))
            zz.writestr("../evil.wav", b"x")
            zz.writestr("soundboard.json", json.dumps({"volume": 100, "sounds": [
                {"id": "1234", "name": "Horn", "file": "sounds/1234-horn.wav", "start": 0.0, "end": None, "vol": 80},
                {"id": "6666", "name": "Evil", "file": "../evil.wav"}]}))
        self.assertEqual(self.lib.import_zip(lz), 1)
        self.assertEqual(self.lib.sound("Horn")["vol"], 80)


class Hotkeys(unittest.TestCase):
    def test_format(self):
        sys.path.insert(0, os.path.join(ROOT, "windows"))
        import sb_hotkeys as hk
        self.assertEqual(hk.to_pynput(["Alt", "Control"], "1"), "<ctrl>+<alt>+1")
        self.assertEqual(hk.to_pynput([], "F13"), "<f13>")
        self.assertEqual(hk.to_pynput(["Shift"], "A"), "<shift>+a")
        self.assertIsNone(hk.to_pynput(["Control"], "Control"))


class FakeWasapi:
    def __init__(self, auto_convert=False):
        self.auto_convert = auto_convert


class FakeSD:
    """PortAudio as Windows shows it: MME (index 0, the PortAudio default) and WASAPI (index 1)."""
    WasapiSettings = FakeWasapi

    def __init__(self, wasapi_works=True):
        self.wasapi_works = wasapi_works
        self.opened = []
        self.default = type("D", (), {"device": [3, 0]})()

    def query_hostapis(self, i=None):
        apis = [{"name": "MME", "default_output_device": 0, "default_input_device": 3},
                {"name": "Windows WASAPI", "default_output_device": 4, "default_input_device": 6}]
        return apis if i is None else apis[i]

    def query_devices(self, i=None):
        d = [{"name": "Microsoft Sound Mapper - Output", "hostapi": 0, "max_output_channels": 2, "max_input_channels": 0},
             {"name": "CABLE Input (VB-Audio Virtual C", "hostapi": 0, "max_output_channels": 8, "max_input_channels": 0},
             {"name": "Speakers (Realtek(R) Audio)", "hostapi": 0, "max_output_channels": 2, "max_input_channels": 0},
             {"name": "Microphone (USB Mic)", "hostapi": 0, "max_output_channels": 0, "max_input_channels": 1},
             {"name": "Speakers (Realtek(R) Audio)", "hostapi": 1, "max_output_channels": 2, "max_input_channels": 0},
             {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 1, "max_output_channels": 2, "max_input_channels": 0},
             {"name": "Microphone (USB Mic)", "hostapi": 1, "max_output_channels": 0, "max_input_channels": 1}]
        return d if i is None else d[i]

    def _stream(self, device=None, extra_settings=None, channels=2, **kw):
        host = self.query_devices(device if device is not None else 0)["hostapi"]
        if extra_settings is not None and host != 1:          # what real PortAudio does (-9984)
            raise RuntimeError("Incompatible host API specific stream info [PaErrorCode -9984]")
        if host == 1 and not self.wasapi_works:
            raise RuntimeError("Invalid sample rate")
        self.opened.append((device, type(extra_settings).__name__, channels))
        return type("S", (), {"start": lambda s: None, "stop": lambda s: None, "close": lambda s: None})()

    OutputStream = InputStream = _stream


@unittest.skipUnless(HAVE, "numpy not installed")
class DeviceChoice(unittest.TestCase):
    """The PaErrorCode -9984 bug: WASAPI settings must only go to WASAPI devices."""

    def setUp(self):
        import sb_audio
        self.A = sb_audio
        self.p1 = unittest.mock.patch.object(sb_audio, "sys", type("S", (), {"platform": "win32"})())
        self.p1.start()
        self.addCleanup(self.p1.stop)

    def use(self, fake):
        p = unittest.mock.patch.object(self.A, "sd", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_default_output_uses_wasapi_default(self):
        sd = self.use(FakeSD())
        self.A.Output(None)
        self.assertEqual(sd.opened, [(4, "FakeWasapi", 2)])

    def test_named_and_default_mic(self):
        sd = self.use(FakeSD())
        self.A.Output("CABLE Input (VB-Audio Virtual Cable)")
        self.A.MicPassthrough("default")
        self.A.MicPassthrough("Microphone (USB Mic)")
        self.assertEqual(sd.opened, [(5, "FakeWasapi", 2), (6, "FakeWasapi", 1), (6, "FakeWasapi", 1)])

    def test_falls_back_to_mme_without_wasapi_settings(self):
        sd = self.use(FakeSD(wasapi_works=False))
        self.A.Output(None)
        self.A.Output("CABLE Input (VB-Audio Virtual Cable)")          # MME cuts the name to 31 characters
        self.assertEqual(sd.opened, [(None, "NoneType", 2), (1, "NoneType", 2)])

    def test_unknown_device(self):
        self.use(FakeSD())
        with self.assertRaisesRegex(LookupError, "isn't connected"):
            self.A.Output("Gone (USB)")

    def test_engine_has_no_error_with_defaults(self):
        self.use(FakeSD())
        os.environ["AMXSB_HOME"] = tempfile.mkdtemp()
        import sb_library
        lib = sb_library.Library()
        lib.cfg.update(monitor="default", mic="default", output=None)
        eng = self.A.Engine(lib)
        self.assertEqual(eng.errors, {})


class TrayAndAutostart(unittest.TestCase):
    def test_modules_degrade_gracefully(self):
        import sb_autostart
        import sb_tray
        self.assertTrue(sb_tray.icon_path().endswith("icon.ico"))
        self.assertTrue(os.path.exists(sb_tray.icon_path()))
        self.assertIn("--tray", sb_autostart.command())
        if not sb_autostart.supported():
            self.assertFalse(sb_autostart.enabled())
            with self.assertRaises(OSError):
                sb_autostart.set_enabled(True)


@unittest.skipUnless(HAVE, "numpy not installed")
class Server(Base):
    def setUp(self):
        super().setUp()
        import sb_audio
        import sb_server
        self.engine = sb_audio.Engine(self.lib)
        self.app = sb_server.App(self.lib, self.engine)
        self.quit = []
        self.app.on_quit = lambda: self.quit.append(1)
        self.srv, port = sb_server.serve(self.app, 0)
        self.base = f"http://127.0.0.1:{port}"
        self.tok = self.lib.cfg["token"]
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, data=None, raw=None, headers=None, token=True):
        h = dict(headers or {})
        if token:
            h["X-Token"] = self.tok
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        req = urllib.request.Request(self.base + path, data=body, headers=h)
        with self.opener.open(req, timeout=10) as r:
            b = r.read()
            return json.loads(b) if r.headers.get_content_type() == "application/json" else b

    def code(self, *a, **k):
        try:
            self.call(*a, **k)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def test_token_and_host_checks(self):
        self.assertEqual(self.code("/api/state", token=False), 401)
        self.assertEqual(self.code("/api/state", headers={"Host": "evil.example:80"}), 403)
        self.assertEqual(self.code(f"/api/state?t={self.tok}", token=False), 200)
        self.assertEqual(self.code("/", token=False), 200)                 # localhost gets the page
        page = self.call("/")
        self.assertIn(self.tok.encode(), page)
        self.assertEqual(self.code("/api/ping", token=False), 200)

    def test_upload_play_and_delete(self):
        r = self.call("/api/upload", raw=wav_bytes(), headers={"X-Filename": "My%20Beep.wav"})
        self.assertTrue(r["ok"])
        sid = r["sound"]
        st = self.call("/api/state")
        self.assertEqual(st["sounds"][0]["name"], "My Beep")
        r2 = self.call("/api/upload", raw=wav_bytes(), headers={"X-Filename": "again.wav"})
        self.assertIsNone(r2["sound"])
        self.assertEqual(r2["skipped"], ["again"])
        self.assertEqual(self.code(f"/api/play/{sid}"), 409)               # no audio device in tests
        self.assertTrue(self.call(f"/api/sound/{sid}", {"name": "X", "hotkey": "<f9>"})["ok"])
        self.assertEqual(len(self.call(f"/api/peaks/{sid}")), 1000)
        self.assertEqual(self.call("/api/export")[:2], b"PK")
        self.assertTrue(self.call(f"/api/delete/{sid}", {})["ok"])
        self.assertEqual(self.call("/api/state")["sounds"], [])
        bad = self.call("/api/upload", raw=b"not audio" * 100, headers={"X-Filename": "x.mp3"})
        self.assertFalse(bad["ok"])

    def test_online_is_opt_in(self):
        self.assertEqual(self.code("/api/web/search", {"source": 0, "q": "x"}), 403)
        self.call("/api/settings", {"web_sounds": True})
        r = self.call("/api/web/search", {"source": 4, "q": "not a link"})
        self.assertFalse(r["ok"])
        self.assertIn("https://", r["error"])

    def test_show_and_tray_settings(self):
        shown = []
        self.app.on_show = lambda: shown.append(1)
        self.assertTrue(self.call("/api/show", {})["ok"])
        self.call("/api/settings", {"close_to_tray": False})
        st = self.call("/api/state")
        self.assertIs(st["settings"]["close_to_tray"], False)
        self.assertIn("autostart_supported", st)
        import time
        time.sleep(0.2)
        self.assertEqual(shown, [1])

    def test_settings_and_quit(self):
        self.call("/api/settings", {"volume": 999, "monitor_volume": 50})
        st = self.call("/api/state")["settings"]
        self.assertEqual((st["volume"], st["monitor_volume"]), (200, 50))
        self.call("/api/quit", {})
        import time
        time.sleep(0.2)
        self.assertEqual(self.quit, [1])


if __name__ == "__main__":
    unittest.main()
