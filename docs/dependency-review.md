# Dependency Security Review: feedparser and feedparser-sgmllib

## Summary verdict

feedparser has no known unpatched CVEs. Its five published advisories are all from 2009–2012 and fixed long ago. It is still unsafe to run unguarded on untrusted feeds. The project's planned hardening fixes the URL-scheme and input-handling problems. It does **not** fix CPU and memory exhaustion inside `feedparser.parse()`. Those are the main open risks.

- **Fixed by the in-progress `build.py`:**
  - feedparser keeps `javascript:` and `data:` links unchanged (F1). The new `sanitize_link()` covers this.
  - `feedparser.parse(<bytes>)` opens the bytes as a local file path (F2). Wrapping the body in `io.BytesIO` covers this.
  - URL-fetch mode has no timeout, no size limit, unbounded gzip/deflate decompression, and follows redirects to private hosts and `ftp://` (F8). The new `download()` function covers this.
- **Not fixed. These need build changes:**
  - **CPU exhaustion (F3, F4).** A 26 KB feed takes 21 s to parse, and the cost grows roughly cubically. Several other inputs grow quadratically and reach hours at the 5 MB download cap. The regex engine holds the GIL, so one bad feed stalls every worker thread. Socket timeouts cannot interrupt a parse.
  - **Memory exhaustion (F5).** A 68 KB feed expands to 300 MB of text, with about 890 MiB peak RSS.
  - **Whole-build abort from feed dates (F6).** feedparser returns year 0 or year 10000 dates. `entry_time()` runs outside the per-feed `try`, so the whole build crashes.

**Status update (2026-10-16, branch `security-hardening`).** F3–F6 are now fixed, and each fix was checked against the reproducers below:

- **F3, F4, F5:** commit 8213006 runs `parse_feed()` in a spawned child process limited to 20 s (`PARSE_TIMEOUT`) and 512 MiB of address space (`PARSE_MEMORY`).
    - Both entity-amplification inputs (68 KB and 148 KB) fail with `MemoryError` in 0.1 s.
    - The 26 KB XML-declaration input and the 1 MB author-email input are killed at 20 s.
    - In a build with four such feeds and one normal feed, the normal feed still builds. The whole build takes 20.1 s, because the timeouts run in parallel.
    - A legitimate 4 MiB feed with 50 items parses within the memory cap.
- **F6:** commit 7cde9f8 makes `entry_time()` skip out-of-range dates and fall back to the next date field.

Tests for both fixes are in `tests/test_parse.py`.

**Scope correction.** feedparser 6.0.14 depends on `feedparser-sgmllib>=2,<3`, not `sgmllib3k`. The change came in 6.0.13 (see feedparser's NEWS file). Today this installs `feedparser-sgmllib` 2.1.0. `sgmllib3k` is no longer installed. The earlier advice to pin `sgmllib3k==1.0.0` (in `security-review.md` §4.3) is obsolete.

## Scope

| Component | Version reviewed | Why |
| --- | --- | --- |
| feedparser | 6.0.14, latest; wheel sha256 `e35e3f76…3131137` | Direct dependency (`feedparser>=6.0`) |
| feedparser-sgmllib | 2.1.0, latest; wheel sha256 `2cab2d43…821e01d` | feedparser's loose (fallback) parser |
| sgmllib3k | 1.0.0 | Predecessor, briefly. No longer used. |
| CPython stdlib parts feedparser uses on this path | 3.12.15 (expat 2.9.0 in the test build) | `xml.sax`/pyexpat, `urllib.request`, `gzip`/`zlib`, `_markupbase`, `html.entities`, `time`/`datetime` |

The review covers only the features the project uses. `build.py` calls `feedparser.parse(...)` and reads:

- `bozo` and `bozo_exception`
- `feed.title` and `feed.link`
- the first 50 `entries`: `title`, `link`, `summary`, `content[0].value`, and `published_parsed` / `updated_parsed` / `created_parsed`

Two call modes are covered:

- **Mode A, URL fetch.** Current code on `main` at 94c85be: `feedparser.parse(url, agent=...)`.
- **Mode B, bytes plus response headers.** This is the in-progress change. `build.py` downloads the body itself (timeout, size cap, SSRF guard), then calls `feedparser.parse(io.BytesIO(data), response_headers={"content-location": ..., "content-type": ...})`.

## Method

- **Sources.** The sdists and wheels were downloaded from PyPI. The wheel contents match the sdist package code exactly, and the installed package matches the wheel. The reviewed files are:
  - all of feedparser (`api`, `http`, `encodings`, `urls`, `mixin`, `sanitizer`, `html`, `sgml`, `parsers/*`, `namespaces/_base`, `datetimes/*`)
  - all of feedparser-sgmllib (`__init__.py`, `_compat.py`)
- **Supply-chain diff.** feedparser-sgmllib was compared against sgmllib3k 1.0.0 after normalising quotes and whitespace.
- **Isolation.**
  - The reproducers ran with `python -I` from a separate directory.
  - Python 3.12.15 matches the CI interpreter. The packages were installed from the hash-pinned wheels.
  - The reproducers are kept outside the source tree.
- **Disclosure.**
  - `pip download --no-binary :all:` generates package metadata, which ran the sdist build backends (`setup.py` for feedparser and sgmllib3k, poetry-core for feedparser-sgmllib) in pip's isolated build environment.
  - Both `setup.py` files were read afterwards. They only call `setuptools.setup()` with static metadata. feedparser's also reads `README.rst`, `__init__.py`, and two optional environment variables.
- **Testing.** ReDoS and quadratic candidates were tested by timing inputs at sizes n and 2n. Every finding below has a reproducer or a direct source citation.
- **Advisories.** The OSV API was queried for all three packages. The upstream issue tracker, the repo's security page, PyPI metadata and attestations, and the GitHub API were all checked directly.

## Findings

Severity is for **this project**:

- **Impact:** a static site on GitHub Pages, built hourly by GitHub Actions from one https feed (`feeds.txt`).
- **Threat:** a hostile or compromised feed host, or anything that host redirects to.

Line numbers refer to feedparser 6.0.14 and feedparser-sgmllib 2.1.0.

| ID | Component | Issue | Severity (our context) | Status | Evidence | Covered by in-progress hardening? |
| --- | --- | --- | --- | --- | --- | --- |
| F1 | feedparser | `link` values are never scheme-filtered: `javascript:`, `data:`, tab-obfuscated variants | High (stored XSS) | Confirmed, R01 | `mixin.py:449-450`, `:517`, `:587`; `namespaces/_base.py:346`, `:374`; `urls.py:90-91` | **Yes**: `sanitize_link()` and CSP |
| F2 | feedparser | `parse(bytes)` first tries `open(bytes)` as a local path. `/dev/zero` gives unbounded memory use; a local feed file gets parsed. | High in mode B if raw bytes are passed | Confirmed, R02 | `api.py:110-136` | **Yes**: `io.BytesIO(data)`. Do not regress. |
| F3 | feedparser | `RE_XML_PI_ENCODING` grows about cubically on a long first line. 26 KB takes 21 s. The regex holds the GIL. | High (build hangs until the job times out) | Confirmed, R05 | `encodings.py:68`, `:189` | **No** |
| F4 | feedparser | Quadratic regexes and loops: author e-mail regex, CSS `url(` stripper, unclosed-comment fallback, namespace scan | High at the 5 MB cap (hours of CPU); Medium below about 100 KB | Confirmed, R05 | `mixin.py:746`; `sanitizer.py:837`, `:873`; `parsers/strict.py:89` | **No** |
| F5 | feedparser + expat | A large flat internal entity referenced many times. expat aborts on its amplification limit, then the loose parser expands it with no limit. | High (OOM; 68 KB in gives ~890 MiB RSS) | Confirmed, R06 | `sanitizer.py:903-950`; `api.py:262-272`; `mixin.py:362-379` | **No** (the 5 MB cap does not help) |
| F6 | feedparser → build.py | The ISO-8601 fallback returns `tm_year` 0 or 10000. `datetime.fromtimestamp` raises outside the per-feed `try`, and the whole build aborts. | Medium (no site update) | Confirmed, R04, and run through the real `build.fetch` | `datetimes/iso8601.py:99-106`, `:158`; `build.py` `entry_time()` | **No** |
| F7 | feedparser | `parse()` raises `ValueError`, `UnicodeEncodeError`, `TypeError`, `re.error` or `zlib.error` instead of setting `bozo` | Low (contained by `except Exception`) | Confirmed, R03 and R07(b) | `mixin.py:358-359`; `sanitizer.py:943`; `encodings.py:192`, `:273`; `namespaces/_base.py:197`; `http.py:182` | Yes, as long as the broad `except` stays around `parse()` |
| F8 | feedparser `http.py` + stdlib urllib | Mode A: no timeout; `f.read()` unbounded; gzip/deflate bomb (97 KB becomes 100 MB); redirects followed to loopback, private IPs and `ftp://`; 4xx/5xx bodies parsed as feeds | High in mode A | Confirmed, R07 (offline urllib harness) | `http.py:171-172`, `:179-201`, `:48-68` | **Yes**: mode B `download()` makes this not applicable |
| F9 | feedparser | Mode B without `content-location`: relative links stay relative. A hostile `xml:base` becomes the base unchecked. | Low | Confirmed, R01 | `mixin.py:227-230`; `api.py:236-238` | Yes: `content-location` is passed, and `sanitize_link` requires a host |
| F10 | feedparser | Attacker text ends up in `bozo_exception` messages (encoding name, Content-Type) | Low | Confirmed, probe | `encodings.py:254`, `:294-296` | Yes: `clean()` and `html.escape` |
| F11 | feedparser + expat | XXE, external DTDs, parameter entities | None | Confirmed not vulnerable | `api.py:255`; `sanitizer.py:906-943`. The loose parser never resolves external entities. | n/a |
| F12 | supply chain | feedparser-sgmllib is a brand-new package (org and repo created 2026-07-19). feedparser 6.0.14 has no PyPI attestation. No SECURITY.md; open ReDoS reports have no maintainer response. `>=6.0` floats. | Low / informational | Verified, sources below | — | Partly: pin with hashes (planned) |
| F13 | sgmllib3k | Predecessor. sdist-only, so `setup.py` ran at install. Unmaintained since 2010. | Not applicable (no longer installed) | Verified | `feedparser.egg-info/requires.txt` | n/a |
| F14 | stdlib (3.12) | Safety depends on the patch level: expat amplification limits, `html.parser` fixes | Informational | Verified locally (expat_2.9.0 enforces the limit; see R06) | — | Log `sys.version` and `pyexpat.EXPAT_VERSION` in CI |

## Detailed findings

### F1 — `javascript:` and `data:` survive in `link` (Confirmed, R01)

feedparser has a scheme allow-list (`urls.py:39-47`) and a checker, `make_safe_absolute_uri()` (`urls.py:86-103`). Entry and feed links never go through the checker.

**Exact code reason.** Every element in `can_be_relative_uri` (including `link`, `id` and `href`) passes through `_FeedParserMixin.pop()`. Its relative-URI step, `mixin.py:514-517`, calls `self.resolve_uri(output)`. `resolve_uri` is defined at `mixin.py:449-450`:

```python
def resolve_uri(self, uri):
    return _urljoin(self.baseuri or '', uri)
```

`_urljoin` is a plain `urllib.parse.urljoin` (`urls.py:52-58`). `urljoin(base, "javascript:alert(1)")` returns the second argument unchanged, because it has its own scheme.

The same `resolve_uri` handles all link sources:

- Atom `<link href>` (`namespaces/_base.py:346`)
- RSS `<guid>` acting as the link (`:374`)
- `<generator href>` (`:421`)

The scheme check is applied only in three places:

- to `xml:base` (`mixin.py:227-228`)
- to URIs inside embedded HTML (`RelativeURIResolver.resolve_uri`, `urls.py:143-144`)
- to `href` attributes in the HTML sanitizer (`sanitizer.py:805-806`)

**Even the checker would not help in mode B without a base.** `make_safe_absolute_uri('', 'javascript:…')` returns its input (`urls.py:90-91`: `if not base: return rel or ''`).

Reproduced in both modes:

- `javascript:alert(document.domain)` in RSS `<link>`, Atom `href` and `<guid>`
- `data:text/html,<script>…`
- `jav\tascript:` (browsers strip the tab)

The allow-list also contains `file`, `ftp`, `smb` and others. These are irrelevant here because the build allows only http(s).

### F2 — bytes are opened as a file path (Confirmed, R02)

`_open_resource` (`api.py:110-136`) only treats file-like objects as data. It treats strings as URLs if their scheme is http, https, ftp, file or feed. Everything else, **including `bytes`**, goes to `open(x, 'rb')` (`api.py:118-120`), and POSIX `open()` accepts bytes paths.

A server that answers with the 9-byte body `/dev/zero` makes feedparser read `/dev/zero` until `MemoryError`. R02 hits `MemoryError` after 0.08 s under a 512 MiB limit; without a limit, the runner would OOM. The build's `MAX_FEED_BYTES` cap does not apply here.

A body naming a local file that is a feed (or happens to parse as one) gets that file parsed and rendered. R02 shows the title of a local temp file. A `str` body is worse still: it can also trigger a network fetch.

feedparser's docstring warns about this (`api.py:160-163`: "Wrap an untrusted string in a io.StringIO or io.BytesIO"). The in-progress `build.py` already does this.

### F3 — cubic regex on the XML declaration (Confirmed, R05)

`RE_XML_PI_ENCODING = re.compile(br'^<\?.*encoding=[\'"](.*?)[\'"].*\?>')` (`encodings.py:68`) runs on every document (`:189`).

`.` does not match newlines, so the attacker supplies one long first line: `<?xml encoding='a' encoding='a' …` with no `?>`. There are three nested backtracking scans:

- `.*` over every `encoding=` occurrence
- `(.*?)` over every quote
- `.*\?>` to the end of the line

| First-line size | Time |
| --- | --- |
| 3.3 KB | 0.05 s |
| 6.5 KB | 0.34 s |
| 13 KB | 2.7 s |
| 26 KB | 21 s |

The time grows about 8× per doubling. Extrapolating, 100 KB takes about 20 minutes, and 5 MB effectively never finishes.

`re` does not release the GIL. R05 measures a second thread dropping from 53 M to 0.3 M loop iterations per second during the parse. One feed therefore stalls all eight `ThreadPoolExecutor` workers, and no socket timeout can interrupt it. The job runs until the workflow timeout: 6 h by default.

No upstream issue or advisory covers this regex. Searches of the issue tracker for "redos", "quadratic", "catastrophic backtracking" and "slow" turned nothing up.

### F4 — quadratic regexes and loops (Confirmed, R05)

Each case grows about 4× per doubling, measured on 3.12.15:

| Location | Trigger | Measured |
| --- | --- | --- |
| `mixin.py:746`, e-mail regex in `_sync_author_detail` | `<author>` = `a.a.a.…` | 32 KB: 2.1 s |
| `sanitizer.py:837`, `url\s*\(\s*[^\s)]+?…` in `sanitize_style` | HTML summary with `style="url(url(url(…"` | 32 KB: 1.5 s |
| `sanitizer.py:873`, `--[^>]*>` fallback for unclosed comments | HTML summary with `<!--` followed by many `-` | 80 KB: 1.9 s |
| `parsers/strict.py:89`, linear scan of `namespaces_in_use` per element | N `xmlns:` declarations and N prefixed elements | 238 KB: 2.5 s |

All of these run whether or not the build reads the field: author, style attributes and so on. At the 5 MB cap, each one extrapolates to hours. Linear behaviour was confirmed for the CSS gauntlet regex (`sanitizer.py:840`), the short-tag and declaration regexes (`html.py:125-126`), sgmllib's `endbracket` (`sgml.py:41`), CDATA, the date parsers, and deep nesting.

The e-mail regex is reported upstream:

- [kurtmckee/feedparser#562](https://github.com/kurtmckee/feedparser/issues/562) has been open since 2026-04-19, with no maintainer response.
- Fix PR [#580](https://github.com/kurtmckee/feedparser/pull/580) has been open since 2026-07-19 and is not merged.

### F5 — entity amplification through the loose-parser fallback (Confirmed, R06)

`replace_doctype()` (`sanitizer.py:906-950`) keeps only "safe" internal entities: values with no `&` (`:903`). That stops nested billion-laughs expansion (the CVE-2012-2921 class). One large flat entity referenced N times still expands to `len × N`.

1. expat ≥ 2.4 notices the amplification and aborts with "limit on input amplification factor".
2. feedparser catches that `SAXException` and re-parses with the loose sgmllib parser (`api.py:262-272`).
3. The loose parser's `handle_entityref` (`mixin.py:362-379`) appends the full entity text for every reference, with no limit.

| Input | Result |
| --- | --- |
| 16 KB | 20 MB summary |
| 68 KB | 300 MB summary, ~890 MiB peak RSS |
| 148 KB | `MemoryError` under a 1 GiB limit |

A few hundred KB would exhaust a GitHub runner's RAM and get the build OOM-killed. Feeds essentially never need internal entity declarations.

### F6 — out-of-range years crash the whole build (Confirmed, R04)

The ISO-8601 fallback parser accepts year `0000` (`datetimes/iso8601.py:106`) and normalises out-of-range fields with `time.mktime()` (`:158`). `0000-01-01T00:00:00Z`, `0001-01-01T00:00:00+01:00`, `9999-12-31T23:59:59-01:00` and `<dc:date>0000-06-15</dc:date>` produce `struct_time`s with `tm_year` 0 or 10000.

The w3dtf parser rejects these dates first, and the ISO-8601 parser then accepts them. `calendar.timegm()` accepts them, but `datetime.fromtimestamp()` raises `ValueError: year 0 is out of range`.

In both the current and the in-progress `build.py`, `entry_time(e)` runs in `fetch()` **after** the `try` that wraps `parse()`. The exception propagates out of `pool.map` and aborts `main()`. Running a feed with `<pubDate>0000-01-01T00:00:00Z</pubDate>` through the real `build.fetch` inside a `ThreadPoolExecutor` gave: `build aborted: ValueError year 0 is out of range`.

### F7 — exceptions escape `parse()` (Confirmed, R03 and R07(b))

`parse()` only catches `urllib.error.URLError` (`api.py:217`) and `SAXException` (`:264`). The loose-parser path (`:272`) has no `try`. Confirmed raises:

| Input | Exception | Location |
| --- | --- | --- |
| `&#x110000;`, `&#99999999;` | `ValueError` from `chr()` | `mixin.py:359` |
| `&#xD800;` | `UnicodeEncodeError` | `mixin.py:359` |
| a charref longer than 4300 digits | `ValueError`, int digit limit | `mixin.py:358` |
| a backslash in an ENTITY value | `re.error` (the replacement string is interpreted) | `sanitizer.py:943` |
| `<?encoding="\x00"?>` | `ValueError` | `encodings.py:273` |
| `<?encoding="\xe3"?>` | `UnicodeDecodeError` | `encodings.py:192` |
| stray `</height>` inside `<image>` | `TypeError` | `namespaces/_base.py:197` |
| mode A only: a corrupt gzip body | `zlib.error`, not caught at `http.py:182` | `http.py:182` |

The out-of-range charref, ENTITY-backslash, encoding-name and stray-tag cases are also reported upstream in [#599](https://github.com/kurtmckee/feedparser/issues/599): open, no response. A fix PR for the charref case, [#590](https://github.com/kurtmckee/feedparser/pull/590), was closed unmerged on 2026-08-15. The build's `except Exception` around `parse()` contains all of these.

### F8 — URL-fetch mode network behaviour (Confirmed, R07)

R07 runs feedparser's real `http.get` against an in-process urllib handler, so no network is used.

- **No timeout.** `http.py:171` calls `opener.open(request)` without `timeout=`. Source only; not demonstrated.
- **Unbounded reads and decompression.**
  - `f.read()` is unbounded (`http.py:172`).
  - feedparser sends `Accept-encoding: gzip, deflate` (`:111`).
  - It decompresses the whole body in memory (`:181`, `:194`, `:198`). A 97 KB gzip body became 100 MB.
- **Redirects are followed anywhere urllib allows.** A 302 to `http://127.0.0.1:8080/admin` and a 302 to `ftp://10.0.0.5/…` were both fetched and parsed. The stdlib `HTTPRedirectHandler` allows http, https and ftp.
- **HTTP errors are swallowed.** `http_error_default` (`http.py:48-52`) parses 4xx/5xx bodies as feeds.

The in-progress `download()` makes mode A obsolete:

- It never calls feedparser's fetcher.
- It sends no `Accept-Encoding`.
- It caps the size and the total time.
- It pins the connection to vetted public IPs.
- It allows only http(s) redirects.

### F9–F11 (lower and negative results)

- **F9.** With no base URI (mode B without `content-location`), relative links stay relative ('rel'). A hostile `xml:base` is joined without a check (`mixin.py:229-230`). The in-progress build passes `content-location` and requires an absolute http(s) URL, so this is covered.
- **F10.** `CharacterEncodingOverride` and `NonXMLContentType` messages include the attacker's encoding name or Content-Type. The build shows `bozo_exception` when a feed has no entries. It is HTML-escaped and passed through `clean()`, so this is covered.
- **F11.** These are not exploitable:
  - `feature_external_ges` is disabled (`api.py:255`).
  - `replace_doctype` removes the DOCTYPE and re-inserts only safe internal entities.
  - The loose parser never resolves external entities.
  - Tested: `<!ENTITY xxe SYSTEM "file:///etc/hostname">` with a parameter-entity DTD fetch gave a title of literally `A&xxe;B` in both the strict and loose paths.

### F12 — supply chain and maintenance (Verified)

**feedparser.**

- Maintainer: Kurt McKee. The GitHub repo `kurtmckee/feedparser` was created in 2010.
- 6.0.13 and 6.0.14 were released in July 2026, after a two-year gap since 6.0.11.
- The 6.0.14 wheel has **no** PyPI provenance attestation: the integrity endpoint returns 404.
- There is no SECURITY.md and there are no published GitHub advisories.
- Recent security-relevant reports and PRs (#562, #580, #599) have no maintainer response. #590 was closed without review of its content.
- The wheel is pure Python; the `setup.py` is static (see Method).

**feedparser-sgmllib 2.1.0.**

- PyPI maintainer: Kurt McKee, the same e-mail address as feedparser's author.
- Source: `python-syndication/feedparser-sgmllib`. The GitHub org `python-syndication` was created 2026-07-19T20:04Z and the repo 2026-07-19T22:12Z. The org's only public member is `kurtmckee`, and the repo has 0 stars.
- Releases: 1.0.0 on 2026-07-27, 2.0.0 and 2.0.1 on 2026-07-30, 2.1.0 on 2026-08-02.
- The 2.1.0 wheel **has** a PyPI Trusted Publishing attestation: publisher GitHub `python-syndication/feedparser-sgmllib`, workflow `publish.yaml`, environment `pypi`, and a subject digest that matches the reviewed wheel.
- Its `pyproject.toml` declares license PSF-2.0, `requires-python >=3.10`, no dependencies, and the poetry-core build backend.

**Code diff, feedparser-sgmllib 2.1.0 against sgmllib3k 1.0.0.** No network, filesystem, subprocess or dynamic-execution code was added. The imports are only `_markupbase`, `re`, `typing` and `_compat` (`io.StringIO`). The non-trivial changes are:

- `charref` now requires `;` and accepts hex (`__init__.py:31`). feedparser previously monkey-patched this.
- `tagfind` allows `:`, and `attrfind` allows a trailing `$`.
- `AssertionError` from `_markupbase` is re-raised as `SGMLParseError`.
- `feed()` buffers into a `StringIO` with exponential back-off before rescanning (`:137-152`, `:280-288`). This is the 2.1.0 fix for quadratic incremental feeding. feedparser feeds the whole document at once, so it is neutral for us.
- `finish_endtag` normalises the end-tag name with `tagfind` and searches the stack from the top. The innermost-match semantics are unchanged.
- The type hints, the removal of the `TestSGMLParser`/`test()` demo code and the reformatting are cosmetic.

**Pinning.** `feedparser>=6.0` floats. Any future feedparser release, or any `feedparser-sgmllib` 2.x, installs on the next hourly run. Pin both with hashes (Recommendations, item 5). `pip --require-hashes` also blocks the optional `chardet`/`cchardet` accelerators (`encodings.py:33-43`), which would change encoding detection. They are not installed today.

### F13 — sgmllib3k (no longer used)

sgmllib3k 1.0.0 was a 2010, sdist-only Python 3 port of sgmllib, so pip ran its `setup.py` at install time. That file was read; it is benign. The package is unmaintained, and feedparser no longer depends on it. Do not pin or install it.

### F14 — stdlib components (brief)

- **Python 3.12** is in its security-fix-only phase, with end of life scheduled for 2028-10 ([devguide](https://devguide.python.org/versions/)). `actions/setup-python` with `"3.12"` installs the latest 3.12.x build.
- **`xml.sax` / pyexpat.** XXE protection comes from feedparser's feature flag. Billion-laughs and amplification protection comes from libexpat ≥ 2.4, which F5 relies on. The test build used expat 2.9.0. CI's copy depends on the setup-python build, so log `pyexpat.EXPAT_VERSION`.
- **`urllib.request`** is relevant only in mode A (F8). Note that the stdlib redirect handler permits `ftp://`.
- **`_markupbase`** is a private CPython module that sgmllib subclasses. It is the source of the `AssertionError` that 2.1.0 converts.
- **`html`.** feedparser uses only `html.entities`. The build's own `HTMLParser` stripper is outside feedparser, but its quadratic-time safety also depends on the CPython patch level.
- **Date parsing.** feedparser does not use `email.utils`. Its own parsers (`datetimes/*`) were linear in every probe. The only issue is the range problem in F6.

## Mapping to the project's hardening (in-progress `build.py`)

| Hardening | Covers | Does not cover |
| --- | --- | --- |
| `sanitize_link()` on feed, entry and rendered links, plus CSP `default-src 'none'` | F1, F9 | — |
| `io.BytesIO(data)` instead of raw bytes | F2 | — |
| `download()`: timeout, 5 MB cap, no Accept-Encoding, pinned public IPs, http(s)-only redirects | F8 (mode A no longer used) | F3–F5: the expensive work happens inside `parse()`, after download |
| `except Exception` around `download()` and `parse()` | F7 | F6: `entry_time()` runs after the `try` |
| `HTMLParser`-based `strip_html()` | build-side regex ReDoS (not a feedparser issue) | feedparser's own regexes (F3, F4) |
| `clean()` and `html.escape` on errors | F10 | — |
| Planned hash pinning | F12 drift and tampering | Upstream bugs in the pinned versions |
| `parse_isolated()`: a child process with a 20 s limit and a 512 MiB address-space cap (added after this review, 8213006) | F3, F4, F5 | CPU time up to the limit on each run |
| `entry_time()` skips out-of-range dates (added after this review, 7cde9f8) | F6 | — |

## Recommendations

**Items 1–3 change `build.py`.**

1. **Run each `feedparser.parse()` in a child process with a wall-clock timeout and a memory cap.** This fixes F3, F4 and F5, and any similar issue not yet found. A thread timeout cannot do this, because the parse holds the GIL. `ProcessPoolExecutor` is also wrong: `future.result(timeout=)` does not kill a stuck worker, so shutdown hangs. Minimal shape:

   ```python
   import multiprocessing as mp, resource

   PARSE_TIMEOUT = 20                 # seconds per feed
   PARSE_MEM = 1024 * 1024 * 1024     # 1 GiB address space per parse

   def _parse_child(conn, data, headers):
       try:
           resource.setrlimit(resource.RLIMIT_AS, (PARSE_MEM, PARSE_MEM))
           d = feedparser.parse(io.BytesIO(data), response_headers=headers)
           conn.send({  # plain, picklable subset of what fetch() uses
               "bozo": bool(d.bozo),
               "bozo_exception": str(d.get("bozo_exception", "")),
               "feed": {k: d.feed.get(k) for k in ("title", "link")},
               "entries": [{
                   "title": e.get("title", ""), "link": e.get("link"),
                   "summary": e.get("summary") or "",
                   "content": [{"value": c.get("value", "")} for c in e.get("content", [])[:1]],
                   **{k: tuple(e[k]) for k in ("published_parsed", "updated_parsed", "created_parsed") if e.get(k)},
               } for e in d.entries[:MAX_PER_FEED]],
           })
       except BaseException as exc:  # noqa: BLE001
           conn.send({"error": f"{type(exc).__name__}: {exc}"})

   def parse_isolated(data, headers):
       ctx = mp.get_context("forkserver")   # not "fork": the parent has threads
       recv, send = ctx.Pipe(duplex=False)
       p = ctx.Process(target=_parse_child, args=(send, data, headers), daemon=True)
       p.start()
       send.close()
       if not recv.poll(PARSE_TIMEOUT):
           p.kill(); p.join()
           raise TimeoutError(f"feed parse exceeded {PARSE_TIMEOUT}s")
       result = recv.recv(); p.join()
       if "error" in result:
           raise ValueError(result["error"])
       return result
   ```

   `fetch()` then reads plain dicts instead of `FeedParserDict` attributes. For example, `e["content"][0]["value"]` replaces `e.content[0].get("value")`. Rebuild the time tuples with `time.struct_time(...)` or pass them straight to `calendar.timegm`.

   If process isolation is too much, use cheap pre-parse filters as a stop-gap. They target the confirmed vectors only:
   - Reject bodies that contain `b"<!ENTITY"` (F5).
   - Reject bodies whose first line contains `b"encoding="` more than once (F3).
   - Lower `MAX_FEED_BYTES` to about 1 MB. This only bounds F4 to minutes; it does not remove it.

2. **Make `entry_time()` total, and keep everything after `parse()` inside the per-feed error handling.** This fixes F6.

   ```python
   def entry_time(e):
       for key in ("published_parsed", "updated_parsed", "created_parsed"):
           t = e.get(key)
           if t:
               try:
                   return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
               except (ValueError, OverflowError, OSError):
                   continue
       return None
   ```

   Also wrap the whole body of `fetch()`, not only `download()` and `parse()`, in `try/except Exception`. Then no per-item processing error can escape `pool.map()` and abort the build.

3. **Keep the in-progress protections exactly as written.**
   - `feedparser.parse(io.BytesIO(data), ...)`. Never pass raw `bytes` or `str` (F2).
   - `sanitize_link()` on every rendered link (F1).
   - The broad `except Exception` (F7).
   - Never call `feedparser.parse(url)` again (F8).

4. **Workflow, as already recommended in `security-review-validation.md`.**
   - Set `timeout-minutes` on the build job. This bounds F3/F4 even if item 1 is skipped.
   - Optionally log `python -c "import sys, pyexpat; print(sys.version, pyexpat.EXPAT_VERSION)"` (F14).

5. **Pin with hashes, and drop sgmllib3k.**

   ```text
   feedparser==6.0.14 --hash=sha256:e35e3f760151b0c3b22cac9684155cae186a233e16c49bcbc6c49e91e3131137
   feedparser-sgmllib==2.1.0 --hash=sha256:2cab2d43b95a954f920f18aebce7a4dbbb3f539780b127e2aa114f579821e01d
   ```

   Install with `pip install --require-hashes -r requirements.txt`. These are the pure-Python wheels, so no build backend runs at install time. Both hashes match the files reviewed here.

6. **Upstream.** F3 (cubic `RE_XML_PI_ENCODING`) and F5 (entity amplification through the loose fallback) do not appear in the upstream tracker. The repo has no SECURITY.md, so report them privately to the maintainer (contact e-mail in the package metadata) before discussing them publicly. Watch #562/#580 (F4) and #599 (F7).

## Reproducers

All reproducers are in [`dependency-review/repro/`](dependency-review/repro/). They use only the stdlib and feedparser, make no network calls, and are safe to run: the memory demonstrations run in a child process with `RLIMIT_AS`. Run each one with `python -I <script>` from any directory.

| Script | Findings | Expected output on 3.12.15 / feedparser 6.0.14 |
| --- | --- | --- |
| `r01_link_schemes.py` | F1, F9 | `javascript:`/`data:` links returned in both modes; `make_safe_absolute_uri` returns `''` with a base, and the input unchanged without one |
| `r02_bytes_treated_as_path.py` | F2 | Title of a local temp file shown; `MemoryError` from body `/dev/zero` |
| `r03_parse_raises.py` | F7 | Eight inputs raise out of `parse()` |
| `r04_dates_crash_build.py` | F6 | `entry_time` raises `ValueError` for year 0 and 10000 |
| `r05_cpu_dos.py` (~1 min) | F3, F4 | ×6–8 per doubling for the PI regex, ×4 for the rest; the second thread starves |
| `r06_entity_amplification.py` | F5 | 16 KB in gives 20 MB, 68 KB gives 300 MB (~890 MiB RSS), then `MemoryError` |
| `r07_url_mode_http.py` | F7(b), F8 | gzip bomb 97 KB → 100 MB; `zlib.error` raised; redirects to 127.0.0.1 and `ftp://10.0.0.5` followed; 404 body parsed |

## Sources (opened during this review)

- OSV API queries for PyPI `feedparser`, `feedparser-sgmllib` and `sgmllib3k`: `https://api.osv.dev/v1/query`. Only `feedparser` has advisories: GHSA-3mwg-gp5g-fv3q / CVE-2009-5065 (fixed 5.0), GHSA-2p78-8hh6-96xc / CVE-2011-1157, GHSA-4m72-rmm9-2qjr / CVE-2011-1158, GHSA-6h52-4vmh-8x4f / CVE-2011-1156 (fixed 5.0.1), and GHSA-hjf3-r7gw-9rwg / CVE-2012-2921 (fixed 5.1.2). Details: https://api.osv.dev/v1/vulns/GHSA-hjf3-r7gw-9rwg and https://api.osv.dev/v1/vulns/GHSA-6h52-4vmh-8x4f
- https://github.com/kurtmckee/feedparser/security (no SECURITY.md, no published advisories)
- https://github.com/kurtmckee/feedparser/issues/562 and https://github.com/kurtmckee/feedparser/pull/580 (e-mail regex ReDoS, F4)
- https://github.com/kurtmckee/feedparser/issues/599 and https://github.com/kurtmckee/feedparser/pull/590 (parse() raises, F7)
- GitHub issue search API, `repo:kurtmckee/feedparser` with redos / quadratic / catastrophic backtracking / slow / hang / entity memory / make_safe_absolute_uri
- https://pypi.org/pypi/feedparser/json, https://pypi.org/pypi/feedparser-sgmllib/json and https://pypi.org/pypi/sgmllib3k/json (maintainers, release dates, file digests)
- https://pypi.org/integrity/feedparser-sgmllib/2.1.0/feedparser_sgmllib-2.1.0-py3-none-any.whl/provenance (Trusted Publishing attestation) and https://pypi.org/integrity/feedparser/6.0.14/feedparser-6.0.14-py3-none-any.whl/provenance (404)
- https://api.github.com/repos/python-syndication/feedparser-sgmllib, https://api.github.com/orgs/python-syndication, https://api.github.com/orgs/python-syndication/public_members and https://api.github.com/repos/kurtmckee/feedparser
- https://devguide.python.org/versions/ (Python 3.12 status)
- Upstream source as distributed on PyPI: feedparser 6.0.14 (sdist and wheel, including `NEWS` and `feedparser.egg-info/requires.txt`), feedparser-sgmllib 2.1.0 (sdist and wheel, including `CHANGELOG.rst`), and sgmllib3k 1.0.0 (sdist)
