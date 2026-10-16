import io
import sys
import tempfile
import time
import unittest
import unittest.mock
from html import escape as html_escape
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


class IsolatedParseTest(unittest.TestCase):
    def parse(self, data: bytes) -> dict:
        return build.parse_isolated(data, {"content-type": "application/rss+xml"}, "https://ex.com/feed", None)

    def test_normal_feed(self):
        r = self.parse(rss("<link>https://ex.com/1</link><pubDate>Mon, 06 Sep 2021 10:00:00 GMT</pubDate>"))
        self.assertIsNone(r["error"])
        self.assertEqual(r["items"][0]["link"], "https://ex.com/1")
        self.assertEqual(r["items"][0]["date"].year, 2021)

    def test_large_legitimate_feed_fits_memory_cap(self):
        body = "<p>" + "lorem ipsum dolor sit amet " * 3500 + "</p>"
        items = "".join(f"<item><title>t{i}</title><link>https://ex.com/{i}</link><description>{html_escape(body)}</description></item>"
                        for i in range(50))
        data = f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{items}</channel></rss>".encode()
        self.assertGreater(len(data), 4 * 1024 * 1024)
        r = self.parse(data)
        self.assertIsNone(r["error"])
        self.assertEqual(len(r["items"]), 50)

    def test_cpu_blowup_is_killed_at_timeout(self):
        data = b"<?xml " + b"encoding='a' " * 2000 + b"\n<rss/>"  # ~20 s+ in encodings.py's regex
        with unittest.mock.patch.object(build, "PARSE_TIMEOUT", 2):
            t = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "parsing took over 2s"):
                self.parse(data)
            self.assertLess(time.monotonic() - t, 6)

    def test_entity_amplification_hits_memory_cap(self):
        doc = ("<?xml version='1.0'?>\n<!DOCTYPE rss [\n<!ENTITY a \"" + "A" * 100000 + "\">\n]>\n"
               "<rss version='2.0'><channel><title>t</title><item><title>x</title>"
               "<description>" + "&a;" * 16000 + "</description></item></channel></rss>").encode()
        with self.assertRaises((ValueError, RuntimeError)):  # MemoryError in the child, or the child dies
            self.parse(doc)

    def test_fetch_reports_parse_failures_per_feed(self):
        with unittest.mock.patch.object(build, "download", return_value=(b"<?xml " + b"encoding='a' " * 2000, {})), \
             unittest.mock.patch.object(build, "PARSE_TIMEOUT", 1):
            r = build.fetch(("https://ex.com/feed", "Slow"))
        self.assertEqual(r["items"], [])
        self.assertIn("parsing took over", r["error"])


class ExternalEntityTest(unittest.TestCase):
    """No form of external entity may pull local files into the output.

    Two layers stop this today, both upstream: feedparser's replace_doctype() drops
    SYSTEM entity declarations (but only when they start a line), and expat runs with
    external entities disabled. This guards against either changing in a dependency bump.
    """

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.marker = "LOCAL-FILE-CONTENT-7f3a"
        cls.text = Path(tmp.name, "secret.txt")
        cls.text.write_text(cls.marker)
        cls.dtd = Path(tmp.name, "evil.dtd")
        cls.dtd.write_text(f'<!ENTITY leak "{cls.marker}">')

    def assertNoLeak(self, doc: str):
        r = build.parse_feed(doc.encode(), {"content-type": "application/rss+xml"}, "https://ex.com/feed", None)
        self.assertNotIn(self.marker, repr(r))

    def body(self, ref: str) -> str:
        return (f"<rss version='2.0'><channel><title>{ref}</title><link>https://ex.com/</link>"
                f"<item><title>{ref}</title><link>https://ex.com/1</link><description>{ref}</description></item>"
                "</channel></rss>")

    def test_general_entity_one_line(self):  # replace_doctype() misses this; only expat's setting stops it
        self.assertNoLeak(f'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "{self.text.as_uri()}">]>' + self.body("&x;"))

    def test_general_entity_multi_line(self):
        self.assertNoLeak(f'<?xml version="1.0"?>\n<!DOCTYPE rss [\n<!ENTITY x SYSTEM "{self.text.as_uri()}">\n]>\n' + self.body("&x;"))

    def test_external_dtd(self):
        self.assertNoLeak(f'<?xml version="1.0"?><!DOCTYPE rss SYSTEM "{self.dtd.as_uri()}">' + self.body("&leak;"))
        self.assertNoLeak(f'<?xml version="1.0"?>\n<!DOCTYPE rss SYSTEM "{self.dtd.as_uri()}">\n' + self.body("&leak;"))

    def test_parameter_entity(self):
        self.assertNoLeak(f'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY % p SYSTEM "{self.dtd.as_uri()}"> %p;]>' + self.body("&leak;"))
        self.assertNoLeak(f'<?xml version="1.0"?>\n<!DOCTYPE rss [\n<!ENTITY % p SYSTEM "{self.dtd.as_uri()}">\n%p;\n]>\n' + self.body("&leak;"))
