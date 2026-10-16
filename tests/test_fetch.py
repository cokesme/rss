import http.server
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402

RSS = ('<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title><link>/home</link>'
       '<item><title>Item</title><link>{link}</link></item></channel></rss>')


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/hang":
            time.sleep(3)
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", self.path.split("?to=", 1)[1])
            self.end_headers()
            return
        self.server.hits.append(self.path)
        if self.path == "/drip":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
            return
        if self.path == "/big":
            body = b"<rss>" + b" " * (build.MAX_FEED_BYTES + 10)
        elif self.path == "/latin1":
            body = RSS.format(title="caf\xe9", link="a.html").encode("latin-1")
        else:
            body = RSS.format(title="Feed", link="posts/1.html").encode()
        self.send_response(200)
        ct = "application/rss+xml" + ("; charset=iso-8859-1" if self.path == "/latin1" else "")
        self.send_header("Content-Type", ct)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class FetchTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.srv.daemon_threads = True
        cls.srv.hits = []
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def fetch(self, path_or_url: str) -> dict:
        url = path_or_url if "://" in path_or_url else self.base + path_or_url
        return build.fetch((url, None))


class FetchLimitsTest(FetchTestBase):
    def test_valid_feed_parses(self):
        r = self.fetch("/blog/feed.xml")
        self.assertIsNone(r["error"])
        self.assertEqual(r["title"], "Feed")
        self.assertEqual(len(r["items"]), 1)

    def test_relative_links_resolve_against_feed_url(self):
        r = self.fetch("/blog/feed.xml")
        self.assertEqual(r["items"][0]["link"], self.base + "/blog/posts/1.html")
        self.assertEqual(r["site"], self.base + "/home")

    def test_relative_links_resolve_against_final_url_after_redirect(self):
        r = self.fetch("/redirect?to=/blog/sub/feed.xml")
        self.assertEqual(r["items"][0]["link"], self.base + "/blog/sub/posts/1.html")

    def test_charset_from_content_type_is_honored(self):
        self.assertEqual(self.fetch("/latin1")["title"], "caf\xe9")

    def test_hanging_server_times_out(self):
        with mock.patch.object(build, "FETCH_TIMEOUT", 0.5):
            t = time.monotonic()
            r = self.fetch("/hang")
        self.assertLess(time.monotonic() - t, 2.5)
        self.assertTrue(r["error"])
        self.assertEqual(r["items"], [])

    def test_slow_drip_body_hits_total_deadline(self):
        with mock.patch.object(build, "FETCH_TIMEOUT", 0.5):
            t = time.monotonic()
            r = self.fetch("/drip")
        self.assertLess(time.monotonic() - t, 2.5)
        self.assertIn("took over", r["error"])

    def test_oversized_body_rejected(self):
        r = self.fetch("/big")
        self.assertIn("exceeds", r["error"])
        self.assertEqual(r["items"], [])


if __name__ == "__main__":
    unittest.main()
