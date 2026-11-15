"""Tests for the online sound sources and the soundboard's duplicate check. No network, no GTK needed:
    python3 -m unittest discover -s tests
"""
import http.server
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import amx_sounds as snd  # noqa: E402

MYINSTANTS_PAGE = '''<div class="instant"><div class="circle"></div>
<button class="small-button" onclick="play('/media/sounds/movie_1.mp3', 'loader-1', 'bruh-1')"
 title="Play Bruh sound" type="button"></button>
<a href="/en/instant/movie-bruh-8687/" class="instant-link link-secondary">Bruh &amp; Co</a></div>
<div class="instant"><button class="small-button" onclick="play('/media/sounds/vine-boom.mp3', 'l', 's')"
 title="Play Vine Boom sound"></button>
<button class="small-button" onclick="play('/media/sounds/vine-boom.mp3')"></button></div>
<button id="other">Not a sound</button><a href="?name=bruh&page=2">next</a>'''


class Sources(unittest.TestCase):
    def test_myinstants_parse(self):
        r = snd.parse_myinstants(MYINSTANTS_PAGE)
        self.assertEqual([x["name"] for x in r], ["Bruh & Co", "Vine Boom"])
        self.assertEqual(r[0]["url"], "https://www.myinstants.com/media/sounds/movie_1.mp3")

    def test_myinstants_search_urls_and_paging(self):
        seen = []
        with mock.patch.object(snd, "http_get", lambda url, *a, **k: (seen.append(url), MYINSTANTS_PAGE.encode())[1]):
            res, more = snd.search_myinstants("bruh", 1)
            self.assertEqual(len(res), 2)
            self.assertTrue(more)
            snd.search_myinstants("", 1)
        self.assertEqual(seen, ["https://www.myinstants.com/en/search/?name=bruh&page=1",
                                "https://www.myinstants.com/en/index/us/?page=1"])

    def test_freesound(self):
        with self.assertRaises(snd.SourceError):
            snd.search_freesound("boom", 1, {})
        payload = {"next": "x", "results": [
            {"name": "Boom", "duration": 3.4, "username": "ann",
             "license": "http://creativecommons.org/licenses/by/4.0/",
             "previews": {"preview-hq-mp3": "https://cdn.freesound.org/previews/1/1-hq.mp3"}},
            {"name": "No preview", "previews": {}}]}
        seen = []
        with mock.patch.object(snd, "http_json", lambda url, **k: (seen.append(url), payload)[1]):
            res, more = snd.search_freesound("boom", 2, {"freesound_key": "KEY"})
        self.assertIn("token=KEY", seen[0])
        self.assertIn("page=2", seen[0])
        self.assertEqual(res, [{"name": "Boom", "url": "https://cdn.freesound.org/previews/1/1-hq.mp3",
                                "info": "0:03 · by ann · CC BY 4.0 · Freesound"}])
        self.assertTrue(more)

    def test_freesound_bad_key(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch.object(snd, "http_json", side_effect=err):
            with self.assertRaisesRegex(snd.SourceError, "didn't accept"):
                snd.search_freesound("x", 1, {"freesound_key": "bad"})

    def test_openverse(self):
        payload = {"page_count": 3, "results": [
            {"title": "Boom", "url": "https://x.org/a.mp3", "duration": 4200, "creator": "bob",
             "license": "cc0", "source": "freesound"},
            {"title": "bad", "url": "ftp://x"}]}
        with mock.patch.object(snd, "http_json", lambda url, **k: payload):
            res, more = snd.search_openverse("boom", 1)
        self.assertEqual(res, [{"name": "Boom", "url": "https://x.org/a.mp3",
                                "info": "0:04 · by bob · CC0 · freesound"}])
        self.assertTrue(more)
        with self.assertRaises(snd.SourceError):
            snd.search_openverse("", 1)

    def test_commons(self):
        payload = {"continue": {"gsroffset": 20}, "query": {"pages": {
            "2": {"index": 2, "title": "File:Big.wav", "imageinfo": [{"url": "https://u/big.wav", "size": 99 << 20}]},
            "1": {"index": 1, "title": "File:Door knock.ogg", "imageinfo": [{
                "url": "https://upload.wikimedia.org/a/Door_knock.ogg", "size": 1000,
                "extmetadata": {"Artist": {"value": "<a href='x'>Jane</a>"},
                                "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}}
        with mock.patch.object(snd, "http_json", lambda url, **k: payload):
            res, more = snd.search_commons("knock", 1)
        self.assertEqual(res, [{"name": "Door knock", "url": "https://upload.wikimedia.org/a/Door_knock.ogg",
                                "info": "by Jane · CC BY-SA 4.0 · Wikimedia Commons"}])
        self.assertTrue(more)

    def test_friendly_errors(self):
        self.assertIn("HTTP 500", snd.friendly_error(urllib.error.HTTPError("u", 500, "x", {}, None)))
        self.assertIn("too many", snd.friendly_error(urllib.error.HTTPError("u", 429, "x", {}, None)))
        self.assertIn("Couldn't reach", snd.friendly_error(urllib.error.URLError("down")))
        self.assertEqual(snd.friendly_error(snd.SourceError("plain")), "plain")

    def test_find_audio_in_page(self):
        doc = ('<html><head><meta property="og:title" content="Vine Boom">'
               '<meta property="og:audio" content="/media/sounds/vine.mp3"></head></html>')
        self.assertEqual(snd.find_audio_in_page(doc, "https://www.myinstants.com/en/instant/vine/"),
                         ("https://www.myinstants.com/media/sounds/vine.mp3", "Vine Boom"))
        doc = '<title>Page</title><audio controls src="clip.ogg"></audio>'
        self.assertEqual(snd.find_audio_in_page(doc, "https://a.b/c/"), ("https://a.b/c/clip.ogg", "Page"))
        self.assertEqual(snd.find_audio_in_page("<p>nothing</p>", "https://a.b/")[0], None)

    def test_link_validation(self):
        with self.assertRaises(snd.SourceError):
            snd.resolve_link("", 1)
        with self.assertRaises(snd.SourceError):
            snd.resolve_link("file:///etc/passwd", 1)
        res, _ = snd.resolve_link("https://x.org/sounds/My%20Clip.mp3", 1)
        self.assertEqual(res[0]["name"], "My Clip")
        with self.assertRaises(snd.SourceError):
            snd.fetch("ytdlp:file:///etc/passwd")

    def test_safe_name(self):
        self.assertEqual(snd.safe_name("a/b: c?.."), "a_b_ c?")
        self.assertEqual(snd.safe_name("///"), "sound")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/page":
            body, ctype = b'<html><title>Clip page</title><audio src="/a.mp3"></audio></html>', "text/html"
        elif self.path in ("/a.mp3", "/noext"):
            body, ctype = b"ID3fakeaudio", "audio/mpeg"
        elif self.path == "/notaudio.mp3":
            body, ctype = b"<!DOCTYPE html><html>login</html>", "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Downloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = mock.patch.object(snd, "CACHE", self.tmp.name)
        self._cache.start()
        self.addCleanup(self._cache.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_fetch_caches(self):
        p = snd.fetch(self.base + "/a.mp3")
        self.assertTrue(p.endswith(".mp3"))
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"ID3fakeaudio")
        self.assertEqual(snd.fetch(self.base + "/a.mp3"), p)

    def test_fetch_refuses_html(self):
        with self.assertRaises(snd.SourceError):
            snd.fetch(self.base + "/notaudio.mp3")

    def test_resolve_page_and_audio_header(self):
        with mock.patch.object(snd, "have_ytdlp", lambda: False):
            res, _ = snd.resolve_link(self.base + "/page", 1)
            self.assertEqual(res[0], {"name": "Clip page", "url": self.base + "/a.mp3", "info": "127.0.0.1:" +
                                      str(self.srv.server_address[1])})
            res, _ = snd.resolve_link(self.base + "/noext", 1)
            self.assertEqual(res[0]["url"], self.base + "/noext")


def _import_extras():
    """amx_extras with GTK stubbed out, for the Soundboard logic."""
    if "amx_extras" in sys.modules:
        return sys.modules["amx_extras"]
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError):
        class _B:
            pass
        rep = types.ModuleType("gi.repository")
        rep.Adw = types.SimpleNamespace(Dialog=_B, PreferencesGroup=_B)
        rep.Gtk = types.SimpleNamespace(Box=_B, DrawingArea=_B)
        rep.GLib = types.SimpleNamespace(get_user_config_dir=lambda: tempfile.gettempdir(),
                                         get_user_cache_dir=lambda: tempfile.gettempdir())
        rep.Gdk = rep.Gio = None
        gi = types.ModuleType("gi")
        gi.require_version = lambda *a: None
        gi.repository = rep
        sys.modules["gi"], sys.modules["gi.repository"] = gi, rep
    import amx_extras
    return amx_extras


class NoDuplicates(unittest.TestCase):
    def setUp(self):
        self.ext = _import_extras()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(self.ext, "SOUNDS_DIR", os.path.join(self.tmp.name, "sounds"))
        p.start()
        self.addCleanup(p.stop)
        self.board = self.ext.Soundboard(types.SimpleNamespace(eng=types.SimpleNamespace(cfg={})))

    def file(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_same_audio_is_skipped(self):
        a, a2, b = self.file("a.mp3", b"AAA"), self.file("a-copy.mp3", b"AAA"), self.file("b.mp3", b"BBB")
        self.assertEqual(len(self.board.add_files([a])), 1)
        added = self.board.add_files([a2, b, b])
        self.assertEqual([s["name"] for s in added], ["b"])
        self.assertEqual(self.board.skipped, ["a-copy", "b"])

    def test_named_download_and_source_url(self):
        c = self.file("1234abcd.mp3", b"CCC")
        s = self.board.add_files([c], ["Vine Boom!"], ["https://x/c.mp3"])[0]
        self.assertEqual(s["name"], "Vine Boom!")
        self.assertTrue(s["file"].endswith("Vine Boom!.mp3"))
        self.assertTrue(self.board.has_src("https://x/c.mp3"))
        self.assertEqual(self.board.add_files([c], ["Again"], ["https://x/c.mp3"]), [])

    def test_reimport_own_export_skips_all(self):
        self.board.add_files([self.file("a.mp3", b"AAA"), self.file("b.mp3", b"BBB")])
        z = os.path.join(self.tmp.name, "board.zip")
        self.board.export_zip(z)
        self.assertEqual(self.board.import_zip(z), 0)
        self.assertEqual(len(self.board.skipped), 2)

    def test_old_entries_without_hash(self):
        self.board.add_files([self.file("a.mp3", b"AAA")])
        for s in self.board.board["sounds"]:
            s.pop("sha1")
        self.assertEqual(self.board.add_files([self.file("again.mp3", b"AAA")]), [])


if __name__ == "__main__":
    unittest.main()
