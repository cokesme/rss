import io
import sys
import time
import unittest
from pathlib import Path

import feedparser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402


def rss(item: str) -> bytes:
    return ("<?xml version='1.0'?><rss version='2.0' xmlns:dc='http://purl.org/dc/elements/1.1/'>"
            f"<channel><title>t</title><link>https://ex.com/</link><item><title>x</title>{item}</item>"
            "</channel></rss>").encode()


class EntryTimeTest(unittest.TestCase):
    def test_out_of_range_years_are_skipped(self):
        for tag, value in [("pubDate", "0000-01-01T00:00:00Z"), ("pubDate", "0001-01-01T00:00:00+01:00"),
                           ("pubDate", "9999-12-31T23:59:59-01:00"), ("dc:date", "0000-06-15")]:
            e = feedparser.parse(io.BytesIO(rss(f"<{tag}>{value}</{tag}>"))).entries[0]
            self.assertIsNone(build.entry_time(e), value)

    def test_falls_through_to_next_valid_field(self):
        e = {"published_parsed": time.struct_time((0, 1, 1, 0, 0, 0, 0, 1, 0)),
             "updated_parsed": time.gmtime(1_700_000_000)}
        self.assertEqual(build.entry_time(e).year, 2023)
