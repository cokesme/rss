"""R02: feedparser.parse(<bytes>) treats the bytes as a *local file path* first.

api._open_resource (feedparser 6.0.14, api.py:110-136): anything that is not a
file-like object and not an http/https/ftp/file/feed URL string is passed to
open(..., 'rb'). On POSIX, open() accepts bytes paths. So if the build
downloads a body itself and calls `feedparser.parse(body_bytes, ...)`, a remote
server that returns a body such as `/dev/zero` or `/some/local/file` makes
feedparser read that local path instead of parsing the body.

  * `/dev/zero`, `/dev/urandom`: unbounded read() -> memory exhaustion,
    bypassing the build's own MAX_FEED_BYTES limit.
  * a local file that happens to be a feed/XML is parsed and rendered.
  * FIFOs / ttys: read() can block forever.

Wrapping the body in io.BytesIO (as feedparser's own docstring recommends,
api.py:160-163) avoids the filesystem lookup completely.

Run:  python -I r02_bytes_treated_as_path.py    (stdlib + feedparser, no network)
"""
import io
import os
import subprocess
import sys
import tempfile
import textwrap

import feedparser

# 1. A local file is read when the *body* is its path.
with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as fh:
    fh.write(b"<rss version='2.0'><channel><title>CONTENT OF A LOCAL FILE</title>"
             b"<item><title>local item</title><link>https://x.invalid/</link></item></channel></rss>")
    local_path = fh.name
try:
    body = local_path.encode()          # what a hostile server would send
    d = feedparser.parse(body, response_headers={"content-type": "application/rss+xml"})
    print("parse(bytes body)            -> feed.title =", repr(d.feed.get("title")))
    d = feedparser.parse(io.BytesIO(body), response_headers={"content-type": "application/rss+xml"})
    print("parse(io.BytesIO(bytes body)) -> feed.title =", repr(d.feed.get("title")),
          " bozo_exception =", type(d.get("bozo_exception")).__name__)
finally:
    os.unlink(local_path)

# 2. Body b"/dev/zero" -> unbounded read. Run in a child with a 512 MiB
#    address-space limit so this demo cannot hurt the machine.
child = textwrap.dedent("""
    import resource, time, feedparser
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    t = time.time()
    try:
        feedparser.parse(b"/dev/zero", response_headers={"content-type": "application/rss+xml"})
        print("returned normally")
    except MemoryError:
        print(f"MemoryError after {time.time() - t:.2f}s reading /dev/zero (body was 9 bytes)")
""")
out = subprocess.run([sys.executable, "-I", "-c", child], capture_output=True, text=True, timeout=120)
print("parse(b'/dev/zero')          ->", (out.stdout + out.stderr).strip().splitlines()[-1])
