"""R03: inputs that make feedparser.parse() raise instead of setting bozo.

Out-of-range numeric character references are a well-formedness error, so
expat rejects the document and feedparser falls back to the loose parser
(api.py:262-272). The loose parser's _FeedParserMixin.handle_charref
(mixin.py:347-360) then calls int()/chr()/.encode() without a try, and the
exception propagates out of parse() (api.py:272 has no try/except).

The build wraps feedparser.parse() in `except Exception`, so today this only
marks that one feed as failed. Any refactor that narrows that except (or moves
parse() outside it) would let one feed abort the whole build. See R07 for a
URL-mode-only case (zlib.error from a corrupt gzip body).

Run:  python -I r03_parse_raises.py    (stdlib + feedparser, no network)
"""
import io
import traceback

import feedparser


def rss(title):
    return ("<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
            f"<item><title>{title}</title></item></channel></rss>").encode()


CASES = {
    "&#99999999;  (> U+10FFFF)": rss("&#99999999;"),
    "&#x110000;   (> U+10FFFF)": rss("&#x110000;"),
    "&#xD800;     (surrogate)": rss("&#xD800;"),
    "&#<5000 digits>;": rss("&#" + "1" * 5000 + ";"),
    # the following four were also reported upstream in kurtmckee/feedparser#599
    "backslash in ENTITY value": (b'<?xml version="1.0"?>\n<!DOCTYPE rss [\n<!ENTITY a "x\\d">\n]>\n'
                                  b'<rss version="2.0"><channel><item><title>&a;</title></item></channel></rss>'),
    "NUL as declared encoding": b'<?encoding="\x00"?><rss/>',
    "non-UTF-8 encoding name": b'<?encoding="\xe3"?><rss/>',
    "stray </height> in <image>": b"<rss><channel><image></height></image><item><title>x</title></item></channel></rss>",
}

for name, data in CASES.items():
    try:
        d = feedparser.parse(io.BytesIO(data))
        print(f"{name:28} returned, bozo={d.bozo}")
    except Exception as exc:  # noqa: BLE001
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        where = frame.filename.split("site-packages/")[-1].split("/lib/")[-1]
        print(f"{name:28} RAISES {type(exc).__name__}: {str(exc)[:60]}  ({where}:{frame.lineno})")
