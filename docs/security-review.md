# Security & Privacy Review: Static RSS Reader

## Overview

This document provides a full security and privacy review of the Static RSS Reader codebase (`build.py`, `.github/workflows/build.yml`, `requirements.txt`, and `feeds.txt`), focusing on three questions:

1. **Does anything in the project collect analytics or telemetry?**
2. **Can a malicious RSS feed compromise a user browsing the generated static site?**
3. **Can a malicious RSS feed compromise the GitHub Actions workflow or local build environment?**

---

## 1. Analytics, Telemetry & Privacy Audit

### Verdict: No Analytics or Tracking Code Exists

- **Zero JavaScript or external requests:** The generated HTML (`site/index.html` and `site/feeds/*.html`) uses inline CSS (`CSS` in `build.py:116-141`), system UI fonts, and contains no `<script>`, `<img>`, `<iframe>`, `<video>`, `<audio>`, or external stylesheet tags.
- **Feed tracking pixels are stripped:** `strip_html()` (`build.py:36-40`) removes all HTML tags from feed titles and summaries, and the output is HTML-escaped before rendering. Tracking pixels (`<img src="https://tracker.example.com/pixel.gif">`) embedded in RSS entries are completely discarded during build time.
- **No telemetry in dependencies:** The only dependency in `requirements.txt` is `feedparser`, which performs no analytics or network calls beyond fetching the requested feed if given a URL.
- **IP privacy when fetching feeds in GitHub Actions:** Because the hourly cron runs on GitHub-hosted runners, remote feed servers only see Microsoft/GitHub IP addresses and the generic `User-Agent` (`static-rss-reader/1.0 (+https://github.com)`), not your personal IP address.

### Passive Privacy Consideration: HTTP `Referer` Header Leak
- **Location:** `build.py:185`
  ```python
  f'<article><h2><a href="{html.escape(it["link"], quote=True)}" rel="noopener">'
  ```
- **Issue:** While `rel="noopener"` prevents `window.opener` tabnabbing, omitting `noreferrer` (or a page-level referrer policy) causes your browser to send the URL of your static RSS reader (`https://<user>.github.io/<repo>/`) in the `Referer` HTTP header whenever you click an article link.
- **Fix:** Add `<meta name="referrer" content="no-referrer">` to `<head>` in `page()` and use `rel="noopener noreferrer"` on external links.

---

## 2. Static Site Browsing Risks (Client-Side)

Because `build.py` escapes text fields (`title`, `summary`, `feed`, `error`, and `url`) with `html.escape()` when generating HTML (`build.py:151-160`, `179-188`, `251`), a malicious RSS feed **cannot** inject raw `<script>` or HTML tags that execute automatically on page load.

However, two injection vectors exist in how links and XML attributes are handled:

### 2.1 High Severity: Stored XSS via `javascript:` or `data:` URIs in Article Links
- **Location:** `build.py:98, 106, 185`
  ```python
  site = d.feed.get("link") or url
  ...
  "link": e.get("link") or site,
  ...
  f'<article><h2><a href="{html.escape(it["link"], quote=True)}" rel="noopener">'
  ```
- **Vulnerability:** `html.escape(it["link"], quote=True)` escapes HTML special characters (`<`, `>`, `&`, `"`, `'`) so the attribute cannot be broken out of, but it **does not validate the URL scheme**. If a compromised or malicious feed serves an item with:
  ```xml
  <link>javascript:alert(document.domain)</link>
  ```
  then clicking the article title on the static site executes arbitrary JavaScript in the context of your site's origin.
- **Impact:** On GitHub Pages (`https://<user>.github.io/<repo>/`), all repositories under `<user>.github.io` share the same web origin. JavaScript execution in the RSS reader can read or modify `localStorage`, `sessionStorage`, `IndexedDB`, or cookies belonging to any other GitHub Pages project hosted under your username.

### 2.2 Medium Severity: XML Attribute Injection in `atom.xml`
- **Location:** `build.py:14, 192-203`
  ```python
  from xml.sax.saxutils import escape as xml_escape
  ...
  <link href="{xml_escape(it["link"])}"/>
  ```
- **Vulnerability:** `xml.sax.saxutils.escape()` only escapes `&`, `<`, and `>`. Unlike `html.escape(..., quote=True)` or `xml.sax.saxutils.quoteattr()`, it **does not escape double quotes (`"`)**.
- **Impact:** A feed entry link containing `"` breaks out of the `href="..."` attribute in `site/atom.xml`, allowing attribute injection or corrupting the merged Atom feed consumed by downstream RSS readers.

### 2.3 Defense-in-Depth: Missing Content Security Policy (CSP)
- **Location:** `build.py:152-158`
- **Issue:** The generated HTML pages do not include a `<meta http-equiv="Content-Security-Policy" ...>` tag. Adding a strict CSP that disables all scripts (`default-src 'none'; style-src 'unsafe-inline'`) provides browser-enforced protection against XSS even if an escaping bug were ever introduced in the future.

---

## 3. GitHub Workflow & Local Build Risks (Build-Time)

In Python 3.12, the standard library `expat` XML parser used by `feedparser` blocks External General Entities (XXE) by default and includes protections against XML entity expansion ("Billion Laughs"). However, the build script and workflow have several denial-of-service (DoS), Server-Side Request Forgery (SSRF), and supply-chain weaknesses:

### 3.1 Medium Severity: Unbounded HTTP Responses & Missing Timeouts (Denial of Service)
- **Location:** `build.py:89`
  ```python
  d = feedparser.parse(url, agent=USER_AGENT)
  ```
- **Vulnerability:** When `feedparser.parse()` fetches a URL directly via `urllib.request`:
  1. **No socket timeout:** Python's default socket timeout is `None` (infinite). A malicious or unresponsive server can hold the connection open indefinitely, stalling `ThreadPoolExecutor` workers until GitHub Actions terminates the job after 6 hours (exhausting GitHub Actions runner minutes).
  2. **No response size limit / Decompression bomb:** `feedparser` sends `Accept-Encoding: gzip, deflate` and reads the entire response body into memory (`resp.read()`). A malicious server can stream gigabytes of compressed zeroes, crashing the runner or local machine with an Out-Of-Memory (OOM) error.

### 3.2 Medium Severity: Regular Expression Denial of Service (ReDoS) in `strip_html()`
- **Location:** `build.py:36-37`
  ```python
  def strip_html(raw: str) -> str:
      text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw or "")
  ```
- **Vulnerability:** If a feed payload contains thousands of unclosed `<script>` tags (e.g., `<script><script><script>...` with no closing `</script>`), the lazy quantifier `.*?` paired with the backreference `</\1>` causes catastrophic quadratic backtracking ($O(N^2)$) across the input string, pegging the CPU during the build.

### 3.3 Medium Severity: SSRF & Local File Inclusion (When Running Locally)
- **Location:** `build.py:89, 250-254`
- **Vulnerability:**
  - `feedparser.parse()` accepts local file paths and `file://` URLs if present in `feeds.txt`.
  - More importantly, if a remote `http://` or `https://` feed responds with an HTTP `301`/`302` redirect to `http://127.0.0.1:...`, `http://localhost:...`, or a private LAN IP (`192.168.x.x`, `10.x.x.x`, or cloud metadata `169.254.169.254`), `urllib` automatically follows the redirect.
  - If the internal endpoint returns an error or XML/HTML content, fragments of the response or error message (up to 200 characters in `err_html` or parsed items) can be published onto your public GitHub Pages site.

### 3.4 Medium Severity: Over-Scoped GitHub Actions Permissions & Unpinned Dependencies
- **Location:** `.github/workflows/build.yml:10-39` and `requirements.txt:1`
- **Vulnerabilities:**
  1. **Excessive job permissions:** `pages: write` and `id-token: write` are granted at the top-level workflow scope (`.github/workflows/build.yml:10-13`), meaning the `build` job—which installs third-party packages from PyPI and parses untrusted remote feeds—runs with write permissions to GitHub Pages and OIDC tokens, even though only the `deploy` job needs them.
  2. **Unpinned dependency (`feedparser>=6.0`):** `requirements.txt` does not pin exact package versions or SHA-256 hashes (`--require-hashes`). If `feedparser` or its transitive dependency (`sgmllib3k`) were compromised on PyPI, a cache miss in GitHub Actions would execute malicious package code during `pip install`.
  3. **Mutable GitHub Action version tags:** Actions (`actions/checkout@v5`, `actions/setup-python@v6`, etc.) are referenced by mutable tags rather than immutable full-length commit SHAs.
  4. **Workflow log command injection:** `build.py:274` prints raw URLs and error strings to `stderr` (`print(f"  ! {f['url']}: {f['error']}", file=sys.stderr)`). Newlines followed by `::error::` or `::warning::` in an exception message can spoof GitHub Actions annotations.

---

## 4. Recommended Fixes

### 4.1 Hardening `build.py`

1. **Validate URLs (`http://` and `https://` only) and block private/loopback IPs:**
   Create a helper that ensures every feed URL, redirect target, and article `<a href>` uses `http` or `https`, and resolves only to public IP addresses.
2. **Fetch feeds with a strict timeout, redirect check, and byte limit:**
   Fetch the raw XML bytes using `urllib.request.urlopen(req, timeout=15)` and read at most `MAX_FEED_BYTES` (e.g., `5 * 1024 * 1024` bytes) before passing the bytes to `feedparser.parse(raw_bytes)`.
3. **Replace regex HTML stripping with `html.parser.HTMLParser`:**
   Use Python's standard library HTML parser to strip tags and ignore `<script>`/`<style>` contents in linear time ($O(N)$), completely eliminating ReDoS.
4. **Fix XML attribute escaping in `render_atom()`:**
   Use `html.escape(val, quote=True)` (or `xml.sax.saxutils.quoteattr`) for XML attributes so `"` is properly escaped as `&quot;`.
5. **Add Content Security Policy (CSP) and Referrer-Policy meta tags:**
   Lock down the generated static HTML pages in `page()`.

#### Example Hardened Snippets for `build.py`

```python
import ipaddress
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser

FETCH_TIMEOUT = 15               # seconds
MAX_FEED_BYTES = 5 * 1024 * 1024 # 5 MB max per feed


def is_safe_http_url(url: str) -> bool:
    """Allow only http:// and https:// URLs with a valid hostname."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def sanitize_link(url: str, fallback: str = "#") -> str:
    """Return the URL only if it uses http(s), otherwise fallback."""
    url = (url or "").strip()
    if is_safe_http_url(url):
        return url
    if is_safe_http_url(fallback):
        return fallback.strip()
    return "#"


def assert_public_host(url: str) -> None:
    """Ensure the URL hostname does not resolve to a loopback, private, or link-local IP."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Disallowed URL scheme or host: {url}")
    for res in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(res[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ValueError(f"Refusing to connect to non-public IP {ip} for {parsed.hostname}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_feed_bytes(url: str) -> bytes:
    assert_public_host(url)
    opener = urllib.request.build_opener(SafeRedirectHandler)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
        data = resp.read(MAX_FEED_BYTES + 1)
        if len(data) > MAX_FEED_BYTES:
            raise ValueError(f"Feed exceeds maximum size of {MAX_FEED_BYTES} bytes")
        return data
```

#### Linear-Time `strip_html()` Using `HTMLParser`

```python
class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        else:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
        else:
            self.parts.append(" ")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    parser = _HTMLStripper()
    parser.feed(raw)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()
```

#### Hardened `<head>` and Link Rendering

In `page()`:
```python
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
```

In `render_items()`:
```python
safe_link = html.escape(sanitize_link(it["link"]), quote=True)
out.append(
    f'<article><h2><a href="{safe_link}" rel="noopener noreferrer">'
    f'{html.escape(it["title"])}</a></h2>'
    f'<div class="meta">{" · ".join(meta)}</div>{summary}</article>'
)
```

In `render_atom()`:
```python
safe_link = html.escape(sanitize_link(it["link"]), quote=True)
entries.append(f"""<entry>
<title>{xml_escape(it["title"])}</title>
<link href="{safe_link}"/>
<id>{safe_link}</id>
<updated>{updated}</updated>
<author><name>{xml_escape(it["feed"])}</name></author>
<summary>{xml_escape(it["summary"])}</summary>
</entry>""")
```

---

### 4.2 Hardening `.github/workflows/build.yml`

Scope permissions per job so `build` only has `contents: read`, and add a `timeout-minutes` limit so hung connections cannot burn hours of runner time:

```yaml
name: Build feed site

on:
  schedule:
    - cron: "17 * * * *"
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v5

      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      - run: python build.py

      - uses: actions/configure-pages@v5

      - uses: actions/upload-pages-artifact@v4
        with:
          path: site

  deploy:
    needs: build
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
```

---

### 4.3 Pinning `requirements.txt`

Pin exact versions (and optionally hashes with `pip install --require-hashes -r requirements.txt`) so automated builds cannot pull an untrusted package update:

```text
feedparser==6.0.11
sgmllib3k==1.0.0
```
