"""R04: feedparser returns *_parsed tuples that make the build's entry_time() raise.

feedparser's ISO-8601 fallback (datetimes/iso8601.py:82-158) accepts year 0000
and normalises out-of-range fields with time.mktime(), so it can return
struct_times with tm_year 0 or 10000. `datetime.fromtimestamp()` rejects those
years. In build.py, entry_time() runs inside fetch() but *outside* the
try/except that wraps feedparser.parse(), so the exception propagates out of
ThreadPoolExecutor.map() and aborts the whole build (no site is published).

entry_time() below is copied verbatim from build.py (commit 94c85be).

Run:  python -I r04_dates_crash_build.py    (stdlib + feedparser, no network)
"""
import calendar
import io
from datetime import datetime, timezone

import feedparser


def entry_time(e):  # verbatim from build.py
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


DATES = [
    ("pubDate", "Mon, 06 Sep 2021 10:00:00 GMT"),          # control: normal
    ("pubDate", "0000-01-01T00:00:00Z"),                   # year 0
    ("pubDate", "0001-01-01T00:00:00+01:00"),              # shifts into year 0
    ("pubDate", "9999-12-31T23:59:59-01:00"),              # shifts into year 10000
    ("dc:date", "0000-06-15"),                             # dc:date -> updated_parsed
]

for tag, value in DATES:
    xml = (
        "<?xml version='1.0'?><rss version='2.0' xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        f"<channel><title>t</title><item><title>x</title><{tag}>{value}</{tag}></item></channel></rss>"
    ).encode()
    e = feedparser.parse(io.BytesIO(xml)).entries[0]
    t = e.get("published_parsed") or e.get("updated_parsed")
    try:
        result = entry_time(e)
        status = f"ok -> {result}"
    except Exception as exc:  # noqa: BLE001
        status = f"RAISES {type(exc).__name__}: {exc}"
    print(f"{tag:8} {value!r:34} tm_year={t.tm_year if t else None!s:6} entry_time {status}")
