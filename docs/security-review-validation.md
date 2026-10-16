# Validation of the Security Review

This document checks the claims in [`security-review.md`](security-review.md) against the code and against tests run on the code.

## Summary

The main findings in `security-review.md` are correct. The stored XSS (§2.1), Atom attribute injection (§2.2), missing fetch limits (§3.1), ReDoS (§3.2) and redirect-following (§3.3) all reproduce. Several secondary claims are overstated. Two of the proposed fixes need changes before they are applied.

**Scope:** `build.py`, `.github/workflows/build.yml`, `requirements.txt`, and feedparser 6.0.14, which is what `feedparser>=6.0` installs today.

**Method:**

- A local HTTP server served malicious feeds to the real `build.fetch()`, `render_items()` and `render_atom()`.
- feedparser's fetch and parse code was read directly.

## Confirmed claims

| Review claim | Verdict | Evidence |
| --- | --- | --- |
| §2.1 Stored XSS via `javascript:` / `data:` links | **Confirmed. This is the most important finding.** | feedparser has a scheme filter (`make_safe_absolute_uri`), but it did not apply to entry links in testing. RSS `<link>javascript:alert(document.domain)</link>` and Atom `href="javascript:…"` both passed through unchanged. `render_items` emitted `<a href="javascript:alert(1)">`. `data:text/html,<script>…` links and tab-obfuscated `jav&#x09;ascript:` links (which browsers still execute) also passed through. The attack needs one click on the title. |
| §2.2 Attribute injection in `atom.xml` | Confirmed | `xml_escape` does not escape `"`. The input link `https://ex.com/a" onx="1` produced `<link href="https://ex.com/a" onx="1"/>`. |
| §3.1 No timeout and unbounded response size | Confirmed (source read) | `feedparser/http.py:171-172` calls `opener.open(request)` with no timeout, then `f.read()`. It sends `Accept-encoding: gzip, deflate` and decompresses the whole body in memory (`:181`, `:194`). |
| §3.2 ReDoS in `strip_html()` | Confirmed | One title made of 20,000 unclosed `<script>` tags (about 160 KB) took **7.2 s** to process, and the cost grows quadratically. HTML-typed summaries are not a vector, because feedparser's sanitizer already removes `<script>`. Titles and `type="text"` summaries reach `strip_html()` unchanged, so the attack still works. |
| §3.3 Redirects to loopback are followed | Confirmed | A 302 redirect to `http://127.0.0.1/internal` was followed, and the internal feed's title appeared in the build output. |
| §3 intro: XXE is blocked | Confirmed: not an issue in feedparser 6.0.14 on CPython 3.12 or 3.13. Re-examined and corrected below. | A `file:///etc/hostname` external entity did not resolve. See "External entities (XXE)" for what actually blocks it. |
| §3.4.1 Over-scoped workflow permissions | Confirmed | `build.yml:10-13` grants `pages: write` and `id-token: write` at the top level, so the `build` job has both. |
| §3.4.2–3 Unpinned dependency and tag-referenced actions | Confirmed | `requirements.txt` contains `feedparser>=6.0`. Actions are referenced by version tags such as `@v5`, not by commit SHA. |
| §1 No analytics or telemetry | Confirmed | The generated HTML has no `<script>`, `<img>`, iframe or external resources. Feed HTML is stripped and then escaped. |

## External entities (XXE)

**Verdict: not an issue with the pinned versions.** No form of external entity reaches the output. One upstream setting does all the work, though, so the project now has regression tests for it (`tests/test_parse.py`, `ExternalEntityTest`).

Two upstream layers are involved:

1. **Expat runs with external entities disabled.** feedparser calls `setFeature(feature_external_ges, 0)` (`api.py:255`), and this is also the stdlib default since Python 3.7.1. In CPython's SAX reader, this one setting also controls external DTDs and external parameter entities. `feature_external_pes` can't be turned on at all; CPython raises "expat does not read external parameter entities".
2. **feedparser's `replace_doctype()` removes external declarations before parsing** (`sanitizer.py:906-950`). It deletes every `<!ENTITY>` and puts back only internal entities with a literal value. Its regexes are anchored to the start of a line (`^\s*<!ENTITY`, `^\s*<!DOCTYPE`, with `re.MULTILINE`), so a DOCTYPE written on one line after `<?xml …?>` passes through unchanged.

The original validation test used a one-line DOCTYPE, so only layer 1 was tested.

To find which layer stops what, the tests were rerun with each layer disabled:

| Form | Both layers on | `replace_doctype()` disabled | Expat external entities forced on |
| --- | --- | --- | --- |
| General entity, one line | blocked | blocked | **leaks file contents** |
| General entity, multi-line | blocked | blocked | blocked |
| External DTD, one line | blocked | blocked | **leaks** |
| External DTD, multi-line | blocked | blocked | blocked |
| Parameter entity, one line | blocked | blocked | **leaks** |
| Parameter entity, multi-line | blocked | blocked | blocked |

**Conclusion:**

- Expat's setting is the one control that covers every form.
- `replace_doctype()` is a partial backup that is easy to get around.
- The project's own code adds no protection here.
- The risk is a future feedparser or Python change that enables external entities. The new tests fail in that case; this was checked by forcing the setting on.
- Internal entities still expand. That is the route for the dependency review's memory blowup (F5), which is now contained by the parser-process memory cap.

## Overstated or inaccurate claims

- **Referer leak (§1).** Modern browsers default to `strict-origin-when-cross-origin`. A click on an article link sends only `https://<user>.github.io/`, not the `/<repo>/` path. The fix is cheap, but this is a minor issue.
- **`rel="noopener"` (§1).** The links have no `target="_blank"`, so `noopener` has no effect here, and tabnabbing was never possible.
- **SSRF severity on GitHub Actions (§3.3).** Redirect-following is real. The cloud-metadata example does not apply to GitHub-hosted runners: Azure IMDS requires a `Metadata: true` request header, which a redirect cannot add. The realistic risk is to local builds. A `file://` entry in `feeds.txt` is the owner's own configuration, not something an attacker controls.
- **"A cache miss would execute malicious code" (§3.4.2).** The pip cache is not relevant. `pip install` re-resolves `>=6.0` on every run, so a malicious release would be installed whether or not the cache hits.
- **Stale version pin (§4.3).** The suggested pin `feedparser==6.0.11` is outdated. The latest release is 6.0.14.
- **Log command injection (§3.4.4).** This is mostly theoretical:
    - Exception text from feedparser and urllib is rarely attacker-controlled.
    - Python `repr`-escapes control characters in its invalid-URL errors.
    - Even if an injection worked, it could only spoof annotations.

    This is low severity, not medium.

## Problems in the proposed fixes

1. **The workflow fix (§4.2) will likely break the build.** Giving `build` only `contents: read` removes `pages: read`. `actions/configure-pages` calls the Pages API and will likely fail with "Resource not accessible by integration". Either add `pages: read` to the `build` job or remove the step, since this site does not use its outputs. This is an inference; the workflow has not been run.
2. **`fetch_feed_bytes()` followed by `feedparser.parse(raw_bytes)` drops the base URI.** Relative links in feeds will no longer resolve. In testing, a feed parsed from bytes also passed `javascript:alert(1)` through unchanged. To fix this:
    - Keep `sanitize_link()` mandatory on every rendered link.
    - Pass `response_headers={"content-location": resp.url}` so relative links still resolve.
3. **`assert_public_host()` can be bypassed with DNS rebinding.** It resolves the hostname once, and urllib resolves it again when it connects. It is useful defence in depth, not a complete SSRF fix.
4. **The `HTMLParser` stripper works, with one caveat.** It processed 200,000 `<script>` tags in 0.004 s, and its output was correct for nested and unclosed tags and for entities. The claim that it "completely" removes the quadratic-time risk is slightly too strong: older CPython releases had quadratic-time bugs in `HTMLParser` that were fixed in 2025. The current 3.12 build from `setup-python` should include those fixes.

## Recommended order

1. **§2.1:** apply `sanitize_link()` to every rendered link and add the CSP `<meta>` tag. The CSP also blocks navigation to `javascript:` URLs.
2. **§3.2:** replace the regex in `strip_html()` with the `HTMLParser` stripper.
3. **§3.1:** add a fetch timeout and a byte limit, and keep the base URI as described in fix 2 above.
4. **§2.2:** escape quotes in `atom.xml` attributes.
5. **Workflow:** scope permissions per job and keep `pages: read` (or remove `configure-pages`). Add `timeout-minutes`. Pin `feedparser==6.0.14` and `sgmllib3k==1.0.0`.
6. **Referer:** add `<meta name="referrer" content="no-referrer">`.
