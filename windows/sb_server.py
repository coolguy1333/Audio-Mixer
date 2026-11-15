"""Local HTTP server: serves the UI and a small JSON API.

Security: every /api call needs the app's token (header X-Token, or ?t= for simple GET links such as Stream
Deck buttons). Websites you visit can't read the token, and a custom header can't be sent cross-site without a
CORS preflight, which this server never allows. The Host header is checked against DNS rebinding. The server
listens on 127.0.0.1 only, unless "phone companion" (LAN) is switched on.
"""
import http.server
import ipaddress
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import urllib.parse

import sb_audio
import sb_library

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import amx_sounds as snd  # noqa: E402

UI_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "ui")
MAX_UPLOAD = 100 << 20


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))            # no packet is sent; just picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class App:
    """Glue between the library, the engine, hotkeys and the server."""

    def __init__(self, lib, engine):
        self.lib, self.engine = lib, engine
        self.hotkeys = None
        self.on_quit = None
        self.notice = lib.problem

    def state(self):
        lib, eng = self.lib, self.engine
        prog = eng.progress()
        c = lib.cfg
        with lib.lock:
            sounds = [{"id": s["id"], "name": s["name"], "start": s.get("start") or 0, "end": s.get("end"),
                       "dur": round(lib.duration(s), 3), "vol": s.get("vol", 100), "hotkey": s.get("hotkey"),
                       "color": s.get("color"), "playing": prog.get(s["id"], [])} for s in lib.sounds]
        return {"sounds": sounds,
                "settings": {k: c.get(k) for k in ("volume", "output", "monitor", "monitor_volume", "mic",
                                                   "mic_volume", "web_sounds", "web_source", "lan", "port",
                                                   "stop_key")},
                "freesound_key_set": bool(c.get("freesound_key")),
                "errors": eng.errors, "hotkeys": self.hotkeys.status if self.hotkeys else "unavailable",
                "preview": eng.preview_playing(), "notice": self.notice,
                "sources": [n for n, _f, _p in snd.SOURCES],
                "placeholders": [p for _n, _f, p in snd.SOURCES],
                "ffmpeg": bool(sb_library.ffmpeg_path()), "ytdlp": snd.have_ytdlp(),
                "lan_urls": [f"http://{ip}:{c['port']}/?t={c['token']}" for ip in lan_ips()] if c.get("lan") else [],
                "data_dir": lib.home}

    def settings(self, d):
        c = self.lib.cfg
        reopen = False
        for k in ("output", "monitor", "mic"):
            if k in d and d[k] != c.get(k):
                c[k] = d[k] or None
                reopen = True
        for k in ("volume", "monitor_volume", "mic_volume"):
            if k in d:
                c[k] = max(0, min(200, int(d[k])))
        for k in ("web_sounds", "lan"):
            if k in d:
                c[k] = bool(d[k])
        if "web_source" in d:
            c["web_source"] = max(0, min(len(snd.SOURCES) - 1, int(d["web_source"])))
        if "freesound_key" in d:
            c["freesound_key"] = str(d["freesound_key"]).strip()
        if "port" in d:
            c["port"] = max(1024, min(65535, int(d["port"])))
        if "stop_key" in d:
            c["stop_key"] = d["stop_key"] or None
        self.lib.save()
        if reopen:
            self.engine.apply()
        else:
            self.engine.set_gains()
        if self.hotkeys and ("stop_key" in d):
            self.hotkeys.reload()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # Linux/macOS: rebind right after a restart (TIME_WAIT). Windows: SO_REUSEADDR would let another program
    # take the port over, so it stays off there.
    allow_reuse_address = sys.platform != "win32"


def make_handler(app):
    lib, eng = app.lib, app.engine

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "AudioMixerSoundboard"

        def log_message(self, *a):
            pass

        # ---- helpers
        def _send(self, code, body=b"", ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host in ("localhost", "127.0.0.1", "::1"):
                return True
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                return False                  # a name other than localhost: possible DNS rebinding
            return bool(lib.cfg.get("lan")) and (ip.is_private or ip.is_link_local)

        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _authed(self, q):
            tok = lib.cfg["token"]
            return self.headers.get("X-Token") == tok or q.get("t", [None])[0] == tok

        def _body(self, limit=1 << 20):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError("too large")
            return self.rfile.read(n) if n else b""

        def _json(self):
            b = self._body()
            return json.loads(b) if b else {}

        # ---- routes
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_OPTIONS(self):                  # no CORS, ever
            self._send(405, {"error": "not allowed"})

        def _route(self, method):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = u.path
            if method == "GET" and path in ("/", "/index.html"):
                if not (self._local() or self._authed(q)):
                    return self._send(403, "Open the link shown in the app's settings (it includes a key).",
                                      "text/plain; charset=utf-8")
                try:
                    with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
                        page = f.read()
                except OSError:
                    return self._send(500, "UI missing", "text/plain")
                page = page.replace("__TOKEN__", lib.cfg["token"])
                return self._send(200, page, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                                              "style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; "
                                                              "frame-ancestors 'none'"})
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if path == "/api/ping":
                return self._send(200, {"app": "amx-soundboard"})
            if not self._authed(q):
                return self._send(401, {"error": "missing or wrong token"})
            try:
                return self._api(method, path[5:], q)
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e) or type(e).__name__})
            except Exception as e:                       # never take the server down
                return self._send(500, {"error": snd.friendly_error(e)})

        def _api(self, method, p, q):
            parts = p.split("/")
            # simple GET links for Stream Deck / scripts: /api/play/<id>?t=TOKEN
            if parts[0] in ("play", "stop", "toggle") and len(parts) == 2:
                fn = {"play": eng.play, "stop": lambda i: eng.stop(i), "toggle": eng.toggle}[parts[0]]
                err = fn(urllib.parse.unquote(parts[1]))
                return self._send(200 if not err else 409, {"ok": not err, "error": err})
            if p == "stopall":
                eng.stop_all()
                return self._send(200, {"ok": True})
            if method == "GET" and p == "state":
                return self._send(200, app.state())
            if method == "GET" and parts[0] == "peaks" and len(parts) == 2:
                s = lib.sound(parts[1])
                return self._send(200, lib.peaks(s) if s else [])
            if method == "GET" and p == "devices":
                return self._send(200, sb_audio.devices())
            if method == "GET" and p == "export":
                fd, tmp = tempfile.mkstemp(suffix=".zip")
                os.close(fd)
                try:
                    lib.export_zip(tmp)
                    with open(tmp, "rb") as f:
                        data = f.read()
                finally:
                    os.unlink(tmp)
                return self._send(200, data, "application/zip",
                                  {"Content-Disposition": 'attachment; filename="soundboard.zip"'})
            if method != "POST":
                return self._send(405, {"error": "use POST"})
            if p == "upload":                            # raw file body; name in X-Filename
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "sound.mp3")
                data = self._body(MAX_UPLOAD)
                lib.skipped = []
                try:
                    s = lib.add_bytes(data, os.path.basename(name))
                except sb_library.DecodeError as e:
                    return self._send(200, {"ok": False, "error": f"{os.path.basename(name)}: {e}"})
                return self._send(200, {"ok": True, "sound": s and s["id"], "skipped": lib.skipped})
            if p == "import":
                data = self._body(MAX_UPLOAD * 5)
                fd, tmp = tempfile.mkstemp(suffix=".zip")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                try:
                    n = lib.import_zip(tmp)
                except Exception:
                    return self._send(200, {"ok": False, "error": "That file isn't a soundboard export"})
                finally:
                    os.unlink(tmp)
                return self._send(200, {"ok": True, "added": n, "skipped": len(lib.skipped)})
            d = self._json()
            if p == "settings":
                app.settings(d)
                return self._send(200, {"ok": True, "errors": eng.errors})
            if p == "rescan":
                return self._send(200, eng.rescan())
            if p == "order":
                lib.reorder([str(i) for i in d["ids"]])
                return self._send(200, {"ok": True})
            if parts[0] == "sound" and len(parts) == 2:
                s = lib.update(parts[1], **{k: d[k] for k in ("name", "start", "end", "vol", "hotkey", "color")
                                            if k in d})
                if s and "hotkey" in d and app.hotkeys:
                    app.hotkeys.reload()
                return self._send(200 if s else 404, {"ok": bool(s)})
            if parts[0] == "delete" and len(parts) == 2:
                eng.stop(parts[1])
                ok = lib.remove(parts[1])
                if app.hotkeys:
                    app.hotkeys.reload()
                return self._send(200, {"ok": ok})
            if p == "quit":
                self._send(200, {"ok": True})
                if app.on_quit:
                    threading.Thread(target=app.on_quit, daemon=True).start()
                return None
            # ---- online sounds (opt-in)
            if p.startswith("web/"):
                if not lib.cfg.get("web_sounds"):
                    return self._send(403, {"error": "Online sound search is off"})
                if p == "web/search":
                    idx = max(0, min(len(snd.SOURCES) - 1, int(d.get("source", 0))))
                    try:
                        res, more = snd.SOURCES[idx][1](str(d.get("q", "")).strip(), max(1, int(d.get("page", 1))),
                                                        {"freesound_key": lib.cfg.get("freesound_key")})
                    except Exception as e:
                        return self._send(200, {"ok": False, "error": snd.friendly_error(e)})
                    for r in res:
                        r["added"] = lib.has_src(r["url"])
                    return self._send(200, {"ok": True, "results": res, "more": more})
                url = str(d.get("url", ""))
                if p == "web/preview":
                    try:
                        path = snd.fetch(url)
                        data = sb_library.decode(path)
                    except Exception as e:
                        return self._send(200, {"ok": False, "error": snd.friendly_error(e)})
                    err = eng.preview_file(data)
                    return self._send(200, {"ok": not err, "error": err})
                if p == "web/add":
                    if lib.has_src(url):
                        return self._send(200, {"ok": True, "added": False, "already": True})
                    try:
                        path = snd.fetch(url)
                        lib.skipped = []
                        s = lib.add_file(path, str(d.get("name") or "Sound")[:40], url)
                    except Exception as e:
                        return self._send(200, {"ok": False, "error": snd.friendly_error(e)})
                    return self._send(200, {"ok": True, "added": bool(s), "already": not s})
            if p == "preview/stop":
                eng.stop_preview()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown call"})

    return H


def serve(app, port):
    """Start the server in a thread. Returns (server, port actually used)."""
    host = "0.0.0.0" if app.lib.cfg.get("lan") else "127.0.0.1"
    for p in (port, 0):
        try:
            srv = _Server((host, p), make_handler(app))
            break
        except OSError:
            if p == 0:
                raise
    used = srv.server_address[1]
    if port and used != port:
        note = (f"Port {port} is in use by another program, so the soundboard is on port {used} this time: "
                f"Stream Deck and script links that use {port} won't work until it's free.")
        app.notice = f"{app.notice} {note}" if app.notice else note
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, used
