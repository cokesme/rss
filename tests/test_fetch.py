import http.server
import io
import ipaddress
import os
import socket
import subprocess
import sys
import tempfile
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
        elif self.path == "/unsafe":
            body = RSS.replace("/home", "javascript:alert(2)").format(title="Feed", link="javascript:alert(1)").encode()
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

    def setUp(self):
        self.srv.hits.clear()
        p = mock.patch.object(build, "ALLOW_PRIVATE", True)  # the test server is on loopback
        p.start()
        self.addCleanup(p.stop)

    def fetch(self, path_or_url: str) -> dict:
        url = self.base + path_or_url if path_or_url.startswith("/") else path_or_url
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

    def test_unsafe_item_and_channel_links_fall_back_to_feed_url(self):
        r = self.fetch("/unsafe")
        self.assertEqual(r["site"], self.base + "/unsafe")
        self.assertEqual(r["items"][0]["link"], self.base + "/unsafe")

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


def fake_dns(table: dict):
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *a, **kw):
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]
        return real(host, port, *a, **kw)
    return mock.patch.object(socket, "getaddrinfo", getaddrinfo)


class SSRFTest(FetchTestBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(build, "ALLOW_PRIVATE", False)
        p.start()
        self.addCleanup(p.stop)

    def assertRefused(self, r: dict, needle: str = "non-public"):
        self.assertIn(needle, r["error"])
        self.assertEqual(r["items"], [])
        self.assertEqual(self.srv.hits, [])

    def test_ip_classification(self):
        blocked = ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                   "100.64.0.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "255.255.255.255",
                   "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1",
                   "::ffff:10.0.0.1", "2002:7f00:1::1"]
        for ip in blocked:
            self.assertFalse(build.ip_allowed(ipaddress.ip_address(ip)), ip)
        for ip in ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"]:
            self.assertTrue(build.ip_allowed(ipaddress.ip_address(ip)), ip)

    def test_loopback_url_refused(self):
        self.assertRefused(self.fetch("/feed.xml"))

    def test_public_looking_host_resolving_to_loopback_refused(self):
        port = self.srv.server_address[1]
        with fake_dns({"feed.example.com": "127.0.0.1"}):
            self.assertRefused(self.fetch(f"http://feed.example.com:{port}/feed.xml"))

    def test_allow_private_flag_used_by_tests(self):
        with mock.patch.object(build, "ALLOW_PRIVATE", True):
            self.assertIsNone(self.fetch("/feed.xml")["error"])

    def test_no_environment_escape_hatch(self):
        env = {**os.environ, "RSS_ALLOW_PRIVATE": "1"}
        out = subprocess.run([sys.executable, "-c", "import build; print(build.ALLOW_PRIVATE)"],
                             cwd=Path(build.__file__).parent, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False")

    def test_connection_pinned_to_validated_address(self):
        # The hostname is only resolved once (by the check) and the socket goes to that address.
        port = self.srv.server_address[1]
        with mock.patch.object(build, "ALLOW_PRIVATE", True), fake_dns({"pinned.test": "127.0.0.1"}):
            r = self.fetch(f"http://pinned.test:{port}/blog/feed.xml")
        self.assertIsNone(r["error"])
        self.assertEqual(r["items"][0]["link"], f"http://pinned.test:{port}/blog/posts/1.html")

    def test_non_http_schemes_refused(self):
        for url in ["file:///etc/passwd", "ftp://example.com/feed.xml", "data:,x", "/etc/passwd"]:
            r = build.fetch((url, None))
            self.assertIn("only http(s)", r["error"], url)

    def allow_only_server(self):
        real = build.ip_allowed
        return mock.patch.object(build, "ip_allowed", lambda ip: str(ip) == "127.0.0.1" or real(ip))

    def test_redirect_to_private_ip_refused(self):
        port = self.srv.server_address[1]
        with self.allow_only_server():
            r = self.fetch(f"/redirect?to=http://127.0.0.2:{port}/internal")
        self.assertRefused(r)

    def test_redirect_to_host_resolving_private_refused(self):
        with self.allow_only_server(), fake_dns({"internal.test": "10.0.0.5"}), \
                mock.patch.object(socket, "create_connection", wraps=socket.create_connection) as cc:
            r = self.fetch("/redirect?to=http://internal.test/secret")
        self.assertRefused(r)
        self.assertEqual(len(cc.call_args_list), 1)  # only the initial, allowed connection

    def test_redirect_to_non_http_refused(self):
        with self.allow_only_server():
            self.assertRefused(self.fetch("/redirect?to=ftp://example.com/x"), "refusing redirect")
            self.assertRefused(self.fetch("/redirect?to=file:///etc/passwd"), "not allowed")  # urllib's own check


class SanitizeTest(unittest.TestCase):
    EVIL = "boom\n::error::x\r\n::warning file=a::y\x1b[31m z"

    def test_clean(self):
        for s in (self.EVIL, "::error::x", "\n::error::x", " :: ::set-output name=a::b"):
            out = build.clean(s)
            self.assertNotRegex(out, r"[\x00-\x1f\x7f ]")
            self.assertFalse(out.startswith(":"), out)
        self.assertEqual(build.clean("\n::error::x"), "error::x")
        self.assertEqual(build.clean(ValueError("a\nb")), "a b")

    def test_fetch_never_raises_and_errors_are_clean(self):
        for url in ("http://[::1", "http://exa mple.com/\n::error::x", "", "http://"):
            r = build.fetch((url, None))
            self.assertTrue(r["error"], url)
            self.assertNotIn("\n", r["error"])

    def test_main_output_sanitized(self):
        bad = {"url": "http://e.test/\n::warning::u", "error": self.EVIL, "items": [], "title": "t"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(build, "OUT", Path(tmp) / "site"), \
                mock.patch.object(build, "read_feed_list", lambda: [("x", None)]), \
                mock.patch.object(build, "fetch", lambda _: dict(bad)), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(build.main(), 0)
            index = (Path(tmp) / "site" / "index.html").read_text()
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(any(l.lstrip().startswith("::") for l in lines))
        self.assertIn("1 feed(s) failed", index)
        self.assertNotIn("\x1b", index)


if __name__ == "__main__":
    unittest.main()
