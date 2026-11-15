"""Online sound sources for the soundboard (optional; off until the user turns it on).

No GTK in here, so it can be tested on its own. Every search function takes (query, page, cfg) and returns
(results, more): results is a list of {"name", "url", "info"}, `more` says whether page + 1 exists.
A SourceError carries a message meant for the user as is (missing key, nothing found, ...).
"""
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "audio-mixer", "web")
MAX_BYTES = 25 << 20              # largest clip we download
PAGE_SIZE = 20
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".webm", ".weba", ".mp4"}
# APIs ask for a User-Agent that names the client; MyInstants is a normal web page and gets a browser one.
UA_API = "AudioMixer/1.0 (+https://github.com/coolguy1333/Audio-Mixer)"
UA_WEB = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
MYINSTANTS = "https://www.myinstants.com"
FREESOUND_KEY_URL = "https://freesound.org/apiv2/apply"


class SourceError(Exception):
    """A problem worth showing to the user word for word."""


def safe_name(name):
    n = re.sub(r"[^\w\-. ()!?',&]+", "_", name).strip(" ._")
    return n[:60] or "sound"


def _open(url, ua=UA_API):
    if not re.match(r"https?://", url):
        raise SourceError("Only http(s) links are supported.")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en"})
    return urllib.request.urlopen(req, timeout=20)


def http_get(url, limit=4 << 20, ua=UA_API):
    with _open(url, ua) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SourceError("That file is too large.")
    return data


def http_json(url, ua=UA_API):
    try:
        return json.loads(http_get(url, ua=ua))
    except ValueError:
        raise SourceError("The site sent an answer that couldn't be read.") from None


def friendly_error(e):
    """One line for the status label."""
    if isinstance(e, SourceError):
        return str(e)
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return f"The site refused the request (HTTP {e.code})."
        if e.code == 429:
            return "The site says too many searches; wait a minute and try again."
        return f"The site answered with an error (HTTP {e.code})."
    if isinstance(e, urllib.error.URLError):
        return f"Couldn't reach the site: {e.reason}"
    if isinstance(e, TimeoutError):
        return "The site took too long to answer."
    return f"Something went wrong: {e}"


def _dur(seconds):
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}" if s else None


def _cc(url_or_code, version=None):
    """'by', '4.0' or a creativecommons.org URL -> 'CC BY 4.0'."""
    v = str(url_or_code or "").lower()
    if "publicdomain/zero" in v or v == "cc0":
        return "CC0"
    if "publicdomain" in v or v == "pdm":
        return "Public domain"
    m = re.search(r"licenses/([a-z-]+)/([\d.]+)", v)
    if m:
        return f"CC {m.group(1).upper()} {m.group(2)}"
    return f"CC {v.upper()} {version or ''}".strip() if v else None


def _info(*bits):
    return " · ".join(b for b in bits if b)


# --------------------------------------------------------------------------- MyInstants
def parse_myinstants(page_html):
    """Sound buttons on a MyInstants listing page. The site has no API, so this reads the page."""
    out, seen = [], set()
    for m in re.finditer(r"<button\b[^>]*>", page_html):
        tag = m.group(0)
        p = re.search(r"(/media/sounds/[^'\"\s<>]+)", tag)
        if not p:
            continue
        url = urllib.parse.urljoin(MYINSTANTS, html.unescape(p.group(1)))
        if url in seen:
            continue
        seen.add(url)
        nxt = page_html.find("<button", m.end())
        ahead = page_html[m.end():nxt if nxt > 0 else m.end() + 3000]
        a = re.search(r"<a\b[^>]*class=\"[^\"]*instant-link[^\"]*\"[^>]*>(.*?)</a>", ahead, re.S)
        t = re.search(r"title=\"(?:Play\s+)?(.*?)(?:\s+sound)?\"", tag)
        name = re.sub(r"<[^>]+>", "", a.group(1)) if a else (t.group(1) if t else "")
        name = html.unescape(name).strip() or os.path.splitext(os.path.basename(url))[0]
        out.append({"name": name, "url": url, "info": "MyInstants"})
    return out


def search_myinstants(q, page, cfg=None):
    if q:
        url = f"{MYINSTANTS}/en/search/?" + urllib.parse.urlencode({"name": q, "page": page})
    else:
        url = f"{MYINSTANTS}/en/index/us/?page={page}"        # popular buttons
    doc = http_get(url, ua=UA_WEB).decode("utf-8", "replace")
    res = parse_myinstants(doc)
    if not res and page == 1 and "/media/sounds/" not in doc and "instant" not in doc:
        raise SourceError("MyInstants changed its page layout; this source needs an update.")
    return res, bool(res) and f"page={page + 1}" in doc


# --------------------------------------------------------------------------- Freesound
def search_freesound(q, page, cfg=None):
    key = ((cfg or {}).get("freesound_key") or "").strip()
    if not key:
        raise SourceError("Freesound needs a free API key: paste it in the box above.")
    params = {"query": q, "page": page, "page_size": PAGE_SIZE, "token": key,
              "fields": "name,previews,duration,username,license"}
    if not q:
        params["sort"] = "downloads_desc"
    try:
        d = http_json("https://freesound.org/apiv2/search/text/?" + urllib.parse.urlencode(params))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SourceError("Freesound didn't accept that API key.") from None
        if e.code == 404:                 # past the last page
            return [], False
        raise
    res = []
    for r in d.get("results", []):
        pv = r.get("previews") or {}
        url = pv.get("preview-hq-mp3") or pv.get("preview-lq-mp3")
        if url:
            res.append({"name": (r.get("name") or "Sound").strip(), "url": url,
                        "info": _info(_dur(r.get("duration")), r.get("username") and f"by {r['username']}",
                                      _cc(r.get("license")), "Freesound")})
    return res, bool(d.get("next"))


# --------------------------------------------------------------------------- Openverse
def search_openverse(q, page, cfg=None):
    if not q:
        raise SourceError("Type something to search Openverse.")
    d = http_json("https://api.openverse.org/v1/audio/?" +
                  urllib.parse.urlencode({"q": q, "page": page, "page_size": PAGE_SIZE}))
    res = []
    for r in d.get("results", []):
        if not re.match(r"https?://", str(r.get("url", ""))):
            continue
        res.append({"name": (r.get("title") or "Sound").strip(), "url": r["url"],
                    "info": _info(_dur((r.get("duration") or 0) / 1000), r.get("creator") and f"by {r['creator']}",
                                  _cc(r.get("license"), r.get("license_version")),
                                  r.get("source") or r.get("provider"))})
    return res, page < (d.get("page_count") or 0)


# --------------------------------------------------------------------------- Wikimedia Commons
def search_commons(q, page, cfg=None):
    if not q:
        raise SourceError("Type something to search Wikimedia Commons.")
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
              "gsrsearch": f"{q} filetype:audio", "gsrlimit": PAGE_SIZE, "gsroffset": (page - 1) * PAGE_SIZE,
              "prop": "imageinfo", "iiprop": "url|size|extmetadata",
              "iiextmetadatafilter": "Artist|LicenseShortName|ObjectName"}
    d = http_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params))
    pages = sorted((d.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    res = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        if not ii.get("url") or (ii.get("size") or 0) > MAX_BYTES:
            continue
        meta = ii.get("extmetadata") or {}
        title = re.sub(r"^File:", "", p.get("title", "Sound"))
        name = (meta.get("ObjectName") or {}).get("value") or os.path.splitext(title)[0]
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value") or "").strip()
        res.append({"name": html.unescape(name).strip(), "url": ii["url"],
                    "info": _info(artist and f"by {artist[:40]}", (meta.get("LicenseShortName") or {}).get("value"),
                                  "Wikimedia Commons")})
    return res, "continue" in d


# --------------------------------------------------------------------------- any link
def have_ytdlp():
    return shutil.which("yt-dlp") is not None


def find_audio_in_page(doc, base):
    """(audio url, title) from a web page: og:audio, <audio>/<source>, or a link to an audio file."""
    title = None
    for pat in (r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)",
                r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:title", r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, doc, re.I | re.S)
        if m:
            title = html.unescape(m.group(1)).strip()
            break
    pats = (r"<meta[^>]+property=[\"']og:audio(?::secure_url|:url)?[\"'][^>]+content=[\"']([^\"']+)",
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:audio",
            r"<(?:audio|source)\b[^>]*\bsrc=[\"']([^\"']+)",
            r"[\"'(]((?:https?:)?/[^\"'()\s<>]+\.(?:mp3|ogg|oga|opus|wav|flac|m4a))(?:\?[^\"'()\s<>]*)?[\"')]")
    for pat in pats:
        m = re.search(pat, doc, re.I)
        if m:
            return urllib.parse.urljoin(base, html.unescape(m.group(1))), title
    return None, title


def _ytdlp_title(url):
    try:
        r = subprocess.run(["yt-dlp", "--no-playlist", "--skip-download", "--no-warnings", "--print", "title",
                            "--", url], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None


def resolve_link(q, page, cfg=None):
    if not q:
        raise SourceError("Paste a link: an audio file, a page with a sound on it (MyInstants, Freesound, …)"
                          + (", or a video (YouTube, TikTok, SoundCloud, …)." if have_ytdlp() else
                             ". Install yt-dlp to also take the audio from videos (YouTube, TikTok, …)."))
    if page > 1:
        return [], False
    url = q.strip()
    if not re.match(r"https?://", url):
        raise SourceError("That isn't a link: it should start with https://")
    host = urllib.parse.urlparse(url).netloc
    if os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() in AUDIO_EXT:
        name = os.path.splitext(os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path)))[0]
        return [{"name": name or "Sound", "url": url, "info": host}], False
    ctype, doc = "", ""
    try:
        with _open(url, UA_WEB) as r:
            ctype = r.headers.get_content_type() or ""
            if ctype.startswith("text/html"):
                doc = r.read(3 << 20).decode("utf-8", "replace")
            final = r.geturl()
    except Exception:
        if not have_ytdlp():
            raise
        final = url
    if ctype.startswith("audio/"):
        return [{"name": os.path.basename(urllib.parse.urlparse(final).path) or "Sound", "url": final,
                 "info": host}], False
    found, title = find_audio_in_page(doc, final) if doc else (None, None)
    if found:
        return [{"name": title or "Sound", "url": found, "info": host}], False
    if have_ytdlp():
        return [{"name": _ytdlp_title(url) or title or host, "url": "ytdlp:" + url, "info": f"{host} · via yt-dlp"}], False
    raise SourceError("No sound found on that page. Install yt-dlp to take the audio from video sites.")


SOURCES = [
    # (name, search function, placeholder)
    ("MyInstants", search_myinstants, "Search meme sounds… (empty = popular)"),
    ("Freesound", search_freesound, "Search 600,000+ sound effects… (empty = popular)"),
    ("Openverse", search_openverse, "Search Creative Commons audio…"),
    ("Wikimedia Commons", search_commons, "Search free audio files…"),
    ("Link (any site)", resolve_link, "Paste a link to a sound, page or video…"),
]


# --------------------------------------------------------------------------- download
def fetch(url):
    """Download a clip once into CACHE and return the local path. "ytdlp:<page>" takes the audio with yt-dlp."""
    os.makedirs(CACHE, exist_ok=True)
    stem = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest()[:20])
    if url.startswith("ytdlp:"):
        page = url[6:]
        if not re.match(r"https?://", page):
            raise SourceError("Only http(s) links are supported.")
        path = stem + ".mp3"
        if os.path.exists(path):
            return path
        if not have_ytdlp():
            raise SourceError("yt-dlp isn't installed.")
        try:
            r = subprocess.run(["yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
                                "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                                "--max-filesize", str(MAX_BYTES * 4), "--match-filter", "duration <? 1200",
                                "-o", stem + ".%(ext)s", "--", page],
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise SourceError("The download took too long.") from None
        if "does not pass filter" in r.stdout:
            raise SourceError("That video is longer than 20 minutes; pick a shorter one.")
        if r.returncode != 0 or not os.path.exists(path):
            msg = (r.stderr.strip().splitlines() or ["yt-dlp couldn't get the audio."])[-1]
            raise SourceError(re.sub(r"^ERROR:\s*", "", msg)[:200])
        return path
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    path = stem + (ext if ext in AUDIO_EXT else ".mp3")
    if not os.path.exists(path):
        data = http_get(url, MAX_BYTES, ua=UA_WEB if "myinstants.com" in url else UA_API)
        if data[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
            raise SourceError("That link is a web page, not a sound.")
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    return path
