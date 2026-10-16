"""R07: URL-fetch mode (feedparser.parse(url)) behaviour of feedparser/http.py.

No network: a urllib handler with a low handler_order answers http(s)/ftp
requests in-process, so feedparser's real http.get() / _FeedURLHandler code runs.

Shows:
  a) gzip bomb: 100 KB on the wire -> 100 MB in memory (http.py:179-181, no cap;
     the whole body is read first with f.read(), http.py:172, also uncapped)
  b) a corrupt gzip body raises zlib.error *out of* parse() (http.py:182 only
     catches EOFError/IOError/struct.error)
  c) redirects are followed to loopback/private hosts and to ftp://
     (stdlib HTTPRedirectHandler allows http/https/ftp)
  d) 4xx/5xx bodies are parsed as feeds (http.py:48-52 swallows HTTP errors)
  e) no timeout: http.py:171 calls opener.open(request) without timeout=
     (source-only; not demonstrated here)

Run:  python -I r07_url_mode_http.py     (stdlib + feedparser, no network)
"""
import gzip
import io
import urllib.request
from email.message import Message
from urllib.response import addinfourl

import feedparser

FEED = b"<rss version='2.0'><channel><title>%s</title><item><title>i</title></item></channel></rss>"
seen = []


class Resp(addinfourl):
    status = None  # http.client.HTTPResponse.status is writable; feedparser assigns it


def response(url, body, code=200, headers=()):
    msg = Message()
    for k, v in headers:
        msg[k] = v
    r = Resp(io.BytesIO(body), msg, url, code)
    r.msg = "x"
    return r


class FakeNet(urllib.request.BaseHandler):
    handler_order = 100  # before the real HTTPHandler / FTPHandler

    def __init__(self, routes):
        self.routes = routes

    def _open(self, req):
        url = req.get_full_url()
        seen.append(url)
        return self.routes[url](url)

    http_open = https_open = ftp_open = _open


def run(label, routes, start):
    seen.clear()
    try:
        d = feedparser.parse(start, handlers=[FakeNet(routes)])
        print(f"{label:34} title={d.feed.get('title')!r} status={d.get('status')} bozo={d.bozo} "
              f"exc={type(d.get('bozo_exception')).__name__} body_len={sum(len(e.get('title', '')) for e in d.entries)}")
    except Exception as exc:  # noqa: BLE001
        print(f"{label:34} RAISED {type(exc).__name__}: {exc}")
    print(f"{'':34} requests: {seen}")


# a) gzip bomb
big = FEED.replace(b"%s", b"bomb").replace(b"<title>i</title>", b"<title>" + b"A" * 100_000_000 + b"</title>")
bomb = gzip.compress(big, 9)
print(f"gzip bomb: {len(bomb)/1e3:.0f} KB compressed -> {len(big)/1e6:.0f} MB decompressed")
run("a) gzip bomb", {"http://feed.example/": lambda u: response(u, bomb, headers=[("Content-Encoding", "gzip")])},
    "http://feed.example/")

# b) corrupt gzip stream
bad = bytearray(gzip.compress(FEED % b"x"))
bad[10] ^= 0xFF  # first byte of the deflate stream
run("b) corrupt gzip", {"http://feed.example/": lambda u: response(u, bytes(bad), headers=[("Content-Encoding", "gzip")])},
    "http://feed.example/")

# c) redirects to loopback and to ftp
run("c1) 302 -> http://127.0.0.1", {
    "http://feed.example/": lambda u: response(u, b"", 302, [("Location", "http://127.0.0.1:8080/admin")]),
    "http://127.0.0.1:8080/admin": lambda u: response(u, FEED % b"INTERNAL SERVICE"),
}, "http://feed.example/")
run("c2) 302 -> ftp://10.0.0.5", {
    "http://feed.example/": lambda u: response(u, b"", 302, [("Location", "ftp://10.0.0.5/pub/x.xml")]),
    "ftp://10.0.0.5/pub/x.xml": lambda u: response(u, FEED % b"FTP ON PRIVATE NET"),
}, "http://feed.example/")

# d) error page parsed as a feed
run("d) 404 body parsed", {"http://feed.example/": lambda u: response(u, FEED % b"from a 404 page", 404)},
    "http://feed.example/")
