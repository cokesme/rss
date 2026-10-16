import sys
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ATOM = "{http://www.w3.org/2005/Atom}"
UNSAFE = [
    "javascript:alert(document.domain)",
    "JavaScript:alert(1)",
    "  javascript:alert(1)",
    "\njavascript:alert(1)",
    "jav\tascript:alert(1)",
    "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "javascript://example.com/%0aalert(1)",
    "http:///nohost",
    "/relative/path",
    "",
    None,
]
SAFE = ["https://example.com/a?b=1&c=2#frag", "http://example.com/", "HTTPS://Example.com/x"]


def item(link: str, **kw) -> dict:
    return {"title": "T", "link": link, "date": NOW, "summary": "S", "feed": "F", "slug": "f", **kw}


class SanitizeLinkTest(unittest.TestCase):
    def test_unsafe_links_fall_back(self):
        for url in UNSAFE:
            with self.subTest(url=url):
                self.assertEqual(build.sanitize_link(url), "#")
                self.assertEqual(build.sanitize_link(url, "https://site.example/"), "https://site.example/")

    def test_unsafe_fallback_rejected(self):
        self.assertEqual(build.sanitize_link("javascript:x", "javascript:y"), "#")
        self.assertEqual(build.sanitize_link(None, None), "#")

    def test_safe_links_unchanged(self):
        for url in SAFE:
            with self.subTest(url=url):
                self.assertEqual(build.sanitize_link(url), url)
        self.assertEqual(build.sanitize_link("  https://example.com/ \n"), "https://example.com/")

    def test_render_items_sanitizes(self):
        for url in UNSAFE[:-1]:
            with self.subTest(url=url):
                out = build.render_items([item(url)], NOW)
                self.assertIn('<a href="#" rel="noopener noreferrer">', out)
        out = build.render_items([item(SAFE[0])], NOW)
        self.assertIn('href="https://example.com/a?b=1&amp;c=2#frag"', out)

    def test_render_atom_sanitizes(self):
        root = ET.fromstring(build.render_atom([item("javascript:alert(1)")], NOW))
        entry = root.find(f"{ATOM}entry")
        self.assertEqual(entry.find(f"{ATOM}link").get("href"), "#")


class AtomEscapingTest(unittest.TestCase):
    def test_quote_in_link_escaped_and_well_formed(self):
        link = 'https://ex.com/a" onx="1&b=<2>\'x'
        xml = build.render_atom([item(link, title='<b>"t"</b> & co', feed="F&G", summary="a < b")], NOW)
        self.assertIn('href="https://ex.com/a&quot; onx=&quot;1&amp;b=&lt;2&gt;\'x"', xml)
        root = ET.fromstring(xml)
        entry = root.find(f"{ATOM}entry")
        link_el = entry.find(f"{ATOM}link")
        self.assertEqual(link_el.get("href"), link)
        self.assertEqual(set(link_el.attrib), {"href"})
        self.assertEqual(entry.find(f"{ATOM}id").text, link)
        self.assertEqual(entry.find(f"{ATOM}title").text, '<b>"t"</b> & co')


class PageHeadTest(unittest.TestCase):
    def test_csp_and_referrer_meta(self):
        feeds = [{"title": "F", "slug": "f", "items": [item("https://example.com/")]}]
        for depth in (0, 1):
            with self.subTest(depth=depth):
                out = build.page("T", "", feeds, None, NOW, depth=depth)
                head = out.split("</head>", 1)[0]
                self.assertIn(
                    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                    'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">', head)
                self.assertIn('<meta name="referrer" content="no-referrer">', head)
                self.assertIn("<style>", head)
                self.assertNotIn("<script", out)


class StripHtmlTest(unittest.TestCase):
    def test_redos_unclosed_script_tags(self):
        raw = "<script>" * 200_000
        start = time.perf_counter()
        self.assertEqual(build.strip_html(raw), "")
        self.assertLess(time.perf_counter() - start, 1.0)
        start = time.perf_counter()
        build.strip_html("<style>" * 100_000 + "x" + "<b>" * 100_000)
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_cases(self):
        cases = {
            None: "",
            "": "",
            "plain text": "plain text",
            "Tom &amp; Jerry &lt;3 &#39;hi&#x27; &eacute; &nbsp;x": "Tom & Jerry <3 'hi' é x",
            "<p>Hello <b>wor<i>ld</i></b></p><p>next</p>": "Hello wor ld next",
            "a<br>b<br/>c": "a b c",
            "a<!-- hidden -->b": "a b",
            "before<script>alert('x<b>')</script>after": "before after",
            "x<SCRIPT type='t'>evil()</SCRIPT>y": "x y",
            "x<style>p{color:red}</style>y": "x y",
            "<script>a</script>keep<script>b</script>": "keep",
            "ok<script>never closed <p>text": "ok",
            "  lots \n\t of   space  ": "lots of space",
            "a < b and c > d": "a < b and c > d",
            "<a href='x'>link</a> text": "link text",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(build.strip_html(raw), expected)
