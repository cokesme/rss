#!/usr/bin/env python3
"""Fetch RSS/Atom feeds and render a static reader site into ./site."""

from __future__ import annotations

import calendar
import html
import http.client
import io
import ipaddress
import os
import re
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import feedparser

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
OUT = ROOT / "site"

SITE_TITLE = "My Feed Reader"
MAX_ITEMS = 300          # items on the front page
MAX_PER_FEED = 50        # items kept per feed
SUMMARY_CHARS = 280
USER_AGENT = "static-rss-reader/1.0 (+https://github.com)"
FETCH_TIMEOUT = 20       # seconds per socket operation, and for the whole body
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOW_PRIVATE = os.environ.get("RSS_ALLOW_PRIVATE") == "1"  # allow non-public IPs (local testing)


# ---------- helpers ----------

def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "feed"


class _TextExtractor(HTMLParser):
    """Linear-time tag stripper: tags become spaces, <script>/<style> bodies are dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip = tag
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == self.skip:
            self.skip = None
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def handle_comment(self, data):
        self.parts.append(" ")


def strip_html(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw or "")
    p.close()
    return re.sub(r"\s+", " ", "".join(p.parts)).strip()


def is_safe_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def sanitize_link(url: str | None, fallback: str | None = "#") -> str:
    """Return url if it is http(s) with a host, else fallback if it is, else '#'."""
    for u in ((url or "").strip(), (fallback or "").strip()):
        if is_safe_http_url(u):
            return u
    return "#"


def truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except (ValueError, OverflowError, OSError):  # feedparser can return year 0 or 10000
                continue
    return None


def rel_time(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return ""
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return dt.strftime("%b %d, %Y")
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            n = secs // size
            if unit == "d" and n > 30:
                return dt.strftime("%b %d, %Y")
            return f"{n}{unit} ago"
    return "just now"


def read_feed_list() -> list[tuple[str, str | None]]:
    """Each line: URL [optional display name]. '#' starts a comment."""
    feeds = []
    for line in FEEDS_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        feeds.append((parts[0], parts[1].strip() if len(parts) > 1 else None))
    return feeds


# ---------- fetching ----------

def ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6:
        ip = ip.ipv4_mapped or ip.sixtofour or ip
    return ALLOW_PRIVATE or (ip.is_global and not ip.is_multicast)


def public_addrs(host: str, port: int) -> list[tuple]:
    """Resolve host; refuse if any address is non-public."""
    addrs = [sa[:2] for *_, sa in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    for ip, _ in addrs:
        if not ip_allowed(ipaddress.ip_address(ip)):
            raise OSError(f"refusing non-public address {ip} for {host!r}")
    return addrs


class PinnedHTTPConnection(http.client.HTTPConnection):
    # Check and connect use the same DNS answer, so DNS rebinding can't get past the check.
    # HTTPS still wraps with server_hostname=self.host, so SNI and cert checks use the name.
    def connect(self):
        err = None
        for addr in public_addrs(self.host, self.port):
            try:
                self.sock = socket.create_connection(addr, self.timeout, self.source_address)
                return
            except OSError as exc:
                err = exc
        raise err or OSError(f"no address for {self.host!r}")


class PinnedHTTPSConnection(http.client.HTTPSConnection, PinnedHTTPConnection):
    pass  # MRO: HTTPSConnection.connect -> PinnedHTTPConnection.connect -> TLS wrap


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(PinnedHTTPConnection, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PinnedHTTPSConnection, req, context=self._context)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise OSError(f"refusing redirect to {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def clean(value) -> str:
    """One line with no control characters and no leading '::' (a GitHub Actions command)."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f  ]+", " ", str(value)).lstrip(": ").rstrip()


def download(url: str) -> tuple[bytes, dict]:
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ValueError("only http(s) feed URLs are allowed")
    opener = urllib.request.OpenerDirector()  # no proxy, file, ftp or data handlers
    for h in (PinnedHTTPHandler(), PinnedHTTPSHandler(), SafeRedirectHandler(),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(h)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # no Accept-Encoding
    deadline = time.monotonic() + FETCH_TIMEOUT
    with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
        buf = bytearray()
        while chunk := resp.read1(65536):
            buf += chunk
            if len(buf) > MAX_FEED_BYTES:
                raise ValueError(f"feed exceeds {MAX_FEED_BYTES} bytes")
            if time.monotonic() > deadline:
                raise TimeoutError(f"feed download took over {FETCH_TIMEOUT}s")
        # content-location keeps relative links resolving against the (final) feed URL
        return bytes(buf), {"content-location": resp.geturl(),
                            "content-type": resp.headers.get("content-type", "")}


def fetch(url_and_name):
    url, name = url_and_name
    try:
        data, headers = download(url)
        # BytesIO, not bytes: feedparser tries to open() a bytes argument as a local file path
        d = feedparser.parse(io.BytesIO(data), response_headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "error": clean(exc) or type(exc).__name__, "items": [], "title": name or url}

    if d.bozo and not d.entries:
        err = getattr(d, "bozo_exception", "could not parse feed")
        return {"url": url, "error": clean(err) or "could not parse feed", "items": [], "title": name or url}

    title = name or d.feed.get("title") or url
    site = sanitize_link(d.feed.get("link"), url)  # url is already known to be http(s)
    items = []
    for e in d.entries[:MAX_PER_FEED]:
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = e.content[0].get("value", "")
        items.append({
            "title": strip_html(e.get("title", "")) or "(untitled)",
            "link": sanitize_link(e.get("link"), site),
            "date": entry_time(e),
            "summary": truncate(strip_html(summary), SUMMARY_CHARS),
            "feed": title,
        })
    return {"url": url, "title": title, "site": site, "items": items, "error": None}


# ---------- rendering ----------

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1c1a;--muted:#6b6862;--line:#e6e2da;--accent:#c2410c;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#9a978f;--line:#2a2926;--accent:#fb923c;--card:#1b1a18}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:inherit}
.wrap{max-width:820px;margin:0 auto;padding:0 16px}
header{border-bottom:1px solid var(--line);padding:28px 0 18px;margin-bottom:8px}
header h1{margin:0;font-size:1.6rem;letter-spacing:-.01em}
header h1 a{text-decoration:none}
header p{margin:4px 0 0;color:var(--muted);font-size:.9rem}
nav{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
nav a{font-size:.82rem;text-decoration:none;padding:3px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);background:var(--card)}
nav a:hover,nav a.on{color:var(--accent);border-color:var(--accent)}
.day{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:28px 0 6px}
article{padding:14px 0;border-bottom:1px solid var(--line)}
article h2{font-size:1.05rem;margin:0 0 4px;line-height:1.35}
article h2 a{text-decoration:none}
article h2 a:hover{color:var(--accent)}
.meta{font-size:.8rem;color:var(--muted)}
.meta a{text-decoration:none;color:var(--accent)}
.sum{margin:6px 0 0;color:var(--muted);font-size:.93rem}
.err{margin:24px 0;padding:12px 14px;border:1px solid var(--line);border-radius:8px;font-size:.85rem;color:var(--muted)}
.err code{word-break:break-all}
footer{padding:32px 0 48px;color:var(--muted);font-size:.8rem}
"""


CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def page(title: str, body: str, feeds, active: str | None, now: datetime, depth: int = 0) -> str:
    up = "../" * depth
    chips = [f'<a href="{up}index.html" class="{"on" if active is None else ""}">All</a>']
    for f in feeds:
        if not f["items"]:
            continue
        cls = "on" if active == f["slug"] else ""
        chips.append(f'<a href="{up}feeds/{f["slug"]}.html" class="{cls}">{html.escape(f["title"])}</a>')
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)}</title>
<link rel="alternate" type="application/atom+xml" title="{html.escape(SITE_TITLE)}" href="{up}atom.xml">
<style>{CSS}</style>
</head><body><div class="wrap">
<header>
<h1><a href="{up}index.html">{html.escape(SITE_TITLE)}</a></h1>
<p>Updated {now.strftime("%a %b %d, %Y %H:%M UTC")} · <a href="{up}atom.xml">Atom feed</a></p>
<nav>{"".join(chips)}</nav>
</header>
<main>{body}</main>
<footer>Built by a GitHub Action.</footer>
</div></body></html>"""


def render_items(items, now: datetime, show_feed=True, depth=0) -> str:
    out, last_day = [], None
    up = "../" * depth
    for it in items:
        day = it["date"].strftime("%A, %B %d") if it["date"] else "Undated"
        if day != last_day:
            out.append(f'<div class="day">{day}</div>')
            last_day = day
        meta = []
        if show_feed:
            meta.append(f'<a href="{up}feeds/{it["slug"]}.html">{html.escape(it["feed"])}</a>')
        if it["date"]:
            iso = it["date"].isoformat()
            meta.append(f'<time datetime="{iso}">{rel_time(it["date"], now)}</time>')
        summary = f'<p class="sum">{html.escape(it["summary"])}</p>' if it["summary"] else ""
        out.append(
            f'<article><h2><a href="{html.escape(sanitize_link(it["link"]), quote=True)}" rel="noopener noreferrer">'
            f'{html.escape(it["title"])}</a></h2>'
            f'<div class="meta">{" · ".join(meta)}</div>{summary}</article>'
        )
    return "\n".join(out) or "<p>No items yet.</p>"


ATTR_ENTITIES = {'"': "&quot;"}


def render_atom(items, now: datetime) -> str:
    entries = []
    for it in items[:100]:
        updated = (it["date"] or now).isoformat()
        link = sanitize_link(it["link"])
        entries.append(f"""<entry>
<title>{xml_escape(it["title"])}</title>
<link href="{xml_escape(link, ATTR_ENTITIES)}"/>
<id>{xml_escape(link)}</id>
<updated>{updated}</updated>
<author><name>{xml_escape(it["feed"])}</name></author>
<summary>{xml_escape(it["summary"])}</summary>
</entry>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>{xml_escape(SITE_TITLE)}</title>
<id>urn:static-rss-reader</id>
<updated>{now.isoformat()}</updated>
{"".join(entries)}
</feed>"""


# ---------- main ----------

def main() -> int:
    feed_list = read_feed_list()
    if not feed_list:
        print("No feeds in feeds.txt", file=sys.stderr)
        return 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        feeds = list(pool.map(fetch, feed_list))

    used = set()
    for f in feeds:
        slug = slugify(f["title"])
        base, i = slug, 2
        while slug in used:
            slug, i = f"{base}-{i}", i + 1
        used.add(slug)
        f["slug"] = slug
        for it in f["items"]:
            it["slug"] = slug

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    all_items = sorted(
        (it for f in feeds for it in f["items"]),
        key=lambda it: it["date"] or epoch,
        reverse=True,
    )

    now = datetime.now(timezone.utc)
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "feeds").mkdir(parents=True)

    errors = [f for f in feeds if f["error"]]
    err_html = ""
    if errors:
        rows = "".join(
            f"<li><code>{html.escape(clean(f['url']))}</code> — {html.escape(clean(f['error'])[:200])}</li>"
            for f in errors
        )
        err_html = f'<div class="err"><strong>{len(errors)} feed(s) failed:</strong><ul>{rows}</ul></div>'

    (OUT / "index.html").write_text(
        page(SITE_TITLE, render_items(all_items[:MAX_ITEMS], now) + err_html, feeds, None, now),
        encoding="utf-8",
    )
    for f in feeds:
        if not f["items"]:
            continue
        body = render_items(f["items"], now, show_feed=False, depth=1)
        (OUT / "feeds" / f"{f['slug']}.html").write_text(
            page(f"{f['title']} · {SITE_TITLE}", body, feeds, f["slug"], now, depth=1),
            encoding="utf-8",
        )
    (OUT / "atom.xml").write_text(render_atom(all_items, now), encoding="utf-8")
    (OUT / ".nojekyll").write_text("")

    ok = len(feeds) - len(errors)
    print(f"Built {len(all_items)} items from {ok}/{len(feeds)} feeds into {OUT}/")
    for f in errors:
        print(f"  ! {clean(f['url'])}: {clean(f['error'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
